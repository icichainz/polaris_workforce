// Contract 1.2 (tag `checkin`): the models round-trip every example of
// docs/api/openapi.yaml, the rules give the examples' answers, and one suite
// runs on the mock repositories and on the HTTP repositories talking to the
// mock server (same results, same errors, answers valid against the schemas).
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/core/checkin_rules.dart';
import 'package:polaris_foreman/data/local/token_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/http/http_repositories.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_backend.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_repositories.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_seed.dart';
import 'package:polaris_foreman/data/repositories/repositories.dart';

import 'support/fakes.dart';
import 'support/mock_http_server.dart';
import 'support/openapi.dart';

const site = MockSeed.siteId;

String uuid(int n) =>
    '00000000-0000-4000-8000-${n.toRadixString(16).padLeft(12, '0')}';

void roundTrip<T>(
  Map<String, dynamic> json,
  T Function(Map<String, dynamic>) from,
  Map<String, dynamic> Function(T) to,
) => expect(to(from(json)), json);

class Fixture {
  Fixture(this.backend, this.repos, this.tokens, this.exchanges);
  final MockBackend backend;
  final Repositories repos;
  final MemoryTokenStore tokens;
  final List<Exchange>? exchanges;

  Future<void> signIn() async => tokens.write(
    (await repos.auth.signIn(
      const PinLoginRequest(
        phone: MockSeed.foremanPhone,
        pin: MockSeed.foremanPin,
        deviceId: 'suite-device',
      ),
    )).token,
  );
}

MockBackend _backend() => MockBackend(
  latency: Duration.zero,
  approvalDelay: null,
  now: () => fixedNow,
)..publicBaseUrl = mockBaseUrl;

final implementations = <String, Fixture Function()>{
  'mock': () {
    final b = _backend();
    final t = MemoryTokenStore();
    return Fixture(b, mockRepositories(b, t.read), t, null);
  },
  'http': () {
    final b = _backend();
    final t = MemoryTokenStore();
    final x = <Exchange>[];
    return Fixture(
      b,
      httpRepositories(
        baseUrl: mockBaseUrl,
        token: t.read,
        language: () => 'en',
        client: mockHttpClient(b, onExchange: x.add),
      ),
      t,
      x,
    );
  },
};

TypeMatcher<ApiException> apiError(int status, String code) =>
    isA<ApiException>()
        .having((e) => e.statusCode, 'status', status)
        .having((e) => e.code, 'code', code);

late File photoFile;

Future<Object?> outcome(Future<Object?> Function() call) async {
  try {
    final v = await call();
    return switch (v) {
      CheckIn() => {'ok': v.toJson(), 'replayed': v.replayed},
      FileDownload() => {
        'bytes': v.bytes?.length,
        'etag': v.etag,
        'notModified': v.notModified,
      },
      CheckInDevice() => {'ok': v.toJson()},
      CheckInSetup() => {'ok': v.toJson()},
      CheckInList() => {'ok': v.toJson()},
      RollCallSuggestions() => {'ok': v.toJson()},
      Crew() => {'ok': v.toJson()},
      AttendanceDay() => {'ok': v.toJson()},
      _ => {'ok': '$v'},
    };
  } on ApiException catch (e) {
    return {'status': e.statusCode, 'error': e.error.toJson()};
  }
}

CheckInCreateRequest req(
  int n, {
  String at = '2026-10-31T07:20:00Z',
  CheckInDirection direction = CheckInDirection.checkIn,
  String? badge,
  int? workerId,
  double? lat,
  double? lon,
  int? accuracy,
  int siteId = site,
}) => CheckInCreateRequest(
  clientUuid: uuid(n),
  siteId: siteId,
  direction: direction,
  at: at,
  badge: badge,
  workerId: workerId,
  latitude: lat,
  longitude: lon,
  accuracyM: accuracy,
);

