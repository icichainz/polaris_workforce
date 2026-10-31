//// Wall-clock time as Unix milliseconds, and RFC 3339 conversion for the API.

import gleam/result
import gleam/time/calendar
import gleam/time/timestamp

@external(erlang, "polaris_tracker_ffi", "now_ms")
pub fn now_ms() -> Int

/// Formats Unix milliseconds as an RFC 3339 timestamp in UTC.
pub fn to_rfc3339(ms: Int) -> String {
  timestamp.from_unix_seconds_and_nanoseconds(ms / 1000, ms % 1000 * 1_000_000)
  |> timestamp.to_rfc3339(calendar.utc_offset)
}

/// Parses an RFC 3339 timestamp (any offset) into Unix milliseconds.
pub fn parse_rfc3339(text: String) -> Result(Int, Nil) {
  use stamp <- result.map(timestamp.parse_rfc3339(text))
  let #(seconds, nanoseconds) = timestamp.to_unix_seconds_and_nanoseconds(stamp)
  seconds * 1000 + nanoseconds / 1_000_000
}
