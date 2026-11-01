//// JSON decoders for request bodies and encoders for responses.
//// The shapes are specified in `docs/api/tracker-openapi.yaml`.

import gleam/dynamic/decode.{type Decoder}
import gleam/int
import gleam/json.{type Json}
import gleam/list
import gleam/option.{None, Some}
import gleam/string
import polaris_tracker/clock
import polaris_tracker/domain.{
  type GatewaySighting, type IngestResult, type Observation, type Person,
  type PresenceEvent, type Rejection, type SiteConfig, type TrackPoint, Badge,
  BlePoint, FromGateway, FromPhone, Gateway, GatewaySighting, GpsFix, GpsPoint,
  PhoneBle, SiteConfig, Zone,
}
import polaris_tracker/geo
import polaris_tracker/presence.{type Presence}

// ---- Decoders -------------------------------------------------------------

pub fn observations_decoder() -> Decoder(List(Observation)) {
  decode.at(["observations"], decode.list(observation_decoder()))
}

fn observation_decoder() -> Decoder(Observation) {
  use kind <- decode.field("type", decode.string)
  use client_uuid <- decode.field("client_uuid", non_empty_string())
  use at <- decode.field("at", timestamp())
  case kind {
    "gps" -> {
      use position <- decode.then(position_decoder())
      use accuracy_m <- decode.field("accuracy_m", non_negative_number())
      decode.success(GpsFix(client_uuid:, at:, position:, accuracy_m:))
    }
    "ble" -> {
      use site_id <- decode.field("site_id", decode.int)
      use badge_id <- decode.field("badge_id", non_empty_string())
      use rssi <- decode.field("rssi", decode.int)
      decode.success(PhoneBle(client_uuid:, at:, site_id:, badge_id:, rssi:))
    }
    _ ->
      decode.failure(
        GpsFix("", 0, geo.LatLng(0.0, 0.0), 0.0),
        "observation type gps or ble",
      )
  }
}

