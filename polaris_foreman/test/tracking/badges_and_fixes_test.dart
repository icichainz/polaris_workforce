import 'package:flutter_blue_plus/flutter_blue_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/tracking/badge_decoder.dart';
import 'package:polaris_foreman/tracking/observations.dart';

import 'tracking_fakes.dart';

/// An iBeacon advertisement as flutter_blue_plus reports it: manufacturer
/// data keyed by Apple's company id, without the two id bytes.
List<int> _iBeacon({
  String uuid = 'f7826da6-4fa2-4e98-8024-bc5b71e0893e',
  int major = 100,
  int minor = 7,
  int power = 0xC5,
}) {
  final hex = uuid.replaceAll('-', '');
  return [
    0x02,
    0x15,
    for (var i = 0; i < 32; i += 2)
      int.parse(hex.substring(i, i + 2), radix: 16),
    major >> 8,
    major & 0xFF,
    minor >> 8,
    minor & 0xFF,
    power,
  ];
}

ScanResult _scan(Map<int, List<int>> manufacturerData, {int rssi = -70}) =>
    ScanResult(
      device: BluetoothDevice.fromId('AA:BB:CC:DD:EE:FF'),
      advertisementData: AdvertisementData(
        advName: '',
        txPowerLevel: null,
        appearance: null,
        connectable: false,
        manufacturerData: manufacturerData,
        serviceData: const {},
        serviceUuids: const [],
      ),
      rssi: rssi,
      timeStamp: trackingNow,
    );

void main() {
  group('badge decoder', () {
    test('iBeacon → ib:<uuid lower case>:<major>:<minor>', () {
      expect(
        decodeBadgeId(_scan({appleCompanyId: _iBeacon()})),
        'ib:f7826da6-4fa2-4e98-8024-bc5b71e0893e:100:7',
      );
      expect(
        decodeBadgeId(
          _scan({
            appleCompanyId: _iBeacon(
              uuid: 'E2C56DB5-DFFB-48D2-B060-D0F5A71096E0',
              major: 65535,
              minor: 0,
            ),
          }),
        ),
        'ib:e2c56db5-dffb-48d2-b060-d0f5a71096e0:65535:0',
      );
    });

    test('anything else is not a badge', () {
      // No manufacturer data (a phone, headphones…).
      expect(decodeBadgeId(_scan(const {})), isNull);
      // Another company's data with the same bytes.
      expect(decodeBadgeId(_scan({0x0059: _iBeacon()})), isNull);
      // Apple data that is not an iBeacon frame (AirPods, Handoff…).
      expect(
        decodeBadgeId(
          _scan({
            appleCompanyId: [0x10, 0x05, 1, 2, 3, 4, 5],
          }),
        ),
        isNull,
      );
      // Cut short.
      expect(
        decodeBadgeId(_scan({appleCompanyId: _iBeacon().sublist(0, 20)})),
        isNull,
      );
    });
  });

  group('BLE de-duplication', () {
    test('each badge at most once per 30 s per site', () {
      final d = BleDeduper();
      final t0 = trackingNow;
      expect(d.accept(1, 'b1', t0), isTrue);
      expect(d.accept(1, 'b1', t0.add(const Duration(seconds: 1))), isFalse);
      expect(d.accept(1, 'b1', t0.add(const Duration(seconds: 29))), isFalse);
      // Another badge, or the same badge on another site, is its own.
      expect(d.accept(1, 'b2', t0.add(const Duration(seconds: 2))), isTrue);
      expect(d.accept(2, 'b1', t0.add(const Duration(seconds: 3))), isTrue);
      expect(d.accept(1, 'b1', t0.add(const Duration(seconds: 30))), isTrue);
      expect(d.accept(1, 'b1', t0.add(const Duration(seconds: 45))), isFalse);
      expect(d.accept(1, 'b1', t0.add(const Duration(seconds: 61))), isTrue);
    });

    test('a clock that jumps back does not silence a badge', () {
      final d = BleDeduper();
      expect(d.accept(1, 'b1', trackingNow), isTrue);
      expect(
        d.accept(1, 'b1', trackingNow.subtract(const Duration(minutes: 5))),
        isTrue,
      );
    });
  });

  group('GPS filter', () {
    test('mock locations are never recorded', () {
      final f = GpsFilter(const Duration(seconds: 60));
      expect(f.check(fixAt(trackingNow, mocked: true)), FixRejection.mocked);
      expect(f.mockedCount, 1);
      // The next real fix is not "too soon" after a mocked one.
      expect(f.check(fixAt(trackingNow)), isNull);
    });

    test('one fix per interval, with a little slack', () {
      final f = GpsFilter(const Duration(seconds: 60));
      final t0 = trackingNow;
      expect(f.check(fixAt(t0)), isNull);
      expect(
        f.check(fixAt(t0.add(const Duration(seconds: 20)))),
        FixRejection.tooSoon,
      );
      expect(f.check(fixAt(t0.add(const Duration(seconds: 58)))), isNull);
      expect(
        f.check(fixAt(t0.add(const Duration(seconds: 100)))),
        FixRejection.tooSoon,
      );
      expect(f.check(fixAt(t0.add(const Duration(seconds: 120)))), isNull);
    });

    test('broken fixes are dropped', () {
      final f = GpsFilter(const Duration(seconds: 60));
      expect(
        f.check(fixAt(trackingNow, lat: double.nan)),
        FixRejection.invalid,
      );
      expect(f.check(fixAt(trackingNow, lat: 91)), FixRejection.invalid);
      expect(f.check(fixAt(trackingNow, accuracy: -1)), FixRejection.invalid);
      expect(f.check(fixAt(trackingNow)), isNull);
    });
  });
}
