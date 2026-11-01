//// SQLite persistence: the track store and the outbox survive a restart,
//// retention prunes, and the outbox deletes only what Django accepted.

import gleam/erlang/process.{type Pid, type Subject}
import gleam/list
import gleam/option.{None, Some}
import gleam/otp/actor
import polaris_tracker/db
import polaris_tracker/domain.{
  type PresenceEvent, BlePoint, Foreman, FromGateway, FromPhone, GpsPoint,
  Person, PresenceEvent, Worker,
}
import polaris_tracker/geo
import polaris_tracker/ids
import polaris_tracker/outbox
import polaris_tracker/presence
import polaris_tracker/track_store

const now = 1_800_000_000_000

const hour = 3_600_000

fn temp_db() -> String {
  "build/test-db/" <> ids.uuid_v4() <> ".sqlite3"
}

fn start_tracks(path: String) -> actor.Started(Subject(track_store.Message)) {
  let assert Ok(started) =
    track_store.start(
      track_store.Args(
        db_path: path,
        retention_ms: 24 * hour,
        prune_every_ms: 0,
        clock: fn() { now },
      ),
      None,
    )
  started
}

/// Kills the actor the way a crash would, without taking the test down.
fn crash(pid: Pid) -> Nil {
  process.unlink(pid)
  process.kill(pid)
  wait_until_dead(pid, 100)
}

fn wait_until_dead(pid: Pid, tries: Int) -> Nil {
  case process.is_alive(pid), tries {
    False, _ -> Nil
    True, 0 -> panic as "the actor did not stop"
    True, _ -> {
      process.sleep(10)
      wait_until_dead(pid, tries - 1)
    }
  }
}

pub fn track_survives_restart_test() {
  let path = temp_db()
  let foreman = Person(Foreman, 5)
  let worker = Person(Worker, 12)
  let gps =
    GpsPoint(
      at: now - 2000,
      position: geo.LatLng(6.132, 1.2228),
      accuracy_m: 9.5,
      site_id: None,
      company: 7,
    )
  let on_site = GpsPoint(..gps, at: now - 1000, site_id: Some(3))
  let ble =
    BlePoint(
      at: now - 1500,
      site_id: 3,
      zone_id: Some("B"),
      rssi: -64,
      source: FromGateway,
    )
  let phone_ble =
    BlePoint(..ble, at: now - 500, zone_id: None, source: FromPhone)

  let tracks = start_tracks(path)
  process.send(
    tracks.data,
    track_store.Record([
      #(foreman, on_site),
      #(foreman, gps),
      #(worker, ble),
      #(worker, phone_ble),
    ]),
  )
  // A call waits until the Record before it has been handled.
  let assert [_, _] =
    process.call(tracks.data, 1000, track_store.Query(foreman, 0, now, _))
  crash(tracks.pid)

  let tracks = start_tracks(path)
  assert process.call(tracks.data, 1000, track_store.Query(foreman, 0, now, _))
    == [gps, on_site]
  assert process.call(tracks.data, 1000, track_store.Query(worker, 0, now, _))
    == [ble, phone_ble]
  // The range is half open: `from <= at < to`.
  assert process.call(tracks.data, 1000, track_store.Query(
      foreman,
      now - 2000,
      now - 1000,
      _,
    ))
    == [gps]
}

pub fn prune_keeps_the_retention_period_test() {
  let tracks = start_tracks(db.memory)
  let worker = Person(Worker, 12)
  let point = fn(at) {
    BlePoint(at:, site_id: 3, zone_id: None, rssi: -70, source: FromPhone)
  }
  // Retention is 24 h: the first two are older.
  process.send(
    tracks.data,
    track_store.Record([
      #(worker, point(now - 48 * hour)),
      #(worker, point(now - 25 * hour)),
      #(worker, point(now - 23 * hour)),
      #(worker, point(now - hour)),
    ]),
  )
  process.send(tracks.data, track_store.Prune)
  assert process.call(tracks.data, 1000, track_store.Query(worker, 0, now, _))
    == [point(now - 23 * hour), point(now - hour)]
}

