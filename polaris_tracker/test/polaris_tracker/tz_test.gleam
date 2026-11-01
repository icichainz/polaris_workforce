//// Local-day bounds. Expected values come from Python's `zoneinfo`.

import gleam/option.{Some}
import gleam/time/calendar.{Date}
import polaris_tracker/tz

fn date(year: Int, month: Int, day: Int) -> calendar.Date {
  let assert Ok(month) = calendar.month_from_int(month)
  Date(year:, month:, day:)
}

fn rule(text: String) -> tz.Zone {
  let assert Ok(posix) = tz.parse_posix(text)
  tz.Rules(transitions: [], initial: posix.standard, footer: Some(posix))
}

pub fn fixed_offsets_test() {
  assert tz.parse_fixed("Z") == Ok(0)
  assert tz.parse_fixed("UTC") == Ok(0)
  assert tz.parse_fixed("+01:00") == Ok(3600)
  assert tz.parse_fixed("-05:30") == Ok(-19_800)
  assert tz.parse_fixed("+0545") == Ok(20_700)
  assert tz.parse_fixed("-03") == Ok(-10_800)
  // An unencoded `+` in a query string arrives as a space.
  assert tz.parse_fixed(" 01:00") == Ok(3600)
  assert tz.parse_fixed("+25:00") == Error(Nil)
  assert tz.parse_fixed("+1:00") == Error(Nil)
  assert tz.parse_fixed("Paris") == Error(Nil)
}

pub fn fixed_day_test() {
  assert tz.day_bounds(tz.Fixed(0), date(2026, 10, 31))
    == #(1_793_404_800, 1_793_491_200)
  assert tz.day_bounds(tz.Fixed(3600), date(2026, 10, 31))
    == #(1_793_404_800 - 3600, 1_793_491_200 - 3600)
}

pub fn parse_date_test() {
  assert tz.parse_date("2026-10-31") == Ok(date(2026, 10, 31))
  assert tz.parse_date("2028-02-29") == Ok(date(2028, 2, 29))
  assert tz.parse_date("2026-02-29") == Error(Nil)
  assert tz.parse_date("2026-13-01") == Error(Nil)
  assert tz.parse_date("2026-1-01") == Error(Nil)
  assert tz.parse_date("yesterday") == Error(Nil)
}

pub fn civil_days_test() {
  assert tz.days_from_civil(1970, 1, 1) == 0
  assert tz.days_from_civil(2026, 10, 31) == 20_757
  assert tz.civil_from_days(20_757) == #(2026, 10, 31)
  assert tz.civil_from_days(-1) == #(1969, 12, 31)
  assert tz.days_from_civil(2000, 3, 1) == 11_017
}

pub fn paris_rule_test() {
  let paris = rule("CET-1CEST,M3.5.0,M10.5.0/3")
  // Spring forward: a 23-hour day.
  assert tz.day_bounds(paris, date(2026, 3, 29))
    == #(1_774_738_800, 1_774_821_600)
  // Fall back: a 25-hour day.
  assert tz.day_bounds(paris, date(2026, 10, 25))
    == #(1_792_879_200, 1_792_969_200)
  assert tz.day_bounds(paris, date(2026, 7, 1))
    == #(1_782_856_800, 1_782_943_200)
  assert tz.day_bounds(paris, date(2045, 3, 26))
    == #(2_374_095_600, 2_374_178_400)
}

pub fn southern_hemisphere_rule_test() {
  let sydney = rule("AEST-10AEDT,M10.1.0,M4.1.0/3")
  assert tz.day_bounds(sydney, date(2026, 10, 4))
    == #(1_791_036_000, 1_791_118_800)
}

/// Santiago skips midnight itself: the day starts at 01:00 local.
pub fn skipped_midnight_rule_test() {
  let santiago = rule("<-04>4<-03>,M9.1.6/24,M4.1.6/24")
  assert tz.day_bounds(santiago, date(2026, 9, 6))
    == #(1_788_667_200, 1_788_750_000)
}

pub fn no_daylight_rule_test() {
  assert tz.day_bounds(rule("GMT0"), date(2026, 10, 31))
    == #(1_793_404_800, 1_793_491_200)
  let assert Ok(posix) = tz.parse_posix("<+0330>-3:30")
  assert posix.standard == 12_600
}

/// A "slim" TZif file: no transitions at all, only the footer rule.
pub fn slim_tzif_test() {
  let header = fn(version) {
    <<
      "TZif",
      version:utf8,
      0:size(120),
      0:size(32),
      0:size(32),
      0:size(32),
      0:size(32),
      1:size(32),
      4:size(32),
    >>
  }
  let data = <<3600:size(32), 0, 0, "CET", 0>>
  let file = <<
    header("2"):bits,
    data:bits,
    header("2"):bits,
    data:bits,
    "\nCET-1CEST,M3.5.0,M10.5.0/3\n",
  >>
  let assert Ok(zone) = tz.parse_tzif(file)
  assert tz.day_bounds(zone, date(2026, 3, 29))
    == #(1_774_738_800, 1_774_821_600)
}

/// The system's zoneinfo, when present (macOS, most Linux images).
pub fn system_zoneinfo_test() {
  case tz.load("Europe/Paris", "/usr/share/zoneinfo") {
    Error(Nil) -> Nil
    Ok(paris) -> {
      assert tz.day_bounds(paris, date(2026, 10, 25))
        == #(1_792_879_200, 1_792_969_200)
      // Past the last transition listed in "fat" files (2037).
      assert tz.day_bounds(paris, date(2045, 3, 26))
        == #(2_374_095_600, 2_374_178_400)
    }
  }
}

pub fn zone_names_stay_in_the_directory_test() {
  assert tz.load("../../etc/passwd", "/usr/share/zoneinfo") == Error(Nil)
  assert tz.load("/etc/passwd", "/usr/share/zoneinfo") == Error(Nil)
  assert tz.load("Nowhere/Atlantis", "/usr/share/zoneinfo") == Error(Nil)
}
