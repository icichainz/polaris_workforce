import 'package:timezone/data/latest_10y.dart' as tzdata;
import 'package:timezone/timezone.dart' as tz;

import 'dates.dart';

bool _tzReady = false;

void initTimeZones() {
  if (_tzReady) return;
  tzdata.initializeTimeZones();
  _tzReady = true;
}

/// "Today" and "now" on the site: a work day is a date in the company's time
/// zone (`GET /me/` → `company.time_zone`), not the phone's.
class SiteClock {
  SiteClock({DateTime Function()? now, String? timeZone})
    : _now = now ?? DateTime.now,
      _location = _resolve(timeZone);

  final DateTime Function() _now;
  final tz.Location? _location;

  static tz.Location? _resolve(String? name) {
    if (name == null) return null;
    try {
      initTimeZones();
      return tz.getLocation(name);
    } catch (_) {
      return null; // Unknown zone: fall back to the phone's clock.
    }
  }

  /// The instant, for timestamps (`paid_at`, …).
  DateTime instant() => _now();

  /// Wall-clock time on the site now.
  DateTime local() => localOf(_now());

  /// Wall-clock time on the site at [instant] (fields only; ignore its zone).
  DateTime localOf(DateTime instant) {
    final loc = _location;
    if (loc == null) return instant.toLocal();
    final t = tz.TZDateTime.from(instant, loc);
    return DateTime(t.year, t.month, t.day, t.hour, t.minute, t.second);
  }

  String today() => isoDate(local());

  /// `HH:MM` on the site.
  String nowHm() => hm(local());
}
