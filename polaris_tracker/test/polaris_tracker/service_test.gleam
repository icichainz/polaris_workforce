//// End-to-end through the JSON API, with the real actors and no sockets.

import gleam/bit_array
import gleam/erlang/process.{type Subject}
import gleam/http
import gleam/http/request
import gleam/http/response.{type Response}
import gleam/int
import gleam/list
import gleam/option.{None, Some}
import gleam/string
import polaris_tracker/api
import polaris_tracker/broadcaster
import polaris_tracker/clock
import polaris_tracker/db
import polaris_tracker/domain.{type PresenceEvent}
import polaris_tracker/hub
import polaris_tracker/outbox
import polaris_tracker/presence
import polaris_tracker/registry
import polaris_tracker/site
import polaris_tracker/token
import polaris_tracker/track_store

const secret = "test-secret"

const site_config =
  "{
  \"tracking_enabled\": true,
  \"geofence\": {\"lat\": 6.1319, \"lng\": 1.2228, \"radius_m\": 150},
  \"zones\": [{\"id\": \"B\", \"name\": \"Block B\"}],
  \"gateways\": [{\"id\": \"GW-1\", \"zone_id\": \"B\"}],
  \"badges\": [{\"badge_id\": \"BDG-1\", \"worker_id\": 12}],
  \"rules\": {\"sightings_to_enter\": 2, \"arrival_window_seconds\": 300,
              \"absence_to_leave_seconds\": 900}
}"

type Stack {
  Stack(
    ctx: api.Context,
    outbox: Subject(outbox.Message),
    delivered: Subject(List(PresenceEvent)),
  )
}

fn start_stack() -> Stack {
  let delivered = process.new_subject()
  let assert Ok(broadcaster) = broadcaster.start(0, None)
  let assert Ok(outbox) =
    outbox.start(
      outbox.Args(
        db_path: db.memory,
        deliver: fn(events) {
          process.send(delivered, events)
          Ok(Nil)
        },
        flush_every_ms: 0,
        batch_size: 500,
        clock: clock.now_ms,
      ),
      None,
    )
  let assert Ok(tracks) =
    track_store.start(
      track_store.Args(
        db_path: db.memory,
        retention_ms: 86_400_000,
        prune_every_ms: 0,
        clock: clock.now_ms,
      ),
      None,
    )
  let publish = fn(events) {
    process.send(broadcaster.data, broadcaster.Publish(events))
    process.send(outbox.data, outbox.Enqueue(events))
  }
  let registry = registry.new()
  let assert Ok(hub) =
    hub.start(
      hub.Args(
        registry:,
        start_site: site.start,
        publish:,
        clock: clock.now_ms,
        heartbeat_ms: 0,
        prune_every_ms: 0,
      ),
      None,
    )
  let ingest =
    hub.Hub(
      subject: hub.data,
      registry:,
      record_track: fn(points) {
        process.send(tracks.data, track_store.Record(points))
      },
      clock: clock.now_ms,
    )
  let ctx =
    api.Context(
      hub: ingest,
      tracks: tracks.data,
      broadcaster: broadcaster.data,
      secret: bit_array.from_string(secret),
      cors_origin: None,
      clock: clock.now_ms,
      zoneinfo_dir: "/usr/share/zoneinfo",
    )
  Stack(ctx:, outbox: outbox.data, delivered:)
}

/// A token of company 1 without travel (company 0 for service tokens).
fn mint(sub: String, kind: token.Kind, sites: List(Int)) -> String {
  let company = case kind {
    token.Service -> 0
    _ -> 1
  }
  mint_for(sub, kind, company:, sites:, travel: False)
}

fn mint_for(
  sub: String,
  kind: token.Kind,
  company company: Int,
  sites sites: List(Int),
  travel travel: Bool,
) -> String {
  token.Claims(
    sub:,
    kind:,
    company:,
    sites:,
    travel:,
    audience: token.tracker_audience,
    expires_at: clock.now_ms() / 1000 + 600,
  )
  |> token.mint(secret: bit_array.from_string(secret))
}

fn send(
  stack: Stack,
  method: http.Method,
  path: String,
  bearer: String,
  body: String,
) -> Response(String) {
  let assert Ok(req) = request.to("http://tracker" <> path)
  req
  |> request.set_method(method)
  |> request.set_header("authorization", "Bearer " <> bearer)
  |> request.set_body(bit_array.from_string(body))
  |> api.handle(stack.ctx)
}

