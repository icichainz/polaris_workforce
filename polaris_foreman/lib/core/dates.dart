/// `YYYY-MM-DD` for a calendar date (ignores the time part).
String isoDate(DateTime d) =>
    '${d.year.toString().padLeft(4, '0')}-'
    '${d.month.toString().padLeft(2, '0')}-'
    '${d.day.toString().padLeft(2, '0')}';

/// Parses `YYYY-MM-DD` as a UTC midnight, safe for day arithmetic.
DateTime parseIsoDate(String s) {
  final p = s.split('-');
  return DateTime.utc(int.parse(p[0]), int.parse(p[1]), int.parse(p[2]));
}

String addDays(String date, int days) =>
    isoDate(parseIsoDate(date).add(Duration(days: days)));

/// `HH:MM`, 24-hour.
String hm(DateTime d) =>
    '${d.hour.toString().padLeft(2, '0')}:${d.minute.toString().padLeft(2, '0')}';

/// ISO 8601 UTC timestamp with a `Z` offset, seconds precision.
String isoTimestamp(DateTime d) {
  final u = d.toUtc();
  return '${isoDate(u)}T${hm(u)}:${u.second.toString().padLeft(2, '0')}Z';
}
