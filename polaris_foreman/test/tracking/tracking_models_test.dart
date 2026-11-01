import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/core/site_clock.dart';
import 'package:polaris_foreman/tracking/observations.dart';
import 'package:polaris_foreman/tracking/tracking_models.dart';
import 'package:yaml/yaml.dart';

import 'tracking_fakes.dart';

Map<String, dynamic> _yaml(String path) =>
    jsonDecode(jsonEncode(loadYaml(File(path).readAsStringSync())))
        as Map<String, dynamic>;

final _django = File('../docs/api/presence-django-openapi.yaml');
final _tracker = File('../docs/api/tracker-openapi.yaml');
final String? _skip = _django.existsSync() && _tracker.existsSync()
    ? null
    : 'presence contracts not found';

Map<String, dynamic> _schema(Map<String, dynamic> doc, String name) =>
    ((doc['components'] as Map)['schemas'] as Map)[name]
        as Map<String, dynamic>;

/// Required fields present, no field the schema does not list, and the
/// JSON types right (the subset these schemas use).
List<String> _check(Map<String, dynamic> value, Map<String, dynamic> schema) {
  final errors = <String>[];
  final props = (schema['properties'] as Map).cast<String, dynamic>();
  for (final r in schema['required'] as List) {
    if (!value.containsKey(r)) errors.add('missing $r');
  }
  for (final e in value.entries) {
    final p = props[e.key] as Map?;
    if (p == null) {
      errors.add('unexpected ${e.key}');
      continue;
    }
    final ok = switch (p['type']) {
      'string' => e.value is String,
      'integer' => e.value is int,
      'number' => e.value is num,
      _ => true,
    };
    if (!ok) errors.add('${e.key}: ${e.value} is not ${p['type']}');
    final c = p['const'];
    if (c != null && e.value != c) errors.add('${e.key} must be $c');
    final min = p['minimum'] as num?, max = p['maximum'] as num?;
    if (min != null && (e.value as num) < min) errors.add('${e.key} < $min');
    if (max != null && (e.value as num) > max) errors.add('${e.key} > $max');
  }
  return errors;
}

DateTime _at(int h, int m, [int s = 0]) => DateTime(2026, 10, 31, h, m, s);

