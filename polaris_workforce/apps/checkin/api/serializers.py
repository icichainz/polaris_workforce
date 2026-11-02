"""Check-in API bodies. Enum values are API words (``in``/``out``, ``present``…)."""

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.api.enums import ATTENDANCE_STATUSES, DAY_STATUSES
from apps.api.fields import ClockTimeField
from apps.api.idempotency import client_uuid_field
from apps.checkin import selectors, tokens
from apps.checkin.models import PHOTO_VALIDATORS, CheckIn
from apps.core.choices import AttendanceMode

DIRECTIONS = [("in", _("In")), ("out", _("Out"))]
SOURCES = [
    ("foreman_scan", _("Badge scanned by the foreman's phone")),
    ("kiosk", _("Badge scanned on a kiosk phone")),
    ("manual", _("Worker picked by hand on the foreman's phone")),
]
FLAGS = [
    (selectors.UNKNOWN_BADGE, _("The badge is not known (made up, damaged, or another company's)")),
    (selectors.REVOKED_BADGE, _("The badge was revoked; the check-in does not count")),
    (selectors.NOT_ON_CREW, _("The worker is not on this site's crew that day")),
    (selectors.OUTSIDE_GEOFENCE, _("The GPS position is outside the site's geofence")),
    (selectors.DUPLICATE, _("Same worker and direction less than 5 minutes after another")),
]


def _coordinate(limit: int, help_text: str):
    return serializers.DecimalField(
        max_digits=9,
        decimal_places=6,
        min_value=-limit,
        max_value=limit,
        coerce_to_string=False,
        required=False,
        allow_null=True,
        help_text=help_text,
    )


# --- requests ---------------------------------------------------------------------------


class CheckInCreateSerializer(serializers.Serializer):
    client_uuid = client_uuid_field()
    site_id = serializers.IntegerField(help_text="A site from `GET /me/`.")
    direction = serializers.ChoiceField(choices=DIRECTIONS)
    at = serializers.DateTimeField(
        help_text="Phone clock when the badge was scanned (ISO 8601 with offset). At most "
        "10 minutes ahead of the server and 31 days old. Decides the work day."
    )
    badge = serializers.CharField(
        required=False,
        allow_null=True,
        max_length=200,
        help_text="The scanned QR text exactly as read, e.g. `PW1:K7Q2M4XR5TBN3WCA."
        "D6HZP2LQ4V7NYE3S` (the `PW1:` prefix may be left out). Send this **or** "
        "`worker_id`. Kiosk phones must send `badge`.",
    )
    worker_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Manual check-in: a worker picked by the foreman (no badge). Send this "
        "**or** `badge`.",
    )
    latitude = _coordinate(90, "GPS latitude in decimal degrees (WGS 84), 6 decimals.")
    longitude = _coordinate(180, "GPS longitude in decimal degrees (WGS 84), 6 decimals.")
    accuracy_m = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        max_value=100000,
        help_text="GPS accuracy radius in metres, as the phone reports it.",
    )
    photo = serializers.FileField(
        required=False,
        allow_null=True,
        validators=PHOTO_VALIDATORS,
        help_text="Optional photo of the worker at the scan: jpg, jpeg, png or webp, at "
        "most 5 MB (aim for ≤ 1280 px, a few hundred KB). Send `multipart/form-data` "
        "with a photo, JSON otherwise.",
    )

    def validate(self, attrs):
        if (attrs.get("badge") is None) == (attrs.get("worker_id") is None):
            raise serializers.ValidationError(
                {"non_field_errors": [_("Send either badge or worker_id.")]}
            )
        if (attrs.get("latitude") is None) != (attrs.get("longitude") is None):
            raise serializers.ValidationError(
                {"latitude": [_("Send latitude and longitude together.")]}
            )
        return attrs


# --- responses ---------------------------------------------------------------------------


class CheckInSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    client_uuid = serializers.UUIDField()
    site_id = serializers.IntegerField()
    worker_id = serializers.IntegerField(
        allow_null=True, help_text="`null` for a badge the server does not know."
    )
    date = serializers.DateField(help_text="Work day of `at` in the company time zone.")
    direction = serializers.ChoiceField(choices=DIRECTIONS)
    at = serializers.DateTimeField()
    received_at = serializers.DateTimeField()
    source = serializers.ChoiceField(choices=SOURCES)
    latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, coerce_to_string=False, allow_null=True
    )
    longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, coerce_to_string=False, allow_null=True
    )
    accuracy_m = serializers.IntegerField(allow_null=True)
    distance_m = serializers.IntegerField(
        allow_null=True,
        help_text="Distance from the geofence centre (no GPS or no geofence: null).",
    )
    inside_geofence = serializers.BooleanField(
        allow_null=True, help_text="`null` without GPS or when the site has no geofence."
    )
    counted = serializers.BooleanField(
        help_text="Counts towards the roll call (known worker, badge not revoked)."
    )
    draft_updated = serializers.BooleanField(
        help_text="`self_check_in` mode: this check-in wrote the worker's draft record."
    )
    flags = serializers.ListField(child=serializers.ChoiceField(choices=FLAGS))
    photo_url = serializers.URLField(
        allow_null=True,
        help_text="Protected download (send the `Authorization` header); null without a photo.",
    )


class CheckInListSerializer(serializers.Serializer):
    site_id = serializers.IntegerField()
    date = serializers.DateField()
    checkins = CheckInSerializer(many=True)


SUGGESTION_SOURCES = [
    (selectors.SOURCE_CHECKIN, _("Counted check-ins of the worker")),
    (selectors.SOURCE_PRESENCE, _("Presence tracking (badge or phone seen on site)")),
]


