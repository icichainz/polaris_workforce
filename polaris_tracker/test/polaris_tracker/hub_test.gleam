//// Ingest off the hub process: concurrency, dedupe pruning, restarts.

import gleam/erlang/process
import gleam/int
import gleam/list
import gleam/option.{None, Some}
import polaris_tracker/clock
import polaris_tracker/domain.{GatewaySighting, SiteConfig}
import polaris_tracker/geo
import polaris_tracker/hub
import polaris_tracker/presence
import polaris_tracker/registry
import polaris_tracker/site

fn config() -> domain.SiteConfig {
  SiteConfig(
    tracking_enabled: True,
    geofence: Some(geo.Geofence(geo.LatLng(6.1319, 1.2228), 150.0)),
    zones: [],
    gateways: [domain.Gateway(id: "GW-1", zone_id: Some("A"))],
    badges: list.map(
      list.repeat(Nil, 20) |> list.index_map(fn(_, i) { i }),
      fn(i) { domain.Badge(badge_id: "B" <> int.to_string(i), worker_id: i) },
    ),
    foremen: [],
    rules: presence.Rules(
      sightings_to_enter: 2,
      arrival_window_ms: 300_000,
      absence_to_leave_ms: 900_000,
    ),
  )
}

/// A hub whose `start_site` reports every start to `starts`.
fn start_hub(
  starts: process.Subject(Int),
  now: fn() -> Int,
) -> #(hub.Hub, process.Pid) {
  let registry = registry.new()
  let assert Ok(started) =
    hub.start(
      hub.Args(
        registry:,
        start_site: fn(args) {
          process.send(starts, args.site_id)
          site.start(args)
        },
        publish: fn(_) { Nil },
        clock: now,
        heartbeat_ms: 0,
        prune_every_ms: 0,
      ),
      None,
    )
  #(
    hub.Hub(
      subject: started.data,
      registry:,
      record_track: fn(_) { Nil },
      clock: now,
    ),
    started.pid,
  )
}

pub fn concurrent_batches_start_one_site_actor_test() {
  let starts = process.new_subject()
  let #(ingest, _) = start_hub(starts, clock.now_ms)
  hub.configure_site(ingest, 3, config())
  let done = process.new_subject()
  let at = clock.now_ms() - 1000
  // Fifty gateway batches at once, each in its own process, like requests.
  list.each(list.repeat(Nil, 50) |> list.index_map(fn(_, i) { i }), fn(i) {
    process.spawn(fn() {
      let sightings = [
        GatewaySighting(badge_id: "B" <> int.to_string(i % 20), at:, rssi: -60),
      ]
      let result =
        hub.ingest_gateway(
          ingest,
          "GW-1",
          3,
          "batch-" <> int.to_string(i),
          sightings,
        )
      process.send(done, result)
    })
  })
  let results =
    list.map(list.repeat(Nil, 50), fn(_) {
      let assert Ok(result) = process.receive(done, 5000)
      result
    })
  assert list.all(results, fn(result) {
    case result {
      Ok(domain.IngestResult(accepted: 1, duplicates: 0, ..)) -> True
      _ -> False
    }
  })
  assert process.receive(starts, 100) == Ok(3)
  assert process.receive(starts, 100) == Error(Nil)
  let assert hub.Running(running) = hub.find_site(ingest, 3)
  // 20 badges, each seen at least twice: all on site.
  assert list.length(process.call(running, 1000, site.Snapshot)) == 20
}

pub fn dedupe_keys_expire_after_the_window_test() {
  let starts = process.new_subject()
  let now = clock.now_ms()
  let #(ingest, _) = start_hub(starts, fn() { now })
  let assert True =
    registry.remember(ingest.registry, "old", now - hub.dedupe_window_ms - 1)
  let assert True = registry.remember(ingest.registry, "recent", now - 1000)
  let assert False = registry.remember(ingest.registry, "recent", now)
  process.send(ingest.subject, hub.PruneSeen)
  // The prune runs in the hub; wait for it with a cheap call.
  let _ = process.call(ingest.subject, 1000, hub.EnsureRunning(99, _))
  assert registry.seen_size(ingest.registry) == 1
  assert registry.remember(ingest.registry, "old", now)
  assert !registry.remember(ingest.registry, "recent", now)
}

pub fn configs_survive_a_hub_crash_test() {
  let starts = process.new_subject()
  let #(ingest, pid) = start_hub(starts, clock.now_ms)
  hub.configure_site(ingest, 3, config())
  process.unlink(pid)
  process.kill(pid)
  assert hub.count_sites(ingest) == 1
  assert hub.find_site(ingest, 3) == hub.Idle
}
