/// The check-in rules of `docs/api/README.md` ("Check-in (1.2)") as pure
/// functions: the phone uses them offline (badge names, flags, suggestions
/// from local scans) and the mock server uses them to answer like Django.
library;

import 'dart:convert';
import 'dart:math' as math;

import 'package:crypto/crypto.dart';

import '../data/models/models.dart';
import 'dates.dart';

/// `sha256(utf8(text))`, lower-case hex: what `checkin-setup` →
/// `badges[].payload_sha256` holds for each active badge.
String badgeHash(String scanned) =>
    sha256.convert(utf8.encode(scanned)).toString();

/// The worker whose active badge printed [scanned], or null ("Badge not
/// recognised": still queue the check-in, the server decides).
///
/// The text is hashed exactly as read; a copy with surrounding whitespace
/// removed is tried too (some scanners add a line break).
int? recogniseBadge(String scanned, Iterable<BadgeHash> badges) {
  final byHash = {for (final b in badges) b.payloadSha256.toLowerCase(): b};
  final exact = byHash[badgeHash(scanned)];
  if (exact != null) return exact.workerId;
  final trimmed = scanned.trim();
  if (trimmed == scanned) return null;
  return byHash[badgeHash(trimmed)]?.workerId;
}

/// A `PW1:<id>.<sig>` badge (37 characters, Base32). Any other QR text can
/// still be sent: the server records it as an unknown badge.
bool looksLikeBadge(String scanned) =>
    RegExp(r'^PW1:[A-Z2-7]{16}\.[A-Z2-7]{16}$').hasMatch(scanned.trim());

/// Midday splits check-ins from check-outs by default.
const midday = '12:00';

/// `in` before 12:00 site time, `out` from 12:00.
CheckInDirection defaultDirection(DateTime siteLocal) =>
    hm(siteLocal).compareTo(midday) < 0
    ? CheckInDirection.checkIn
    : CheckInDirection.checkOut;

/// Great-circle distance in metres (WGS 84 mean radius).
double distanceMetres(double lat1, double lon1, double lat2, double lon2) {
  const r = 6371008.8;
  double rad(double d) => d * math.pi / 180;
  final dLat = rad(lat2 - lat1);
  final dLon = rad(lon2 - lon1);
  final a =
      math.pow(math.sin(dLat / 2), 2) +
      math.cos(rad(lat1)) *
          math.cos(rad(lat2)) *
          math.pow(math.sin(dLon / 2), 2);
  return 2 * r * math.asin(math.min(1, math.sqrt(a)));
}

/// Inside the site's circle; GPS accuracy up to 50 m is given the benefit of
/// the doubt.
bool insideGeofence(
  Geofence fence,
  double latitude,
  double longitude,
  int? accuracyM,
) {
  final d = distanceMetres(
    fence.latitude,
    fence.longitude,
    latitude,
    longitude,
  );
  final slack = math.min(accuracyM ?? 0, 50);
  return d - slack <= fence.radiusM;
}

/// One counted check-in of a crew worker.
class CheckInEvent {
  const CheckInEvent(this.workerId, this.direction, this.at);
  final int workerId;
  final CheckInDirection direction;

  /// The instant of the scan.
  final DateTime at;
}

/// The status a worker's first `in` and last `out` suggest (site local
/// times, `HH:MM`):
///
/// 1. an `in` and an `out` before 12:00, or a first `in` at or after 12:00
///    → half day;
/// 2. else a first `in` after [lateAfter] (`07:15` is not late when
///    [lateAfter] is `07:15`) → late;
/// 3. else present;
/// 4. only an `out` → null (the foreman decides).
AttendanceStatus? suggestedStatus({
  required String? firstIn,
  required String? lastOut,
  required String? lateAfter,
}) {
  if (firstIn == null) return null;
  if (firstIn.compareTo(midday) >= 0) return AttendanceStatus.halfDay;
  if (lastOut != null && lastOut.compareTo(midday) < 0) {
    return AttendanceStatus.halfDay;
  }
  if (lateAfter != null && firstIn.compareTo(lateAfter) > 0) {
    return AttendanceStatus.late;
  }
  return AttendanceStatus.present;
}

