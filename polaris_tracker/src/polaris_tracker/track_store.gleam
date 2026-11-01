//// Raw track points per person, kept for the retention period, in the
//// tracker's own SQLite file (never Django's), see
//// `docs/presence-tracking.md` §9.
////
//// One actor owns the connection. Each `Record` (one HTTP batch) is one
//// short transaction; statements are prepared once. Retention is enforced by
//// a prune every `prune_every_ms` (hourly in production), deleting in small
//// chunks so the write lock is never held for long.

import gleam/dynamic/decode.{type Decoder}
import gleam/erlang/process.{type Name, type Subject}
import gleam/io
import gleam/list
import gleam/option.{type Option, None, Some}
import gleam/otp/actor
import gleam/result
import polaris_tracker/db
import polaris_tracker/domain.{
  type Person, type TrackPoint, BlePoint, FromGateway, FromPhone, GpsPoint,
  Person,
}
import polaris_tracker/geo
import sqlight

pub type Message {
  /// One batch of points, written in one transaction.
  Record(points: List(#(Person, TrackPoint)))
  /// Points with `from <= at < to`, oldest first.
  Query(person: Person, from: Int, to: Int, reply: Subject(List(TrackPoint)))
  /// Everything a site's sessions are recomputed from, for `from <= at < to`,
  /// oldest first: the BLE points on the site, and every GPS point of the
  /// foremen who had at least one GPS point on the site in that range (their
  /// fixes outside the geofence close visits early).
  SiteDay(
    site_id: Int,
    from: Int,
    to: Int,
    reply: Subject(List(#(Person, TrackPoint))),
  )
  /// Delete points older than the retention period.
  Prune
}

pub type Args {
  Args(
    /// A file path, or `db.memory`.
    db_path: String,
    retention_ms: Int,
    /// 0 disables the timer (tests send `Prune` themselves).
    prune_every_ms: Int,
    clock: fn() -> Int,
  )
}

type State {
  State(
    args: Args,
    connection: sqlight.Connection,
    statements: Statements,
    self: Subject(Message),
  )
}

type Statements {
  Statements(
    insert: db.Statement,
    by_person: db.Statement,
    by_site: db.Statement,
    prune: db.Statement,
  )
}

const schema_version = 1

/// Rows deleted per prune transaction.
const prune_chunk = 5000

const columns =
  "person_kind, person_id, at, type, site_id, company,
  lat, lng, accuracy_m, zone_id, rssi, source"

pub fn start(
  args: Args,
  name: Option(Name(Message)),
) -> actor.StartResult(Subject(Message)) {
  let builder =
    actor.new_with_initialiser(5000, fn(self) {
      use #(connection, statements) <- result.map(
        open(args.db_path)
        |> result.map_error(fn(error) {
          "track store: " <> db.describe_error(error)
        }),
      )
      schedule_prune(self, args.prune_every_ms)
      State(args:, connection:, statements:, self:)
      |> actor.initialised
      |> actor.returning(self)
    })
    |> actor.on_message(handle_message)
  case name {
    Some(name) -> actor.named(builder, name)
    None -> builder
  }
  |> actor.start
}

fn open(path: String) -> Result(#(sqlight.Connection, Statements), db.Error) {
  use connection <- result.try(db.open(path))
  use _ <- result.try(migrate(connection))
  use insert <- result.try(db.prepare(
    "INSERT INTO track_points (" <> columns <> ")
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
    on: connection,
  ))
  use by_person <- result.try(db.prepare(
    "SELECT " <> columns <> " FROM track_points
     WHERE person_kind = ?1 AND person_id = ?2 AND at >= ?3 AND at < ?4
     ORDER BY at, id",
    on: connection,
  ))
  use by_site <- result.try(db.prepare(
    "SELECT " <> columns <> " FROM track_points
     WHERE at >= ?2 AND at < ?3 AND (
       (type = 'ble' AND site_id = ?1)
       OR (type = 'gps' AND person_kind = 'foreman' AND person_id IN (
         SELECT DISTINCT person_id FROM track_points
         WHERE type = 'gps' AND person_kind = 'foreman' AND site_id = ?1
           AND at >= ?2 AND at < ?3)))
     ORDER BY at, id",
    on: connection,
  ))
  use prune <- result.map(db.prepare(
    "DELETE FROM track_points WHERE id IN (
       SELECT id FROM track_points WHERE at < ?1 LIMIT ?2)",
    on: connection,
  ))
  #(connection, Statements(insert:, by_person:, by_site:, prune:))
}

fn migrate(connection: sqlight.Connection) -> Result(Nil, db.Error) {
  use version <- result.try(db.user_version_of(connection, "track_points"))
  case version {
    0 ->
      db.exec(
        "BEGIN;
         CREATE TABLE IF NOT EXISTS track_points (
           id INTEGER PRIMARY KEY,
           person_kind TEXT NOT NULL,   -- worker | foreman
           person_id INTEGER NOT NULL,
           at INTEGER NOT NULL,         -- Unix milliseconds
           type TEXT NOT NULL,          -- gps | ble
           site_id INTEGER,             -- gps: the geofence it is in, if any
           company INTEGER,             -- gps: company of the device token
           lat REAL,
           lng REAL,
           accuracy_m REAL,
           zone_id TEXT,
           rssi INTEGER,
           source TEXT                  -- ble: gateway | phone
         );
         CREATE INDEX IF NOT EXISTS track_points_by_person
           ON track_points (person_kind, person_id, at);
         CREATE INDEX IF NOT EXISTS track_points_by_site
           ON track_points (site_id, at);
         CREATE INDEX IF NOT EXISTS track_points_by_time
           ON track_points (at);
         INSERT OR REPLACE INTO schema_versions (name, version)
           VALUES ('track_points', 1);
         COMMIT;",
        on: connection,
      )
    _ if version == schema_version -> Ok(Nil)
    _ ->
      Error(
        db.Sqlite(sqlight.SqlightError(
          sqlight.GenericError,
          "unknown track schema version; newer tracker?",
          -1,
        )),
      )
  }
}

fn handle_message(
  state: State,
  message: Message,
) -> actor.Next(State, Message) {
  case message {
    Record(points: []) -> actor.continue(state)
    Record(points:) -> {
      let written =
        db.transaction(state.connection, fn() {
          list.try_each(points, fn(entry) {
            db.execute(
              state.statements.insert,
              on: state.connection,
              with: point_row(entry.0, entry.1),
            )
          })
        })
      case written {
        Ok(Nil) -> Nil
        Error(error) ->
          io.println_error(
            "track store: points not stored: " <> db.describe_error(error),
          )
      }
      actor.continue(state)
    }

    Query(person:, from:, to:, reply:) -> {
      db.run(
        state.statements.by_person,
        on: state.connection,
        with: [
          sqlight.text(domain.kind_to_string(person.kind)),
          sqlight.int(person.id),
          sqlight.int(from),
          sqlight.int(to),
        ],
        expecting: row_decoder(),
      )
      |> or_log_empty("query")
      |> list.map(fn(entry) { entry.1 })
      |> process.send(reply, _)
      actor.continue(state)
    }

    SiteDay(site_id:, from:, to:, reply:) -> {
      db.run(
        state.statements.by_site,
        on: state.connection,
        with: [sqlight.int(site_id), sqlight.int(from), sqlight.int(to)],
        expecting: row_decoder(),
      )
      |> or_log_empty("site query")
      |> process.send(reply, _)
      actor.continue(state)
    }

    Prune -> {
      schedule_prune(state.self, state.args.prune_every_ms)
      prune(state, state.args.clock() - state.args.retention_ms)
      actor.continue(state)
    }
  }
}

/// Deletes in chunks, each its own transaction, until nothing is left.
fn prune(state: State, oldest: Int) -> Nil {
  let deleted =
    db.execute(state.statements.prune, on: state.connection, with: [
      sqlight.int(oldest),
      sqlight.int(prune_chunk),
    ])
    |> result.map(fn(_) { db.changes(state.connection) })
  case deleted {
    Ok(count) if count >= prune_chunk -> prune(state, oldest)
    Ok(_) -> Nil
    Error(error) ->
      io.println_error(
        "track store: prune failed: " <> db.describe_error(error),
      )
  }
}

fn or_log_empty(rows: Result(List(a), db.Error), what: String) -> List(a) {
  case rows {
    Ok(rows) -> rows
    Error(error) -> {
      io.println_error(
        "track store: " <> what <> " failed: " <> db.describe_error(error),
      )
      []
    }
  }
}

// ---- Rows -------------------------------------------------------------------

fn point_row(person: Person, point: TrackPoint) -> List(sqlight.Value) {
  let kind = sqlight.text(domain.kind_to_string(person.kind))
  let id = sqlight.int(person.id)
  case point {
    GpsPoint(at:, position:, accuracy_m:, site_id:, company:) -> [
      kind,
      id,
      sqlight.int(at),
      sqlight.text("gps"),
      sqlight.nullable(sqlight.int, site_id),
      sqlight.int(company),
      sqlight.float(position.lat),
      sqlight.float(position.lng),
      sqlight.float(accuracy_m),
      sqlight.null(),
      sqlight.null(),
      sqlight.null(),
    ]
    BlePoint(at:, site_id:, zone_id:, rssi:, source:) -> [
      kind,
      id,
      sqlight.int(at),
      sqlight.text("ble"),
      sqlight.int(site_id),
      sqlight.null(),
      sqlight.null(),
      sqlight.null(),
      sqlight.null(),
      sqlight.nullable(sqlight.text, zone_id),
      sqlight.int(rssi),
      sqlight.text(case source {
        FromGateway -> "gateway"
        FromPhone -> "phone"
      }),
    ]
  }
}

fn row_decoder() -> Decoder(#(Person, TrackPoint)) {
  use kind <- decode.field(0, person_kind_decoder())
  use id <- decode.field(1, decode.int)
  use at <- decode.field(2, decode.int)
  use type_ <- decode.field(3, decode.string)
  use site_id <- decode.field(4, decode.optional(decode.int))
  let person = Person(kind, id)
  case type_, site_id {
    "gps", _ -> {
      use company <- decode.field(5, decode.optional(decode.int))
      use lat <- decode.field(6, decode.float)
      use lng <- decode.field(7, decode.float)
      use accuracy_m <- decode.field(8, decode.float)
      decode.success(#(
        person,
        GpsPoint(
          at:,
          position: geo.LatLng(lat:, lng:),
          accuracy_m:,
          site_id:,
          company: option.unwrap(company, 0),
        ),
      ))
    }
    "ble", Some(site_id) -> {
      use zone_id <- decode.field(9, decode.optional(decode.string))
      use rssi <- decode.field(10, decode.int)
      use source <- decode.field(11, decode.string)
      let source = case source {
        "gateway" -> FromGateway
        _ -> FromPhone
      }
      decode.success(#(
        person,
        BlePoint(at:, site_id:, zone_id:, rssi:, source:),
      ))
    }
    _, _ ->
      decode.failure(
        #(
          person,
          BlePoint(at:, site_id: 0, zone_id: None, rssi: 0, source: FromPhone),
        ),
        "track point",
      )
  }
}

fn person_kind_decoder() -> Decoder(domain.PersonKind) {
  use text <- decode.then(decode.string)
  case domain.kind_from_string(text) {
    Ok(kind) -> decode.success(kind)
    Error(Nil) -> decode.failure(domain.Worker, "worker or foreman")
  }
}

fn schedule_prune(self: Subject(Message), every_ms: Int) -> Nil {
  case every_ms > 0 {
    True -> {
      process.send_after(self, every_ms, Prune)
      Nil
    }
    False -> Nil
  }
}