fn configure(stack: Stack) -> Nil {
  let service = mint("service:django", token.Service, [])
  let response =
    send(stack, http.Put, "/internal/sites/3/config", service, site_config)
  assert response.status == 204
}

fn ago(ms: Int) -> String {
  clock.to_rfc3339(clock.now_ms() - ms)
}

/// Waits until the site actor has handled everything sent to it so far,
/// then lets time run and collects what reached the outbox.
fn advance_and_flush(stack: Stack, by ms: Int) -> List(PresenceEvent) {
  let assert hub.Running(site) = hub.find_site(stack.ctx.hub, 3)
  process.send(site, site.AdvanceTo(clock.now_ms() + ms))
  let _ = process.call(site, 1000, site.Snapshot)
  process.send(stack.outbox, outbox.Flush)
  let assert Ok(events) = process.receive(stack.delivered, 1000)
  events
}

pub fn gateway_sightings_make_a_visit_test() {
  let stack = start_stack()
  configure(stack)
  let gateway = mint("gateway:GW-1", token.Gateway, [3])
  let batch = "{\"batch_id\": \"b1\", \"sightings\": [
      {\"badge_id\": \"BDG-1\", \"at\": \"" <> ago(60_000) <> "\", \"rssi\": -64},
      {\"badge_id\": \"BDG-1\", \"at\": \"" <> ago(30_000) <> "\", \"rssi\": -61},
      {\"badge_id\": \"BDG-9\", \"at\": \"" <> ago(30_000) <> "\", \"rssi\": -80}]}"

  let response =
    send(stack, http.Post, "/v1/gateways/sightings", gateway, batch)
  assert response.status == 200
  assert string.contains(response.body, "\"accepted\":2")
  assert string.contains(response.body, "\"code\":\"unknown_badge\"")

  // Repeating the batch changes nothing (rule 7).
  let response =
    send(stack, http.Post, "/v1/gateways/sightings", gateway, batch)
  assert string.contains(response.body, "\"accepted\":0,\"duplicates\":3")

  let dashboard = mint("user:1", token.Dashboard, [3])
  let response = send(stack, http.Get, "/v1/sites/3/presence", dashboard, "")
  assert response.status == 200
  assert string.contains(response.body, "\"state\":\"on_site\"")
  assert string.contains(response.body, "\"zone_id\":\"B\"")
  assert string.contains(response.body, "{\"kind\":\"worker\",\"id\":12}")

  // Fifteen minutes of silence: the visit closes.
  let events = advance_and_flush(stack, by: 3_600_000)
  let changes = list.map(events, fn(event) { event.change })
  let assert [presence.Entered(zone: Some("B"), ..), presence.Left(reason:, ..)] =
    changes
  assert reason == presence.Timeout

  let from = ago(3_600_000)
  let to = clock.to_rfc3339(clock.now_ms() + 1000)
  let response =
    send(
      stack,
      http.Get,
      "/v1/people/worker/12/track?from=" <> from <> "&to=" <> to,
      dashboard,
      "",
    )
  assert response.status == 200
  assert list.length(string.split(response.body, "\"type\":\"ble\"")) == 3
}

pub fn foreman_gps_enters_and_leaves_geofence_test() {
  let stack = start_stack()
  configure(stack)
  let device = mint("foreman:5", token.Device, [3])
  let fix = fn(uuid, at, lat) {
    "{\"client_uuid\": \""
    <> uuid
    <> "\", \"type\": \"gps\", \"at\": \""
    <> at
    <> "\", \"lat\": "
    <> lat
    <> ", \"lng\": 1.2228, \"accuracy_m\": 10}"
  }
  let batch = fn(items) {
    "{\"observations\": [" <> string.join(items, ",") <> "]}"
  }

  let inside =
    batch([fix("u1", ago(120_000), "6.1320"), fix("u2", ago(90_000), "6.1321")])
  let response = send(stack, http.Post, "/v1/observations", device, inside)
  assert string.contains(response.body, "\"accepted\":2")

  let response = send(stack, http.Get, "/v1/sites/3/presence", device, "")
  assert string.contains(response.body, "{\"kind\":\"foreman\",\"id\":5}")

  // About 2 km north: clearly outside.
  let outside = batch([fix("u3", ago(10_000), "6.1500")])
  let response = send(stack, http.Post, "/v1/observations", device, outside)
  assert string.contains(response.body, "\"accepted\":1")

  let events = advance_and_flush(stack, by: 0)
  let changes = list.map(events, fn(event) { event.change })
  let assert [presence.Entered(..), presence.Left(reason:, ..)] = changes
  assert reason == presence.LeftGeofence
}

pub fn phone_ble_is_scoped_to_assigned_sites_test() {
  let stack = start_stack()
  configure(stack)
  let device = mint("foreman:5", token.Device, [3])
  let body = "{\"observations\": [
      {\"client_uuid\": \"x1\", \"type\": \"ble\", \"at\": \"" <> ago(1000) <> "\",
       \"site_id\": 9, \"badge_id\": \"BDG-1\", \"rssi\": -70},
      {\"client_uuid\": \"x2\", \"type\": \"ble\", \"at\": \"" <> ago(1000) <> "\",
       \"site_id\": 3, \"badge_id\": \"BDG-1\", \"rssi\": -70},
      {\"client_uuid\": \"x3\", \"type\": \"ble\",
       \"at\": \"" <> clock.to_rfc3339(clock.now_ms() + 3_600_000) <> "\",
       \"site_id\": 3, \"badge_id\": \"BDG-1\", \"rssi\": -70}]}"
  let response = send(stack, http.Post, "/v1/observations", device, body)
  assert response.status == 200
  assert string.contains(response.body, "\"accepted\":1")
  assert string.contains(response.body, "\"code\":\"site_not_allowed\"")
  assert string.contains(response.body, "\"code\":\"future_timestamp\"")
}

pub fn late_observations_only_go_to_the_track_test() {
  let stack = start_stack()
  configure(stack)
  let device = mint("foreman:5", token.Device, [3])
  let body = "{\"observations\": [{\"client_uuid\": \"old\", \"type\": \"ble\",
      \"at\": \"" <> ago(2 * 3_600_000) <> "\",
      \"site_id\": 3, \"badge_id\": \"BDG-1\", \"rssi\": -70}]}"
  let response = send(stack, http.Post, "/v1/observations", device, body)
  assert string.contains(
    response.body,
    "\"accepted\":1,\"duplicates\":0,\"late\":1",
  )
  assert hub.find_site(stack.ctx.hub, 3) == hub.Idle
}

pub fn site_scoping_and_auth_test() {
  let stack = start_stack()
  configure(stack)
  let other_site = mint("user:2", token.Dashboard, [4])
  let response = send(stack, http.Get, "/v1/sites/3/presence", other_site, "")
  assert response.status == 403
  assert string.contains(response.body, "\"code\":\"site_not_allowed\"")

  let response = send(stack, http.Get, "/v1/sites/3/presence", "nope", "")
  assert response.status == 401

  let device = mint("foreman:5", token.Device, [3])
  let response =
    send(stack, http.Put, "/internal/sites/3/config", device, site_config)
  assert response.status == 403

  // A gateway not in the site's config is refused.
  let rogue = mint("gateway:GW-X", token.Gateway, [3])
  let response =
    send(
      stack,
      http.Post,
      "/v1/gateways/sightings",
      rogue,
      "{\"batch_id\": \"b\", \"sightings\": []}",
    )
  assert response.status == 403
  assert string.contains(response.body, "\"code\":\"unknown_gateway\"")

  let response =
    send(
      stack,
      http.Get,
      "/v1/sites/99/presence",
      mint("user:1", token.Dashboard, [99]),
      "",
    )
  assert response.status == 404
}

pub fn bad_body_and_limits_test() {
  let stack = start_stack()
  configure(stack)
  let device = mint("foreman:5", token.Device, [3])
  let response = send(stack, http.Post, "/v1/observations", device, "{")
  assert response.status == 400
  assert string.contains(response.body, "\"code\":\"invalid_json\"")

  let response =
    send(
      stack,
      http.Post,
      "/v1/observations",
      device,
      "{\"observations\": [{}]}",
    )
  assert response.status == 400
  assert string.contains(response.body, "\"code\":\"invalid_body\"")

  let item =
    "{\"client_uuid\": \"u\", \"type\": \"gps\", \"at\": \""
    <> ago(0)
    <> "\", \"lat\": 6.1, \"lng\": 1.2, \"accuracy_m\": 5}"
  let too_many =
    "{\"observations\": ["
    <> string.join(list.repeat(item, api.max_phone_batch + 1), ",")
    <> "]}"
  let response = send(stack, http.Post, "/v1/observations", device, too_many)
  assert response.status == 413

  let dashboard = mint("user:1", token.Dashboard, [3])
  let response =
    send(
      stack,
      http.Get,
      "/v1/people/worker/12/track?from=x&to=y",
      dashboard,
      "",
    )
  assert response.status == 400
}

pub fn health_test() {
  let stack = start_stack()
  configure(stack)
  let response = send(stack, http.Get, "/health", "", "")
  assert response.status == 200
  assert response.body
    == "{\"status\":\"ok\",\"sites\":" <> int.to_string(1) <> "}"
}

fn gps_batch(uuid: String, at: String, lat: String) -> String {
  "{\"observations\": [{\"client_uuid\": \""
  <> uuid
  <> "\", \"type\": \"gps\", \"at\": \""
  <> at
  <> "\", \"lat\": "
  <> lat
  <> ", \"lng\": 1.2228, \"accuracy_m\": 10}]}"
}

fn count(haystack: String, needle: String) -> Int {
  list.length(string.split(haystack, needle)) - 1
}

/// Site 3 belongs to company 1, site 4 to company 2. Site ids are global, so
/// only the explicit `sites` list and the `company` claim keep them apart.
pub fn two_companies_are_kept_apart_test() {
  let stack = start_stack()
  configure(stack)
  let service = mint("service:django", token.Service, [])
  let response =
    send(stack, http.Put, "/internal/sites/4/config", service, site_config)
  assert response.status == 204

  let admin_1 =
    mint_for("user:1", token.Dashboard, company: 1, sites: [3], travel: True)
  let viewer_1 =
    mint_for("user:2", token.Dashboard, company: 1, sites: [3], travel: False)
  let admin_2 =
    mint_for("user:3", token.Dashboard, company: 2, sites: [4], travel: True)
  let device_1 =
    mint_for("foreman:5", token.Device, company: 1, sites: [3], travel: False)
  let device_2 =
    mint_for("foreman:8", token.Device, company: 2, sites: [4], travel: False)

  // Sites: another company's admin cannot read or write site 3.
  let response = send(stack, http.Get, "/v1/sites/3/presence", admin_2, "")
  assert response.status == 403
  assert string.contains(response.body, "\"code\":\"site_not_allowed\"")
  let ble = "{\"observations\": [{\"client_uuid\": \"c2\", \"type\": \"ble\",
      \"at\": \"" <> ago(1000) <> "\", \"site_id\": 3, \"badge_id\": \"BDG-1\",
      \"rssi\": -70}]}"
  let response = send(stack, http.Post, "/v1/observations", device_2, ble)
  assert string.contains(response.body, "\"accepted\":0")
  assert string.contains(response.body, "\"code\":\"site_not_allowed\"")

  // Foreman 5 (company 1): one fix on site 3, one about 2 km away (travel).
  let response =
    send(
      stack,
      http.Post,
      "/v1/observations",
      device_1,
      gps_batch("t1", ago(60_000), "6.1320"),
    )
  assert string.contains(response.body, "\"accepted\":1")
  let response =
    send(
      stack,
      http.Post,
      "/v1/observations",
      device_1,
      gps_batch("t2", ago(30_000), "6.1500"),
    )
  assert string.contains(response.body, "\"accepted\":1")

  // Foreman 8 (company 2) stands inside site 3's geofence, but site 3 is not
  // in the token: the fix is stored off site, with company 2.
  let response =
    send(
      stack,
      http.Post,
      "/v1/observations",
      device_2,
      gps_batch("t3", ago(30_000), "6.1320"),
    )
  assert string.contains(response.body, "\"accepted\":1")

  let track = fn(path, bearer) {
    let range =
      "?from=" <> ago(3_600_000) <> "&to=" <> clock.to_rfc3339(clock.now_ms())
    let response = send(stack, http.Get, path <> range, bearer, "")
    assert response.status == 200
    response.body
  }
  let foreman_5 = "/v1/people/foreman/5/track"
  let foreman_8 = "/v1/people/foreman/8/track"

  // The travel admin of company 1 sees the on-site point and the travel.
  let body = track(foreman_5, admin_1)
  assert count(body, "\"type\":\"gps\"") == 2
  assert count(body, "\"site_id\":null") == 1
  // Without `travel`, only the on-site point.
  let body = track(foreman_5, viewer_1)
  assert count(body, "\"type\":\"gps\"") == 1
  assert count(body, "\"site_id\":3") == 1
  // Company 2's travel admin sees none of company 1's points.
  assert count(track(foreman_5, admin_2), "\"type\":\"gps\"") == 0
  // And company 1 does not see company 2's travel, even inside its own site.
  assert count(track(foreman_8, admin_1), "\"type\":\"gps\"") == 0
  assert count(track(foreman_8, admin_2), "\"type\":\"gps\"") == 1
}

pub fn token_without_company_is_refused_test() {
  let stack = start_stack()
  configure(stack)
  let no_company =
    mint_for("user:1", token.Dashboard, company: 0, sites: [3], travel: True)
  let response = send(stack, http.Get, "/v1/sites/3/presence", no_company, "")
  assert response.status == 401
  assert string.contains(response.body, "\"code\":\"invalid_token\"")
}

/// Sightings from a past day are all late (track only), yet the sessions
/// endpoint replays them; the local day follows `tz`.
pub fn sessions_are_recomputed_from_the_track_test() {
  let stack = start_stack()
  configure(stack)
  let gateway = mint("gateway:GW-1", token.Gateway, [3])
  let device = mint("foreman:5", token.Device, [3])
  let sighting = fn(at) {
    "{\"badge_id\": \"BDG-1\", \"at\": \"" <> at <> "\", \"rssi\": -60}"
  }
  let batch =
    "{\"batch_id\": \"day\", \"sightings\": ["
    <> string.join(
      [
        sighting("2025-06-02T08:00:00Z"),
        sighting("2025-06-02T08:00:30Z"),
        // Back after a long gap: a second visit.
        sighting("2025-06-02T12:00:00Z"),
        sighting("2025-06-02T12:00:20Z"),
        // 00:30 on June 3 at +01:00, still June 2 in UTC.
        sighting("2025-06-02T23:30:00Z"),
        sighting("2025-06-02T23:30:30Z"),
      ],
      ",",
    )
    <> "]}"
  let response =
    send(stack, http.Post, "/v1/gateways/sightings", gateway, batch)
  assert string.contains(
    response.body,
    "\"accepted\":6,\"duplicates\":0,\"late\":6",
  )
  let fixes =
    "{\"observations\": ["
    <> string.join(
      [
        gps_fix("g1", "2025-06-02T09:00:00Z", "6.1320"),
        gps_fix("g2", "2025-06-02T09:01:00Z", "6.1321"),
        gps_fix("g3", "2025-06-02T09:05:00Z", "6.1500"),
      ],
      ",",
    )
    <> "]}"
  let response = send(stack, http.Post, "/v1/observations", device, fixes)
  assert string.contains(response.body, "\"accepted\":3")

  let service = mint("service:django", token.Service, [])
  let get = fn(query, bearer) {
    send(stack, http.Get, "/internal/sites/3/sessions?" <> query, bearer, "")
  }
  let response = get("date=2025-06-02&tz=Z", service)
  assert response.status == 200
  assert response.body
    == "{\"site_id\":3,\"date\":\"2025-06-02\",\"tz\":\"Z\","
    <> "\"from\":\"2025-06-02T00:00:00Z\",\"to\":\"2025-06-03T00:00:00Z\","
    <> "\"sessions\":["
    <> "{\"subject\":{\"kind\":\"worker\",\"id\":12},\"started_at\":\"2025-06-02T08:00:00Z\",\"ended_at\":\"2025-06-02T08:00:30Z\",\"zones\":[\"B\"]},"
    <> "{\"subject\":{\"kind\":\"foreman\",\"id\":5},\"started_at\":\"2025-06-02T09:00:00Z\",\"ended_at\":\"2025-06-02T09:01:00Z\",\"zones\":[]},"
    <> "{\"subject\":{\"kind\":\"worker\",\"id\":12},\"started_at\":\"2025-06-02T12:00:00Z\",\"ended_at\":\"2025-06-02T12:00:20Z\",\"zones\":[\"B\"]},"
    <> "{\"subject\":{\"kind\":\"worker\",\"id\":12},\"started_at\":\"2025-06-02T23:30:00Z\",\"ended_at\":\"2025-06-02T23:30:30Z\",\"zones\":[\"B\"]}"
    <> "]}"

  // At +01:00 (`+` encoded), the late-evening visit belongs to June 3.
  let response = get("date=2025-06-02&tz=%2B01:00", service)
  assert string.contains(response.body, "\"from\":\"2025-06-01T23:00:00Z\"")
  assert count(response.body, "\"started_at\"") == 3
  let response = get("date=2025-06-03&tz=%2B01:00", service)
  assert count(response.body, "\"started_at\"") == 1

  // An IANA name, when the system has zoneinfo.
  let response = get("date=2025-06-02&tz=Europe/Paris", service)
  case response.status {
    200 -> {
      assert string.contains(response.body, "\"from\":\"2025-06-01T22:00:00Z\"")
      assert string.contains(response.body, "\"tz\":\"Europe/Paris\"")
    }
    // No zoneinfo on this machine.
    status -> {
      assert status == 400
    }
  }

  // Errors.
  let dashboard = mint("user:1", token.Dashboard, [3])
  assert get("date=2025-06-02&tz=Z", dashboard).status == 403
  assert get("date=2025-6-2&tz=Z", service).status == 400
  assert get("tz=Z", service).status == 400
  assert string.contains(
    get("date=2025-06-02&tz=Mars/Olympus", service).body,
    "\"code\":\"invalid_tz\"",
  )
  assert get("date=2025-06-02", service).status == 400
  let response =
    send(
      stack,
      http.Get,
      "/internal/sites/99/sessions?date=2025-06-02&tz=Z",
      service,
      "",
    )
  assert response.status == 404
}

fn gps_fix(uuid: String, at: String, lat: String) -> String {
  "{\"client_uuid\": \""
  <> uuid
  <> "\", \"type\": \"gps\", \"at\": \""
  <> at
  <> "\", \"lat\": "
  <> lat
  <> ", \"lng\": 1.2228, \"accuracy_m\": 10}"
}

/// Django's ids: gateways `gw-<site>-<generation>` (a new token raises the
/// generation, so the old id leaves the config), badges normalized to lower
/// case and trimmed, as phones report iBeacons (`ib:<uuid>:<major>:<minor>`).
pub fn django_gateway_and_badge_ids_test() {
  let stack = start_stack()
  let service = mint("service:django", token.Service, [])
  let config =
    "{\"tracking_enabled\": true,
    \"gateways\": [{\"id\": \"gw-12-4\", \"zone_id\": \"A\"}],
    \"badges\": [{\"badge_id\": \"ib:f7826da6-4fa2-4e98-8024-bc5b71e0893e:1:42\",
                  \"worker_id\": 31}],
    \"rules\": {\"sightings_to_enter\": 1, \"arrival_window_seconds\": 300,
                \"absence_to_leave_seconds\": 900}}"
  let response =
    send(stack, http.Put, "/internal/sites/12/config", service, config)
  assert response.status == 204
  let sightings = fn(batch_id) {
    "{\"batch_id\": \"" <> batch_id <> "\", \"sightings\": [
      {\"badge_id\": \"  IB:F7826DA6-4FA2-4E98-8024-BC5B71E0893E:1:42 \",
       \"at\": \"" <> ago(1000) <> "\", \"rssi\": -60}]}"
  }

  // The previous generation's token is no longer in the config.
  let old = mint("gateway:gw-12-3", token.Gateway, [12])
  let response =
    send(stack, http.Post, "/v1/gateways/sightings", old, sightings("b1"))
  assert response.status == 403
  assert string.contains(response.body, "\"code\":\"unknown_gateway\"")

  let current = mint("gateway:gw-12-4", token.Gateway, [12])
  let response =
    send(stack, http.Post, "/v1/gateways/sightings", current, sightings("b1"))
  assert response.status == 200
  assert string.contains(response.body, "\"accepted\":1")

  // The same badge from a foreman's phone, in yet another spelling.
  let device = mint("foreman:5", token.Device, [12])
  let body = "{\"observations\": [{\"client_uuid\": \"p1\", \"type\": \"ble\",
      \"at\": \"" <> ago(500) <> "\", \"site_id\": 12,
      \"badge_id\": \"Ib:F7826da6-4fa2-4e98-8024-bc5b71e0893e:1:42\", \"rssi\": -70}]}"
  let response = send(stack, http.Post, "/v1/observations", device, body)
  assert string.contains(response.body, "\"accepted\":1")
}
