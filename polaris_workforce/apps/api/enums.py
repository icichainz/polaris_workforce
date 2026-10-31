"""The enum vocabulary of the foreman API (v1).

These values are part of the public contract in ``docs/api/openapi.yaml``: the phone
stores them, so never rename a value. Add new values only together with a contract
update. ``SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`` points at these lists so every
enum gets one stable name in the schema.

The API uses words (``present``, ``half_day``), not the one-letter codes shown on
screen (P, ½). Mapping to the database values is the job of each app's serializers.
"""

from django.utils.translation import gettext_lazy as _

ERROR_CODES = [
    ("not_authenticated", _("Missing, invalid or revoked credentials.")),
    ("permission_denied", _("Authenticated, but not allowed (for example another site).")),
    ("not_found", _("The resource does not exist.")),
    ("validation_error", _("The request body or query string is invalid.")),
    ("throttled", _("Too many requests; retry later.")),
    ("conflict", _("The request conflicts with the current state.")),
    ("locked", _("The resource is locked (a locked day or pay period, or a locked device).")),
    ("not_implemented", _("The endpoint is part of the contract but not built yet.")),
    ("method_not_allowed", _("The HTTP method is not allowed on this path.")),
    ("unsupported_media_type", _("The request content type is not supported.")),
    ("not_acceptable", _("The Accept header cannot be satisfied.")),
    ("server_error", _("Unexpected server error.")),
]

# Mirrors accounts.User.Role.
USER_ROLES = [
    ("admin", _("Admin")),
    ("site_manager", _("Site manager")),
    ("foreman", _("Foreman")),
    ("accountant", _("Accountant")),
]

LANGUAGES = [
    ("en", _("English")),
    ("fr", _("French")),
]

PAY_TYPES = [
    ("daily", _("Daily")),
    ("hourly", _("Hourly")),
]

# A worker's mark for the day. Shown on the phone as P, A, ½ and L.
ATTENDANCE_STATUSES = [
    ("present", _("Present")),
    ("absent", _("Absent")),
    ("half_day", _("Half day")),
    ("late", _("Late")),
]

# ``not_started`` is API-only: the day has no row on the server yet.
DAY_STATUSES = [
    ("not_started", _("Not started")),
    ("draft", _("Draft")),
    ("submitted", _("Submitted")),
    ("approved", _("Approved")),
    ("rejected", _("Rejected")),
    ("locked", _("Locked")),
]

# Fields of an attendance record that a correction can change.
CORRECTABLE_FIELDS = [
    ("status", _("Status")),
    ("arrival", _("Arrival")),
    ("departure", _("Departure")),
    ("note", _("Note")),
]

# The cash sheet of one site and day, as the phone sees it.
PAYOUT_STATUSES = [
    ("not_open", _("Not open (waiting for approval)")),
    ("open", _("Open")),
    ("paid", _("Everyone paid")),
    ("closed", _("Closed by the office")),
]

PROOF_TYPES = [
    ("photo", _("Photo")),
    ("signature", _("Signature")),
    ("thumb_mark", _("Thumb mark")),
    ("none", _("No proof")),
]
