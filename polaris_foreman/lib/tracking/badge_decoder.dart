import 'package:flutter_blue_plus/flutter_blue_plus.dart' show ScanResult;

/// THE badge decoder: the one place to change when the badge hardware is
/// chosen. It turns a BLE advertisement into the opaque badge id the office
/// registers (`presence.BleBadge`, the tracker's `badge_id`), or null when
/// the advertisement is not a badge.
///
/// Today it reads **iBeacon** frames and returns
/// `ib:<proximity uuid, lower case, with dashes>:<major>:<minor>`, for
/// example `ib:f7826da6-4fa2-4e98-8024-bc5b71e0893e:100:7` (major and minor
/// in decimal). The office must register badges with exactly that text.
///
/// Limits of iBeacon on phones:
/// * **iOS hides iBeacon frames from Bluetooth scans** (CoreBluetooth strips
///   Apple's manufacturer data): an iPhone sees no iBeacon badge this way.
///   Ranging needs CoreLocation and known UUIDs, which this app does not
///   do. Badges that advertise something else (Eddystone-UID, a service
///   data frame, a custom manufacturer id) work on both platforms: decode
///   them here.
/// * Android needs the fine location permission for beacons (the scan runs
///   with `androidUsesFineLocation`).
String? decodeBadgeId(ScanResult result) =>
    iBeaconBadgeId(result.advertisementData.manufacturerData);

/// Apple's company id, the key of iBeacon manufacturer data.
const appleCompanyId = 0x004C;

/// `ib:<uuid>:<major>:<minor>` from manufacturer data (flutter_blue_plus
/// keys it by company id and leaves the two id bytes out), or null.
///
/// iBeacon payload after the company id: `0x02 0x15`, 16 bytes of
/// proximity UUID, major (2 bytes, big-endian), minor (2 bytes,
/// big-endian), measured power (1 byte).
String? iBeaconBadgeId(Map<int, List<int>> manufacturerData) {
  final data = manufacturerData[appleCompanyId];
  if (data == null || data.length < 22) return null;
  if (data[0] != 0x02 || data[1] != 0x15) return null;
  String hex(int from, int to) => [
    for (var i = from; i < to; i++)
      (data[i] & 0xFF).toRadixString(16).padLeft(2, '0'),
  ].join();
  final uuid =
      '${hex(2, 6)}-${hex(6, 8)}-${hex(8, 10)}-${hex(10, 12)}-${hex(12, 18)}';
  final major = ((data[18] & 0xFF) << 8) | (data[19] & 0xFF);
  final minor = ((data[20] & 0xFF) << 8) | (data[21] & 0xFF);
  return 'ib:$uuid:$major:$minor';
}
