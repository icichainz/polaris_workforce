import gleam/option.{None, Some}
import polaris_tracker/presence.{
  Arriving, Away, Entered, Left, LeftGeofence, MovedZone, OnSite, Seen,
  SeenOutside, Tick, Timeout,
}

const minute = 60_000

fn rules() -> presence.Rules {
  presence.Rules(
    sightings_to_enter: 2,
    arrival_window_ms: 5 * minute,
    absence_to_leave_ms: 15 * minute,
  )
}

pub fn one_sighting_is_only_arriving_test() {
  let #(state, changes) = presence.step(Away, Seen(Some("A"), 1000), rules())
  assert state
    == Arriving(
      zone: Some("A"),
      first_seen: 1000,
      last_seen: 1000,
      sightings: 1,
    )
  assert changes == []
}

pub fn second_sighting_enters_dated_at_first_test() {
  let #(state, _) = presence.step(Away, Seen(Some("A"), 1000), rules())
  let #(state, changes) = presence.step(state, Seen(None, 2000), rules())
  assert state == OnSite(zone: Some("A"), since: 1000, last_seen: 2000)
  assert changes == [Entered(zone: Some("A"), at: 1000)]
}

pub fn arrival_window_expires_without_event_test() {
  let #(state, _) = presence.step(Away, Seen(None, 0), rules())
  let #(state, changes) = presence.step(state, Tick(6 * minute), rules())
  assert state == Away
  assert changes == []
}

pub fn sighting_after_window_restarts_arrival_test() {
  let #(state, _) = presence.step(Away, Seen(None, 0), rules())
  let #(state, changes) = presence.step(state, Seen(None, 6 * minute), rules())
  assert state
    == Arriving(
      zone: None,
      first_seen: 6 * minute,
      last_seen: 6 * minute,
      sightings: 1,
    )
  assert changes == []
}

pub fn zone_change_moves_test() {
  let on_site = OnSite(zone: Some("A"), since: 0, last_seen: minute)
  let #(state, changes) =
    presence.step(on_site, Seen(Some("B"), 2 * minute), rules())
  assert state == OnSite(zone: Some("B"), since: 0, last_seen: 2 * minute)
  assert changes == [MovedZone(from: Some("A"), to: "B", at: 2 * minute)]
}

pub fn late_sighting_in_other_zone_does_not_move_test() {
  let on_site = OnSite(zone: Some("A"), since: 0, last_seen: 5 * minute)
  let #(state, changes) =
    presence.step(on_site, Seen(Some("B"), 2 * minute), rules())
  assert state == on_site
  assert changes == []
}

pub fn sighting_without_zone_keeps_zone_test() {
  let on_site = OnSite(zone: Some("A"), since: 0, last_seen: minute)
  let #(state, changes) =
    presence.step(on_site, Seen(None, 2 * minute), rules())
  assert state == OnSite(zone: Some("A"), since: 0, last_seen: 2 * minute)
  assert changes == []
}

pub fn silence_leaves_dated_at_last_sighting_test() {
  let on_site = OnSite(zone: Some("A"), since: 0, last_seen: minute)
  let #(state, changes) = presence.step(on_site, Tick(10 * minute), rules())
  assert state == on_site
  assert changes == []
  let #(state, changes) = presence.step(on_site, Tick(17 * minute), rules())
  assert state == Away
  assert changes == [Left(zone: Some("A"), at: minute, reason: Timeout)]
}

pub fn gps_outside_leaves_early_test() {
  let on_site = OnSite(zone: None, since: 0, last_seen: minute)
  let #(state, changes) =
    presence.step(on_site, SeenOutside(2 * minute), rules())
  assert state == Away
  assert changes == [Left(zone: None, at: minute, reason: LeftGeofence)]
}

pub fn stale_outside_fix_is_ignored_test() {
  let on_site = OnSite(zone: None, since: 0, last_seen: 3 * minute)
  let #(state, changes) =
    presence.step(on_site, SeenOutside(2 * minute), rules())
  assert state == on_site
  assert changes == []
}

pub fn return_after_long_silence_leaves_then_arrives_test() {
  let on_site = OnSite(zone: Some("A"), since: 0, last_seen: minute)
  let #(state, changes) =
    presence.step(on_site, Seen(Some("A"), 30 * minute), rules())
  assert state
    == Arriving(
      zone: Some("A"),
      first_seen: 30 * minute,
      last_seen: 30 * minute,
      sightings: 1,
    )
  assert changes == [Left(zone: Some("A"), at: minute, reason: Timeout)]
}

pub fn replay_sorts_and_closes_visits_test() {
  // Out of order, as an offline phone would upload them.
  let signals = [
    Seen(Some("B"), 4 * minute),
    Seen(Some("A"), 0),
    Seen(Some("A"), minute),
    Seen(Some("A"), 40 * minute),
    Seen(Some("A"), 41 * minute),
  ]
  assert presence.replay(signals, rules(), until: 120 * minute)
    == [
      Entered(zone: Some("A"), at: 0),
      MovedZone(from: Some("A"), to: "B", at: 4 * minute),
      Left(zone: Some("B"), at: 4 * minute, reason: Timeout),
      Entered(zone: Some("A"), at: 40 * minute),
      Left(zone: Some("A"), at: 41 * minute, reason: Timeout),
    ]
}
