"""Validators shared by every app (uploads, time zones)."""

import zoneinfo

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.utils.translation import gettext_lazy as _

MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_UPLOAD_EXTENSIONS = ["jpg", "jpeg", "png", "webp", "pdf"]

validate_upload_extension = FileExtensionValidator(allowed_extensions=ALLOWED_UPLOAD_EXTENSIONS)


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
