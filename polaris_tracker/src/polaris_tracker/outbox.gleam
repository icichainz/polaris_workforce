//// Delivers presence events to Django in batches, oldest first, and keeps
//// them until Django accepts them. Django stores events idempotently by
//// `event_id`, so a retry after a lost response is harmless.
////
//// The queue is a table in the tracker's own SQLite file: an event is
//// written when it is enqueued and deleted only after Django answered 2xx,
//// so a restart does not lose undelivered events. After a failed delivery
//// the next attempt waits twice as long (up to `max_backoff_ms`).

import gleam/dynamic/decode
import gleam/erlang/process.{type Name, type Subject}
import gleam/int
import gleam/io
import gleam/json
import gleam/list
import gleam/option.{type Option, None, Some}
import gleam/otp/actor
import gleam/result
import polaris_tracker/db
import polaris_tracker/domain.{type PresenceEvent}
import polaris_tracker/wire
import sqlight

pub type Message {
  Enqueue(events: List(PresenceEvent))
  /// Deliver one batch now (tests, and draining a long queue).
  Flush
  /// The periodic timer.
  Tick
  Pending(reply: Subject(Int))
}

pub type Args {
  Args(
    /// A file path, or `db.memory`.
    db_path: String,
    deliver: fn(List(PresenceEvent)) -> Result(Nil, String),
    /// 0 disables the timer (tests send `Flush` themselves).
    flush_every_ms: Int,
    batch_size: Int,
    clock: fn() -> Int,
  )
}

/// The longest wait between two attempts while Django keeps failing.
pub const max_backoff_ms = 60_000

type State {
  State(
    args: Args,
    connection: sqlight.Connection,
    statements: Statements,
    self: Subject(Message),
    /// Failed deliveries in a row.
    failures: Int,
  )
}

type Statements {
  Statements(
    insert: db.Statement,
    oldest: db.Statement,
    delete_up_to: db.Statement,
    count: db.Statement,
  )
}

const schema_version = 1

