"""Presence API bodies (contract: ``docs/api/presence-django-openapi.yaml``)."""

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.api.fields import ClockTimeField
from apps.api.idempotency import client_uuid_field

from ..services import MAX_EVENTS, MAX_ID, EventIn

SUBJECT_KINDS = [
    ("worker", _("A worker (`Worker` id)")),
    ("foreman", _("A foreman (user id)")),
]
EVENT_TYPES = [
    ("entered", _("Arrived on the site")),
    ("moved_zone", _("Moved to another zone")),
    ("left", _("Left the site")),
]
LEAVE_REASONS = [
    ("timeout", _("Not seen for `absence_to_leave_seconds`")),
    ("left_geofence", _("A GPS fix clearly outside the site")),
]


def office_alert_kinds():
    """``SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]["KindEnum"]``: the office alerts'
    ``kind`` (``apps.agents``) keeps the name ``KindEnum`` now that ``subject.kind`` is a
    second enum called ``kind`` in the schema."""
    from apps.agents.api.serializers import AlertSerializer

    return list(AlertSerializer().fields["kind"].choices.items())


# --- tracking token and consent (phone) --------------------------------------------------


class TrackingHoursSerializer(serializers.Serializer):
    start = ClockTimeField()
    end = ClockTimeField()


class TrackingSettingsSerializer(serializers.Serializer):
    gps_interval_seconds = serializers.IntegerField(
        help_text="Setting `foreman_gps_interval_seconds` (the shortest of the token's sites)."
    )
    tracking_hours = TrackingHoursSerializer(
        help_text="Settings `tracking_start` / `tracking_end`, company time zone (the "
        "earliest start and the latest end of the token's sites). No GPS outside them."
    )
    retention_days = serializers.IntegerField(
        help_text="Setting `track_retention_days`: raw positions are deleted after this."
    )


class TrackingTokenSerializer(serializers.Serializer):
    token = serializers.CharField(help_text='JWT (HS256) for the tracker, `kind = "device"`.')
    expires_at = serializers.DateTimeField(help_text="12 hours after issue. Ask again before.")
    tracker_url = serializers.CharField(help_text="Base URL of the tracker service.")
    sites = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="Sites the token covers (its `sites` claim): your sites with tracking on.",
    )
    settings = TrackingSettingsSerializer()


class ConsentRequestSerializer(serializers.Serializer):
    client_uuid = client_uuid_field()
    accepted = serializers.BooleanField(
        help_text="true: the foreman accepted the notice; false: refused or withdrawn "
        "(taken at any time)."
    )
    text_version = serializers.CharField(
        max_length=32,
        help_text="The version of the notice shown (`details.text_version` of the "
        "`consent_required` answer; today `2026-11-01`). An acceptance must name the "
        "current one; a refusal may name any.",
    )


class ConsentSerializer(serializers.Serializer):
    accepted = serializers.BooleanField()
    at = serializers.DateTimeField(help_text="When the server recorded the answer.")
    text_version = serializers.CharField()


# --- events (tracker) --------------------------------------------------------------------


class SubjectSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=SUBJECT_KINDS)
    id = serializers.IntegerField(min_value=1, max_value=MAX_ID)


class PresenceEventSerializer(serializers.Serializer):
    """Same shape as ``PresenceEvent`` in ``tracker-openapi.yaml``."""

    event_id = serializers.UUIDField(help_text="Unique; stored once.")
    site_id = serializers.IntegerField(min_value=1, max_value=MAX_ID)
    subject = SubjectSerializer()
    type = serializers.ChoiceField(choices=EVENT_TYPES)
    at = serializers.DateTimeField(
        help_text="`entered`: first sighting of the visit; `moved_zone`: first sighting in "
        "the new zone; `left`: last sighting on the site."
    )
    zone_id = serializers.CharField(
        max_length=64, allow_null=True, allow_blank=True, help_text="Zone code, or null."
    )
    from_zone_id = serializers.CharField(
        max_length=64, allow_null=True, allow_blank=True, help_text="Only for `moved_zone`."
    )
    reason = serializers.ChoiceField(
        choices=LEAVE_REASONS, allow_null=True, help_text="Only for `left`."
    )

    def to_event(self) -> EventIn:
        data = self.validated_data
        return EventIn(
            event_id=data["event_id"],
            site_id=data["site_id"],
            subject_kind=data["subject"]["kind"],
            subject_id=data["subject"]["id"],
            type=data["type"],
            at=data["at"],
            zone=data["zone_id"] or "",
            from_zone=data["from_zone_id"] or "",
            reason=data["reason"] or "",
        )


class EventBatchSerializer(serializers.Serializer):
    """The envelope: each item is checked on its own (``PresenceEventSerializer``), so one
    bad item does not hold up the tracker's queue."""

    events = serializers.ListField(
        child=serializers.DictField(), max_length=MAX_EVENTS, allow_empty=True
    )


class PresenceEventBatchSerializer(serializers.Serializer):
    """The request as documented (``EventBatchSerializer`` validates it)."""

    events = PresenceEventSerializer(many=True, max_length=MAX_EVENTS)


class PresenceIngestResultSerializer(serializers.Serializer):
    stored = serializers.IntegerField(help_text="New events, applied to the sessions.")
    duplicates = serializers.IntegerField(
        help_text="Already stored earlier (same `event_id`), or twice in this batch."
    )
    ignored = serializers.IntegerField(
        help_text="New, stored, but not applied: an unknown site or subject (or another "
        "company's), a site of an inactive company, a worker inactive or with tracking off, "
        "a foreman who had not accepted the notice at the event's time."
    )
    rejected = serializers.IntegerField(
        help_text="Items that do not match the schema: not stored, listed in the log. "
        "The batch still counts as delivered."
    )


# --- sessions (phone) --------------------------------------------------------------------


class SessionsQuerySerializer(serializers.Serializer):
    date = serializers.DateField(help_text="Local date in the company time zone.")


class PresenceSessionSerializer(serializers.Serializer):
    subject = SubjectSerializer()
    first_seen_at = serializers.DateTimeField()
    last_seen_at = serializers.DateTimeField(
        allow_null=True, help_text="Null while the person is still on site."
    )
    open = serializers.BooleanField()
    zones = serializers.ListField(
        child=serializers.CharField(), help_text="Zone codes visited, in order of first visit."
    )
    visits = serializers.IntegerField(help_text="Separate visits (entered → left) that day.")


class SitePresenceSessionsSerializer(serializers.Serializer):
    site_id = serializers.IntegerField()
    date = serializers.DateField()
    sessions = PresenceSessionSerializer(many=True)
