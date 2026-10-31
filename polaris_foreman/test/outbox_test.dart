import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/data/local/local_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_seed.dart';
import 'package:polaris_foreman/data/repositories/repositories.dart';
import 'package:polaris_foreman/services/payout_service.dart';
import 'package:polaris_foreman/services/roll_call_service.dart';
import 'package:polaris_foreman/services/sync_service.dart';

import 'support/fakes.dart';

const site = MockSeed.siteId;
final site2 = MockSeed.site2.id;

ApiException apiError(int status, String code, [Map<String, dynamic>? d]) =>
    ApiException(
      status,
      ApiError(code: code, message: code, details: d ?? const {}),
    );

void main() {
  group('rules (pure)', () {
    OutboxEntry e(int id, OutboxKind kind) => OutboxEntry(
      id: id,
      kind: kind,
      siteId: 1,
      date: today,
      clientUuid: '$id',
      payload: const {},
      createdAt: '',
    );

    test('sending order is records → submit → corrections → payments', () {
      final sorted = sendingOrder([
        e(1, OutboxKind.payment),
        e(2, OutboxKind.correction),
        e(3, OutboxKind.submit),
        e(4, OutboxKind.records),
        e(5, OutboxKind.records),
        e(6, OutboxKind.payment),
      ]);
      expect(sorted.map((x) => x.id), [4, 5, 3, 2, 1, 6]);
    });

    test('retry network errors, 5xx and 429; never 4xx; 401 needs PIN', () {
      expect(retryDecision(const NetworkException(), 1), isA<RetryLater>());
      expect(
        retryDecision(apiError(500, 'server_error'), 1),
        isA<RetryLater>(),
      );
      expect(
        retryDecision(apiError(503, 'server_error'), 1),
        isA<RetryLater>(),
      );
      expect(
        retryDecision(apiError(401, 'not_authenticated'), 1),
        isA<SignInAgain>(),
      );
      for (final s in [400, 403, 404, 409, 423]) {
        expect(retryDecision(apiError(s, 'x'), 1), isA<GiveUp>(), reason: '$s');
      }
      final throttled = retryDecision(
        apiError(429, 'throttled', {'retry_after': 42}),
        1,
      );
      expect((throttled as RetryLater).delay, const Duration(seconds: 42));
    });

    test('backoff doubles and is capped at 5 minutes', () {
      expect(backoff(1), const Duration(seconds: 2));
      expect(backoff(2), const Duration(seconds: 4));
      expect(backoff(3), const Duration(seconds: 8));
      expect(backoff(50), const Duration(minutes: 5));
    });
  });

  group('client_uuid', () {
    late Harness h;
    late RollCallService rc;

    setUp(() async {
      h = await Harness.create();
      rc = RollCallService(h.store, clock: h.clock);
    });

    test('one per worker per day, reused on every edit', () async {
      final a = await rc.mark(
        siteId: site,
        date: today,
        workerId: 44,
        status: AttendanceStatus.present,
        workStart: '07:00',
      );
      final b = await rc.mark(
        siteId: site,
        date: today,
        workerId: 44,
        status: AttendanceStatus.late,
        workStart: '07:00',
      );
      final other = await rc.mark(
        siteId: site,
        date: today,
        workerId: 51,
        status: AttendanceStatus.present,
      );
      final tomorrow = await rc.mark(
        siteId: site,
        date: '2026-11-01',
        workerId: 44,
        status: AttendanceStatus.present,
      );
      expect(b.clientUuid, a.clientUuid);
      expect(other.clientUuid, isNot(a.clientUuid));
      expect(tomorrow.clientUuid, isNot(a.clientUuid));
      expect(b.arrival, '07:30', reason: 'late arrives now');

      final outbox = await h.store.loadOutbox();
      final todays = outbox.where((x) => x.date == today).single;
      expect(todays.kind, OutboxKind.records);
      expect(todays.version, 3);
      final items = todays.payload['records'] as Map<String, dynamic>;
      expect(items.keys, unorderedEquals(['44', '51']));
      expect((items['44'] as Map)['status'], 'late');
      expect((items['44'] as Map)['client_uuid'], a.clientUuid);
    });

    test(
      'the server client_uuid is adopted (worker marked on two phones)',
      () async {
        await h.backend.putRecords(await h.token(), site, today, const [
          RecordUpsert(
            clientUuid: '11111111-1111-4111-8111-111111111111',
            workerId: 44,
            status: AttendanceStatus.present,
          ),
        ]);
        final mine = await rc.mark(
          siteId: site,
          date: today,
          workerId: 44,
          status: AttendanceStatus.absent,
        );
        expect(mine.clientUuid, isNot(startsWith('1111')));
        final sync = SyncService(
          store: h.store,
          repos: h.repos,
          clock: h.clock,
        );
        expect(await sync.drain(), DrainResult.done);
        final local = await h.store.loadRecord(site, today, 44);
        expect(local!.clientUuid, '11111111-1111-4111-8111-111111111111');
        expect(local.serverId, isNotNull);
        expect(local.status, AttendanceStatus.absent);
        final again = await rc.mark(
          siteId: site,
          date: today,
          workerId: 44,
          status: AttendanceStatus.present,
        );
        expect(again.clientUuid, '11111111-1111-4111-8111-111111111111');
      },
    );
  });

  group('SyncService', () {
    late Harness h;
    late RollCallService rc;
    late PayoutService pay;
    late SyncService sync;
    late DateTime now;
    var signedOut = false;

    setUp(() async {
      now = fixedNow;
      h = await Harness.create(now: () => now);
      rc = RollCallService(h.store, clock: h.clock);
      pay = PayoutService(h.store, clock: h.clock);
      signedOut = false;
      sync = SyncService(
        store: h.store,
        repos: h.repos,
        clock: h.clock,
        now: () => now,
        onSignedOut: () => signedOut = true,
      );
    });

    tearDown(() => sync.dispose());

    Future<void> markAll(int siteId) async {
      final crew = await h.backend.crew(await h.token(), siteId, today);
      await rc.markMany(
        siteId: siteId,
        date: today,
        marks: {for (final w in crew.workers) w.id: AttendanceStatus.present},
        workStart: '07:00',
      );
    }

    Future<void> submit(int siteId) async {
      final crew = await h.backend.crew(await h.token(), siteId, today);
      await rc.submit(
        siteId: siteId,
        date: today,
        crewWorkerIds: crew.workers.map((w) => w.id),
      );
    }

    test(
      'sends records → submit → corrections → payments, one at a time',
      () async {
        // Today on site 3 is submitted, approved, and the sheet is open.
        await markAll(site);
        await submit(site);
        expect(await sync.drain(), DrainResult.done);
        h.backend.approve(site, today);
        await sync.refreshPayout(site, today);
        final sheet = (await h.store.loadPayout(site, today))!;
        expect(sheet.status, PayoutStatus.open);
        h.recording.log.clear();

        // Created in the "wrong" order on purpose.
        await pay.confirmPaid(
          siteId: site,
          date: today,
          line: sheet.lines.first,
          proofType: ProofType.photo,
          proofPath: '/tmp/p.jpg',
        );
        await markAll(site2);
        await submit(site2);
        await rc.correct(
          siteId: site2,
          date: today,
          workerId: 120,
          field: CorrectableField.status,
          newValue: 'late',
          reason: 'Arrived 07:40',
        );

        expect(await sync.drain(), DrainResult.done);
        expect(h.recording.log, ['putRecords', 'submit', 'correct', 'pay']);
        expect(await h.store.loadOutbox(), isEmpty);

        // The payment is stored with the server's id; the sheet adds up.
        final payments = await h.store.loadPayments(site, today);
        expect(payments.single.sent, isTrue);
        final view = buildPayoutView(
          (await h.store.loadPayout(site, today))!,
          payments,
        );
        expect(view.sheet.paidCount, 1);
        expect(
          view.sheet.leftToPay,
          view.sheet.totalToPay - sheet.lines.first.net,
        );
        expect(view.unsent, isEmpty);
      },
    );

    test('an edit made while the batch is in flight is sent too', () async {
      await rc.mark(
        siteId: site,
        date: today,
        workerId: 44,
        status: AttendanceStatus.present,
      );
      h.recording.duringPut = () => rc.mark(
        siteId: site,
        date: today,
        workerId: 44,
        status: AttendanceStatus.halfDay,
      );
      expect(await sync.drain(), DrainResult.done);
      expect(h.recording.log, ['putRecords', 'putRecords']);
      final day = await h.backend.day(await h.token(), site, today);
      expect(day.records.single.status, AttendanceStatus.halfDay);
      expect(await h.store.loadOutbox(), isEmpty);
    });

    test('401 stops, keeps the outbox and asks for the PIN', () async {
      await markAll(site);
      await submit(site);
      h.backend.revokeToken();
      expect(await sync.drain(), DrainResult.signedOut);
      expect(signedOut, isTrue);
      expect(await h.store.loadOutbox(), hasLength(2));
      expect(h.recording.log, ['putRecords']);
    });

    test('network errors keep the entry and stop the queue', () async {
      await markAll(site);
      await submit(site);
      h.backend.offline = true;
      expect(await sync.drain(), DrainResult.waiting);
      final outbox = await h.store.loadOutbox();
      expect(outbox, hasLength(2));
      expect(outbox.every((e) => e.state == OutboxState.pending), isTrue);
      expect(outbox.first.attempts, 1);
      expect(outbox.first.errorCode, networkErrorCode);
      expect(h.recording.log, ['putRecords']);

      // Back online: the same writes go, in order.
      h.backend.offline = false;
      h.recording.log.clear();
      expect(await sync.drain(), DrainResult.done);
      expect(h.recording.log, ['putRecords', 'submit']);
    });

    test('5xx waits for the backoff before retrying', () async {
      await rc.mark(
        siteId: site,
        date: today,
        workerId: 44,
        status: AttendanceStatus.present,
      );
      h.recording.failures['putRecords'] = [apiError(503, 'server_error')];
      expect(await sync.drain(), DrainResult.waiting);
      final e = (await h.store.loadOutbox()).single;
      expect(e.attempts, 1);
      expect(e.nextAttemptAt, now.add(const Duration(seconds: 2)));

      h.recording.log.clear();
      expect(await sync.drain(), DrainResult.waiting, reason: 'too early');
      expect(h.recording.log, isEmpty);

      now = now.add(const Duration(seconds: 3));
      expect(await sync.drain(), DrainResult.done);
      expect(h.recording.log, ['putRecords']);
    });

    test('429 waits retry_after', () async {
      await rc.mark(
        siteId: site,
        date: today,
        workerId: 44,
        status: AttendanceStatus.present,
      );
      h.recording.failures['putRecords'] = [
        apiError(429, 'throttled', {'retry_after': 42}),
      ];
      expect(await sync.drain(), DrainResult.waiting);
      final e = (await h.store.loadOutbox()).single;
      expect(e.nextAttemptAt, now.add(const Duration(seconds: 42)));
      expect(e.state, OutboxState.pending);
    });

    test('409/423 mark the entry failed and the queue moves on', () async {
      await markAll(site2);
      await submit(site2);
      await markAll(site);
      h.recording.failures['submit'] = [
        apiError(409, 'conflict', {
          'unmarked_worker_ids': [1],
        }),
      ];
      expect(await sync.drain(), DrainResult.done);
      expect(h.recording.log, ['putRecords', 'putRecords', 'submit']);
      final outbox = await h.store.loadOutbox();
      expect(outbox.single.kind, OutboxKind.submit);
      expect(outbox.single.state, OutboxState.failed);
      expect(outbox.single.errorCode, 'conflict');
      // Not retried.
      h.recording.log.clear();
      expect(await sync.drain(), DrainResult.done);
      expect(h.recording.log, isEmpty);
    });

    test('runOnce checks /health/ first; offline sends nothing', () async {
      await rc.mark(
        siteId: site,
        date: today,
        workerId: 44,
        status: AttendanceStatus.present,
      );
      h.backend.offline = true;
      await sync.runOnce();
      expect(h.recording.log, ['health']);
      expect(sync.status.online, isFalse);
      expect(sync.status.waiting, 1);
    });

    test('pull: the server wins for days and records', () async {
      await h.store.setValue(
        StoreKeys.me,
        '{"id":7,"name":"Kossi Amegah","role":"foreman","phone":"+22890112233",'
        '"language":"fr","company":{"country":"TG","time_zone":"Africa/Lome",'
        '"currency":{"code":"XOF","decimals":0}},"sites":[]}',
      );
      await markAll(site);
      await submit(site);
      await sync.runOnce();
      expect((await h.store.loadDay(site, today))!.status, DayStatus.submitted);

      h.backend.approve(site, today);
      now = now.add(const Duration(hours: 9));
      await sync.runOnce();
      final day = (await h.store.loadDay(site, today))!;
      expect(day.status, DayStatus.approved);
      expect(day.payoutStatus, PayoutStatus.open);
      expect(await h.store.getValue(StoreKeys.syncSince), isNotNull);
    });
  });

  group('contract 1.1', () {
    test('submit carries submitted_at from the phone clock', () async {
      final h = await Harness.create();
      final rc = RollCallService(h.store, clock: h.clock);
      await rc.mark(
        siteId: site,
        date: today,
        workerId: 44,
        status: AttendanceStatus.present,
      );
      await rc.submit(siteId: site, date: today, crewWorkerIds: [44]);
      final entry = (await h.store.loadOutbox()).firstWhere(
        (e) => e.kind == OutboxKind.submit,
      );
      expect(entry.payload['submitted_at'], '2026-10-31T07:30:00Z');
      expect(
        SubmitDayRequest.fromJson(entry.payload).submittedAt,
        '2026-10-31T07:30:00Z',
      );
    });

    test('pull stores records whose day is not on the phone', () async {
      final h = await Harness.create();
      await h.store.setValue(
        StoreKeys.me,
        '{"id":7,"name":"Kossi Amegah","role":"foreman","phone":"+22890112233",'
        '"language":"fr","company":{"country":"TG","time_zone":"Africa/Lome",'
        '"currency":{"code":"XOF","decimals":0}},"sites":[]}',
      );
      // Marked on another phone: the server has the records.
      await h.backend.putRecords(await h.token(), site, today, const [
        RecordUpsert(
          clientUuid: '22222222-2222-4222-8222-222222222222',
          workerId: 51,
          status: AttendanceStatus.halfDay,
        ),
      ]);
      // A sync without the day: only its records changed.
      final resp = await h.backend.sync(await h.token(), null);
      expect(resp.records, isNotEmpty);
      final sync = SyncService(
        store: h.store,
        repos: Repositories(
          auth: h.repos.auth,
          crew: h.repos.crew,
          attendance: h.repos.attendance,
          payout: h.repos.payout,
          checkin: h.repos.checkin,
          files: h.repos.files,
          sync: _FixedSync(
            SyncResponse(
              serverTime: resp.serverTime,
              since: null,
              days: const [],
              records: resp.records,
              corrections: const [],
            ),
          ),
        ),
        clock: h.clock,
      );
      addTearDown(sync.dispose);
      await sync.pull();
      expect(await h.store.loadDay(site, today), isNull);
      final r = await h.store.loadRecord(site, today, 51);
      expect(r, isNotNull);
      expect(r!.status, AttendanceStatus.halfDay);
      expect(r.serverId, resp.records.single.id);
    });
  });

  group('roll call rules', () {
    test('submit needs every worker marked', () async {
      final h = await Harness.create();
      final rc = RollCallService(h.store, clock: h.clock);
      await rc.mark(
        siteId: site,
        date: today,
        workerId: 44,
        status: AttendanceStatus.present,
      );
      expect(
        () => rc.submit(siteId: site, date: today, crewWorkerIds: [44, 51]),
        throwsA(
          isA<UnmarkedWorkersException>().having((e) => e.workerIds, 'ids', [
            51,
          ]),
        ),
      );
    });

    test(
      'after submit, marks are refused; corrections need a reason',
      () async {
        final h = await Harness.create();
        final rc = RollCallService(h.store, clock: h.clock);
        await rc.mark(
          siteId: site,
          date: today,
          workerId: 44,
          status: AttendanceStatus.present,
        );
        await rc.submit(siteId: site, date: today, crewWorkerIds: [44]);
        expect(
          () => rc.mark(
            siteId: site,
            date: today,
            workerId: 44,
            status: AttendanceStatus.late,
          ),
          throwsA(isA<DayNotEditableException>()),
        );
        expect(
          () => rc.correct(
            siteId: site,
            date: today,
            workerId: 44,
            field: CorrectableField.status,
            newValue: 'late',
            reason: '  ',
          ),
          throwsArgumentError,
        );
      },
    );
  });
}

class _FixedSync implements SyncRepository {
  _FixedSync(this.response);
  final SyncResponse response;

  @override
  Future<bool> health() async => true;

  @override
  Future<SyncResponse> sync({String? since}) async => response;
}
