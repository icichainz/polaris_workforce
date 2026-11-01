import gleam/json
import gleam/option.{None, Some}
import gleam/string
import polaris_tracker/clock
import polaris_tracker/domain.{
  Badge, Gateway, GatewaySighting, GpsFix, Person, PhoneBle, PresenceEvent,
  SiteConfig, Worker, Zone,
}
import polaris_tracker/geo
import polaris_tracker/presence
import polaris_tracker/wire

pub fn rfc3339_round_trip_test() {
  let assert Ok(ms) = clock.parse_rfc3339("2026-10-31T07:12:03.250Z")
  assert clock.to_rfc3339(ms) == "2026-10-31T07:12:03.25Z"
    || clock.to_rfc3339(ms) == "2026-10-31T07:12:03.250Z"
  assert clock.parse_rfc3339("2026-10-31T08:12:03+01:00")
    == clock.parse_rfc3339("2026-10-31T07:12:03Z")
}

pub fn observations_decode_test() {
  let body =
    "{\"observations\": [
      {\"client_uuid\": \"a\", \"type\": \"gps\", \"at\": \"1970-01-01T00:00:01Z\",
       \"lat\": 6.13, \"lng\": 1, \"accuracy_m\": 12},
      {\"client_uuid\": \"b\", \"type\": \"ble\", \"at\": \"1970-01-01T00:00:02Z\",
       \"site_id\": 3, \"badge_id\": \"BDG-1\", \"rssi\": -70}
    ]}"
  assert json.parse(body, wire.observations_decoder())
    == Ok([
      GpsFix("a", 1000, geo.LatLng(6.13, 1.0), 12.0),
      PhoneBle("b", 2000, 3, "bdg-1", -70),
    ])
}

pub fn invalid_position_is_rejected_test() {
  let body =
    "{\"observations\": [{\"client_uuid\": \"a\", \"type\": \"gps\",
      \"at\": \"1970-01-01T00:00:01Z\", \"lat\": 120, \"lng\": 1, \"accuracy_m\": 5}]}"
  let assert Error(_) = json.parse(body, wire.observations_decoder())
}

pub fn unknown_type_is_rejected_test() {
  let body =
    "{\"observations\": [{\"client_uuid\": \"a\", \"type\": \"wifi\",
      \"at\": \"1970-01-01T00:00:01Z\"}]}"
  let assert Error(_) = json.parse(body, wire.observations_decoder())
}

pub fn gateway_batch_decode_test() {
  let body =
    "{\"batch_id\": \"b1\", \"sightings\": [
      {\"badge_id\": \"BDG-1\", \"at\": \"1970-01-01T00:00:03Z\", \"rssi\": -64}]}"
  assert json.parse(body, wire.gateway_batch_decoder())
    == Ok(#("b1", [GatewaySighting("bdg-1", 3000, -64)]))
}

pub fn site_config_decode_test() {
  let body =
    "{\"tracking_enabled\": true,
      \"geofence\": {\"lat\": 6.1319, \"lng\": 1.2228, \"radius_m\": 150},
      \"zones\": [{\"id\": \"B\", \"name\": \"Block B\"}],
      \"gateways\": [{\"id\": \"GW-1\", \"zone_id\": \"B\"}, {\"id\": \"GW-2\"}],
      \"badges\": [{\"badge_id\": \"BDG-1\", \"worker_id\": 12}],
      \"rules\": {\"sightings_to_enter\": 2, \"arrival_window_seconds\": 300,
                  \"absence_to_leave_seconds\": 900}}"
  assert json.parse(body, wire.site_config_decoder())
    == Ok(SiteConfig(
      tracking_enabled: True,
      geofence: Some(geo.Geofence(geo.LatLng(6.1319, 1.2228), 150.0)),
      zones: [Zone("B", "Block B")],
      gateways: [Gateway("GW-1", Some("B")), Gateway("GW-2", None)],
      badges: [Badge("bdg-1", 12)],
      rules: presence.Rules(2, 300_000, 900_000),
    ))
}

pub fn presence_event_encode_test() {
  let event =
    PresenceEvent(
      event_id: "e1",
      site_id: 3,
      person: Person(Worker, 12),
      change: presence.Left(zone: Some("B"), at: 0, reason: presence.Timeout),
    )
  let text = json.to_string(wire.presence_event_json(event))
  assert string.contains(text, "\"type\":\"left\"")
  assert string.contains(text, "\"reason\":\"timeout\"")
  assert string.contains(text, "\"subject\":{\"kind\":\"worker\",\"id\":12}")
  assert string.contains(text, "\"at\":\"1970-01-01T00:00:00Z\"")
}

/// Badge ids are compared as Django stores them: trimmed, lower case.
pub fn badge_ids_are_normalized_test() {
  assert wire.normalize_badge_id("  IB:F7826DA6:1:42 ") == "ib:f7826da6:1:42"
  let blank =
    "{\"batch_id\": \"b\", \"sightings\": [{\"badge_id\": \"  \", \"at\": \"1970-01-01T00:00:01Z\", \"rssi\": -60}]}"
  let assert Error(_) = json.parse(blank, wire.gateway_batch_decoder())
}