pub fn site_day_query_test() {
  let tracks = start_tracks(db.memory)
  let gps = fn(at, site_id) {
    GpsPoint(
      at:,
      position: geo.LatLng(6.132, 1.2228),
      accuracy_m: 9.0,
      site_id:,
      company: 1,
    )
  }
  let ble = fn(at, site_id) {
    BlePoint(at:, site_id:, zone_id: None, rssi: -70, source: FromPhone)
  }
  process.send(
    tracks.data,
    track_store.Record([
      // Foreman 5 was on site 3, then off site: both fixes count.
      #(Person(Foreman, 5), gps(now - 3000, Some(3))),
      #(Person(Foreman, 5), gps(now - 2000, None)),
      // Foreman 6 never came to site 3.
      #(Person(Foreman, 6), gps(now - 2000, None)),
      #(Person(Foreman, 6), gps(now - 1500, Some(4))),
      #(Person(Worker, 12), ble(now - 1000, 3)),
      #(Person(Worker, 13), ble(now - 1000, 4)),
      // Outside the range.
      #(Person(Worker, 12), ble(now + 1000, 3)),
    ]),
  )
  assert process.call(tracks.data, 1000, track_store.SiteDay(3, 0, now, _))
    == [
      #(Person(Foreman, 5), gps(now - 3000, Some(3))),
      #(Person(Foreman, 5), gps(now - 2000, None)),
      #(Person(Worker, 12), ble(now - 1000, 3)),
    ]
}

// ---- Outbox -----------------------------------------------------------------

fn event(n: Int) -> PresenceEvent {
  let person = Person(Worker, n)
  PresenceEvent(
    event_id: ids.uuid_v4(),
    site_id: 3,
    person:,
    change: case n % 3 {
      0 -> presence.Entered(zone: Some("A"), at: now + n)
      1 -> presence.MovedZone(from: Some("A"), to: "B", at: now + n)
      _ -> presence.Left(zone: None, at: now + n, reason: presence.LeftGeofence)
    },
  )
}

fn start_outbox(
  path: String,
  deliver: fn(List(PresenceEvent)) -> Result(Nil, String),
  batch_size: Int,
) -> actor.Started(Subject(outbox.Message)) {
  let assert Ok(started) =
    outbox.start(
      outbox.Args(
        db_path: path,
        deliver:,
        flush_every_ms: 0,
        batch_size:,
        clock: fn() { now },
      ),
      None,
    )
  started
}

pub fn outbox_survives_restart_and_waits_for_2xx_test() {
  let path = temp_db()
  let events = [event(1), event(2), event(3)]
  let failing = fn(_) { Error("Django answered 503") }

  let box = start_outbox(path, failing, 500)
  process.send(box.data, outbox.Enqueue(events))
  process.send(box.data, outbox.Flush)
  // Not accepted: still queued.
  assert process.call(box.data, 1000, outbox.Pending) == 3
  crash(box.pid)

  let delivered = process.new_subject()
  let accepting = fn(batch) {
    process.send(delivered, batch)
    Ok(Nil)
  }
  let box = start_outbox(path, accepting, 500)
  assert process.call(box.data, 1000, outbox.Pending) == 3
  process.send(box.data, outbox.Flush)
  let assert Ok(batch) = process.receive(delivered, 1000)
  // Same events, same ids, same order: Django dedupes by `event_id`.
  assert batch == events
  assert process.call(box.data, 1000, outbox.Pending) == 0
}

pub fn outbox_drains_in_batches_test() {
  let delivered = process.new_subject()
  let box =
    start_outbox(
      db.memory,
      fn(batch) {
        process.send(delivered, batch)
        Ok(Nil)
      },
      2,
    )
  let events = list.map([1, 2, 3, 4, 5], event)
  process.send(box.data, outbox.Enqueue(events))
  // Enqueuing an event twice stores it once.
  process.send(box.data, outbox.Enqueue([event(1), ..list.take(events, 1)]))
  process.send(box.data, outbox.Flush)
  let assert Ok(first) = process.receive(delivered, 1000)
  let assert Ok(second) = process.receive(delivered, 1000)
  let assert Ok(third) = process.receive(delivered, 1000)
  assert list.map([first, second, third], list.length) == [2, 2, 2]
  assert list.flatten([first, second, third]) |> list.take(5) == events
  assert process.call(box.data, 1000, outbox.Pending) == 0
}
