"""Validators shared by every app (uploads, time zones)."""

import zoneinfo
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _

MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_UPLOAD_EXTENSIONS = ["jpg", "jpeg", "png", "webp", "pdf"]


def _is_jpeg(head: bytes) -> bool:
    return head.startswith(b"\xff\xd8\xff")


def _is_png(head: bytes) -> bool:
    return head.startswith(b"\x89PNG\r\n\x1a\n")


def _is_webp(head: bytes) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _is_pdf(head: bytes) -> bool:
    return head.startswith(b"%PDF-")


# The first bytes ("magic bytes") each allowed extension must start with. No Pillow: a
# file whose header lies about its type is refused; decoding is the viewer's job.
MAGIC_BYTES = {
    "jpg": _is_jpeg,
    "jpeg": _is_jpeg,
    "png": _is_png,
    "webp": _is_webp,
    "pdf": _is_pdf,
}
HEADER_SIZE = 16


def validate_file_content(file) -> None:
    """Reject a file whose first bytes don't match its extension (an HTML page named
    ``x.jpg``, a PDF named ``x.png``…). Files already stored are not read again."""
    if getattr(file, "_committed", False):
        return  # an existing FieldFile, checked when it was uploaded
    extension = Path(getattr(file, "name", "") or "").suffix[1:].lower()
    check = MAGIC_BYTES.get(extension)
    if check is None:
        return  # the extension validator refuses it
    try:
        file.seek(0)
        head = file.read(HEADER_SIZE)
        file.seek(0)
    except (AttributeError, OSError, ValueError):
        head = b""
    if isinstance(head, str):
        head = head.encode("latin-1", "replace")
    if not check(head or b""):
        raise ValidationError(
            _("The file's content does not match its type (.%(extension)s)."),
            code="invalid_file_content",
            params={"extension": extension},
        )


@deconstructible(path="django.core.validators.FileExtensionValidator")
class UploadTypeValidator(FileExtensionValidator):
    """The allowed extensions, then the file's magic bytes (``validate_file_content``).

    It deconstructs as Django's ``FileExtensionValidator`` with the same arguments, so
    adding the content check changes no migration (validators don't touch the schema).
    """

    def __call__(self, value):
        super().__call__(value)
        validate_file_content(value)


validate_upload_extension = UploadTypeValidator(allowed_extensions=ALLOWED_UPLOAD_EXTENSIONS)


def validate_file_size(file) -> None:
    """Reject uploads larger than ``MAX_UPLOAD_SIZE`` bytes."""
    size = getattr(file, "size", None)
    if size is not None and size > MAX_UPLOAD_SIZE:
        raise ValidationError(
            _("The file is too large (%(size)s bytes). The maximum is 5 MB."),
            code="file_too_large",
            params={"size": size},
        )


# Use on every FileField that stores a photo or a proof of payment.
UPLOAD_VALIDATORS = [validate_upload_extension, validate_file_size]


def validate_time_zone(value: str) -> None:
    """Accept only IANA time zone names, such as ``Africa/Lome``."""
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(
            _("“%(value)s” is not a known time zone."),
            code="invalid_time_zone",
            params={"value": value},
        ) from exc
