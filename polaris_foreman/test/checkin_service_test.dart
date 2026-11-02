// The phone side of check-in: scans through the outbox (check-ins go
// first), offline badge recognition, suggestions that never overwrite a hand
// mark, self_check_in drafts, GPS permissions, the photo cache and its
// headers, the device name, kiosk routing.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:geolocator/geolocator.dart' show LocationPermission;
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:polaris_foreman/app.dart';
import 'package:polaris_foreman/data/local/local_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/http/http_repositories.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_backend.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_seed.dart';
import 'package:polaris_foreman/services/checkin_service.dart';
import 'package:polaris_foreman/services/location_service.dart';
import 'package:polaris_foreman/services/photo_cache.dart';
import 'package:polaris_foreman/services/roll_call_service.dart';
import 'package:polaris_foreman/services/sync_service.dart';
import 'package:polaris_foreman/state/session.dart';

import 'support/fakes.dart';

const site = MockSeed.siteId;

class Phone {
  Phone(this.h)
    : rollCall = RollCallService(h.store, clock: h.clock),
      sync = SyncService(store: h.store, repos: h.repos, clock: h.clock);

  final Harness h;
  final RollCallService rollCall;
  final SyncService sync;
  late final CheckInService checkins = CheckInService(
    h.store,
    clock: h.clock,
    rollCall: rollCall,
  );

  /// Crew and check-in setup on the phone, as a sync would leave them.
  Future<void> prime({AttendanceMode? mode}) async {
    if (mode != null) h.backend.attendanceMode = mode;
    final t = await h.token();
    await h.store.saveCrew(await h.backend.crew(t, site, today));
    await checkins.saveSetup(await h.backend.checkInSetup(t, site), today);
  }

  Future<ScanResult> scan(
    int workerId, {
    CheckInDirection direction = CheckInDirection.checkIn,
  }) => checkins.scan(
    siteId: site,
    text: h.backend.badgeOf(workerId),
    direction: direction,
  );

  Future<LocalRecord?> record(int workerId) =>
      h.store.loadRecord(site, today, workerId);
}

