"""Providers: what actually delivers a message (owner decision: pluggable, log-only now).

A provider is a class with a ``name`` and a ``send(message) -> SendResult`` method. The
installation uses one provider, chosen by the ``MESSAGING_PROVIDER`` environment variable:

- ``log`` (default): writes the message to the ``polaris.messaging`` logger, marks it sent.
- ``console``: prints the message on standard output (development).
- ``file``: appends one JSON line per message to ``MESSAGING_FILE_PATH``
  (default ``<project>/var/messages.jsonl``) (development).
- a dotted path, e.g. ``apps.messaging.providers_twilio.TwilioProvider``: any class with
  the same interface. A real adapter (Africa's Talking, Twilio…) reads its own credentials
  from the environment, calls the provider's API in ``send`` and returns a
  ``SendResult``: ``ok=True`` with the provider's message id, or ``ok=False`` with the
  error (``permanent=True`` when retrying cannot help, e.g. an invalid number).
  ``send`` may raise; the outbox then counts a failed attempt and retries later.

Providers never touch the database: ``apps.messaging.services`` records the outcome.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from django.utils.module_loading import import_string

if TYPE_CHECKING:  # pragma: no cover
    from .models import Message

logger = logging.getLogger("polaris.messaging")

PROVIDER_ENV = "MESSAGING_PROVIDER"
FILE_PATH_ENV = "MESSAGING_FILE_PATH"
DEFAULT_PROVIDER = "log"


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_id: str = ""
    error: str = ""
    permanent: bool = False  # a failure that retrying cannot fix


class Provider:
    """The interface. Subclass it (or write any class with the same two members)."""

    name = "base"

    def send(self, message: Message) -> SendResult:  # pragma: no cover - interface
        raise NotImplementedError


def _summary(message: Message) -> dict:
    return {
        "id": message.pk,
        "company": message.company_id,
        "channel": message.channel,
        "to": message.to,
        "kind": message.kind,
        "language": message.language,
        "text": message.text,
    }


class LogProvider(Provider):
    """Sends nothing: writes the message to the log and reports it sent."""

    name = "log"

    def send(self, message: Message) -> SendResult:
        logger.info("message %s", json.dumps(_summary(message), ensure_ascii=False))
        return SendResult(ok=True, provider_id=f"log-{message.pk}")


class ConsoleProvider(Provider):
    """Development: prints each message."""

    name = "console"

    def send(self, message: Message) -> SendResult:
        sys.stdout.write(
            f"[{message.channel} → {message.to}] ({message.kind}, {message.language})\n"
            f"{message.text}\n"
        )
        sys.stdout.flush()
        return SendResult(ok=True, provider_id=f"console-{message.pk}")


class FileProvider(Provider):
    """Development: appends one JSON line per message to a file."""

    name = "file"

    def __init__(self, path: str | os.PathLike | None = None):
        default = Path(settings.BASE_DIR) / "var" / "messages.jsonl"
        self.path = Path(path or os.environ.get(FILE_PATH_ENV) or default)

    def send(self, message: Message) -> SendResult:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = {**_summary(message), "at": timezone.now().isoformat()}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        return SendResult(ok=True, provider_id=f"file-{message.pk}")


PROVIDERS: dict[str, type[Provider]] = {
    LogProvider.name: LogProvider,
    ConsoleProvider.name: ConsoleProvider,
    FileProvider.name: FileProvider,
}


def provider_name() -> str:
    return os.environ.get(PROVIDER_ENV, "").strip() or DEFAULT_PROVIDER


def get_provider(name: str | None = None) -> Provider:
    """The provider named by ``name``, else by ``MESSAGING_PROVIDER`` (default ``log``)."""
    name = name or provider_name()
    if name in PROVIDERS:
        return PROVIDERS[name]()
    if "." in name:
        try:
            provider_class = import_string(name)
        except ImportError as exc:
            raise ImproperlyConfigured(f"{PROVIDER_ENV}: cannot import {name!r}") from exc
        return provider_class()
    raise ImproperlyConfigured(
        f"{PROVIDER_ENV}={name!r}: use one of {sorted(PROVIDERS)} or a dotted class path."
    )
