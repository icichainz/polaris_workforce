//// Types shared by the actors, the API and the wire format.

import gleam/int
import gleam/list
import gleam/option.{type Option}
import gleam/result
import polaris_tracker/geo
import polaris_tracker/presence

pub type PersonKind {
  /// A Django `Worker`, detected through a BLE badge.
  Worker
  /// A Django `User` with the foreman role, detected through phone GPS.
  Foreman
}

pub type Person {
  Person(kind: PersonKind, id: Int)
}

pub fn person_to_string(person: Person) -> String {
  kind_to_string(person.kind) <> ":" <> int.to_string(person.id)
}

pub fn kind_to_string(kind: PersonKind) -> String {
  case kind {
    Worker -> "worker"
    Foreman -> "foreman"
  }
}

pub fn kind_from_string(text: String) -> Result(PersonKind, Nil) {
  case text {
    "worker" -> Ok(Worker)
    "foreman" -> Ok(Foreman)
    _ -> Error(Nil)
  }
}

pub type Zone {
  Zone(id: String, name: String)
}

pub type Gateway {
  Gateway(id: String, zone_id: Option(String))
}

pub type Badge {
  Badge(badge_id: String, worker_id: Int)
}

/// Everything the tracker knows about a site. Pushed by Django; the tracker
/// has no other source for it.
pub type SiteConfig {
  SiteConfig(
    tracking_enabled: Bool,
    geofence: Option(geo.Geofence),
    zones: List(Zone),
    gateways: List(Gateway),
    /// Only badges of workers with tracking enabled are pushed.
    badges: List(Badge),
    rules: presence.Rules,
  )
}

pub fn find_worker(config: SiteConfig, badge_id: String) -> Result(Int, Nil) {
  config.badges
  |> list.find(fn(badge) { badge.badge_id == badge_id })
  |> result.map(fn(badge) { badge.worker_id })
}

pub fn find_gateway(
  config: SiteConfig,
  gateway_id: String,
) -> Result(Gateway, Nil) {
  list.find(config.gateways, fn(gateway) { gateway.id == gateway_id })
}

/// Input from a foreman's phone. The foreman is identified by the token.
pub type Observation {
  GpsFix(client_uuid: String, at: Int, position: geo.LatLng, accuracy_m: Float)
  PhoneBle(
    client_uuid: String,
    at: Int,
    site_id: Int,
    badge_id: String,
    rssi: Int,
  )
}

pub fn observation_time(observation: Observation) -> Int {
  case observation {
    GpsFix(at:, ..) -> at
    PhoneBle(at:, ..) -> at
  }
}

pub fn observation_ref(observation: Observation) -> String {
  case observation {
    GpsFix(client_uuid:, ..) -> client_uuid
    PhoneBle(client_uuid:, ..) -> client_uuid
  }
}

/// Input from a fixed gateway. Site and zone come from the gateway's config.
pub type GatewaySighting {
  GatewaySighting(badge_id: String, at: Int, rssi: Int)
}

pub type BleSource {
  FromGateway
  FromPhone
}

/// One stored point of a person's trace.
pub type TrackPoint {
  GpsPoint(
    at: Int,
    position: geo.LatLng,
    accuracy_m: Float,
    /// The site whose geofence contains the fix, if any.
    site_id: Option(Int),
    /// The company of the device token that sent the fix. Off-site points
    /// are only shown to `travel` tokens of this company.
    company: Int,
  )
  BlePoint(
    at: Int,
    site_id: Int,
    zone_id: Option(String),
    rssi: Int,
    source: BleSource,
  )
}

pub fn track_point_time(point: TrackPoint) -> Int {
  case point {
    GpsPoint(at:, ..) -> at
    BlePoint(at:, ..) -> at
  }
}

/// A change in someone's presence on a site, as sent to Django and to the
/// live dashboard.
pub type PresenceEvent {
  PresenceEvent(
    event_id: String,
    site_id: Int,
    person: Person,
    change: presence.Change,
  )
}

pub type Rejection {
  Rejection(ref: String, code: String, message: String)
}

pub type IngestResult {
  IngestResult(
    accepted: Int,
    duplicates: Int,
    /// Accepted, but too old to change live presence: stored in the track only.
    late: Int,
    rejected: List(Rejection),
  )
}
