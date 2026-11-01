import 'dart:async';
import 'dart:io' show Platform;

import 'package:flutter/foundation.dart'
    show defaultTargetPlatform, TargetPlatform;
import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:geolocator/geolocator.dart';

import 'badge_decoder.dart';
import 'observations.dart';

/// Location permission as tracking sees it.
enum LocationAccess {
  unknown,

  /// Allowed (while in use or always).
  ready,

  /// Refused, or "don't ask again".
  denied,

  /// Location services are off on the phone.
  serviceOff,
}

/// The Android notification that keeps GPS running while the app is in the
/// background (a foreground service: the OS requires it, and it tells the
/// foreman that the position is being recorded).
class BackgroundNotice {
  const BackgroundNotice({required this.title, required this.text});
  final String title;
  final String text;
}

/// GPS for tracking (geolocator on the phone; a fake in tests).
abstract interface class TrackingLocationSource {
  /// The permission; asks for it when [request] and not decided yet.
  Future<LocationAccess> access({bool request = false});

  /// Positions about every [interval] until the subscription is cancelled.
  /// Must be listened to while the app is in the foreground; it may then
  /// keep going in the background where the platform allows it.
  Stream<TrackingFix> positions({
    required Duration interval,
    required BackgroundNotice notice,
  });
}

/// Background limits (no paid plugin, while-in-use permission only):
///
/// * **Android:** the position stream runs in geolocator's foreground
///   service (type `location`) with a notification, so fixes keep coming
///   with the screen off or another app open. It must be started while the
///   app is visible (Android 12+ forbids starting it from the background):
///   when tracking hours begin with the app in the background, recording
///   starts the next time the foreman opens it. Swiping the app away, or
///   aggressive battery savers (some Tecno/Infinix/Xiaomi builds), stop it.
///   No `ACCESS_BACKGROUND_LOCATION` (it needs a Play Store review).
/// * **iOS:** `allowsBackgroundLocationUpdates` with the blue status-bar
///   indicator (Info.plist `UIBackgroundModes: location`), also started in
///   the foreground only. iOS may still suspend the app; force-quitting it
///   stops recording until it is opened again.
class GeolocatorTrackingSource implements TrackingLocationSource {
  const GeolocatorTrackingSource();

  @override
  Future<LocationAccess> access({bool request = false}) async {
    try {
      if (!await Geolocator.isLocationServiceEnabled()) {
        return LocationAccess.serviceOff;
      }
      var p = await Geolocator.checkPermission();
      if (p == LocationPermission.denied && request) {
        p = await Geolocator.requestPermission();
      }
      return switch (p) {
        LocationPermission.always ||
        LocationPermission.whileInUse => LocationAccess.ready,
        LocationPermission.unableToDetermine => LocationAccess.unknown,
        _ => LocationAccess.denied,
      };
    } catch (_) {
      return LocationAccess.denied;
    }
  }

  @override
  Stream<TrackingFix> positions({
    required Duration interval,
    required BackgroundNotice notice,
  }) {
    final LocationSettings settings = switch (defaultTargetPlatform) {
      TargetPlatform.android => AndroidSettings(
        accuracy: LocationAccuracy.high,
        distanceFilter: 0,
        intervalDuration: interval,
        foregroundNotificationConfig: ForegroundNotificationConfig(
          notificationTitle: notice.title,
          notificationText: notice.text,
          notificationChannelName: notice.title,
          setOngoing: true,
          enableWakeLock: false,
        ),
      ),
      TargetPlatform.iOS => AppleSettings(
        accuracy: LocationAccuracy.high,
        distanceFilter: 0,
        activityType: ActivityType.other,
        pauseLocationUpdatesAutomatically: false,
        allowBackgroundLocationUpdates: true,
        showBackgroundLocationIndicator: true,
      ),
      _ => const LocationSettings(accuracy: LocationAccuracy.high),
    };
    return Geolocator.getPositionStream(locationSettings: settings).map(
      (p) => TrackingFix(
        latitude: p.latitude,
        longitude: p.longitude,
        accuracyM: p.accuracy,
        at: p.timestamp,
        isMocked: p.isMocked,
      ),
    );
  }
}

/// A badge seen by the phone.
class BadgeSighting {
  const BadgeSighting({
    required this.badgeId,
    required this.rssi,
    required this.at,
  });

  final String badgeId;
  final int rssi;
  final DateTime at;
}

/// BLE badge scanning (flutter_blue_plus on the phone; a fake in tests).
abstract interface class BadgeScanner {
  /// Scans while listened to; each advertisement that [decodeBadgeId]
  /// recognises is one event (no de-duplication here).
  Stream<BadgeSighting> sightings();
}

/// [BadgeScanner] with flutter_blue_plus. Waits for Bluetooth to be on,
/// scans continuously (one result per advertisement) and restarts the scan
/// every [restartEvery]: Android quietly downgrades scans that run for more
/// than 30 minutes.
class BluePlusBadgeScanner implements BadgeScanner {
  BluePlusBadgeScanner({this.restartEvery = const Duration(minutes: 10)});

  final Duration restartEvery;

  @override
  Stream<BadgeSighting> sightings() {
    late StreamController<BadgeSighting> out;
    StreamSubscription<BluetoothAdapterState>? adapter;
    StreamSubscription<List<ScanResult>>? results;
    Timer? restart;
    var on = false;
    var cancelled = false;

    Future<void> startScan() async {
      try {
        await FlutterBluePlus.startScan(
          continuousUpdates: true,
          oneByOne: true,
          androidUsesFineLocation: true,
          androidScanMode: AndroidScanMode.balanced,
        );
      } catch (_) {
        // Permission refused or Bluetooth busy: try again at the next
        // restart (or when the adapter turns on again).
      }
    }

    Future<void> stopScan() async {
      try {
        await FlutterBluePlus.stopScan();
      } catch (_) {}
    }

    Future<void> begin() async {
      if (!await _supported() || cancelled) return;
      results = FlutterBluePlus.onScanResults.listen((list) {
        for (final r in list) {
          final id = decodeBadgeId(r);
          if (id != null && !out.isClosed) {
            out.add(BadgeSighting(badgeId: id, rssi: r.rssi, at: r.timeStamp));
          }
        }
      });
      adapter = FlutterBluePlus.adapterState.listen((s) async {
        final nowOn = s == BluetoothAdapterState.on;
        if (nowOn == on) return;
        on = nowOn;
        if (on && !cancelled) {
          await startScan();
          restart?.cancel();
          restart = Timer.periodic(restartEvery, (_) async {
            await stopScan();
            await startScan();
          });
        } else {
          restart?.cancel();
        }
      });
    }

    out = StreamController<BadgeSighting>(
      onListen: () => unawaited(begin()),
      onCancel: () async {
        cancelled = true;
        restart?.cancel();
        await adapter?.cancel();
        await results?.cancel();
        if (on) await stopScan();
      },
    );
    return out.stream;
  }

  static Future<bool> _supported() async {
    try {
      if (!(Platform.isAndroid || Platform.isIOS)) return false;
      return await FlutterBluePlus.isSupported;
    } catch (_) {
      return false;
    }
  }
}
