//// Time zones for the local-day boundaries of `GET /internal/sites/{id}/sessions`.
////
//// A zone is either a fixed UTC offset (`Z`, `UTC`, `+01:00`, `-0530`) or an
//// IANA name (`Europe/Paris`, `Africa/Lome`) read from the system's compiled
//// zoneinfo files (TZif, RFC 8536), including the POSIX TZ rule in their
//// footer that "slim" files rely on for recent and future years.
////
//// **Local day.** Day `D` in zone `Z` runs from the first instant whose local
//// date in `Z` is `D` (normally local midnight) up to, not including, the first
//// instant whose local date is the day after. It is 24 hours long except on
//// daylight-saving changes (23 or 25 hours). When midnight itself is skipped,
//// the day starts at the transition.
////
//// All instants here are Unix seconds; offsets are seconds east of UTC.

import gleam/bit_array
import gleam/int
import gleam/list
import gleam/option.{type Option, None, Some}
import gleam/result
import gleam/string
import gleam/time/calendar.{type Date, Date}

pub type Zone {
  Fixed(offset: Int)
  Rules(
    /// `#(utc seconds, offset from then on)`, oldest first.
    transitions: List(#(Int, Int)),
    /// Before the first transition.
    initial: Int,
    /// After the last transition.
    footer: Option(Posix),
  )
}

/// A POSIX TZ rule such as `CET-1CEST,M3.5.0,M10.5.0/3`.
pub type Posix {
  Posix(standard: Int, daylight: Option(Daylight))
}

pub type Daylight {
  Daylight(offset: Int, start: Rule, end: Rule)
}

pub type Rule {
  /// `Mm.w.d/time`: day `d` (0 = Sunday) of week `w` (5 = last) of month `m`.
  MonthWeekDay(month: Int, week: Int, weekday: Int, time: Int)
  /// `Jn/time`: day 1..365, February 29 never counted.
  JulianNoLeap(day: Int, time: Int)
  /// `n/time`: day 0..365, February 29 counted.
  DayOfYear(day: Int, time: Int)
}

/// Parses a `tz` parameter: a fixed offset or an IANA name looked up in
/// `zoneinfo_dir` (usually `/usr/share/zoneinfo`).
pub fn load(text: String, zoneinfo_dir: String) -> Result(Zone, Nil) {
  case parse_fixed(text) {
    Ok(offset) -> Ok(Fixed(offset))
    Error(Nil) -> {
      use name <- result.try(valid_name(text))
      use bits <- result.try(read_file(zoneinfo_dir <> "/" <> name))
      parse_tzif(bits)
    }
  }
}

/// `Z`, `UTC`, `GMT`, `±HH`, `±HHMM` or `±HH:MM`. A leading space counts as
/// `+`, since an unencoded `+` in a query string decodes to a space.
pub fn parse_fixed(text: String) -> Result(Int, Nil) {
  case text {
    "Z" | "UTC" | "GMT" | "Etc/UTC" -> Ok(0)
    "+" <> rest | " " <> rest -> parse_hours_minutes(rest)
    "-" <> rest -> parse_hours_minutes(rest) |> result.map(int.negate)
    _ -> Error(Nil)
  }
}

fn parse_hours_minutes(text: String) -> Result(Int, Nil) {
  let parts = case string.split(text, ":") {
    [hours, minutes] -> Ok(#(hours, minutes))
    [both] if both != "" ->
      case string.length(both) {
        2 -> Ok(#(both, "00"))
        4 -> Ok(#(string.slice(both, 0, 2), string.slice(both, 2, 2)))
        _ -> Error(Nil)
      }
    _ -> Error(Nil)
  }
  use #(hours, minutes) <- result.try(parts)
  use <- guard(string.length(hours) != 2 || string.length(minutes) != 2)
  use hours <- result.try(int.parse(hours))
  use minutes <- result.try(int.parse(minutes))
  use <- guard(hours < 0 || hours > 18 || minutes < 0 || minutes > 59)
  Ok(hours * 3600 + minutes * 60)
}

fn guard(failed: Bool, next: fn() -> Result(a, Nil)) -> Result(a, Nil) {
  case failed {
    True -> Error(Nil)
    False -> next()
  }
}

/// IANA names are letters, digits, `_`, `-`, `+` and `/`; no `..`, no
/// leading `/`, so the lookup stays inside the zoneinfo directory.
fn valid_name(text: String) -> Result(String, Nil) {
  let allowed =
    string.to_graphemes(text)
    |> list.all(fn(char) {
      string.contains(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-+/",
        char,
      )
    })
  case
    allowed
    && text != ""
    && !string.starts_with(text, "/")
    && !string.contains(text, "..")
  {
    True -> Ok(text)
    False -> Error(Nil)
  }
}

// ---- Offsets and days ---------------------------------------------------------

/// The zone's offset from UTC at an instant.
pub fn offset_at(zone: Zone, at: Int) -> Int {
  case zone {
    Fixed(offset) -> offset
    Rules(transitions:, initial:, footer:) -> {
      let last =
        list.fold_until(transitions, None, fn(found, transition) {
          case transition.0 <= at {
            True -> list.Continue(Some(transition))
            False -> list.Stop(found)
          }
        })
      let is_last = case last, list.last(transitions) {
        Some(found), Ok(final) -> found == final
        None, Error(Nil) -> True
        _, _ -> False
      }
      case last, footer, is_last {
        _, Some(posix), True -> posix_offset_at(posix, at)
        Some(#(_, offset)), _, _ -> offset
        None, _, _ -> initial
      }
    }
  }
}

/// `#(from, to)` in UTC seconds: the local day `date` in `zone`, `to`
/// excluded (see the module documentation).
pub fn day_bounds(zone: Zone, date: Date) -> #(Int, Int) {
  let start =
    days_from_civil(date.year, calendar.month_to_int(date.month), date.day)
  #(day_start(zone, start * 86_400), day_start(zone, { start + 1 } * 86_400))
}

/// The first instant whose local time is at or after `local` (local
/// midnight, as seconds since the epoch on the local clock).
fn day_start(zone: Zone, local: Int) -> Int {
  let before = offset_at(zone, local - 86_400)
  let after = offset_at(zone, local + 86_400)
  let valid =
    [before, after]
    |> list.filter(fn(offset) { offset_at(zone, local - offset) == offset })
    |> list.map(fn(offset) { local - offset })
  case valid {
    // Midnight exists (twice when clocks go back: take the first).
    [first, ..rest] -> list.fold(rest, first, int.min)
    // Midnight was skipped: the day starts at the transition.
    [] -> local - int.min(before, after)
  }
}

/// Parses `YYYY-MM-DD`.
pub fn parse_date(text: String) -> Result(Date, Nil) {
  case string.split(text, "-") {
    [year, month, day] -> {
      use <- guard(
        string.length(year) != 4
        || string.length(month) != 2
        || string.length(day) != 2,
      )
      use year <- result.try(int.parse(year))
      use month <- result.try(
        int.parse(month) |> result.try(calendar.month_from_int),
      )
      use day <- result.try(int.parse(day))
      let date = Date(year:, month:, day:)
      case calendar.is_valid_date(date) {
        True -> Ok(date)
        False -> Error(Nil)
      }
    }
    _ -> Error(Nil)
  }
}

// ---- POSIX TZ rules -----------------------------------------------------------

pub fn posix_offset_at(posix: Posix, at: Int) -> Int {
  case posix.daylight {
    None -> posix.standard
    Some(Daylight(offset: daylight, start:, end:)) -> {
      let #(year, _, _) =
        civil_from_days(floor_div(at + posix.standard, 86_400))
      // Starts are given in standard time, ends in daylight time.
      let starts = rule_local_seconds(start, year) - posix.standard
      let ends = rule_local_seconds(end, year) - daylight
      let in_daylight = case starts < ends {
        True -> at >= starts && at < ends
        // Southern hemisphere: daylight time spans the new year.
        False -> !{ at >= ends && at < starts }
      }
      case in_daylight {
        True -> daylight
        False -> posix.standard
      }
    }
  }
}

fn rule_local_seconds(rule: Rule, year: Int) -> Int {
  let jan_1 = days_from_civil(year, 1, 1)
  let #(day, time) = case rule {
    MonthWeekDay(month:, week:, weekday:, time:) -> {
      let first = days_from_civil(year, month, 1)
      let first_weekday = int.modulo(first + 4, 7) |> result.unwrap(0)
      let first_match =
        first + { int.modulo(weekday - first_weekday, 7) |> result.unwrap(0) }
      let length = month_length(year, month)
      let day = first_match + { week - 1 } * 7
      let day = case day - first >= length {
        True -> day - 7
        False -> day
      }
      #(day, time)
    }
    JulianNoLeap(day:, time:) -> {
      let skip_leap = calendar.is_leap_year(year) && day >= 60
      #(
        jan_1
          + day
          - 1
          + case skip_leap {
          True -> 1
          False -> 0
        },
        time,
      )
    }
    DayOfYear(day:, time:) -> #(jan_1 + day, time)
  }
  day * 86_400 + time
}

/// Parses the rule in a TZif footer, for example `CET-1CEST,M3.5.0,M10.5.0/3`,
/// `WAT-1`, `<-03>3` or `<+0330>-3:30`.
pub fn parse_posix(text: String) -> Result(Posix, Nil) {
  let chars = string.to_graphemes(text)
  use #(_, chars) <- result.try(posix_name(chars))
  use #(standard, chars) <- result.try(posix_offset(chars))
  case chars {
    [] -> Ok(Posix(standard:, daylight: None))
    _ -> {
      use #(_, chars) <- result.try(posix_name(chars))
      let #(daylight, chars) = case posix_offset(chars) {
        Ok(#(offset, rest)) -> #(offset, rest)
        Error(Nil) -> #(standard + 3600, chars)
      }
      case chars {
        [",", ..chars] -> {
          use #(start, chars) <- result.try(posix_rule(chars))
          case chars {
            [",", ..chars] -> {
              use #(end, chars) <- result.try(posix_rule(chars))
              case chars {
                [] ->
                  Ok(Posix(
                    standard:,
                    daylight: Some(Daylight(daylight, start, end)),
                  ))
                _ -> Error(Nil)
              }
            }
            _ -> Error(Nil)
          }
        }
        _ -> Error(Nil)
      }
    }
  }
}

fn posix_name(chars: List(String)) -> Result(#(String, List(String)), Nil) {
  case chars {
    ["<", ..rest] -> {
      let #(name, rest) = list.split_while(rest, fn(c) { c != ">" })
      case rest {
        [">", ..rest] if name != [] -> Ok(#(string.concat(name), rest))
        _ -> Error(Nil)
      }
    }
    _ -> {
      let #(name, rest) = list.split_while(chars, is_letter)
      case list.length(name) >= 3 {
        True -> Ok(#(string.concat(name), rest))
        False -> Error(Nil)
      }
    }
  }
}

/// A POSIX offset is positive west of Greenwich: `CET-1` is UTC+1.
fn posix_offset(chars: List(String)) -> Result(#(Int, List(String)), Nil) {
  use #(seconds, rest) <- result.map(posix_time(chars))
  #(0 - seconds, rest)
}

/// `[+-]hh[:mm[:ss]]`
fn posix_time(chars: List(String)) -> Result(#(Int, List(String)), Nil) {
  let #(sign, chars) = case chars {
    ["-", ..rest] -> #(-1, rest)
    ["+", ..rest] -> #(1, rest)
    _ -> #(1, chars)
  }
  use #(hours, chars) <- result.try(number(chars))
  let #(minutes, chars) = case chars {
    [":", ..rest] -> number(rest) |> result.unwrap(#(0, rest))
    _ -> #(0, chars)
  }
  let #(seconds, chars) = case chars {
    [":", ..rest] -> number(rest) |> result.unwrap(#(0, rest))
    _ -> #(0, chars)
  }
  Ok(#(sign * { hours * 3600 + minutes * 60 + seconds }, chars))
}

fn posix_rule(chars: List(String)) -> Result(#(Rule, List(String)), Nil) {
  let time = fn(chars) {
    case chars {
      ["/", ..rest] -> posix_time(rest)
      _ -> Ok(#(7200, chars))
    }
  }
  case chars {
    ["M", ..rest] -> {
      use #(month, rest) <- result.try(number(rest))
      use rest <- result.try(expect(rest, "."))
      use #(week, rest) <- result.try(number(rest))
      use rest <- result.try(expect(rest, "."))
      use #(weekday, rest) <- result.try(number(rest))
      use #(time, rest) <- result.try(time(rest))
      use <- guard(
        month < 1 || month > 12 || week < 1 || week > 5 || weekday > 6,
      )
      Ok(#(MonthWeekDay(month:, week:, weekday:, time:), rest))
    }
    ["J", ..rest] -> {
      use #(day, rest) <- result.try(number(rest))
      use #(time, rest) <- result.try(time(rest))
      use <- guard(day < 1 || day > 365)
      Ok(#(JulianNoLeap(day:, time:), rest))
    }
    _ -> {
      use #(day, rest) <- result.try(number(chars))
      use #(time, rest) <- result.try(time(rest))
      use <- guard(day > 365)
      Ok(#(DayOfYear(day:, time:), rest))
    }
  }
}

fn expect(chars: List(String), char: String) -> Result(List(String), Nil) {
  case chars {
    [first, ..rest] if first == char -> Ok(rest)
    _ -> Error(Nil)
  }
}

fn number(chars: List(String)) -> Result(#(Int, List(String)), Nil) {
  let #(digits, rest) = list.split_while(chars, is_digit)
  use value <- result.map(int.parse(string.concat(digits)))
  #(value, rest)
}

fn is_digit(char: String) -> Bool {
  string.contains("0123456789", char) && char != ""
}

fn is_letter(char: String) -> Bool {
  char != ""
  && string.contains(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    char,
  )
}

// ---- TZif files ---------------------------------------------------------------

/// Reads a compiled zoneinfo file (versions 1 to 4). Version 2+ files are
/// read from their 64-bit section and footer.
pub fn parse_tzif(bits: BitArray) -> Result(Zone, Nil) {
  use #(version, counts, rest) <- result.try(tzif_header(bits))
  case version {
    "1" ->
      tzif_data(rest, counts, 4)
      |> result.map(fn(data) { Rules(data.0, data.1, None) })
    _ -> {
      let v1_size = data_size(counts, 4)
      case rest {
        <<_:bytes-size(v1_size), rest:bytes>> -> {
          use #(_, counts, rest) <- result.try(tzif_header(rest))
          use #(transitions, initial, rest) <- result.try(tzif_data(
            rest,
            counts,
            8,
          ))
          Ok(Rules(transitions:, initial:, footer: tzif_footer(rest)))
        }
        _ -> Error(Nil)
      }
    }
  }
}

type Counts {
  Counts(isut: Int, isstd: Int, leap: Int, time: Int, type_: Int, char: Int)
}

fn tzif_header(bits: BitArray) -> Result(#(String, Counts, BitArray), Nil) {
  case bits {
    <<
      "TZif",
      version:bytes-size(1),
      _:bytes-size(15),
      isut:size(32),
      isstd:size(32),
      leap:size(32),
      time:size(32),
      type_:size(32),
      char:size(32),
      rest:bytes,
    >> -> {
      let version = case version {
        <<0>> -> "1"
        _ -> bit_array.to_string(version) |> result.unwrap("2")
      }
      Ok(#(version, Counts(isut:, isstd:, leap:, time:, type_:, char:), rest))
    }
    _ -> Error(Nil)
  }
}

fn data_size(counts: Counts, time_size: Int) -> Int {
  counts.time
  * time_size
  + counts.time
  + counts.type_
  * 6
  + counts.char
  + counts.leap
  * { time_size + 4 }
  + counts.isstd
  + counts.isut
}

fn tzif_data(
  bits: BitArray,
  counts: Counts,
  time_size: Int,
) -> Result(#(List(#(Int, Int)), Int, BitArray), Nil) {
  let times_size = counts.time * time_size
  let types_size = counts.type_ * 6
  let index_count = counts.time
  let skip =
    data_size(counts, time_size) - times_size - index_count - types_size
  case bits {
    <<
      times:bytes-size(times_size),
      indices:bytes-size(index_count),
      types:bytes-size(types_size),
      _:bytes-size(skip),
      rest:bytes,
    >> -> {
      use times <- result.try(read_times(times, time_size * 8, []))
      let indices = read_bytes(indices, [])
      let offsets = read_types(types, [])
      use transitions <- result.try(
        list.zip(times, indices)
        |> list.try_map(fn(pair) {
          list.drop(offsets, pair.1)
          |> list.first
          |> result.map(fn(offset) { #(pair.0, offset) })
        }),
      )
      use initial <- result.map(list.first(offsets))
      #(transitions, initial, rest)
    }
    _ -> Error(Nil)
  }
}

fn read_times(
  bits: BitArray,
  bits_per: Int,
  acc: List(Int),
) -> Result(List(Int), Nil) {
  case bits {
    <<>> -> Ok(list.reverse(acc))
    <<time:signed-size(bits_per), rest:bytes>> ->
      read_times(rest, bits_per, [time, ..acc])
    _ -> Error(Nil)
  }
}

fn read_bytes(bits: BitArray, acc: List(Int)) -> List(Int) {
  case bits {
    <<byte, rest:bytes>> -> read_bytes(rest, [byte, ..acc])
    _ -> list.reverse(acc)
  }
}

/// Each type is `utoff:int32, isdst:uint8, desigidx:uint8`; only the offset
/// is needed.
fn read_types(bits: BitArray, acc: List(Int)) -> List(Int) {
  case bits {
    <<offset:signed-size(32), _, _, rest:bytes>> ->
      read_types(rest, [offset, ..acc])
    _ -> list.reverse(acc)
  }
}

fn tzif_footer(bits: BitArray) -> Option(Posix) {
  case bits {
    <<"\n", rest:bytes>> ->
      case bit_array.to_string(rest) {
        Ok(text) ->
          case string.split_once(text, "\n") {
            Ok(#(rule, _)) if rule != "" ->
              parse_posix(rule) |> option.from_result
            _ -> None
          }
        Error(Nil) -> None
      }
    _ -> None
  }
}

// ---- Civil dates (proleptic Gregorian) ------------------------------------------

/// Days since 1970-01-01 (Howard Hinnant's algorithm).
pub fn days_from_civil(year: Int, month: Int, day: Int) -> Int {
  let year = case month <= 2 {
    True -> year - 1
    False -> year
  }
  let era = floor_div(year, 400)
  let year_of_era = year - era * 400
  let month_index = case month > 2 {
    True -> month - 3
    False -> month + 9
  }
  let day_of_year = { 153 * month_index + 2 } / 5 + day - 1
  let day_of_era =
    year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year
  era * 146_097 + day_of_era - 719_468
}

pub fn civil_from_days(days: Int) -> #(Int, Int, Int) {
  let days = days + 719_468
  let era = floor_div(days, 146_097)
  let day_of_era = days - era * 146_097
  let year_of_era =
    {
      day_of_era
      - day_of_era
      / 1460
      + day_of_era
      / 36_524
      - day_of_era
      / 146_096
    }
    / 365
  let day_of_year =
    day_of_era - { 365 * year_of_era + year_of_era / 4 - year_of_era / 100 }
  let month_index = { 5 * day_of_year + 2 } / 153
  let day = day_of_year - { 153 * month_index + 2 } / 5 + 1
  let month = case month_index < 10 {
    True -> month_index + 3
    False -> month_index - 9
  }
  let year = year_of_era + era * 400
  let year = case month <= 2 {
    True -> year + 1
    False -> year
  }
  #(year, month, day)
}

fn month_length(year: Int, month: Int) -> Int {
  case month {
    2 ->
      case calendar.is_leap_year(year) {
        True -> 29
        False -> 28
      }
    4 | 6 | 9 | 11 -> 30
    _ -> 31
  }
}

fn floor_div(a: Int, b: Int) -> Int {
  int.floor_divide(a, b) |> result.unwrap(0)
}

@external(erlang, "polaris_tracker_ffi", "read_file")
fn read_file(path: String) -> Result(BitArray, Nil)
