//// Presence sessions recomputed from the stored track, for Django's nightly
//// reconciliation (`GET /internal/sites/{site_id}/sessions`).
////
//// The same `presence.step` that drives live presence is replayed over the
//// day's points with the site's current rules and geofence, so late data
//// (stored but never applied live) is taken into account.

import gleam/dict
import gleam/int
import gleam/list
import gleam/option.{type Option, None, Some}
import gleam/order
import gleam/string
import polaris_tracker/domain.{
  type Person, type SiteConfig, type TrackPoint, BlePoint, GpsPoint,
}
import polaris_tracker/geo
import polaris_tracker/presence

pub type Session {
  Session(
    person: Person,
    /// The first sighting of the visit (Unix milliseconds).
    started_at: Int,
    /// The last sighting, or `None` while the visit may still be going on.
    ended_at: Option(Int),
    /// Zones visited, in order of first visit; empty for GPS and phone BLE.
    zones: List(String),
  )
}

/// `points` are a site's points for the day, oldest first (see
/// `track_store.SiteDay`). Time then runs until `until`, closing visits that
/// went silent before it; visits still open at `until` have no `ended_at`.
pub fn recompute(
  points: List(#(Person, TrackPoint)),
  site_id site_id: Int,
  config config: SiteConfig,
  until until: Int,
) -> List(Session) {
  points
  |> list.fold(dict.new(), fn(signals, entry) {
    let #(person, point) = entry
    case signal(point, site_id, config) {
      Some(signal) ->
        dict.upsert(signals, person, fn(existing) {
          [signal, ..option.unwrap(existing, [])]
        })
      None -> signals
    }
  })
  |> dict.to_list
  |> list.flat_map(fn(entry) {
    let #(person, signals) = entry
    presence.replay(list.reverse(signals), config.rules, until:)
    |> to_sessions(person, _)
  })
  |> list.sort(fn(a, b) {
    case int.compare(a.started_at, b.started_at) {
      order.Eq ->
        string.compare(
          domain.person_to_string(a.person),
          domain.person_to_string(b.person),
        )
      other -> other
    }
  })
}

/// What a stored point says about this site. GPS fixes are classified again
/// against the site's current geofence (or, without one, by the site they
/// were stored with).
fn signal(
  point: TrackPoint,
  site_id: Int,
  config: SiteConfig,
) -> Option(presence.Signal) {
  case point {
    BlePoint(at:, site_id: seen_on, zone_id:, ..) if seen_on == site_id ->
      Some(presence.Seen(zone: zone_id, at:))
    BlePoint(..) -> None
    GpsPoint(at:, position:, accuracy_m:, site_id: stored, ..) ->
      case config.geofence {
        Some(fence) ->
          case geo.classify(fence, position, accuracy_m:) {
            geo.Inside -> Some(presence.Seen(zone: None, at:))
            geo.Outside -> Some(presence.SeenOutside(at))
            geo.Uncertain -> None
          }
        None ->
          case stored == Some(site_id) {
            True -> Some(presence.Seen(zone: None, at:))
            False -> None
          }
      }
  }
}

fn to_sessions(
  person: Person,
  changes: List(presence.Change),
) -> List(Session) {
  let #(open, done) =
    list.fold(changes, #(None, []), fn(acc, change) {
      let #(open, done) = acc
      case change, open {
        presence.Entered(zone:, at:), _ -> #(
          Some(
            Session(person:, started_at: at, ended_at: None, zones: case zone {
              Some(zone) -> [zone]
              None -> []
            }),
          ),
          close(open, None, done),
        )
        presence.MovedZone(to:, ..), Some(session) -> #(
          Some(case list.contains(session.zones, to) {
            True -> session
            False -> Session(..session, zones: list.append(session.zones, [to]))
          }),
          done,
        )
        presence.Left(at:, ..), Some(_) -> #(None, close(open, Some(at), done))
        _, None -> #(None, done)
      }
    })
  close(open, None, done) |> list.reverse
}

fn close(
  open: Option(Session),
  ended_at: Option(Int),
  done: List(Session),
) -> List(Session) {
  case open {
    Some(session) -> [Session(..session, ended_at:), ..done]
    None -> done
  }
}
