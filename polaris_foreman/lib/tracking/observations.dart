import 'package:uuid/uuid.dart';

import '../core/dates.dart';
import '../data/local/observations.dart';

/// One position from the phone's GPS.
class TrackingFix {
  const TrackingFix({
    required this.latitude,
    required this.longitude,
    required this.accuracyM,
    required this.at,
    this.isMocked = false,
  });

  final double latitude;
  final double longitude;

  /// Horizontal accuracy reported by the OS, in metres.
  final double accuracyM;

  /// Device time of the fix.
  final DateTime at;

  /// Android: the position came from a mock-location app (or a developer
  /// setting). Always false on iOS.
  final bool isMocked;
}

/// Why a fix was not recorded.
enum FixRejection {
  /// From a mock-location provider (Android `isMocked`): never sent.
  mocked,

  /// NaN, out of range or a negative accuracy.
  invalid,

  /// Came sooner than `gps_interval_seconds` after the last recorded one.
  tooSoon,
}

/// Decides which fixes become observations: one per `gps_interval_seconds`,
/// never a mocked or broken one.
class GpsFilter {
  GpsFilter(this.interval);

  Duration interval;
  DateTime? _last;
  int mockedCount = 0;

  /// Null when [fix] is to be recorded (and remembers it).
  FixRejection? check(TrackingFix fix) {
    if (fix.isMocked) {
      mockedCount++;
      return FixRejection.mocked;
    }
    final ok =
        fix.latitude.isFinite &&
        fix.longitude.isFinite &&
        fix.latitude.abs() <= 90 &&
        fix.longitude.abs() <= 180 &&
        fix.accuracyM.isFinite &&
        fix.accuracyM >= 0;
    if (!ok) return FixRejection.invalid;
    final last = _last;
    // A little slack: the OS delivers "every 60 s" as 59.8 s, 60.1 s…
    final slack = interval ~/ 10;
    if (last != null && fix.at.difference(last) < interval - slack) {
      // A clock moved backwards (time zone or manual change) restarts.
      if (!fix.at.isBefore(last)) return FixRejection.tooSoon;
    }
    _last = fix.at;
    return null;
  }

  void reset() => _last = null;
}

/// Reports each badge at most once per [window] per site (phones see an
/// advertisement several times a second).
class BleDeduper {
  BleDeduper({this.window = const Duration(seconds: 30)});

  final Duration window;
  final Map<String, DateTime> _last = {};

  /// Whether the sighting of [badgeId] on [siteId] at [at] is to be sent.
  bool accept(int siteId, String badgeId, DateTime at) {
    final key = '$siteId|$badgeId';
    final last = _last[key];
    if (last != null && at.difference(last) < window && !at.isBefore(last)) {
      return false;
    }
    _last[key] = at;
    if (_last.length > 2000) {
      _last.removeWhere((_, t) => at.difference(t) >= window);
    }
    return true;
  }

  void clear() => _last.clear();
}

double _round(double v, int places) => double.parse(v.toStringAsFixed(places));

const _uuid = Uuid();

/// A `gps` observation (`GpsObservation` of the tracker contract).
QueuedObservation gpsObservation({
  required String owner,
  required TrackingFix fix,
  String? clientUuid,
}) {
  final id = clientUuid ?? _uuid.v4();
  final at = isoTimestamp(fix.at);
  return QueuedObservation(
    clientUuid: id,
    owner: owner,
    at: at,
    json: {
      'client_uuid': id,
      'type': 'gps',
      'at': at,
      'lat': _round(fix.latitude, 6),
      'lng': _round(fix.longitude, 6),
      'accuracy_m': _round(fix.accuracyM, 1),
    },
  );
}

/// A `ble` observation (`BleObservation` of the tracker contract).
QueuedObservation bleObservation({
  required String owner,
  required int siteId,
  required String badgeId,
  required int rssi,
  required DateTime at,
  String? clientUuid,
}) {
  final id = clientUuid ?? _uuid.v4();
  final stamp = isoTimestamp(at);
  return QueuedObservation(
    clientUuid: id,
    owner: owner,
    at: stamp,
    json: {
      'client_uuid': id,
      'type': 'ble',
      'at': stamp,
      'site_id': siteId,
      'badge_id': badgeId,
      'rssi': rssi,
    },
  );
}