pub fn start(
  args: Args,
  name: Option(Name(Message)),
) -> actor.StartResult(Subject(Message)) {
  let builder =
    actor.new_with_initialiser(5000, fn(self) {
      use #(connection, statements) <- result.map(
        open(args.db_path)
        |> result.map_error(fn(error) { "outbox: " <> db.describe_error(error) }),
      )
      schedule(self, args.flush_every_ms)
      State(args:, connection:, statements:, self:, failures: 0)
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
  use version <- result.try(db.user_version_of(connection, "outbox"))
  use _ <- result.try(case version {
    0 ->
      db.exec(
        "BEGIN;
         CREATE TABLE IF NOT EXISTS outbox (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           event_id TEXT NOT NULL UNIQUE,
           body TEXT NOT NULL,          -- the event as sent to Django (JSON)
           created_at INTEGER NOT NULL  -- Unix milliseconds
         );
         INSERT OR REPLACE INTO schema_versions (name, version)
           VALUES ('outbox', 1);
         COMMIT;",
        on: connection,
      )
    _ if version == schema_version -> Ok(Nil)
    _ ->
      Error(
        db.Sqlite(sqlight.SqlightError(
          sqlight.GenericError,
          "unknown outbox schema version; newer tracker?",
          -1,
        )),
      )
  })
  use insert <- result.try(db.prepare(
    "INSERT OR IGNORE INTO outbox (event_id, body, created_at)
     VALUES (?1, ?2, ?3)",
    on: connection,
  ))
  use oldest <- result.try(db.prepare(
    "SELECT id, body FROM outbox ORDER BY id LIMIT ?1",
    on: connection,
  ))
  use delete_up_to <- result.try(db.prepare(
    "DELETE FROM outbox WHERE id <= ?1",
    on: connection,
  ))
  use count <- result.map(db.prepare(
    "SELECT count(*) FROM outbox",
    on: connection,
  ))
  #(connection, Statements(insert:, oldest:, delete_up_to:, count:))
}

fn handle_message(
  state: State,
  message: Message,
) -> actor.Next(State, Message) {
  case message {
    Enqueue(events:) -> {
      let now = state.args.clock()
      let written =
        db.transaction(state.connection, fn() {
          list.try_each(events, fn(event) {
            db.execute(state.statements.insert, on: state.connection, with: [
              sqlight.text(event.event_id),
              sqlight.text(json.to_string(wire.presence_event_json(event))),
              sqlight.int(now),
            ])
          })
        })
      case written {
        Ok(Nil) -> Nil
        Error(error) ->
          io.println_error(
            "outbox: events not stored: " <> db.describe_error(error),
          )
      }
      actor.continue(state)
    }

    Flush -> actor.continue(flush(state))

    Tick -> {
      let state = flush(state)
      schedule(state.self, delay(state))
      actor.continue(state)
    }

    Pending(reply:) -> {
      db.run(
        state.statements.count,
        on: state.connection,
        with: [],
        expecting: decode.field(0, decode.int, decode.success),
      )
      |> result.try(fn(rows) {
        list.first(rows) |> result.replace_error(db.BadRow([]))
      })
      |> result.unwrap(0)
      |> process.send(reply, _)
      actor.continue(state)
    }
  }
}

/// Sends the oldest batch. Rows are deleted only after Django's 2xx.
fn flush(state: State) -> State {
  let rows =
    db.run(
      state.statements.oldest,
      on: state.connection,
      with: [sqlight.int(state.args.batch_size)],
      expecting: {
        use id <- decode.field(0, decode.int)
        use body <- decode.field(1, decode.string)
        decode.success(#(id, body))
      },
    )
  case rows {
    Error(error) -> {
      io.println_error(
        "outbox: cannot read the queue: " <> db.describe_error(error),
      )
      state
    }
    Ok([]) -> State(..state, failures: 0)
    Ok(rows) -> {
      let last_id = list.fold(rows, 0, fn(last, row) { int.max(last, row.0) })
      // A row that no longer decodes would block the queue forever: it is
      // logged and dropped with the batch.
      let events =
        list.filter_map(rows, fn(row) {
          case json.parse(row.1, wire.presence_event_decoder()) {
            Ok(event) -> Ok(event)
            Error(_) -> {
              io.println_error("outbox: dropping unreadable event: " <> row.1)
              Error(Nil)
            }
          }
        })
      let delivered = case events {
        [] -> Ok(Nil)
        _ -> state.args.deliver(events)
      }
      case delivered {
        Ok(Nil) -> {
          case
            db.execute(
              state.statements.delete_up_to,
              on: state.connection,
              with: [
                sqlight.int(last_id),
              ],
            )
          {
            Ok(Nil) -> Nil
            Error(error) ->
              io.println_error(
                "outbox: delivered but not deleted (will resend): "
                <> db.describe_error(error),
              )
          }
          // A full batch means more may be waiting: keep draining.
          case list.length(rows) >= state.args.batch_size {
            True -> process.send(state.self, Flush)
            False -> Nil
          }
          State(..state, failures: 0)
        }
        Error(reason) -> {
          io.println_error(
            "outbox: delivery to Django failed, will retry: " <> reason,
          )
          State(..state, failures: state.failures + 1)
        }
      }
    }
  }
}

/// The normal period, doubled for each failure in a row, capped.
fn delay(state: State) -> Int {
  backoff(state.args.flush_every_ms, state.failures)
  |> int.max(state.args.flush_every_ms)
}

fn backoff(ms: Int, failures: Int) -> Int {
  case failures <= 0 || ms >= max_backoff_ms {
    True -> int.min(ms, max_backoff_ms)
    False -> backoff(ms * 2, failures - 1)
  }
}

fn schedule(self: Subject(Message), every_ms: Int) -> Nil {
  case every_ms > 0 {
    True -> {
      process.send_after(self, every_ms, Tick)
      Nil
    }
    False -> Nil
  }
}