/// The check-in story on any implementation; returns its trace.
Future<List<Object?>> story(Fixture f) async {
  final r = f.repos;
  final b = f.backend;
  final trace = <Object?>[];
  Future<Object?> t(Future<Object?> Function() call) async {
    final o = await outcome(call);
    trace.add(o);
    return o;
  }

  await t(() => r.checkin.device()); // 401: no token yet.
  await f.signIn();
  await t(() => r.checkin.device());
  await t(() => r.checkin.setup(site));
  await t(() => r.checkin.setup(99));
  final scan = req(
    1,
    at: '2026-10-31T07:35:10Z',
    badge: MockBackend.exampleBadge,
    lat: 6.132101,
    lon: 1.223012,
    accuracy: 12,
  );
  await t(() => r.checkin.checkIn(scan));
  await t(() => r.checkin.checkIn(scan)); // Replay.
  await t(
    () => r.checkin.checkIn(
      req(
        1,
        at: scan.at,
        badge: scan.badge,
        direction: CheckInDirection.checkOut,
      ),
    ),
  ); // Same uuid, other check-in: 409.
  await t(
    () => r.checkin.checkIn(
      req(
        2,
        at: '2026-10-31T07:03:15Z',
        badge: 'PW1:AAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBB',
        lat: 6.141002,
        lon: 1.230113,
        accuracy: 20,
      ),
    ),
  ); // Unknown badge, outside the geofence.
  await t(() => r.checkin.checkIn(req(3, badge: b.revokedBadgeOf(58))));
  await t(
    () => r.checkin.checkIn(req(4, at: '2026-10-31T07:05:02Z', workerId: 44)),
  );
  await t(
    () => r.checkin.checkIn(
      req(5, workerId: 44, direction: CheckInDirection.checkOut),
    ),
  );
  await t(() => r.checkin.checkIn(req(6, workerId: 120))); // Not on crew.
  await t(
    () => r.checkin.checkIn(
      req(
        7,
        at: '2026-10-31T07:37:00Z',
        badge: '  ${b.badgeOf(63).toLowerCase()} ',
      ),
    ),
  ); // Duplicate (and case/spaces do not matter).
  await t(() => r.checkin.checkIn(req(8, workerId: 9999)));
  await t(() => r.checkin.checkIn(req(9, workerId: 44, badge: b.badgeOf(44))));
  await t(() => r.checkin.checkIn(req(10)));
  await t(
    () => r.checkin.checkIn(req(11, workerId: 51, at: '2026-10-31T08:00:00Z')),
  );
  await t(() => r.checkin.checkIn(req(12, workerId: 51, lat: 6.13)));
  await t(() => r.checkin.checkIn(req(13, workerId: 51, siteId: 99)));
  final withPhoto = await t(
    () => r.checkin.checkIn(
      req(14, at: '2026-10-31T07:02:40Z', badge: b.badgeOf(51)),
      photoPath: photoFile.path,
    ),
  );
  final url =
      ((withPhoto as Map)['ok'] as Map<String, dynamic>)['photo_url'] as String;
  await t(() => r.files.download(url));
  final etag = (trace.last as Map)['etag'] as String;
  await t(() => r.files.download(url, etag: etag));
  await t(() => r.checkin.checkins(site, today));
  await t(() => r.checkin.suggestions(site, today));

  // Worker photos.
  b.setWorkerPhoto(63, List.generate(300, (i) => i % 256));
  final crew = await r.crew.crew(site, date: today);
  final photo = crew.workers.firstWhere((w) => w.id == 63).photoUrl!;
  await t(() => r.files.download(photo));
  await t(() => r.files.download(photo, etag: '"w63-1"'));
  b.setWorkerPhoto(63, List.generate(200, (i) => i % 256));
  await t(() => r.files.download(photo, etag: '"w63-1"'));
  await t(() => r.files.download('${mockBaseUrl}files/workers/9999/photo/'));

  // Self check-in: the scan writes the worker's draft.
  b.attendanceMode = AttendanceMode.selfCheckIn;
  await t(() => r.checkin.checkIn(req(15, badge: b.badgeOf(60))));
  await t(() => r.attendance.day(site, today));

  // Kiosk phone for site 3.
  b.setKiosk(site);
  await t(() => r.checkin.device());
  await t(() => r.checkin.checkIn(req(16, workerId: 52)));
  await t(() => r.checkin.checkIn(req(17, badge: b.badgeOf(120), siteId: 5)));
  await t(() => r.checkin.setup(5));
  await t(() => r.checkin.checkIn(req(18, badge: b.badgeOf(52))));
  return trace;
}

