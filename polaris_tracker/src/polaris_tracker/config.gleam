//// Service configuration from environment variables.

import gleam/bit_array
import gleam/int
import gleam/option.{type Option, None, Some}
import gleam/result

pub type Config {
  Config(
    port: Int,
    bind: String,
    secret: BitArray,
    /// Where presence events go; unset means "log them only".
    django_events_url: Option(String),
    /// Origin of the office dashboard, when it is served from another origin.
    cors_origin: Option(String),
    track_retention_days: Int,
    /// The tracker's own SQLite file (tracks and outbox), or `:memory:`.
    db_path: String,
    /// Compiled IANA time zones, for the `tz` of the sessions endpoint.
    zoneinfo_dir: String,
  )
}

/// Where the SQLite file goes when `TRACKER_DB_PATH` is not set, relative to
/// the working directory.
pub const default_db_path = "var/tracker.sqlite3"

const dev_secret = "dev-only-insecure-tracker-secret"

pub fn load() -> Result(Config, String) {
  from_env(get_env)
}

/// `lookup` returns an environment variable, so tests can pass a fake one.
pub fn from_env(
  lookup: fn(String) -> Result(String, Nil),
) -> Result(Config, String) {
  let optional = fn(name) { lookup(name) |> option.from_result }
  use port <- result.try(int_var(lookup, "TRACKER_PORT", 4100))
  use track_retention_days <- result.try(int_var(
    lookup,
    "TRACK_RETENTION_DAYS",
    30,
  ))
  use secret <- result.try(
    case lookup("TRACKER_SHARED_SECRET"), lookup("TRACKER_DEBUG") {
      Ok(secret), _ if secret != "" -> Ok(secret)
      _, Ok("1") -> Ok(dev_secret)
      _, _ ->
        Error("TRACKER_SHARED_SECRET must be set when TRACKER_DEBUG is not 1.")
    },
  )
  Ok(Config(
    port:,
    bind: lookup("TRACKER_BIND") |> result.unwrap("127.0.0.1"),
    secret: bit_array.from_string(secret),
    django_events_url: optional("DJANGO_EVENTS_URL"),
    cors_origin: optional("TRACKER_CORS_ORIGIN"),
    track_retention_days:,
    db_path: case lookup("TRACKER_DB_PATH") {
      Ok(path) if path != "" -> path
      _ -> default_db_path
    },
    zoneinfo_dir: lookup("TRACKER_ZONEINFO_DIR")
      |> result.unwrap("/usr/share/zoneinfo"),
  ))
}

fn int_var(
  lookup: fn(String) -> Result(String, Nil),
  name: String,
  default: Int,
) -> Result(Int, String) {
  case lookup(name) {
    Error(Nil) -> Ok(default)
    Ok(text) ->
      case int.parse(text) {
        Ok(value) if value > 0 -> Ok(value)
        _ -> Error(name <> " must be a positive integer.")
      }
  }
}

@external(erlang, "polaris_tracker_ffi", "get_env")
fn get_env(name: String) -> Result(String, Nil)

/// One line for the startup log.
pub fn describe_sink(config: Config) -> String {
  let sink = case config.django_events_url {
    Some(url) -> "posting presence events to " <> url
    None -> "DJANGO_EVENTS_URL not set: logging presence events only"
  }
  let store = case config.db_path {
    ":memory:" -> "storing tracks and the outbox in memory (TRACKER_DB_PATH)"
    path -> "storing tracks and the outbox in " <> path
  }
  sink <> "; " <> store
}