/// Suggestions per worker from counted check-ins, like
/// `GET /sites/{id}/days/{date}/suggestions/` (sorted by worker id).
/// [localOf] turns an instant into the site's wall-clock time.
List<RollCallSuggestion> suggestFromEvents(
  Iterable<CheckInEvent> events, {
  required String? lateAfter,
  required DateTime Function(DateTime instant) localOf,
}) {
  final byWorker = <int, List<CheckInEvent>>{};
  for (final e in events) {
    byWorker.putIfAbsent(e.workerId, () => []).add(e);
  }
  final out = <RollCallSuggestion>[];
  for (final id in byWorker.keys.toList()..sort()) {
    final list = byWorker[id]!;
    DateTime? firstIn, lastOut;
    for (final e in list) {
      if (e.direction == CheckInDirection.checkIn) {
        if (firstIn == null || e.at.isBefore(firstIn)) firstIn = e.at;
      } else if (lastOut == null || e.at.isAfter(lastOut)) {
        lastOut = e.at;
      }
    }
    out.add(
      suggestionFor(
        id,
        firstIn: firstIn,
        lastOut: lastOut,
        count: list.length,
        lateAfter: lateAfter,
        localOf: localOf,
      ),
    );
  }
  return out;
}

/// One worker's suggestion from their first `in` and last `out`.
RollCallSuggestion suggestionFor(
  int workerId, {
  required DateTime? firstIn,
  required DateTime? lastOut,
  required int count,
  required String? lateAfter,
  required DateTime Function(DateTime instant) localOf,
}) {
  final arrival = firstIn == null ? null : hm(localOf(firstIn));
  final departure = lastOut == null ? null : hm(localOf(lastOut));
  return RollCallSuggestion(
    workerId: workerId,
    status: suggestedStatus(
      firstIn: arrival,
      lastOut: departure,
      lateAfter: lateAfter,
    ),
    arrival: arrival,
    departure: departure,
    firstInAt: firstIn == null ? null : isoTimestamp(firstIn),
    lastOutAt: lastOut == null ? null : isoTimestamp(lastOut),
    checkinCount: count,
  );
}

/// Joins the server's suggestions with the phone's own scans (not sent yet,
/// or sent after the suggestions were fetched): per worker, the earliest
/// `in` and the latest `out` of both, then the same rules.
List<RollCallSuggestion> mergeSuggestions(
  Iterable<RollCallSuggestion> server,
  Iterable<CheckInEvent> local, {
  required String? lateAfter,
  required DateTime Function(DateTime instant) localOf,
}) {
  final firstIn = <int, DateTime>{};
  final lastOut = <int, DateTime>{};
  final count = <int, int>{};
  final localCount = <int, int>{};
  for (final s in server) {
    final fi = s.firstInAt == null ? null : DateTime.tryParse(s.firstInAt!);
    final lo = s.lastOutAt == null ? null : DateTime.tryParse(s.lastOutAt!);
    if (fi != null) firstIn[s.workerId] = fi;
    if (lo != null) lastOut[s.workerId] = lo;
    count[s.workerId] = s.checkinCount;
  }
  for (final e in local) {
    localCount[e.workerId] = (localCount[e.workerId] ?? 0) + 1;
    if (e.direction == CheckInDirection.checkIn) {
      final cur = firstIn[e.workerId];
      if (cur == null || e.at.isBefore(cur)) firstIn[e.workerId] = e.at;
    } else {
      final cur = lastOut[e.workerId];
      if (cur == null || e.at.isAfter(cur)) lastOut[e.workerId] = e.at;
    }
  }
  final ids = {...count.keys, ...localCount.keys}.toList()..sort();
  return [
    for (final id in ids)
      suggestionFor(
        id,
        firstIn: firstIn[id],
        lastOut: lastOut[id],
        // Sent scans are in both: the larger count is the closer one.
        count: math.max(count[id] ?? 0, localCount[id] ?? 0),
        lateAfter: lateAfter,
        localOf: localOf,
      ),
  ];
}
