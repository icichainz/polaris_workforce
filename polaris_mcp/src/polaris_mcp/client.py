"""HTTP client for the Polaris office API (``/api/v1/office/``).

Every call carries the agent token. The server does all the checking (role, sites,
company, what the token may do); this client only speaks HTTP and turns the API's error
format ``{"code", "message", "details"}`` into ``PolarisError``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

USER_AGENT = "polaris-mcp/0.1"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class PolarisError(Exception):
    """An error answer from Polaris, or Polaris could not be reached."""

    def __init__(
        self, message: str, *, code: str = "error", status: int | None = None, details=None
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details or {}

    def __str__(self) -> str:
        if not self.details:
            return self.message
        fields = "; ".join(
            f"{name}: {' '.join(map(str, errors)) if isinstance(errors, list) else errors}"
            for name, errors in self.details.items()
        )
        return f"{self.message} ({fields})"


class SettingsError(Exception):
    """No usable address or token."""


def credentials_path() -> Path:
    """Where ``polaris login`` keeps the address and token (``POLARIS_CONFIG`` overrides)."""
    if os.environ.get("POLARIS_CONFIG"):
        return Path(os.environ["POLARIS_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "polaris" / "credentials.json"


@dataclass(frozen=True)
class Settings:
    url: str
    token: str

    @classmethod
    def checked(cls, url: str, token: str) -> Settings:
        url, token = url.strip().rstrip("/"), token.strip()
        if not token.startswith("pol_"):
            raise SettingsError("The token must be an agent token (pol_…).")
        if not url.startswith("https://") and not _is_local(url):
            raise SettingsError("The Polaris address must use https:// (except on this computer).")
        return cls(url=url, token=token)

    @classmethod
    def from_env(cls) -> Settings | None:
        url = os.environ.get("POLARIS_URL", "").strip()
        token = os.environ.get("POLARIS_TOKEN", "").strip()
        if not url and not token:
            return None
        if not (url and token):
            raise SettingsError("Set both POLARIS_URL and POLARIS_TOKEN, or neither.")
        return cls.checked(url, token)

    @classmethod
    def from_file(cls, path: Path | None = None) -> Settings | None:
        path = path or credentials_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.checked(data["url"], data["token"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SettingsError(f"{path} is not a valid credentials file.") from exc

    @classmethod
    def load(cls) -> Settings:
        """``POLARIS_URL`` + ``POLARIS_TOKEN`` first, then the file ``polaris login`` wrote."""
        settings = cls.from_env() or cls.from_file()
        if settings is None:
            raise SettingsError(
                "Not signed in. Run `polaris login`, or set POLARIS_URL and POLARIS_TOKEN "
                "(an agent token from Polaris > AI assistant access)."
            )
        return settings

    def save(self, path: Path | None = None) -> Path:
        """Write the credentials file, readable by this user only."""
        path = path or credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"url": self.url, "token": self.token}, f)
        os.chmod(path, 0o600)
        return path


def _is_local(url: str) -> bool:
    host = httpx.URL(url).host
    return url.startswith("http://") and host in {"localhost", "127.0.0.1", "::1"}


class PolarisClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=f"{settings.url}/api/v1/office/",
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=TIMEOUT,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise PolarisError(f"Polaris could not be reached: {exc}", code="unreachable") from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_success and isinstance(body, dict):
            return body
        if isinstance(body, dict) and "message" in body:
            raise PolarisError(
                str(body["message"]),
                code=str(body.get("code", "error")),
                status=response.status_code,
                details=body.get("details") or {},
            )
        raise PolarisError(
            f"Polaris answered HTTP {response.status_code}.", status=response.status_code
        )

    # --- reads -----------------------------------------------------------------------------

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "me/")

    async def today(self) -> dict[str, Any]:
        return await self._request("GET", "today/")

    async def approvals(self) -> dict[str, Any]:
        return await self._request("GET", "approvals/")

    async def day(self, site_id: int, date: str) -> dict[str, Any]:
        return await self._request("GET", f"sites/{site_id}/days/{date}/")

    async def cash_sheet(self, site_id: int, date: str) -> dict[str, Any]:
        return await self._request("GET", f"sites/{site_id}/cash-sheets/{date}/")

    async def workers(self, query: str = "", site_id: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if query:
            params["q"] = query
        if site_id is not None:
            params["site"] = site_id
        return await self._request("GET", "workers/", params=params)

    async def worker(self, worker_id: int, date_from: str | None, date_to: str | None):
        params = {k: v for k, v in (("from", date_from), ("to", date_to)) if v}
        return await self._request("GET", f"workers/{worker_id}/", params=params)

    # --- decisions -------------------------------------------------------------------------

    async def preview_decision(self, day_id: int, decision: str, note: str = ""):
        body = {"decision": decision, "note": note}
        return await self._request("POST", f"days/{day_id}/decision-preview/", json=body)

    async def confirm_decision(self, confirmation: str) -> dict[str, Any]:
        return await self._request("POST", "decisions/", json={"confirmation": confirmation})
