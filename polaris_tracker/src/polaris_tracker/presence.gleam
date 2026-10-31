//// The presence state machine for one person on one site.
////
//// Raw sightings flicker (BLE) and jump (GPS). `step` turns them into clean
//// `Entered`, `MovedZone` and `Left` changes. It is a pure function: the site
//// actor applies it to live data, and `replay` folds it over a day's stored
//// observations to recompute the same changes after the fact.
////
//// All times are Unix milliseconds.

import gleam/int
import gleam/list
import gleam/option.{type Option, Some}

pub type Presence {
  Away
  /// Seen, but not yet often enough to count as on site.
  Arriving(
    zone: Option(String),
    first_seen: Int,
    last_seen: Int,
    sightings: Int,
  )
  OnSite(zone: Option(String), since: Int, last_seen: Int)
}

pub type Signal {
  /// A BLE sighting or a GPS fix inside the geofence. Phone BLE and GPS have
  /// no zone; a gateway sighting carries the gateway's zone.
  Seen(zone: Option(String), at: Int)
  /// A GPS fix clearly outside the geofence.
  SeenOutside(at: Int)
  /// Time passing, from the site actor's timer.
  Tick(now: Int)
}

pub type LeaveReason {
  Timeout
  LeftGeofence
}

pub type Change {
  /// Dated at the first sighting of the visit.
  Entered(zone: Option(String), at: Int)
  MovedZone(from: Option(String), to: String, at: Int)
  /// Dated at the last moment the person was known to be on site.
  Left(zone: Option(String), at: Int, reason: LeaveReason)
}

/// Thresholds, from the site's settings (company → site in Django).
pub type Rules {
  Rules(
    /// Sightings needed before `Entered` fires.
    sightings_to_enter: Int,
    /// Time allowed, from the first sighting, to collect them.
    arrival_window_ms: Int,
    /// Silence after which someone on site has left.
    absence_to_leave_ms: Int,
  )
}

pub fn step(
  state: Presence,
  signal: Signal,
  rules: Rules,
) -> #(Presence, List(Change)) {
  case state, signal {
    Away, Seen(zone:, at:) -> arrive(zone, at, at, 1, rules)
    Away, SeenOutside(_) | Away, Tick(_) -> #(Away, [])

    // Too long since the first sighting: this is a fresh arrival.
    Arriving(first_seen:, ..), Seen(zone:, at:)
      if at - first_seen > rules.arrival_window_ms
    -> arrive(zone, at, at, 1, rules)
    Arriving(zone: known, first_seen:, last_seen:, sightings:), Seen(zone:, at:)
    ->
      arrive(
        option.or(zone, known),
        int.min(first_seen, at),
        int.max(last_seen, at),
        sightings + 1,
        rules,
      )
    Arriving(last_seen:, ..), SeenOutside(at) if at >= last_seen -> #(Away, [])
    Arriving(first_seen:, ..), Tick(now)
      if now - first_seen > rules.arrival_window_ms
    -> #(Away, [])
    Arriving(..), SeenOutside(_) | Arriving(..), Tick(_) -> #(state, [])

    // Silent for too long before this sighting: they left and came back.
    OnSite(zone: current, last_seen:, ..), Seen(at:, ..)
      if at - last_seen > rules.absence_to_leave_ms
    -> {
      let #(next, changes) = step(Away, signal, rules)
      #(next, [Left(zone: current, at: last_seen, reason: Timeout), ..changes])
    }
    OnSite(zone: current, since:, last_seen:), Seen(zone: seen_zone, at:) ->
      case seen_zone {
        // Only a newer sighting can move someone; a late one only refreshes.
        Some(new_zone) if seen_zone != current && at >= last_seen -> #(
          OnSite(zone: seen_zone, since:, last_seen: at),
          [MovedZone(from: current, to: new_zone, at:)],
        )
        _ -> #(
          OnSite(zone: current, since:, last_seen: int.max(last_seen, at)),
          [],
        )
      }
    OnSite(zone:, last_seen:, ..), SeenOutside(at) if at >= last_seen -> #(
      Away,
      [
        Left(zone:, at: last_seen, reason: LeftGeofence),
      ],
    )
    OnSite(zone:, last_seen:, ..), Tick(now)
      if now - last_seen > rules.absence_to_leave_ms
    -> #(Away, [Left(zone:, at: last_seen, reason: Timeout)])
    OnSite(..), SeenOutside(_) | OnSite(..), Tick(_) -> #(state, [])
  }
}

fn arrive(
  zone: Option(String),
  first_seen: Int,
  last_seen: Int,
  sightings: Int,
  rules: Rules,
) -> #(Presence, List(Change)) {
  case sightings >= rules.sightings_to_enter {
    True -> #(OnSite(zone:, since: first_seen, last_seen:), [
      Entered(zone:, at: first_seen),
    ])
    False -> #(Arriving(zone:, first_seen:, last_seen:, sightings:), [])
  }
}

/// Recomputes the changes for one person from stored signals, in time order,
/// then lets time run until `until` so that open visits can close.
pub fn replay(
  signals: List(Signal),
  rules: Rules,
  until until: Int,
) -> List(Change) {
  let #(_, changes) =
    signals
    |> list.sort(fn(a, b) { int.compare(signal_time(a), signal_time(b)) })
    |> list.append([Tick(until)])
    |> list.fold(#(Away, []), fn(acc, signal) {
      let #(state, changes) = acc
      let #(state, new_changes) = step(state, signal, rules)
      #(state, list.append(list.reverse(new_changes), changes))
    })
  list.reverse(changes)
}

pub fn signal_time(signal: Signal) -> Int {
  case signal {
    Seen(at:, ..) -> at
    SeenOutside(at) -> at
    Tick(now) -> now
  }
}