/// `#(batch_id, sightings)`
pub fn gateway_batch_decoder() -> Decoder(#(String, List(GatewaySighting))) {
  use batch_id <- decode.field("batch_id", non_empty_string())
  use sightings <- decode.field("sightings", decode.list(sighting_decoder()))
  decode.success(#(batch_id, sightings))
}

fn sighting_decoder() -> Decoder(GatewaySighting) {
  use badge_id <- decode.field("badge_id", non_empty_string())
  use at <- decode.field("at", timestamp())
  use rssi <- decode.field("rssi", decode.int)
  decode.success(GatewaySighting(badge_id:, at:, rssi:))
}

pub fn site_config_decoder() -> Decoder(SiteConfig) {
  use tracking_enabled <- decode.field("tracking_enabled", decode.bool)
  use geofence <- decode.optional_field(
    "geofence",
    None,
    decode.optional(geofence_decoder()),
  )
  use zones <- decode.optional_field("zones", [], decode.list(zone_decoder()))
  use gateways <- decode.optional_field(
    "gateways",
    [],
    decode.list(gateway_decoder()),
  )
  use badges <- decode.optional_field(
    "badges",
    [],
    decode.list(badge_decoder()),
  )
  use rules <- decode.field("rules", rules_decoder())
  decode.success(SiteConfig(
    tracking_enabled:,
    geofence:,
    zones:,
    gateways:,
    badges:,
    rules:,
  ))
}

fn geofence_decoder() -> Decoder(geo.Geofence) {
  use center <- decode.then(position_decoder())
  use radius_m <- decode.field("radius_m", positive_number())
  decode.success(geo.Geofence(center:, radius_m:))
}

fn zone_decoder() -> Decoder(domain.Zone) {
  use id <- decode.field("id", non_empty_string())
  use name <- decode.field("name", decode.string)
  decode.success(Zone(id:, name:))
}

fn gateway_decoder() -> Decoder(domain.Gateway) {
  use id <- decode.field("id", non_empty_string())
  use zone_id <- decode.optional_field(
    "zone_id",
    None,
    decode.optional(decode.string),
  )
  decode.success(Gateway(id:, zone_id:))
}

fn badge_decoder() -> Decoder(domain.Badge) {
  use badge_id <- decode.field("badge_id", non_empty_string())
  use worker_id <- decode.field("worker_id", decode.int)
  decode.success(Badge(badge_id:, worker_id:))
}

fn rules_decoder() -> Decoder(presence.Rules) {
  use sightings_to_enter <- decode.field("sightings_to_enter", positive_int())
  use arrival_window <- decode.field("arrival_window_seconds", positive_int())
  use absence_to_leave <- decode.field(
    "absence_to_leave_seconds",
    positive_int(),
  )
  decode.success(presence.Rules(
    sightings_to_enter:,
    arrival_window_ms: arrival_window * 1000,
    absence_to_leave_ms: absence_to_leave * 1000,
  ))
}

/// The inverse of `presence_event_json`, for events read back from the
/// outbox table.
pub fn presence_event_decoder() -> Decoder(PresenceEvent) {
  use event_id <- decode.field("event_id", non_empty_string())
  use site_id <- decode.field("site_id", decode.int)
  use person <- decode.field("subject", person_decoder())
  use kind <- decode.field("type", decode.string)
  use at <- decode.field("at", timestamp())
  use zone_id <- decode.field("zone_id", decode.optional(decode.string))
  use from_zone_id <- decode.field(
    "from_zone_id",
    decode.optional(decode.string),
  )
  use reason <- decode.field("reason", decode.optional(decode.string))
  let event = fn(change) {
    decode.success(domain.PresenceEvent(event_id:, site_id:, person:, change:))
  }
  case kind, zone_id, reason {
    "entered", _, _ -> event(presence.Entered(zone: zone_id, at:))
    "moved_zone", Some(to), _ ->
      event(presence.MovedZone(from: from_zone_id, to:, at:))
    "left", _, Some("timeout") ->
      event(presence.Left(zone: zone_id, at:, reason: presence.Timeout))
    "left", _, Some("left_geofence") ->
      event(presence.Left(zone: zone_id, at:, reason: presence.LeftGeofence))
    _, _, _ ->
      decode.failure(
        domain.PresenceEvent(
          event_id:,
          site_id:,
          person:,
          change: presence.Entered(zone: None, at:),
        ),
        "presence event",
      )
  }
}

pub fn person_decoder() -> Decoder(Person) {
  use kind <- decode.field("kind", {
    use text <- decode.then(decode.string)
    case domain.kind_from_string(text) {
      Ok(kind) -> decode.success(kind)
      Error(Nil) -> decode.failure(domain.Worker, "worker or foreman")
    }
  })
  use id <- decode.field("id", decode.int)
  decode.success(domain.Person(kind:, id:))
}

fn position_decoder() -> Decoder(geo.LatLng) {
  use lat <- decode.field("lat", number())
  use lng <- decode.field("lng", number())
  let position = geo.LatLng(lat:, lng:)
  case geo.is_valid(position) {
    True -> decode.success(position)
    False ->
      decode.failure(position, "latitude -90..90 and longitude -180..180")
  }
}

fn timestamp() -> Decoder(Int) {
  use text <- decode.then(decode.string)
  case clock.parse_rfc3339(text) {
    Ok(ms) -> decode.success(ms)
    Error(Nil) -> decode.failure(0, "RFC 3339 timestamp")
  }
}

/// JSON numbers may arrive with or without a decimal point.
fn number() -> Decoder(Float) {
  decode.one_of(decode.float, [decode.int |> decode.map(int.to_float)])
}

fn non_negative_number() -> Decoder(Float) {
  use value <- decode.then(number())
  case value >=. 0.0 {
    True -> decode.success(value)
    False -> decode.failure(value, "number >= 0")
  }
}

fn positive_number() -> Decoder(Float) {
  use value <- decode.then(number())
  case value >. 0.0 {
    True -> decode.success(value)
    False -> decode.failure(value, "number > 0")
  }
}

fn positive_int() -> Decoder(Int) {
  use value <- decode.then(decode.int)
  case value > 0 {
    True -> decode.success(value)
    False -> decode.failure(value, "integer > 0")
  }
}

fn non_empty_string() -> Decoder(String) {
  use value <- decode.then(decode.string)
  case value {
    "" -> decode.failure(value, "non-empty string")
    _ -> decode.success(value)
  }
}

// ---- Encoders -------------------------------------------------------------

pub fn person_json(person: Person) -> Json {
  json.object([
    #("kind", json.string(domain.kind_to_string(person.kind))),
    #("id", json.int(person.id)),
  ])
}

pub fn presence_event_json(event: PresenceEvent) -> Json {
  let #(kind, at, zone_id, from_zone_id, reason) = case event.change {
    presence.Entered(zone:, at:) -> #("entered", at, zone, None, None)
    presence.MovedZone(from:, to:, at:) -> #(
      "moved_zone",
      at,
      Some(to),
      from,
      None,
    )
    presence.Left(zone:, at:, reason:) -> #(
      "left",
      at,
      zone,
      None,
      Some(case reason {
        presence.Timeout -> "timeout"
        presence.LeftGeofence -> "left_geofence"
      }),
    )
  }
  json.object([
    #("event_id", json.string(event.event_id)),
    #("site_id", json.int(event.site_id)),
    #("subject", person_json(event.person)),
    #("type", json.string(kind)),
    #("at", json.string(clock.to_rfc3339(at))),
    #("zone_id", json.nullable(zone_id, json.string)),
    #("from_zone_id", json.nullable(from_zone_id, json.string)),
    #("reason", json.nullable(reason, json.string)),
  ])
}

