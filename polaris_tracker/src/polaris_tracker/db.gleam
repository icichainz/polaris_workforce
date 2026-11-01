//// The tracker's own SQLite file (never Django's): opening it, prepared
//// statements and short transactions. Built on `sqlight` (esqlite).
////
//// `TRACKER_DB_PATH` names the file; `:memory:` keeps everything in memory
//// (tests). Each actor that stores something opens its own connection; the
//// file is in WAL mode, so readers never block the writer.

import gleam/dynamic.{type Dynamic}
import gleam/dynamic/decode.{type Decoder}
import gleam/list
import gleam/result
import gleam/string
import sqlight.{type Connection, type Value}

/// In-memory database: nothing survives the connection.
pub const memory = ":memory:"

/// A compiled statement, kept for the life of its connection.
pub type Statement

pub type Error {
  /// The directory for the file could not be created.
  CannotCreateDirectory(path: String, reason: String)
  Sqlite(sqlight.Error)
  /// A row did not have the expected columns.
  BadRow(List(decode.DecodeError))
}

/// Opens (creating if needed) the database at `path`, in WAL mode with
/// `synchronous = NORMAL`: a commit survives a crash of the tracker, and the
/// last commits before a power cut may be lost, which the outbox and the
/// phones' own retries tolerate.
pub fn open(path: String) -> Result(Connection, Error) {
  use _ <- result.try(case path {
    ":memory:" -> Ok(Nil)
    _ ->
      ensure_parent_dir(path)
      |> result.map_error(CannotCreateDirectory(path, _))
  })
  use connection <- result.try(sqlight.open(path) |> result.map_error(Sqlite))
  use _ <- result.map(
    sqlight.exec(
      "PRAGMA journal_mode = WAL;
       PRAGMA synchronous = NORMAL;
       PRAGMA busy_timeout = 5000;
       PRAGMA temp_store = MEMORY;",
      on: connection,
    )
    |> result.map_error(Sqlite),
  )
  connection
}

pub fn exec(sql: String, on connection: Connection) -> Result(Nil, Error) {
  sqlight.exec(sql, on: connection) |> result.map_error(Sqlite)
}

pub fn prepare(
  sql: String,
  on connection: Connection,
) -> Result(Statement, Error) {
  ffi_prepare(connection, sql) |> result.map_error(Sqlite)
}

/// Runs a prepared statement with positional arguments (`?1`, `?2`, … or
/// `?`) and decodes every row, a tuple of its columns.
pub fn run(
  statement: Statement,
  on connection: Connection,
  with arguments: List(Value),
  expecting decoder: Decoder(a),
) -> Result(List(a), Error) {
  use rows <- result.try(
    ffi_run(connection, statement, arguments) |> result.map_error(Sqlite),
  )
  list.try_map(rows, decode.run(_, decoder)) |> result.map_error(BadRow)
}

/// Runs a statement that returns no rows.
pub fn execute(
  statement: Statement,
  on connection: Connection,
  with arguments: List(Value),
) -> Result(Nil, Error) {
  run(
    statement,
    on: connection,
    with: arguments,
    expecting: decode.success(Nil),
  )
  |> result.replace(Nil)
}

/// Rows changed by the last INSERT, UPDATE or DELETE on the connection.
@external(erlang, "polaris_tracker_ffi", "db_changes")
pub fn changes(connection: Connection) -> Int

/// Runs `body` in one `BEGIN IMMEDIATE … COMMIT` transaction, rolled back
/// if `body` fails. Keep the body short: it holds the write lock.
pub fn transaction(
  connection: Connection,
  body: fn() -> Result(a, Error),
) -> Result(a, Error) {
  use _ <- result.try(exec("BEGIN IMMEDIATE", on: connection))
  case body() {
    Ok(value) ->
      case exec("COMMIT", on: connection) {
        Ok(Nil) -> Ok(value)
        Error(error) -> {
          let _ = exec("ROLLBACK", on: connection)
          Error(error)
        }
      }
    Error(error) -> {
      let _ = exec("ROLLBACK", on: connection)
      Error(error)
    }
  }
}

/// The schema version of one component (`track_points`, `outbox`) in the
/// file, 0 when it has no tables yet. Components share the file, so each
/// keeps its version in the `schema_versions` table.
pub fn user_version_of(
  connection: Connection,
  component: String,
) -> Result(Int, Error) {
  use _ <- result.try(exec(
    "CREATE TABLE IF NOT EXISTS schema_versions (
       name TEXT PRIMARY KEY,
       version INTEGER NOT NULL
     )",
    on: connection,
  ))
  sqlight.query(
    "SELECT version FROM schema_versions WHERE name = ?1",
    on: connection,
    with: [sqlight.text(component)],
    expecting: decode.field(0, decode.int, decode.success),
  )
  |> result.map_error(Sqlite)
  |> result.map(fn(rows) { list.first(rows) |> result.unwrap(0) })
}

pub fn describe_error(error: Error) -> String {
  case error {
    CannotCreateDirectory(path:, reason:) ->
      "cannot create the directory for " <> path <> ": " <> reason
    Sqlite(sqlight.SqlightError(code:, message:, ..)) ->
      "SQLite error " <> string.inspect(code) <> ": " <> message
    BadRow(errors) -> "unexpected row: " <> string.inspect(errors)
  }
}

@external(erlang, "polaris_tracker_ffi", "ensure_parent_dir")
fn ensure_parent_dir(path: String) -> Result(Nil, String)

@external(erlang, "polaris_tracker_ffi", "db_prepare")
fn ffi_prepare(
  connection: Connection,
  sql: String,
) -> Result(Statement, sqlight.Error)

@external(erlang, "polaris_tracker_ffi", "db_run")
fn ffi_run(
  connection: Connection,
  statement: Statement,
  arguments: List(Value),
) -> Result(List(Dynamic), sqlight.Error)