void main() {
  setUpAll(() {
    final dir = Directory.systemTemp.createTempSync('polaris_checkin');
    // JPEG magic bytes first: the server checks them.
    photoFile = File('${dir.path}/checkin.jpg')
      ..writeAsBytesSync([0xFF, 0xD8, 0xFF, ...List.generate(61, (i) => i)]);
  });

  group('examples round-trip', skip: specSkip, () {
    late OpenApi api;
    setUpAll(() => api = OpenApi.load());

    test('CheckInDevice: kiosk and foreman phone', () {
      final all = api.responses('/checkin/device/', 'get', '200');
      expect(all.keys, containsAll(['KioskPhone', 'ForemanPhone']));
      for (final json in all.values) {
        roundTrip(json, CheckInDevice.fromJson, (v) => v.toJson());
      }
      expect(CheckInDevice.fromJson(all['KioskPhone']!).kiosk!.siteId, 3);
      expect(CheckInDevice.fromJson(all['ForemanPhone']!).isKiosk, isFalse);
    });

    test('CheckInSetup', () {
      final json = api.response(
        '/sites/{site_id}/checkin-setup/',
        'get',
        '200',
        'Setup',
      );
      roundTrip(json, CheckInSetup.fromJson, (v) => v.toJson());
      final s = CheckInSetup.fromJson(json);
      expect((s.lateAfter, s.geofence!.radiusM), ('07:15', 150));
    });

    test('CheckIn (201, 200) and CheckInList', () {
      for (final status in ['201', '200']) {
        for (final json in api.responses('/checkins/', 'post', status).values) {
          roundTrip(json, CheckIn.fromJson, (v) => v.toJson());
        }
      }
      final list = api.response(
        '/sites/{site_id}/checkins/{date}/',
        'get',
        '200',
        'Morning',
      );
      roundTrip(list, CheckInList.fromJson, (v) => v.toJson());
      final unknown = CheckInList.fromJson(list).checkins[1];
      expect(unknown.flags, [
        CheckInFlag.unknownBadge,
        CheckInFlag.outsideGeofence,
      ]);
      expect((unknown.workerId, unknown.counted), (null, false));
    });

    test('RollCallSuggestions', () {
      final json = api.response(
        '/sites/{site_id}/days/{date}/suggestions/',
        'get',
        '200',
        'Suggestions',
      );
      roundTrip(json, RollCallSuggestions.fromJson, (v) => v.toJson());
    });

    test('CheckInCreateRequest examples, and what the models send', () {
      for (final name in ['BadgeScanWithGPS', 'ManualCheck-out']) {
        final json = api.request('/checkins/', 'post', name);
        roundTrip(json, CheckInCreateRequest.fromJson, (v) => v.toJson());
        expect(api.checkSchema(json, 'CheckInCreateRequest'), isEmpty);
      }
      // A local check-in becomes a request with badge or worker_id, never
      // both.
      final manual = req(1, workerId: 44).toJson();
      expect(manual.containsKey('badge'), isFalse);
      expect(api.checkSchema(manual, 'CheckInCreateRequest'), isEmpty);
    });

    test('the rules give the suggestions of the example check-ins', () {
      final checkins = CheckInList.fromJson(
        api.response(
          '/sites/{site_id}/checkins/{date}/',
          'get',
          '200',
          'Morning',
        ),
      ).checkins;
      final expected = api.response(
        '/sites/{site_id}/days/{date}/suggestions/',
        'get',
        '200',
        'Suggestions',
      );
      final events = [
        for (final c in checkins)
          if (c.counted)
            CheckInEvent(c.workerId!, c.direction, DateTime.parse(c.at)),
        // Worker 44 of the example (not in the list example).
        CheckInEvent(
          44,
          CheckInDirection.checkIn,
          DateTime.utc(2026, 10, 31, 7, 5, 2),
        ),
        CheckInEvent(
          44,
          CheckInDirection.checkOut,
          DateTime.utc(2026, 10, 31, 11, 30),
        ),
      ];
      final got = suggestFromEvents(
        events,
        lateAfter: expected['late_after'] as String,
        localOf: (t) => t.toUtc(),
      );
      expect(got.map((s) => s.toJson()).toList(), expected['suggestions']);
    });
  });

  group('rules', () {
    test('badge hash: sha256 of the text as read, lower-case hex', () {
      expect(
        badgeHash(MockBackend.exampleBadge),
        'fec1b7ccaaa29a119b19b2850565412f63b92fc748ef294c78726a3f2358a2aa',
      );
      final badges = [
        BadgeHash(
          workerId: 63,
          payloadSha256: badgeHash(MockBackend.exampleBadge).toUpperCase(),
          issuedAt: '2026-10-20T09:00:00Z',
        ),
      ];
      expect(recogniseBadge(MockBackend.exampleBadge, badges), 63);
      expect(recogniseBadge('${MockBackend.exampleBadge}\n', badges), 63);
      expect(
        recogniseBadge('PW1:K7Q2M4XR5TBN3WCA.D6HZP2LQ4V7NYE3T', badges),
        isNull,
      );
      expect(recogniseBadge('https://example.com', badges), isNull);
      expect(looksLikeBadge(MockBackend.exampleBadge), isTrue);
      expect(MockBackend.exampleBadge.length, 37);
    });

    test('status rules: half day, late after late_after (not at it)', () {
      AttendanceStatus? s(String? i, String? o) =>
          suggestedStatus(firstIn: i, lastOut: o, lateAfter: '07:15');
      expect(s('07:15', null), AttendanceStatus.present);
      expect(s('07:16', null), AttendanceStatus.late);
      expect(s('07:05', '11:30'), AttendanceStatus.halfDay);
      expect(s('12:00', null), AttendanceStatus.halfDay);
      expect(s('07:40', '17:00'), AttendanceStatus.late);
      expect(s(null, '17:00'), isNull);
    });

    test('geofence: accuracy up to 50 m is given the benefit of the doubt', () {
      const fence = Geofence(
        latitude: 6.131944,
        longitude: 1.222778,
        radiusM: 150,
      );
      // ~190 m north of the centre.
      const lat = 6.133653;
      expect(insideGeofence(fence, lat, 1.222778, 0), isFalse);
      expect(insideGeofence(fence, lat, 1.222778, 50), isTrue);
      expect(insideGeofence(fence, lat, 1.222778, 500), isTrue);
      expect(insideGeofence(fence, 6.141002, 1.230113, 20), isFalse);
      expect(insideGeofence(fence, 6.132101, 1.223012, 12), isTrue);
    });

    test('default direction: in before midday, out from 12:00', () {
      expect(
        defaultDirection(DateTime(2026, 10, 31, 11, 59)),
        CheckInDirection.checkIn,
      );
      expect(
        defaultDirection(DateTime(2026, 10, 31, 12)),
        CheckInDirection.checkOut,
      );
    });
  });

  group('the mock server follows the contract rules', () {
    test(
      'setup lists the active badges of the crew (not the revoked one)',
      () async {
        final f = implementations['mock']!();
        await f.signIn();
        final setup = await f.repos.checkin.setup(site);
        final b = f.backend;
        expect(setup.badges.length, MockSeed.workers.length);
        expect(recogniseBadge(b.badgeOf(58), setup.badges), 58);
        expect(recogniseBadge(b.revokedBadgeOf(58)!, setup.badges), isNull);
        expect(recogniseBadge(MockBackend.exampleBadge, setup.badges), 63);
      },
    );
  });

  for (final impl in implementations.entries) {
    group(impl.key, () {
      late Fixture f;
      setUp(() => f = impl.value());

      test('badge scan with GPS, replay, 409 for another check-in', () async {
        await f.signIn();
        final scan = req(
          1,
          at: '2026-10-31T07:35:10Z',
          badge: MockBackend.exampleBadge,
          lat: 6.132101,
          lon: 1.223012,
          accuracy: 12,
        );
        final c1 = await f.repos.checkin.checkIn(scan);
        final c2 = await f.repos.checkin.checkIn(scan);
        expect((c1.replayed, c2.replayed), (false, true));
        expect(c2.toJson(), c1.toJson());
        expect(
          (
            c1.workerId,
            c1.source,
            c1.counted,
            c1.flags.isEmpty,
            c1.insideGeofence,
          ),
          (63, CheckInSource.foremanScan, true, true, true),
        );
        expect(c1.distanceM, closeTo(32, 3));
        await expectLater(
          f.repos.checkin.checkIn(req(1, at: scan.at, workerId: 63)),
          throwsA(apiError(409, 'conflict')),
        );
      });

      test(
        'flags: unknown, revoked, not on crew, outside, duplicate',
        () async {
          await f.signIn();
          final c = f.repos.checkin;
          final unknown = await c.checkIn(
            req(
              2,
              badge: 'NOT-A-BADGE',
              lat: 6.141002,
              lon: 1.230113,
              accuracy: 20,
            ),
          );
          expect(unknown.flags, [
            CheckInFlag.unknownBadge,
            CheckInFlag.outsideGeofence,
          ]);
          expect((unknown.workerId, unknown.counted), (null, false));
          final revoked = await c.checkIn(
            req(3, badge: f.backend.revokedBadgeOf(58)),
          );
          expect(
            (revoked.workerId, revoked.counted, revoked.flags.single),
            (58, false, CheckInFlag.revokedBadge),
          );
          final other = await c.checkIn(req(4, workerId: 120));
          expect(
            (other.counted, other.flags.single, other.source),
            (true, CheckInFlag.notOnCrew, CheckInSource.manual),
          );
          await c.checkIn(req(5, badge: f.backend.badgeOf(44)));
          final dup = await c.checkIn(
            req(6, badge: f.backend.badgeOf(44), at: '2026-10-31T07:24:00Z'),
          );
          expect(dup.flags, [CheckInFlag.duplicate]);
          expect(dup.counted, isTrue);
        },
      );

      test(
        '400s: badge and worker_id, future, old, coordinates, worker',
        () async {
          await f.signIn();
          final c = f.repos.checkin;
          Future<void> bad(CheckInCreateRequest r, String field) => expectLater(
            c.checkIn(r),
            throwsA(
              apiError(
                400,
                'validation_error',
              ).having((e) => e.details.keys, 'f', [field]),
            ),
          );
          await bad(req(1, workerId: 44, badge: 'X'), 'non_field_errors');
          await bad(req(2), 'non_field_errors');
          await bad(req(3, workerId: 44, at: '2026-10-31T07:41:00Z'), 'at');
          await bad(req(4, workerId: 44, at: '2026-09-29T07:00:00Z'), 'at');
          await bad(req(5, workerId: 44, lat: 6.1), 'longitude');
          await bad(req(6, workerId: 44, lat: 91, lon: 1), 'latitude');
          await bad(req(7, workerId: 9999), 'worker_id');
          await expectLater(
            c.checkIn(req(8, workerId: 44, siteId: 99)),
            throwsA(apiError(403, 'permission_denied')),
          );
        },
      );

      test('suggestions from counted check-ins of crew workers', () async {
        await f.signIn();
        final c = f.repos.checkin;
        await c.checkIn(req(1, at: '2026-10-31T07:05:02Z', workerId: 44));
        await c.checkIn(
          req(
            2,
            at: '2026-10-31T07:25:00Z',
            workerId: 44,
            direction: CheckInDirection.checkOut,
          ),
        );
        await c.checkIn(
          req(3, at: '2026-10-31T07:02:40Z', badge: f.backend.badgeOf(51)),
        );
        await c.checkIn(
          req(4, at: '2026-10-31T07:16:00Z', badge: f.backend.badgeOf(63)),
        );
        await c.checkIn(
          req(5, workerId: 52, direction: CheckInDirection.checkOut),
        );
        await c.checkIn(req(6, badge: f.backend.revokedBadgeOf(58)));
        await c.checkIn(req(7, workerId: 120));
        final s = await c.suggestions(site, today);
        expect(
          (s.attendanceMode, s.lateAfter, s.dayStatus),
          (AttendanceMode.both, '07:15', DayStatus.notStarted),
        );
        expect(
          {
            for (final x in s.suggestions)
              x.workerId: (x.status, x.arrival, x.departure),
          },
          {
            44: (AttendanceStatus.halfDay, '07:05', '07:25'),
            51: (AttendanceStatus.present, '07:02', null),
            52: (null, null, '07:20'),
            63: (AttendanceStatus.late, '07:16', null),
          },
        );
        final list = await c.checkins(site, today);
        expect(list.checkins.length, 7);
        expect(list.checkins.first.workerId, 51); // Oldest first.
      });

      test(
        'self_check_in: a counted scan writes the draft, not a hand mark',
        () async {
          await f.signIn();
          f.backend.attendanceMode = AttendanceMode.selfCheckIn;
          final c1 = await f.repos.checkin.checkIn(
            req(1, badge: f.backend.badgeOf(63)),
          );
          expect(c1.draftUpdated, isTrue);
          var day = await f.repos.attendance.day(site, today);
          var r = day.records.single;
          expect(
            (r.workerId, r.status, r.arrival),
            (63, AttendanceStatus.late, '07:20'),
          );
          // The foreman changes it by hand: later scans leave it.
          await f.repos.attendance.putRecords(site, today, [
            RecordUpsert(
              clientUuid: r.clientUuid,
              workerId: 63,
              status: AttendanceStatus.present,
              arrival: '07:00',
            ),
          ]);
          await f.repos.checkin.checkIn(
            req(2, badge: f.backend.badgeOf(63), at: '2026-10-31T07:10:00Z'),
          );
          day = await f.repos.attendance.day(site, today);
          r = day.records.single;
          expect((r.status, r.arrival), (AttendanceStatus.present, '07:00'));
          // An unknown badge never writes anything.
          final u = await f.repos.checkin.checkIn(req(3, badge: 'NOPE'));
          expect(u.draftUpdated, isFalse);
        },
      );

      test('kiosk: badge only, own site only, source kiosk', () async {
        await f.signIn();
        f.backend.setKiosk(site);
        final d = await f.repos.checkin.device();
        expect(
          (d.kiosk!.siteId, d.kiosk!.siteName),
          (site, 'Riverside Apartments'),
        );
        final setup = await f.repos.checkin.setup(site);
        expect(setup.kiosk, isTrue);
        await expectLater(
          f.repos.checkin.checkIn(req(1, workerId: 44)),
          throwsA(
            apiError(
              400,
              'validation_error',
            ).having((e) => e.details.keys, 'f', ['badge']),
          ),
        );
        await expectLater(
          f.repos.checkin.checkIn(
            req(2, badge: f.backend.badgeOf(120), siteId: 5),
          ),
          throwsA(
            apiError(403, 'permission_denied').having(
              (e) => e.message,
              'msg',
              'This phone is a kiosk for another site.',
            ),
          ),
        );
        await expectLater(
          f.repos.checkin.suggestions(5, today),
          throwsA(apiError(403, 'permission_denied')),
        );
        final k = await f.repos.checkin.checkIn(
          req(3, badge: f.backend.badgeOf(44)),
        );
        expect(k.source, CheckInSource.kiosk);
        f.backend.setKiosk(null);
        expect((await f.repos.checkin.device()).kiosk, isNull);
      });

      test('photos: multipart upload, ETag and 304, 404 elsewhere', () async {
        await f.signIn();
        final c = await f.repos.checkin.checkIn(
          req(1, badge: f.backend.badgeOf(51)),
          photoPath: photoFile.path,
        );
        expect(c.photoUrl, '${mockBaseUrl}files/checkins/${c.id}/photo/');
        final first = await f.repos.files.download(c.photoUrl!);
        expect(first.bytes, photoFile.readAsBytesSync());
        final again = await f.repos.files.download(
          c.photoUrl!,
          etag: first.etag,
        );
        expect(again.notModified, isTrue);
        // A photo that is gone from the phone: the check-in still goes.
        final noPhoto = await f.repos.checkin.checkIn(
          req(2, badge: f.backend.badgeOf(52)),
          photoPath: '/nowhere/gone.jpg',
        );
        expect(noPhoto.photoUrl, isNull);
        await expectLater(
          f.repos.files.download('${mockBaseUrl}files/workers/63/photo/'),
          throwsA(apiError(404, 'not_found')),
        );
      });
    });
  }

  test('both implementations tell the same check-in story', () async {
    final mock = await story(implementations['mock']!());
    final http = implementations['http']!();
    final viaHttp = await story(http);
    expect(viaHttp.length, mock.length);
    for (var i = 0; i < mock.length; i++) {
      expect(viaHttp[i], mock[i], reason: 'step $i');
    }
    final api = OpenApi.load();
    for (final x in http.exchanges!) {
      expect(x.template, isNotNull, reason: '${x.method} ${x.request.url}');
      final op = '${x.method} ${x.template} → ${x.status}';
      expect(
        api.statuses(x.template!, x.method.toLowerCase()),
        contains('${x.status}'),
        reason: '$op is not in the contract',
      );
      final schema = api.responseSchema(
        x.template!,
        x.method.toLowerCase(),
        '${x.status}',
      );
      if (schema == null) {
        expect(x.body, isNull, reason: op);
        continue;
      }
      expect(api.check(x.body, schema), isEmpty, reason: op);
    }
    expect(
      http.exchanges!.map((x) => '${x.method} ${x.template}').toSet(),
      containsAll([
        'GET /checkin/device/',
        'GET /sites/{site_id}/checkin-setup/',
        'POST /checkins/',
        'GET /sites/{site_id}/checkins/{date}/',
        'GET /sites/{site_id}/days/{date}/suggestions/',
        'GET /files/checkins/{checkin_id}/photo/',
        'GET /files/workers/{worker_id}/photo/',
      ]),
    );
  }, skip: specSkip);
}