pub fn presence_events_json(events: List(PresenceEvent)) -> Json {
  json.object([#("events", json.array(events, presence_event_json))])
}

pub fn snapshot_json(
  site_id: Int,
  generated_at: Int,
  people: List(#(Person, Presence)),
) -> Json {
  json.object([
    #("site_id", json.int(site_id)),
    #("generated_at", json.string(clock.to_rfc3339(generated_at))),
    #(
      "people",
      json.preprocessed_array(list.filter_map(people, person_presence_json)),
    ),
  ])
}

fn person_presence_json(entry: #(Person, Presence)) -> Result(Json, Nil) {
  let #(person, state) = entry
  let fields = case state {
    presence.Away -> Error(Nil)
    presence.Arriving(zone:, first_seen:, last_seen:, ..) ->
      Ok(#("arriving", zone, first_seen, last_seen))
    presence.OnSite(zone:, since:, last_seen:) ->
      Ok(#("on_site", zone, since, last_seen))
  }
  case fields {
    Error(Nil) -> Error(Nil)
    Ok(#(state, zone, since, last_seen)) ->
      Ok(
        json.object([
          #("subject", person_json(person)),
          #("state", json.string(state)),
          #("zone_id", json.nullable(zone, json.string)),
          #("since", json.string(clock.to_rfc3339(since))),
          #("last_seen", json.string(clock.to_rfc3339(last_seen))),
        ]),
      )
  }
}

pub fn track_json(
  person: Person,
  from: Int,
  to: Int,
  points: List(TrackPoint),
) -> Json {
  json.object([
    #("subject", person_json(person)),
    #("from", json.string(clock.to_rfc3339(from))),
    #("to", json.string(clock.to_rfc3339(to))),
    #("points", json.array(points, track_point_json)),
  ])
}

fn track_point_json(point: TrackPoint) -> Json {
  case point {
    GpsPoint(at:, position:, accuracy_m:, site_id:, ..) ->
      json.object([
        #("type", json.string("gps")),
        #("at", json.string(clock.to_rfc3339(at))),
        #("lat", json.float(position.lat)),
        #("lng", json.float(position.lng)),
        #("accuracy_m", json.float(accuracy_m)),
        #("site_id", json.nullable(site_id, json.int)),
      ])
    BlePoint(at:, site_id:, zone_id:, rssi:, source:) ->
      json.object([
        #("type", json.string("ble")),
        #("at", json.string(clock.to_rfc3339(at))),
        #("site_id", json.int(site_id)),
        #("zone_id", json.nullable(zone_id, json.string)),
        #("rssi", json.int(rssi)),
        #(
          "source",
          json.string(case source {
            FromGateway -> "gateway"
            FromPhone -> "phone"
          }),
        ),
      ])
  }
}

pub fn ingest_result_json(result: IngestResult) -> Json {
  json.object([
    #("accepted", json.int(result.accepted)),
    #("duplicates", json.int(result.duplicates)),
    #("late", json.int(result.late)),
    #("rejected", json.array(result.rejected, rejection_json)),
  ])
}

fn rejection_json(rejection: Rejection) -> Json {
  json.object([
    #("ref", json.string(rejection.ref)),
    #("code", json.string(rejection.code)),
    #("message", json.string(rejection.message)),
  ])
}

/// The error shape shared with the Django API.
pub fn error_json(code: String, message: String, details: Json) -> Json {
  json.object([
    #("code", json.string(code)),
    #("message", json.string(message)),
    #("details", details),
  ])
}

pub fn decode_errors_json(errors: List(decode.DecodeError)) -> Json {
  json.object([
    #(
      "errors",
      json.array(errors, fn(error) {
        json.object([
          #("path", json.string(join_path(error.path))),
          #("expected", json.string(error.expected)),
          #("found", json.string(error.found)),
        ])
      }),
    ),
  ])
}

fn join_path(path: List(String)) -> String {
  case path {
    [] -> "$"
    _ -> "$." <> string.join(path, ".")
  }
}