class SuggestionPresenceSerializer(serializers.Serializer):
    first_seen_at = serializers.DateTimeField(help_text="First sighting on the site that day.")
    last_seen_at = serializers.DateTimeField(
        allow_null=True, help_text="Last sighting; `null` while the worker is still on site."
    )
    open = serializers.BooleanField(help_text="Still on site (no `last_seen_at`).")
    seen_from = ClockTimeField(help_text="`first_seen_at`, company time zone (`HH:MM`).")
    seen_until = ClockTimeField(
        allow_null=True, help_text="`last_seen_at`, company time zone; `null` while open."
    )
    zones = serializers.ListField(
        child=serializers.CharField(), help_text="Zone codes visited, in order (may be empty)."
    )
    visits = serializers.IntegerField(help_text="Visits that day (left and came back).")
    status = serializers.ChoiceField(
        choices=ATTENDANCE_STATUSES,
        help_text="What the presence alone suggests (same rules as a presence-only "
        "suggestion), to show when it differs from the check-ins.",
    )


class RollCallSuggestionSerializer(serializers.Serializer):
    worker_id = serializers.IntegerField()
    status = serializers.ChoiceField(
        choices=ATTENDANCE_STATUSES,
        allow_null=True,
        help_text="`null` when the worker only checked out: let the foreman decide.",
    )
    arrival = ClockTimeField(allow_null=True)
    departure = ClockTimeField(allow_null=True)
    first_in_at = serializers.DateTimeField(allow_null=True)
    last_out_at = serializers.DateTimeField(allow_null=True)
    checkin_count = serializers.IntegerField(help_text="`0` for a presence-only suggestion.")
    sources = serializers.ListField(
        child=serializers.ChoiceField(choices=SUGGESTION_SOURCES),
        help_text="Where the suggestion comes from (1.5): `[checkin]`, `[presence]` or "
        "`[checkin, presence]`. With check-ins, `status`, `arrival` and `departure` come "
        "from the check-ins.",
    )
    presence = SuggestionPresenceSerializer(
        allow_null=True,
        help_text="What presence tracking saw (1.5); `null` without a presence (tracking "
        "off for the site or the worker, or not seen).",
    )


class RollCallSuggestionsSerializer(serializers.Serializer):
    site_id = serializers.IntegerField()
    date = serializers.DateField()
    attendance_mode = serializers.ChoiceField(choices=AttendanceMode.choices)
    late_after = ClockTimeField()
    day_status = serializers.ChoiceField(choices=DAY_STATUSES)
    suggestions = RollCallSuggestionSerializer(many=True)


def suggestion_payload(s: selectors.Suggestion) -> dict:
    """``RollCallSuggestion`` data of a ``selectors.Suggestion``."""
    p = s.presence
    return {
        "worker_id": s.worker_id,
        "status": s.status,
        "arrival": s.arrival,
        "departure": s.departure,
        "first_in_at": s.first_in_at,
        "last_out_at": s.last_out_at,
        "checkin_count": s.checkin_count,
        "sources": list(s.sources),
        "presence": None
        if p is None
        else {
            "first_seen_at": p.first_seen_at,
            "last_seen_at": p.last_seen_at,
            "open": p.open,
            "seen_from": p.seen_from,
            "seen_until": p.seen_until,
            "zones": p.zones,
            "visits": p.visits,
            "status": p.status,
        },
    }


class GeofenceSerializer(serializers.Serializer):
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, coerce_to_string=False)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, coerce_to_string=False)
    radius_m = serializers.IntegerField()


class BadgeHashSerializer(serializers.Serializer):
    worker_id = serializers.IntegerField()
    payload_sha256 = serializers.CharField(
        help_text="Hex SHA-256 of the badge's QR text (UTF-8, as printed, with `PW1:`)."
    )
    issued_at = serializers.DateTimeField()


class KioskSerializer(serializers.Serializer):
    site_id = serializers.IntegerField()
    site_name = serializers.CharField()
    since = serializers.DateTimeField()


class CheckInSetupSerializer(serializers.Serializer):
    site_id = serializers.IntegerField()
    attendance_mode = serializers.ChoiceField(choices=AttendanceMode.choices)
    late_after = ClockTimeField()
    kiosk = serializers.BooleanField(help_text="This phone is the site's kiosk.")
    geofence = GeofenceSerializer(allow_null=True)
    badges = BadgeHashSerializer(many=True)


class CheckInDeviceSerializer(serializers.Serializer):
    attendance_mode = serializers.ChoiceField(choices=AttendanceMode.choices)
    kiosk = KioskSerializer(allow_null=True)


# --- payloads -----------------------------------------------------------------------------


def checkin_payload(request, checkin: CheckIn, duplicates=None) -> dict:
    from apps.checkin.files import api_checkin_photo_url

    flags = (
        selectors.flags_of(checkin, duplicates)
        if duplicates is not None
        else selectors.flags_for(checkin)
    )
    return {
        "id": checkin.pk,
        "client_uuid": checkin.client_uuid,
        "site_id": checkin.site_id,
        "worker_id": checkin.worker_id,
        "date": checkin.date,
        "direction": checkin.direction,
        "at": checkin.at,
        "received_at": checkin.received_at,
        "source": checkin.source,
        "latitude": checkin.latitude,
        "longitude": checkin.longitude,
        "accuracy_m": checkin.accuracy_m,
        "distance_m": checkin.distance_m,
        "inside_geofence": checkin.inside_geofence,
        "counted": checkin.counted,
        "draft_updated": checkin.draft_updated,
        "flags": flags,
        "photo_url": api_checkin_photo_url(request, checkin),
    }


def badge_hash_payload(badge) -> dict:
    return {
        "worker_id": badge.worker_id,
        "payload_sha256": tokens.payload_sha256(badge.payload),
        "issued_at": badge.issued_at,
    }