void main() {
  late Harness h;
  late Phone phone;
  setUp(() async {
    h = await Harness.create();
    phone = Phone(h);
    addTearDown(phone.sync.dispose);
  });

  group('outbox', () {
    test('order: check-ins → records → submit → corrections → payments', () {
      OutboxEntry e(int id, OutboxKind kind) => OutboxEntry(
        id: id,
        kind: kind,
        siteId: 1,
        date: today,
        clientUuid: '$id',
        payload: const {},
        createdAt: '',
      );
      final sorted = sendingOrder([
        e(1, OutboxKind.payment),
        e(2, OutboxKind.records),
        e(3, OutboxKind.checkin),
        e(4, OutboxKind.submit),
        e(5, OutboxKind.checkin),
        e(6, OutboxKind.correction),
      ]);
      expect(sorted.map((x) => x.id), [3, 5, 2, 4, 6, 1]);
    });

    test(
      'a scan queued after a mark is still sent before the records',
      () async {
        await phone.prime();
        await phone.rollCall.mark(
          siteId: site,
          date: today,
          workerId: 44,
          status: AttendanceStatus.present,
          workStart: '07:00',
        );
        final r = await phone.scan(63);
        expect((r.workerId, r.flags.isEmpty), (63, true));
        final queued = await h.store.loadOutbox();
        expect(queued.map((e) => e.kind), [
          OutboxKind.records,
          OutboxKind.checkin,
        ]);
        final body = queued.last.payload;
        expect(
          (body['client_uuid'], body['at'], body['badge']),
          (r.checkIn.clientUuid, '2026-10-31T07:30:00Z', h.backend.badgeOf(63)),
        );
        h.recording.log.clear();
        expect(await phone.sync.drain(), DrainResult.done);
        expect(h.recording.log, ['checkIn', 'putRecords']);
        final stored = (await h.store.loadCheckIns(site, today)).single;
        expect(
          (stored.sent, stored.server!.workerId, stored.server!.counted),
          (true, 63, true),
        );
      },
    );

    test('a 400 is not retried; a network error keeps the scan', () async {
      await phone.prime();
      await phone.scan(63);
      h.recording.failures['checkIn'] = [const NetworkException()];
      expect(await phone.sync.drain(), DrainResult.waiting);
      expect(
        (await h.store.loadOutbox())
            .firstWhere((e) => e.kind == OutboxKind.checkin)
            .state,
        OutboxState.pending,
      );
      final e = (await h.store.loadOutbox()).firstWhere(
        (e) => e.kind == OutboxKind.checkin,
      );
      await h.store.updateOutbox(e.copyWith(nextAttemptAt: () => null));
      h.recording.failures['checkIn'] = [
        const ApiException(
          400,
          ApiError(code: 'validation_error', message: 'Invalid input.'),
        ),
      ];
      expect(await phone.sync.drain(), DrainResult.done);
      expect(
        (await h.store.loadOutbox())
            .firstWhere((e) => e.kind == OutboxKind.checkin)
            .state,
        OutboxState.failed,
      );
    });
  });

  group('scans offline', () {
    test('recognised by badge hash, unknown still queued, duplicate', () async {
      await phone.prime();
      h.backend.offline = true;
      final a = await phone.scan(51);
      expect(a.workerId, 51);
      final unknown = await phone.checkins.scan(
        siteId: site,
        text: 'PW1:AAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBB',
        direction: CheckInDirection.checkIn,
      );
      expect(
        (unknown.workerId, unknown.flags.single),
        (null, CheckInFlag.unknownBadge),
      );
      final again = await phone.scan(51);
      expect(again.flags, [CheckInFlag.duplicate]);
      final revoked = await phone.checkins.scan(
        siteId: site,
        text: h.backend.revokedBadgeOf(58)!,
        direction: CheckInDirection.checkIn,
      );
      expect(revoked.workerId, isNull, reason: 'not in the active list');
      expect(
        (await h.store.loadOutbox()).where((e) => e.kind == OutboxKind.checkin),
        hasLength(4),
      );
      expect(await phone.sync.drain(), DrainResult.waiting);
    });

    test('outside the geofence is flagged on the phone already', () async {
      await phone.prime();
      final r = await phone.checkins.scan(
        siteId: site,
        text: h.backend.badgeOf(63),
        direction: CheckInDirection.checkIn,
        fix: GpsFix(
          latitude: 6.141002,
          longitude: 1.230113,
          accuracyM: 20,
          at: fixedNow,
        ),
      );
      expect(r.flags, [CheckInFlag.outsideGeofence]);
      final body = (await h.store.loadOutbox())
          .firstWhere((e) => e.kind == OutboxKind.checkin)
          .payload;
      expect(
        (body['latitude'], body['longitude'], body['accuracy_m']),
        (6.141002, 1.230113, 20),
      );
    });
  });

  group('suggestions pre-fill the roll call', () {
    test('scans mark workers; hand marks are never overwritten', () async {
      await phone.prime();
      // The foreman marks 51 absent by hand first.
      await phone.rollCall.mark(
        siteId: site,
        date: today,
        workerId: 51,
        status: AttendanceStatus.absent,
      );
      await phone.scan(51);
      await phone.scan(63); // 07:30 > late_after 07:15.
      expect((await phone.record(51))!.status, AttendanceStatus.absent);
      var r63 = (await phone.record(63))!;
      expect((r63.status, r63.arrival), (AttendanceStatus.late, '07:30'));
      expect(await phone.record(44), isNull, reason: 'no scan: unmarked');
      // The foreman changes 63 by hand: later check-ins leave it alone.
      await phone.rollCall.mark(
        siteId: site,
        date: today,
        workerId: 63,
        status: AttendanceStatus.present,
        workStart: '07:00',
      );
      await phone.scan(63, direction: CheckInDirection.checkOut);
      r63 = (await phone.record(63))!;
      expect(
        (r63.status, r63.arrival, r63.departure),
        (AttendanceStatus.present, '07:00', null),
      );
    });

    test(
      'a pre-filled mark follows newer check-ins (server and phone)',
      () async {
        await phone.prime();
        await phone.scan(44);
        expect((await phone.record(44))!.status, AttendanceStatus.late);
        // Another phone (the kiosk) recorded an earlier in for 44, and an out.
        final t = await h.token();
        await h.backend.checkIn(
          t,
          const CheckInCreateRequest(
            clientUuid: '11111111-1111-4111-8111-111111111111',
            siteId: site,
            direction: CheckInDirection.checkIn,
            at: '2026-10-31T07:01:00Z',
            workerId: 44,
          ),
        );
        await phone.sync.drain();
        await h.store.setValue(
          StoreKeys.checkinDevice,
          jsonEncode(
            const CheckInDevice(
              attendanceMode: AttendanceMode.both,
              kiosk: null,
            ).toJson(),
          ),
        );
        await phone.sync.refreshCheckIns(
          site,
          const CheckInDevice(attendanceMode: AttendanceMode.both, kiosk: null),
        );
        expect(await phone.checkins.applySuggestions(site, today), 1);
        final r = (await phone.record(44))!;
        expect((r.status, r.arrival), (AttendanceStatus.present, '07:01'));
      },
    );

    test('only a check-out: the foreman decides (no mark)', () async {
      await phone.prime();
      await phone.scan(52, direction: CheckInDirection.checkOut);
      expect(await phone.record(52), isNull);
    });

    test('a submitted day is not pre-filled', () async {
      await phone.prime();
      await h.store.upsertDays([
        (await h.backend.day(
          await h.token(),
          site,
          today,
        )).summary.copyWith(status: DayStatus.submitted),
      ]);
      await phone.scan(63);
      expect(await phone.record(63), isNull);
    });

    test('self_check_in: the phone waits for the server draft', () async {
      await phone.prime(mode: AttendanceMode.selfCheckIn);
      await phone.scan(63);
      expect(await phone.record(63), isNull, reason: 'the server writes it');
      await phone.sync.runOnce();
      final r = (await phone.record(63))!;
      expect(
        (r.status, r.arrival, r.serverId != null),
        (AttendanceStatus.late, '07:30', true),
      );
    });

    test(
      'foreman_roll_call: suggestions are not fetched without scans',
      () async {
        await phone.prime(mode: AttendanceMode.foremanRollCall);
        await h.store.setValue(
          StoreKeys.me,
          jsonEncode(MockSeed.me().toJson()),
        );
        await phone.sync.pull();
        expect(h.recording.log, contains('device'));
        expect(h.recording.log, contains('setup'));
        expect(h.recording.log, isNot(contains('suggestions')));
      },
    );
  });

  group('presence suggestions (1.5)', () {
    const seen = SuggestionPresence(
      firstSeenAt: '2026-10-31T07:31:10Z',
      lastSeenAt: null,
      open: true,
      seenFrom: '07:31',
      seenUntil: null,
      zones: ['A'],
      visits: 1,
      status: AttendanceStatus.late,
    );
    const both = CheckInDevice(
      attendanceMode: AttendanceMode.both,
      kiosk: null,
    );

    Future<Map<String, dynamic>?> queued(int workerId) async {
      final e = (await h.store.loadOutbox())
          .where((e) => e.kind == OutboxKind.records)
          .firstOrNull;
      return (e?.payload['records'] as Map?)?['$workerId']
          as Map<String, dynamic>?;
    }

    Future<void> fetch() async {
      await h.store.setValue(
        StoreKeys.checkinDevice,
        jsonEncode(both.toJson()),
      );
      await phone.sync.refreshCheckIns(site, both);
    }

    test('shown, never pre-filled; accepting writes the mark', () async {
      await phone.prime();
      h.backend.seePresence(site, today, 58, seen);
      await fetch();
      expect(await phone.checkins.applySuggestions(site, today), 0);
      expect(await phone.record(58), isNull, reason: 'presence never marks');

      final s = (await phone.checkins.suggestions(
        site,
        today,
      )).singleWhere((s) => s.workerId == 58);
      expect((s.presenceOnly, s.status), (true, AttendanceStatus.late));

      final r = (await phone.rollCall.acceptSuggestion(
        siteId: site,
        date: today,
        suggestion: s,
      ))!;
      expect(
        (r.status, r.arrival, r.departure),
        (AttendanceStatus.late, '07:31', null),
      );
      expect((await queued(58))!['from_suggestion'], ['presence']);
      // The normal upsert: the record's own client_uuid, audited on the
      // server with its source.
      expect((await queued(58))!['client_uuid'], r.clientUuid);
      await phone.sync.drain();
      expect(h.backend.suggestionAudit['$site|$today'], {
        58: [SuggestionSource.presence],
      });
      // Accepted marks are not pre-fills: newer check-ins leave them alone.
      expect(await phone.checkins.applySuggestions(site, today), 0);
    });

    test('a hand change after accepting is a hand mark again', () async {
      await phone.prime();
      h.backend.seePresence(site, today, 58, seen);
      await fetch();
      final s = (await phone.checkins.suggestions(
        site,
        today,
      )).singleWhere((s) => s.workerId == 58);
      await phone.rollCall.acceptSuggestion(
        siteId: site,
        date: today,
        suggestion: s,
      );
      await phone.rollCall.mark(
        siteId: site,
        date: today,
        workerId: 58,
        status: AttendanceStatus.present,
      );
      expect((await queued(58))!.containsKey('from_suggestion'), isFalse);
    });

    test('check-in pre-fills are sent as check-in suggestions', () async {
      await phone.prime();
      await phone.scan(63);
      expect((await queued(63))!['from_suggestion'], ['checkin']);
    });

    test('check-ins win; the server presence stays next to them', () async {
      await phone.prime();
      h.backend.seePresence(site, today, 63, seen);
      await fetch();
      await phone.scan(63); // 07:30 on the phone: late, from the scan.
      final s = (await phone.checkins.suggestions(
        site,
        today,
      )).singleWhere((s) => s.workerId == 63);
      expect(s.sources, [SuggestionSource.checkin, SuggestionSource.presence]);
      expect(s.presence!.seenFrom, '07:31');
      expect((s.status, s.arrival), (AttendanceStatus.late, '07:30'));
      final r = (await phone.record(63))!;
      expect((r.status, r.arrival), (AttendanceStatus.late, '07:30'));
    });

    test(
      'foreman_roll_call on a tracked site: fetched without scans',
      () async {
        await phone.prime(mode: AttendanceMode.foremanRollCall);
        await h.store.setValue(
          StoreKeys.me,
          jsonEncode(MockSeed.me().toJson()),
        );
        phone.sync.isTracked = (id) => id == site;
        await phone.sync.pull();
        expect(h.recording.log, contains('suggestions'));
      },
    );
  });

  group('GPS', () {
    test('permission denied: no position, scans still go', () async {
      final platform = FakeLocationPlatform(
        permission: LocationPermission.denied,
        onRequest: LocationPermission.denied,
        fix: GpsFix(
          latitude: 6.13,
          longitude: 1.22,
          accuracyM: 8,
          at: fixedNow,
        ),
      );
      final gps = GpsTracker(platform, now: () => fixedNow);
      expect(await gps.start(), GpsState.denied);
      expect(platform.requests, 1);
      expect(await gps.refresh(), isNull);
      expect(gps.fresh(), isNull);
      await phone.prime();
      final r = await phone.checkins.scan(
        siteId: site,
        text: h.backend.badgeOf(63),
        direction: CheckInDirection.checkIn,
        fix: gps.fresh(),
      );
      expect(r.workerId, 63);
      final body = (await h.store.loadOutbox())
          .firstWhere((e) => e.kind == OutboxKind.checkin)
          .payload;
      expect(body.containsKey('latitude'), isFalse);
    });

    test('denied forever and location off are told apart', () async {
      expect(
        await GpsTracker(
          FakeLocationPlatform(permission: LocationPermission.deniedForever),
        ).start(),
        GpsState.deniedForever,
      );
      expect(
        await GpsTracker(FakeLocationPlatform(enabled: false)).start(),
        GpsState.serviceOff,
      );
    });

    test('allowed: the latest fix is sent while fresh', () async {
      var now = fixedNow;
      final gps = GpsTracker(
        FakeLocationPlatform(
          permission: LocationPermission.denied,
          onRequest: LocationPermission.whileInUse,
          fix: GpsFix(
            latitude: 6.132101,
            longitude: 1.223012,
            accuracyM: 12,
            at: fixedNow,
          ),
        ),
        now: () => now,
      );
      expect(await gps.start(), GpsState.ready);
      await gps.refresh();
      expect(gps.fresh()!.accuracyM, 12);
      now = now.add(const Duration(minutes: 4));
      expect(gps.fresh(), isNull, reason: 'too old');
    });
  });

  group('photos', () {
    test('cached, revalidated with If-None-Match, 404 drops it', () async {
      final b = h.backend;
      b.setWorkerPhoto(63, [1, 2, 3]);
      final url = '${b.publicBaseUrl}files/workers/63/photo/';
      final storage = MemoryPhotoStorage();
      final cache = PhotoCache(h.repos.files, storage, now: () => fixedNow);
      expect(await cache.load(url), [1, 2, 3]);
      expect(storage.photos[url]!.etag, '"w63-1"');
      h.recording.log.clear();
      expect(await cache.load(url), [1, 2, 3]);
      expect(h.recording.log, isEmpty, reason: 'checked this run');
      // Next app run: revalidate; 304 keeps the copy.
      final next = PhotoCache(h.repos.files, storage, now: () => fixedNow);
      expect(await next.load(url), [1, 2, 3]);
      expect(h.recording.log, ['download']);
      // Offline: the cached copy.
      b.offline = true;
      expect(await PhotoCache(h.repos.files, storage).load(url), [1, 2, 3]);
      b.offline = false;
      // A new photo: new ETag, new bytes.
      b.setWorkerPhoto(63, [9, 9]);
      expect(await PhotoCache(h.repos.files, storage).load(url), [9, 9]);
      // Not your site any more / no photo: initials.
      final gone = '${b.publicBaseUrl}files/workers/9999/photo/';
      storage.photos[gone] = CachedPhoto(Uint8List.fromList([7]), '"x"');
      expect(await PhotoCache(h.repos.files, storage).load(gone), isNull);
      expect(storage.photos.containsKey(gone), isFalse);
    });

    test(
      'HTTP: Authorization and If-None-Match; no token to another host',
      () async {
        final seen = <http.Request>[];
        final client = MockClient((r) async {
          seen.add(r);
          if (r.headers['if-none-match'] == '"v1"') {
            return http.Response('', 304, headers: {'etag': '"v1"'});
          }
          return http.Response.bytes(
            [0xFF, 0xD8, 0xFF],
            200,
            headers: {'etag': '"v1"', 'content-type': 'image/jpeg'},
          );
        });
        final repos = httpRepositories(
          baseUrl: 'https://polaris.example.com/api/v1/',
          token: () async => 'tok',
          language: () => 'fr',
          client: client,
        );
        const url =
            'https://polaris.example.com/api/v1/files/workers/63/photo/';
        final first = await repos.files.download(url);
        expect(first.bytes, [0xFF, 0xD8, 0xFF]);
        expect((first.etag, first.notModified), ('"v1"', false));
        expect(seen.last.headers['authorization'], 'Device tok');
        expect(seen.last.headers.containsKey('if-none-match'), isFalse);
        final again = await repos.files.download(url, etag: '"v1"');
        expect(again.notModified, isTrue);
        expect(seen.last.headers['if-none-match'], '"v1"');
        await repos.files.download('https://cdn.example.net/x.jpg');
        expect(seen.last.headers.containsKey('authorization'), isFalse);
      },
    );

    test('HTTP: a check-in photo goes as multipart `photo`', () async {
      late http.Request sent;
      final client = MockClient((r) async {
        sent = r;
        return http.Response(
          jsonEncode({
            'id': 1,
            'client_uuid': '9d5e1f7a-3b2c-4e8d-a1f0-6c7b8a9d0e12',
            'site_id': 3,
            'worker_id': 63,
            'date': today,
            'direction': 'in',
            'at': '2026-10-31T07:35:10Z',
            'received_at': '2026-10-31T07:35:12Z',
            'source': 'foreman_scan',
            'latitude': null,
            'longitude': null,
            'accuracy_m': null,
            'distance_m': null,
            'inside_geofence': null,
            'counted': true,
            'draft_updated': false,
            'flags': <String>[],
            'photo_url':
                'https://polaris.example.com/api/v1/files/checkins/1/photo/',
          }),
          201,
          headers: {'content-type': 'application/json'},
        );
      });
      final repos = httpRepositories(
        baseUrl: 'https://polaris.example.com/api/v1/',
        token: () async => 'tok',
        language: () => 'en',
        client: client,
      );
      final dir = Directory.systemTemp.createTempSync('polaris_photo');
      final f = File('${dir.path}/c.jpg')
        ..writeAsBytesSync([0xFF, 0xD8, 0xFF, 1]);
      const r = CheckInCreateRequest(
        clientUuid: '9d5e1f7a-3b2c-4e8d-a1f0-6c7b8a9d0e12',
        siteId: 3,
        direction: CheckInDirection.checkIn,
        at: '2026-10-31T07:35:10Z',
        badge: MockBackend.exampleBadge,
      );
      final c = await repos.checkin.checkIn(r, photoPath: f.path);
      expect(c.photoUrl, isNotNull);
      expect(sent.headers['content-type'], startsWith('multipart/form-data'));
      final body = latin1.decode(sent.bodyBytes);
      expect(body, contains('name="photo"; filename="c.jpg"'));
      expect(body, contains('content-type: image/jpeg'));
      expect(body, contains(MockBackend.exampleBadge));
      // Without a file: JSON.
      await repos.checkin.checkIn(r, photoPath: '/gone.jpg');
      expect(sent.headers['content-type'], startsWith('application/json'));
      expect(jsonDecode(sent.body), r.toJson());
    });
  });

  group('device and routing', () {
    test('device name from the model', () {
      expect(SessionController.androidName('TECNO', 'KI5q'), 'Tecno KI5q');
      expect(
        SessionController.androidName('samsung', 'SM-A145F'),
        'Samsung SM-A145F',
      );
      expect(
        SessionController.androidName('Google', 'Google Pixel 7'),
        'Google Pixel 7',
      );
      expect(SessionController.androidName('', ''), 'Android phone');
    });

    test('PIN check: salted, never the PIN itself', () {
      final a = SessionController.pinCheck('dev-1', '+228', '1234');
      expect(a, isNot(contains('1234')));
      expect(a, isNot(SessionController.pinCheck('dev-2', '+228', '1234')));
      expect(a, SessionController.pinCheck('dev-1', '+228', '1234'));
    });

    test('kiosk phones only reach the kiosk screen', () {
      String? go(String at, {bool kiosk = false, bool signedIn = true}) =>
          appRedirect(signedIn: signedIn, kiosk: kiosk, location: at);
      expect(go('/today', signedIn: false), '/pin');
      expect(go('/kiosk', signedIn: false), '/pin');
      for (final at in [
        '/today',
        '/roll-call',
        '/review',
        '/payout',
        '/scan',
        '/pin',
      ]) {
        expect(go(at, kiosk: true), '/kiosk', reason: at);
      }
      expect(go('/kiosk', kiosk: true), isNull);
      expect(go('/kiosk'), '/today');
      expect(go('/pin'), '/today');
      expect(go('/scan'), isNull);
    });
  });
}
