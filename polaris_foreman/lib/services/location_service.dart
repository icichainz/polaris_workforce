import 'dart:async';

import 'package:geolocator/geolocator.dart';

/// One GPS position, rounded the way `POST /checkins/` takes it (6 decimals,
/// accuracy in whole metres).
class GpsFix {
  const GpsFix({
    required this.latitude,
    required this.longitude,
    required this.accuracyM,
    required this.at,
  });

  final double latitude;
  final double longitude;
  final int? accuracyM;
  final DateTime at;
}

enum GpsState {
  /// Not asked yet.
  unknown,

  /// Location services are off on the phone.
  serviceOff,

  /// The foreman (or worker) said no; check-ins are sent without GPS.
  denied,

  /// "Don't ask again": only the phone's settings can turn it back on.
  deniedForever,

  /// Allowed; [GpsTracker.latest] may still be null while looking.
  ready,
}

/// The few calls the tracker needs from the platform (geolocator on the
/// phone; a fake in tests).
abstract interface class LocationPlatform {
  Future<bool> serviceEnabled();
  Future<LocationPermission> checkPermission();
  Future<LocationPermission> requestPermission();

  /// Throws when no position comes in time.
  Future<GpsFix> currentPosition({required Duration timeLimit});
}

class GeolocatorPlatformAdapter implements LocationPlatform {
  const GeolocatorPlatformAdapter();

  @override
  Future<bool> serviceEnabled() => Geolocator.isLocationServiceEnabled();

  @override
  Future<LocationPermission> checkPermission() => Geolocator.checkPermission();

  @override
  Future<LocationPermission> requestPermission() =>
      Geolocator.requestPermission();

  @override
  Future<GpsFix> currentPosition({required Duration timeLimit}) async {
    final p = await Geolocator.getCurrentPosition(
      locationSettings: LocationSettings(
        accuracy: LocationAccuracy.high,
        timeLimit: timeLimit,
      ),
    );
    return GpsFix(
      latitude: _six(p.latitude),
      longitude: _six(p.longitude),
      accuracyM: p.accuracy.isFinite && p.accuracy >= 0
          ? p.accuracy.round()
          : null,
      at: p.timestamp,
    );
  }
}

double _six(double v) => double.parse(v.toStringAsFixed(6));

/// Keeps a recent GPS fix while a scan screen is open, so a scan never waits
/// for the GPS: each check-in takes the latest fix if it is fresh enough, or
/// goes without one. GPS is optional; a refusal never blocks a scan.
class GpsTracker {
  GpsTracker(
    this.platform, {
    DateTime Function()? now,
    this.maxAge = const Duration(minutes: 3),
    this.timeLimit = const Duration(seconds: 15),
  }) : _now = now ?? DateTime.now;

  final LocationPlatform platform;
  final DateTime Function() _now;

  /// A fix older than this is not sent.
  final Duration maxAge;
  final Duration timeLimit;

  GpsState state = GpsState.unknown;
  GpsFix? latest;
  DateTime? _fixedAt;
  Future<GpsFix?>? _refreshing;

  /// Asks for the permission when needed, then looks for a first fix.
  Future<GpsState> start() async {
    try {
      if (!await platform.serviceEnabled()) {
        return state = GpsState.serviceOff;
      }
      var permission = await platform.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await platform.requestPermission();
      }
      state = switch (permission) {
        LocationPermission.always ||
        LocationPermission.whileInUse => GpsState.ready,
        LocationPermission.deniedForever => GpsState.deniedForever,
        _ => GpsState.denied,
      };
    } catch (_) {
      state = GpsState.denied;
    }
    if (state == GpsState.ready) unawaited(refresh());
    return state;
  }

  /// Looks for a new fix (one at a time). Null when none came.
  Future<GpsFix?> refresh() {
    if (state != GpsState.ready) return Future.value(null);
    return _refreshing ??= _fetch().whenComplete(() {
      _refreshing = null;
    });
  }

  Future<GpsFix?> _fetch() async {
    try {
      final fix = await platform.currentPosition(timeLimit: timeLimit);
      latest = fix;
      _fixedAt = _now();
      return fix;
    } catch (_) {
      return null;
    }
  }

  /// The latest fix if it is recent enough to send with a check-in.
  GpsFix? fresh() {
    final fix = latest, at = _fixedAt;
    if (fix == null || at == null) return null;
    if (_now().difference(at) > maxAge) return null;
    return fix;
  }
}