void main() {
  group('contract', () {
    test('the token example parses and round-trips', () {
      final doc = _yaml(_django.path);
      final example =
          ((((((doc['paths'] as Map)['/tracking/token/'] as Map)['post']
                              as Map)['responses']
                          as Map)['200']
                      as Map)['content']
                  as Map)['application/json']
              as Map;
      final json = (example['example'] as Map).cast<String, dynamic>();
      final t = TrackingToken.fromJson(json);
      expect(t.trackerUrl, 'https://tracker.example.com');
      expect(t.sites, [3]);
      expect(t.gpsIntervalSeconds, 60);
      expect(t.hours.start, const TimeOfDay24(6, 0));
      expect(t.hours.end, const TimeOfDay24(19, 0));
      expect(t.expiresAt, DateTime.utc(2026, 10, 31, 19));
      final back = TrackingToken.fromJson(t.toJson());
      expect(back.toJson(), t.toJson());
      final schema = _schema(doc, 'TrackingToken');
      expect((t.toJson().keys.toSet()), {...schema['required'] as List});
    }, skip: _skip);

    test('GPS and BLE observations match the tracker schemas', () {
      final doc = _yaml(_tracker.path);
      final gps = gpsObservation(
        owner: '5',
        fix: fixAt(DateTime.utc(2026, 10, 31, 7, 12, 3), lat: 6.1320004),
      );
      final ble = bleObservation(
        owner: '5',
        siteId: 3,
        badgeId: 'ib:f7826da6-4fa2-4e98-8024-bc5b71e0893e:100:7',
        rssi: -71,
        at: DateTime.utc(2026, 10, 31, 7, 12, 5),
      );
      expect(_check(gps.json, _schema(doc, 'GpsObservation')), isEmpty);
      expect(_check(ble.json, _schema(doc, 'BleObservation')), isEmpty);
      expect(gps.json['at'], '2026-10-31T07:12:03Z');
      expect(gps.json['lat'], 6.132);
      expect(gps.json['client_uuid'], gps.clientUuid);
      expect(ble.json['site_id'], 3);
      expect(gps.clientUuid, isNot(ble.clientUuid));
    }, skip: _skip);

    test('the ingest result example parses', () {
      final doc = _yaml(_tracker.path);
      final example =
          ((((doc['components'] as Map)['responses'] as Map)['IngestResult']
                      as Map)['content']
                  as Map)['application/json']
              as Map;
      final r = IngestResult.fromJson(
        (example['example'] as Map).cast<String, dynamic>(),
      );
      expect(r.accepted, 2);
      expect(r.rejected.single.code, 'unknown_badge');
    }, skip: _skip);

    test('a consent request has the fields of the plan (§14)', () {
      const r = ConsentRequest(
        clientUuid: 'c-1',
        accepted: true,
        textVersion: consentTextVersion,
      );
      expect(r.toJson(), {
        'client_uuid': 'c-1',
        'accepted': true,
        'text_version': consentTextVersion,
      });
      expect(ConsentRequest.fromJson(r.toJson()).accepted, isTrue);
    });
  });

  group('tracking hours', () {
    final day = TrackingHours(
      TimeOfDay24.parse('06:00'),
      TimeOfDay24.parse('19:00'),
    );

    test('start is inside, end is outside', () {
      expect(day.contains(_at(5, 59, 59)), isFalse);
      expect(day.contains(_at(6, 0)), isTrue);
      expect(day.contains(_at(12, 30)), isTrue);
      expect(day.contains(_at(18, 59, 59)), isTrue);
      expect(day.contains(_at(19, 0)), isFalse);
      expect(day.contains(_at(23, 0)), isFalse);
    });

    test('a night window goes over midnight', () {
      final night = TrackingHours(
        TimeOfDay24.parse('22:00'),
        TimeOfDay24.parse('06:00'),
      );
      expect(night.contains(_at(21, 59)), isFalse);
      expect(night.contains(_at(22, 0)), isTrue);
      expect(night.contains(_at(2, 0)), isTrue);
      expect(night.contains(_at(6, 0)), isFalse);
    });

    test('an empty window never tracks', () {
      final none = TrackingHours(
        TimeOfDay24.parse('07:00'),
        TimeOfDay24.parse('07:00'),
      );
      expect(none.contains(_at(7, 0)), isFalse);
      expect(none.contains(_at(12, 0)), isFalse);
      expect(none.untilNextChange(_at(7, 0)), isNull);
    });

    test('time to the next boundary', () {
      expect(day.untilNextChange(_at(5, 0)), const Duration(hours: 1));
      expect(
        day.untilNextChange(_at(18, 30, 30)),
        const Duration(minutes: 29, seconds: 30),
      );
      // After the end: tomorrow's start.
      expect(day.untilNextChange(_at(20, 0)), const Duration(hours: 10));
      expect(day.untilNextChange(_at(6, 0)), const Duration(hours: 13));
    });

    test('bad times are refused', () {
      expect(() => TimeOfDay24.parse('25:00'), throwsFormatException);
      expect(() => TimeOfDay24.parse('7h'), throwsFormatException);
      expect(TimeOfDay24.parse('06:30:00'), const TimeOfDay24(6, 30));
    });

    test('the window is read on the site clock, not the phone clock', () {
      // 18:30 UTC is 19:30 in Lagos (UTC+1): outside there, inside in Lomé.
      final instant = DateTime.utc(2026, 10, 31, 18, 30);
      final lome = SiteClock(now: () => instant, timeZone: 'Africa/Lome');
      final lagos = SiteClock(now: () => instant, timeZone: 'Africa/Lagos');
      expect(day.contains(lome.local()), isTrue);
      expect(day.contains(lagos.local()), isFalse);
    });
  });

  group('token timing', () {
    test('refresh 30 minutes before expiry; half way for short tokens', () {
      final t = tokenFor(expiresAt: trackingNow.add(const Duration(hours: 12)));
      expect(
        t.refreshAt(trackingNow),
        trackingNow.add(const Duration(hours: 11, minutes: 30)),
      );
      final short = tokenFor(
        expiresAt: trackingNow.add(const Duration(minutes: 20)),
      );
      expect(
        short.refreshAt(trackingNow),
        trackingNow.add(const Duration(minutes: 10)),
      );
      expect(t.expiredAt(t.expiresAt), isTrue);
      expect(t.expiredAt(trackingNow), isFalse);
    });

    test('the GPS interval never goes below 10 s', () {
      expect(tokenFor(interval: 2).gpsInterval, const Duration(seconds: 10));
      expect(tokenFor(interval: 90).gpsInterval, const Duration(seconds: 90));
    });
  });

  test('consent context comes from consent_required details', () {
    final c = ConsentContext.fromDetails({
      'tracking_hours': {'start': '07:00', 'end': '18:00'},
      'gps_interval_seconds': 120,
      'retention_days': 30,
    });
    expect(c.hours?.start, const TimeOfDay24(7, 0));
    expect(c.gpsIntervalSeconds, 120);
    expect(c.retentionDays, 30);
    final empty = ConsentContext.fromDetails({
      'tracking_hours': {'start': 'soon'},
    });
    expect(empty.hours, isNull);
    expect(empty.retentionDays, isNull);
  });
}
