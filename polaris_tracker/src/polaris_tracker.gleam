//// Polaris presence tracker: live presence and movement traces for workers
//// and foremen. Design: `docs/presence-tracking.md`.

import gleam/erlang/process
import gleam/io
import gleam/option.{Some}
import gleam/otp/actor
import gleam/otp/factory_supervisor
import gleam/otp/static_supervisor as supervisor
import gleam/otp/supervision
import mist
import polaris_tracker/api
import polaris_tracker/broadcaster
import polaris_tracker/clock
import polaris_tracker/config.{type Config}
import polaris_tracker/django
import polaris_tracker/hub
import polaris_tracker/outbox
import polaris_tracker/server
import polaris_tracker/site
import polaris_tracker/track_store

const heartbeat_ms = 5000

const keepalive_ms = 25_000

const flush_every_ms = 5000

const outbox_batch_size = 500

const prune_every_ms = 3_600_000

pub fn main() -> Nil {
  case config.load() {
    Error(message) -> {
      io.println_error("polaris_tracker: " <> message)
      halt(1)
    }
    Ok(config) -> {
      io.println("polaris_tracker: " <> config.describe_sink(config))
      let assert Ok(_) = start(config)
      process.sleep_forever()
    }
  }
}

/// The supervision tree, in start order. With `RestForOne`, a crashed child
/// restarts with every child after it: if the hub restarts, the site actors
/// and the HTTP server restart too, so nothing holds a stale reference.
/// Site configs are lost on a hub restart; Django's periodic resync pushes
/// them again.
pub fn start(config: Config) -> actor.StartResult(supervisor.Supervisor) {
  let broadcaster_name = process.new_name("polaris_tracker_broadcaster")
  let outbox_name = process.new_name("polaris_tracker_outbox")
  let tracks_name = process.new_name("polaris_tracker_tracks")
  let hub_name = process.new_name("polaris_tracker_hub")
  let sites_name = process.new_name("polaris_tracker_sites")

  let publish = fn(events) {
    process.send(
      process.named_subject(broadcaster_name),
      broadcaster.Publish(events),
    )
    process.send(process.named_subject(outbox_name), outbox.Enqueue(events))
  }
  let deliver = case config.django_events_url {
    Some(url) -> django.deliver(url, config.secret, clock.now_ms)
    _ -> django.log_only
  }
  let hub_args =
    hub.Args(
      start_site: fn(args) {
        factory_supervisor.get_by_name(sites_name)
        |> factory_supervisor.start_child(args)
      },
      record_track: fn(person, points) {
        process.send(
          process.named_subject(tracks_name),
          track_store.Record(person, points),
        )
      },
      publish:,
      clock: clock.now_ms,
      heartbeat_ms:,
    )
  let ctx =
    api.Context(
      hub: process.named_subject(hub_name),
      tracks: process.named_subject(tracks_name),
      broadcaster: process.named_subject(broadcaster_name),
      secret: config.secret,
      cors_origin: config.cors_origin,
      clock: clock.now_ms,
    )

  supervisor.new(supervisor.RestForOne)
  |> supervisor.add(
    supervision.worker(fn() {
      broadcaster.start(keepalive_ms, Some(broadcaster_name))
    }),
  )
  |> supervisor.add(
    supervision.worker(fn() {
      outbox.Args(deliver:, flush_every_ms:, batch_size: outbox_batch_size)
      |> outbox.start(Some(outbox_name))
    }),
  )
  |> supervisor.add(
    supervision.worker(fn() {
      track_store.Args(
        retention_ms: config.track_retention_days * 86_400_000,
        prune_every_ms:,
        clock: clock.now_ms,
      )
      |> track_store.start(Some(tracks_name))
    }),
  )
  |> supervisor.add(
    supervision.worker(fn() { hub.start(hub_args, Some(hub_name)) }),
  )
  |> supervisor.add(
    factory_supervisor.worker_child(site.start)
    |> factory_supervisor.named(sites_name)
    |> factory_supervisor.restart_strategy(supervision.Temporary)
    |> factory_supervisor.supervised,
  )
  |> supervisor.add(
    mist.new(server.handler(ctx))
    |> mist.bind(config.bind)
    |> mist.port(config.port)
    |> mist.supervised,
  )
  |> supervisor.start
}

@external(erlang, "erlang", "halt")
fn halt(code: Int) -> Nil
