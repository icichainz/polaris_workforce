"""Presence sessions from presence events: a pure fold, no database.

``services.rebuild_sessions`` reads one person's events on one site (from a safe starting
point), folds them with ``fold`` and stores the visits. Because the visits are always
recomputed from every stored event in time order, events that arrive late, out of order
or twice give the same sessions as a clean, ordered stream.

Rules, over the events sorted by time (then ``entered`` < ``moved_zone`` < ``left`` at
the same instant, then event id):

- ``entered`` opens a visit. If one is still open (its ``left`` has not arrived yet), that
  one ends at its last event.
- ``moved_zone`` adds the zones (``from_zone`` then ``zone``) to the open visit, or opens
  one when its ``entered`` is missing.
- ``left`` closes the open visit at the event's time (or makes a visit of one instant when
  its ``entered`` is missing) with the reason.

Zones are listed once each, in order of first visit.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass, field

ENTERED = "entered"
MOVED_ZONE = "moved_zone"
LEFT = "left"
_RANK = {ENTERED: 0, MOVED_ZONE: 1, LEFT: 2}


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str
    at: datetime.datetime
    zone: str = ""
    from_zone: str = ""
    reason: str = ""


@dataclass
class Visit:
    started_at: datetime.datetime
    last_event_at: datetime.datetime
    ended_at: datetime.datetime | None = None
    end_reason: str = ""
    zones: list[str] = field(default_factory=list)
    event_count: int = 0

    def add_zone(self, zone: str) -> None:
        if zone and zone not in self.zones:
            self.zones.append(zone)

    def key(self) -> tuple:
        """What a stored session must equal for the rebuild to leave it alone."""
        return (
            self.started_at,
            self.ended_at,
            self.end_reason,
            tuple(self.zones),
            self.last_event_at,
            self.event_count,
        )


def sort_key(event: Event) -> tuple:
    return (event.at, _RANK.get(event.type, 3), str(event.event_id))


def fold(events: Iterable[Event]) -> list[Visit]:
    """The visits the events describe, oldest first."""
    visits: list[Visit] = []
    current: Visit | None = None
    for event in sorted(events, key=sort_key):
        if event.type == ENTERED:
            if current is not None:
                current.ended_at = current.last_event_at
                visits.append(current)
            current = Visit(started_at=event.at, last_event_at=event.at)
            current.add_zone(event.zone)
        elif event.type == MOVED_ZONE:
            if current is None:
                current = Visit(started_at=event.at, last_event_at=event.at)
            current.add_zone(event.from_zone)
            current.add_zone(event.zone)
        elif event.type == LEFT:
            if current is None:
                current = Visit(started_at=event.at, last_event_at=event.at)
            current.add_zone(event.zone)
            current.ended_at = event.at
            current.end_reason = event.reason
        else:  # an unknown type never reaches here (validated on ingest)
            continue
        current.last_event_at = max(current.last_event_at, event.at)
        current.event_count += 1
        if event.type == LEFT:
            visits.append(current)
            current = None
    if current is not None:
        visits.append(current)
    return visits
