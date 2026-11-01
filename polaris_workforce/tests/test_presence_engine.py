"""The session fold (``apps.presence.engine``): pure, no database."""

import datetime
import itertools

from apps.presence import engine

T = datetime.datetime(2026, 10, 31, 7, 0, tzinfo=datetime.UTC)


def at(minutes: int) -> datetime.datetime:
    return T + datetime.timedelta(minutes=minutes)


def ev(n, kind, minutes, zone="", from_zone="", reason=""):
    return engine.Event(f"e{n}", kind, at(minutes), zone, from_zone, reason)


DAY = [
    ev(1, "entered", 0, "A"),
    ev(2, "moved_zone", 10, "B", "A"),
    ev(3, "left", 300, "B", reason="timeout"),
    ev(4, "entered", 360, "C"),
    ev(5, "left", 600, "C", reason="left_geofence"),
]


def summary(visits):
    return [(v.started_at, v.ended_at, v.zones, v.end_reason, v.event_count) for v in visits]


EXPECTED = [
    (at(0), at(300), ["A", "B"], "timeout", 3),
    (at(360), at(600), ["C"], "left_geofence", 2),
]


def test_a_day_folds_into_two_visits():
    assert summary(engine.fold(DAY)) == EXPECTED


def test_every_order_of_the_same_events_gives_the_same_visits():
    for order in itertools.permutations(DAY):
        assert summary(engine.fold(order)) == EXPECTED


def test_an_open_visit_has_no_end():
    (visit,) = engine.fold(DAY[:2])
    assert visit.ended_at is None
    assert visit.last_event_at == at(10)


def test_a_visit_whose_entered_is_missing_starts_at_its_first_event():
    (visit,) = engine.fold([DAY[1], DAY[2]])
    assert (visit.started_at, visit.ended_at, visit.zones) == (at(10), at(300), ["A", "B"])


def test_a_lone_left_is_a_visit_of_one_instant():
    (visit,) = engine.fold([DAY[2]])
    assert visit.started_at == visit.ended_at == at(300)


def test_an_entered_while_open_ends_the_open_visit_at_its_last_event():
    first, second = engine.fold([DAY[0], DAY[1], DAY[3]])
    assert first.ended_at == at(10)
    assert second.started_at == at(360) and second.ended_at is None


def test_same_instant_entered_sorts_before_left():
    (visit,) = engine.fold([ev(2, "left", 0, "A"), ev(1, "entered", 0, "A")])
    assert visit.event_count == 2 and visit.ended_at == at(0)
