// One suite, two implementations: the mock repositories, and the HTTP
// repositories talking to the same mock server over HTTP (MockClient). Both
// must give the same results and the same errors; every HTTP answer must
// match its schema in docs/api/openapi.yaml.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
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
const crewIds = [44, 51, 52, 58, 59, 60, 63, 64];
const pin = PinLoginRequest(
  phone: MockSeed.foremanPhone,
  pin: MockSeed.foremanPin,
  deviceId: '0b6f4c4e-61a2-4d5b-9a50-8f3c0f5d2a11',
  deviceName: 'Tecno Spark 10',
);

String uuid(int n) =>
    '00000000-0000-4000-8000-${n.toRadixString(16).padLeft(12, '0')}';

class Fixture {
  Fixture(this.backend, this.repos, this.tokens, this.exchanges);

  final MockBackend backend;
  final Repositories repos;
  final MemoryTokenStore tokens;

  /// HTTP only: every request and answer.
  final List<Exchange>? exchanges;

  Future<void> signIn() async =>
      tokens.write((await repos.auth.signIn(pin)).token);
}

MockBackend _backend() => MockBackend(
  latency: Duration.zero,
  approvalDelay: null,
  now: () => fixedNow,
);

final implementations = <String, Fixture Function()>{
  'mock': () {
    final b = _backend();
    final tokens = MemoryTokenStore();
    return Fixture(b, mockRepositories(b, tokens.read), tokens, null);
  },
  'http': () {
    final b = _backend();
    final tokens = MemoryTokenStore();
    final exchanges = <Exchange>[];
    final repos = httpRepositories(
      baseUrl: mockBaseUrl,
      token: tokens.read,
      language: () => 'en',
      client: mockHttpClient(b, onExchange: exchanges.add),
    );
    return Fixture(b, repos, tokens, exchanges);
  },
};

TypeMatcher<ApiException> apiError(int status, String code) =>
    isA<ApiException>()
        .having((e) => e.statusCode, 'status', status)
        .having((e) => e.code, 'code', code);

late File proofFile;

/// A JSON-able trace of an outcome, to compare the two implementations.
Future<Object?> outcome(Future<Object?> Function() call) async {
  try {
    final v = await call();
    return switch (v) {
      Payment() => {'ok': v.toJson(), 'replayed': v.replayed},
      Correction() => {'ok': v.toJson(), 'replayed': v.replayed},
      PinLoginResponse() => {'ok': v.toJson()},
      Me() => {'ok': v.toJson()},
      Crew() => {'ok': v.toJson()},
      AttendanceDay() => {'ok': v.toJson()},
      AttendanceDayList() => {'ok': v.toJson()},
      Payout() => {'ok': v.toJson()},
      SyncResponse() => {'ok': v.toJson()},
      _ => {'ok': '$v'},
    };
  } on ApiException catch (e) {
    return {'status': e.statusCode, 'error': e.error.toJson()};
  } on NetworkException {
    return {'network': true};
  }
}

/// The same story on any implementation; returns its trace.
Future<List<Object?>> story(Fixture f) async {
  final r = f.repos;
  final trace = <Object?>[];
  Future<void> t(Future<Object?> Function() call) async =>
      trace.add(await outcome(call));

  await t(() => r.auth.me()); // No token yet.
  await t(
    () => r.auth.signIn(
      const PinLoginRequest(phone: '+22800000000', pin: '1234', deviceId: 'x'),
    ),
  );
  await t(
    () => r.auth.signIn(
      PinLoginRequest(
        phone: pin.phone,
        pin: '0000',
        deviceId: pin.deviceId,
        deviceName: pin.deviceName,
      ),
    ),
  );
  trace.add(await outcome(() => r.auth.signIn(pin)));
  await f.signIn();
  await t(() => r.auth.me());
  await t(() => r.crew.crew(site, date: today));
  await t(() => r.crew.crew(99, date: today));
  await t(() => r.attendance.days(site));
  await t(() => r.attendance.day(site, today));
  final marks = [
    for (final id in crewIds)
      RecordUpsert(
        clientUuid: uuid(id),
        workerId: id,
        status: id == 58 ? AttendanceStatus.absent : AttendanceStatus.present,
        arrival: id == 58 ? null : '07:00',
      ),
  ];
  await t(
    () => r.attendance.putRecords(site, today, [
      marks[0],
      const RecordUpsert(
        clientUuid: '00000000-0000-4000-8000-0000000003e7',
        workerId: 999,
        status: AttendanceStatus.present,
      ),
      marks[1],
    ]),
  );
  await t(() => r.attendance.putRecords(site, today, marks.sublist(0, 4)));
  await t(() => r.attendance.putRecords(site, today, marks.sublist(0, 4)));
  await t(
    () => r.attendance.submit(
      site,
      today,
      const SubmitDayRequest(
        clientUuid: '00000000-0000-4000-8000-00000000a001',
      ),
    ),
  );
  await t(() => r.attendance.putRecords(site, today, marks));
  final submit = SubmitDayRequest(
    clientUuid: '00000000-0000-4000-8000-00000000a002',
    foremanNote: 'Rain at 10:00.',
    submittedAt: '2026-10-31T07:29:00Z',
  );
  await t(() => r.attendance.submit(site, today, submit));
  await t(() => r.attendance.submit(site, today, submit)); // Replay.
  await t(() => r.attendance.putRecords(site, today, marks.sublist(0, 1)));
  await t(
    () => r.attendance.putRecords(site, today, [
      RecordUpsert(
        clientUuid: uuid(44),
        workerId: 44,
        status: AttendanceStatus.late,
      ),
    ]),
  );
  final day = await r.attendance.day(site, today);
  final rec = day.records.firstWhere((x) => x.workerId == 63);
  final fix = CorrectionCreateRequest(
    clientUuid: '00000000-0000-4000-8000-00000000c001',
    field: CorrectableField.arrival,
    newValue: '07:20',
    reason: 'Signed in at the gate at 07:20.',
  );
  await t(() => r.attendance.correct(rec.id, fix));
  await t(() => r.attendance.correct(rec.id, fix)); // Replay.
  await t(() => r.attendance.correct(rec.id + 1, fix)); // Same uuid, other.
  await t(() => r.attendance.correct(123456, fix.copyWithUuid(uuid(77))));
  await t(() => r.payout.payout(site, today)); // Preview.
  final preview = await r.payout.payout(site, today);
  final line = preview.lines.first;
  PaymentCreateRequest pay(String id, {int? amount, ProofType? proof}) =>
      PaymentCreateRequest(
        clientUuid: id,
        payLineId: line.payLineId,
        amount: amount ?? line.net,
        proofType: proof ?? ProofType.photo,
        paidAt: '2026-10-31T16:31:00Z',
      );
  await t(
    () => r.payout.pay(
      pay('00000000-0000-4000-8000-00000000b000'),
      proofFilePath: proofFile.path,
    ),
  ); // Not open yet.
  f.backend.approve(site, today);
  await t(() => r.payout.payout(site, today));
  await t(
    () => r.payout.pay(pay('00000000-0000-4000-8000-00000000b001')),
  ); // Photo without the file.
  await t(
    () => r.payout.pay(
      pay('00000000-0000-4000-8000-00000000b002', proof: ProofType.none),
    ),
  ); // `none` is not accepted.
  await t(
    () => r.payout.pay(
      pay('00000000-0000-4000-8000-00000000b003', amount: 1),
      proofFilePath: proofFile.path,
    ),
  );
  final good = pay('00000000-0000-4000-8000-00000000b004');
  await t(() => r.payout.pay(good, proofFilePath: proofFile.path));
  await t(() => r.payout.pay(good, proofFilePath: proofFile.path)); // Replay.
  await t(
    () => r.payout.pay(
      pay('00000000-0000-4000-8000-00000000b005'),
      proofFilePath: proofFile.path,
    ),
  ); // Already paid.
  await t(
    () => r.payout.pay(
      PaymentCreateRequest(
        clientUuid: '00000000-0000-4000-8000-00000000b006',
        payLineId: 424242,
        amount: 1,
        proofType: ProofType.signature,
      ),
    ),
  );
  await t(() => r.payout.payout(site, today));
  await t(() => r.sync.sync());
  await t(() => r.sync.health());
  await t(() => r.auth.logout());
  await t(() => r.auth.me());
  f.backend.offline = true;
  await t(() => r.sync.health());
  return trace;
}

extension on CorrectionCreateRequest {
  CorrectionCreateRequest copyWithUuid(String id) => CorrectionCreateRequest(
    clientUuid: id,
    field: field,
    newValue: newValue,
    reason: reason,
  );
}

void main() {
  setUpAll(() {
    final dir = Directory.systemTemp.createTempSync('polaris_suite');
    proofFile = File('${dir.path}/proof.jpg')
      ..writeAsBytesSync(List.generate(64, (i) => i));
  });

  for (final impl in implementations.entries) {
    group(impl.key, () {
      late Fixture f;
      setUp(() => f = impl.value());

      test(
        'PIN: unknown phone, wrong PIN, lock on the 5th, 423 after',
        () async {
          final auth = f.repos.auth;
          await expectLater(
            auth.signIn(
              const PinLoginRequest(phone: '+228', pin: '1234', deviceId: 'x'),
            ),
            throwsA(
              apiError(
                401,
                'not_authenticated',
              ).having((e) => e.details, 'details', isEmpty),
            ),
          );
          final wrong = PinLoginRequest(
            phone: pin.phone,
            pin: '9999',
            deviceId: pin.deviceId,
          );
          for (var left = 4; left >= 1; left--) {
            await expectLater(
              auth.signIn(wrong),
              throwsA(
                apiError(401, 'not_authenticated')
                    .having((e) => e.attemptsLeft, 'left', left)
                    .having((e) => e.lockedUntil, 'until', isNull),
              ),
            );
          }
          await expectLater(
            auth.signIn(wrong),
            throwsA(
              apiError(401, 'not_authenticated')
                  .having((e) => e.attemptsLeft, 'left', 0)
                  .having(
                    (e) => e.lockedUntil,
                    'until',
                    '2026-10-31T07:45:00Z',
                  ),
            ),
          );
          await expectLater(
            auth.signIn(pin),
            throwsA(
              apiError(
                423,
                'locked',
              ).having((e) => e.lockedUntil, 'until', '2026-10-31T07:45:00Z'),
            ),
          );
        },
      );

      test('sign in, me with payment rules, crew, 403 elsewhere', () async {
        await expectLater(
          f.repos.auth.me(),
          throwsA(apiError(401, 'not_authenticated')),
        );
        final login = await f.repos.auth.signIn(pin);
        expect(login.device.name, 'Tecno Spark 10');
        await f.tokens.write(login.token);
        final me = await f.repos.auth.me();
        expect(me.company.payments.acceptedProofs, [
          ProofType.photo,
          ProofType.signature,
          ProofType.thumbMark,
        ]);
        expect(me.company.payments.fileRequiredFor, [ProofType.photo]);
        final crew = await f.repos.crew.crew(site, date: today);
        final delali = crew.workers.firstWhere((w) => w.id == 63);
        expect((delali.number, delali.name), ('W-0163', 'Delali Agbo'));
        await expectLater(
          f.repos.crew.crew(99),
          throwsA(apiError(403, 'permission_denied')),
        );
      });

      test(
        'records: idempotent PUT, site_id/date, list-shaped errors',
        () async {
          await f.signIn();
          final a = f.repos.attendance;
          final blank = await a.day(site, today);
          expect(
            (blank.summary.id, blank.summary.status),
            (null, DayStatus.notStarted),
          );
          final marks = [
            for (final id in [44, 51])
              RecordUpsert(
                clientUuid: uuid(id),
                workerId: id,
                status: AttendanceStatus.present,
              ),
          ];
          final first = await a.putRecords(site, today, marks);
          final second = await a.putRecords(site, today, marks);
          expect(second.toJson(), first.toJson());
          expect(first.summary.status, DayStatus.draft);
          for (final r in first.records) {
            expect(
              (r.siteId, r.date, r.dayId),
              (site, today, first.summary.id),
            );
          }

          final err = await a
              .putRecords(site, today, [
                marks[0],
                RecordUpsert(
                  clientUuid: uuid(999),
                  workerId: 999,
                  status: AttendanceStatus.present,
                ),
                marks[1],
              ])
              .then<ApiException?>(
                (_) => null,
                onError: (Object e) => e as ApiException,
              );
          expect(err!.statusCode, 400);
          // One entry per item sent, `{}` for items without errors (the
          // server's upsert_records; the README also allows a list that stops
          // at the last item with an error: itemErrors takes both).
          expect(err.error.itemErrors('records'), [
            <String, dynamic>{},
            {
              'worker_id': [
                'This worker is not on the crew of this site that day.',
              ],
            },
            <String, dynamic>{},
          ]);

          final tooMany = [
            for (var i = 0; i < 201; i++)
              RecordUpsert(
                clientUuid: uuid(5000 + i),
                workerId: 44,
                status: AttendanceStatus.present,
              ),
          ];
          await expectLater(
            a.putRecords(site, today, tooMany),
            throwsA(
              apiError(400, 'validation_error')
                  .having((e) => e.error.itemErrors('records'), 'items', isNull)
                  .having(
                    (e) => (e.details['records'] as Map)['non_field_errors'],
                    'field error',
                    isNotEmpty,
                  ),
            ),
          );
          await expectLater(
            a.putRecords(site, today, [
              RecordUpsert(
                clientUuid: uuid(44),
                workerId: 51,
                status: AttendanceStatus.present,
              ),
            ]),
            throwsA(apiError(409, 'conflict')),
          );
        },
      );

      test(
        'submit: unmarked 409, replay, 423 after; corrections replay',
        () async {
          await f.signIn();
          final a = f.repos.attendance;
          await a.putRecords(site, today, [
            for (final id in crewIds.take(3))
              RecordUpsert(
                clientUuid: uuid(id),
                workerId: id,
                status: AttendanceStatus.present,
              ),
          ]);
          final req = SubmitDayRequest(
            clientUuid: uuid(0xa1),
            submittedAt: '2026-10-31T07:29:00Z',
          );
          final unmarked = await a
              .submit(site, today, req)
              .then<ApiException?>(
                (_) => null,
                onError: (Object e) => e as ApiException,
              );
          expect(unmarked!.statusCode, 409);
          expect(unmarked.unmarkedWorkerIds, contains(58));
          expect(unmarked.unmarkedWorkerIds, isNot(contains(44)));

          final crew = await f.repos.crew.crew(site);
          await a.putRecords(site, today, [
            for (final w in crew.workers)
              RecordUpsert(
                clientUuid: uuid(w.id),
                workerId: w.id,
                status: AttendanceStatus.present,
              ),
          ]);
          final day = await a.submit(site, today, req);
          expect(day.summary.status, DayStatus.submitted);
          expect((await a.submit(site, today, req)).toJson(), day.toJson());
          await expectLater(
            a.submit(site, today, SubmitDayRequest(clientUuid: uuid(0xa2))),
            throwsA(
              apiError(
                409,
                'conflict',
              ).having((e) => e.details['status'], 'status', 'submitted'),
            ),
          );
          await expectLater(
            a.putRecords(site, today, [
              RecordUpsert(
                clientUuid: uuid(44),
                workerId: 44,
                status: AttendanceStatus.late,
              ),
            ]),
            throwsA(apiError(423, 'locked')),
          );
          // A batch that changes nothing is a replay: 200 whatever the status.
          final replay = await a.putRecords(site, today, [
            RecordUpsert(
              clientUuid: uuid(44),
              workerId: 44,
              status: AttendanceStatus.present,
            ),
          ]);
          expect(replay.summary.status, DayStatus.submitted);

          final rec = day.records.first;
          final fix = CorrectionCreateRequest(
            clientUuid: uuid(0xc1),
            field: CorrectableField.status,
            newValue: 'late',
            reason: 'Arrived 07:40',
          );
          final c1 = await a.correct(rec.id, fix);
          final c2 = await a.correct(rec.id, fix);
          expect((c1.replayed, c2.replayed), (false, true));
          expect(c2.toJson(), c1.toJson());
          expect((c1.record.siteId, c1.record.date), (site, today));
          await expectLater(
            a.correct(rec.id + 1, fix),
            throwsA(apiError(409, 'conflict')),
          );
        },
      );

      test(
        'payments: proof rules, amount, replay, already paid, 404',
        () async {
          await f.signIn();
          final crew = await f.repos.crew.crew(site);
          await f.repos.attendance.putRecords(site, today, [
            for (final w in crew.workers)
              RecordUpsert(
                clientUuid: uuid(w.id),
                workerId: w.id,
                status: AttendanceStatus.present,
              ),
          ]);
          await f.repos.attendance.submit(
            site,
            today,
            SubmitDayRequest(clientUuid: uuid(0xa1)),
          );
          final preview = await f.repos.payout.payout(site, today);
          expect(preview.status, PayoutStatus.notOpen);
          final line = preview.lines.first;
          PaymentCreateRequest req(int n, {ProofType? proof, int? amount}) =>
              PaymentCreateRequest(
                clientUuid: uuid(0xb000 + n),
                payLineId: line.payLineId,
                amount: amount ?? line.net,
                proofType: proof ?? ProofType.photo,
                paidAt: '2026-10-31T16:31:00Z',
              );
          await expectLater(
            f.repos.payout.pay(req(0), proofFilePath: proofFile.path),
            throwsA(
              apiError(
                409,
                'conflict',
              ).having((e) => e.details['status'], 'status', 'not_open'),
            ),
          );
          f.backend.approve(site, today);
          expect(
            (await f.repos.payout.payout(site, today)).status,
            PayoutStatus.open,
          );
          await expectLater(
            f.repos.payout.pay(req(1)),
            throwsA(
              apiError(
                400,
                'validation_error',
              ).having((e) => e.details.keys, 'fields', ['proof_file']),
            ),
          );
          await expectLater(
            f.repos.payout.pay(req(2, proof: ProofType.none)),
            throwsA(
              apiError(
                400,
                'validation_error',
              ).having((e) => e.details.keys, 'fields', ['proof_type']),
            ),
          );
          await expectLater(
            f.repos.payout.pay(
              req(3, amount: 1),
              proofFilePath: proofFile.path,
            ),
            throwsA(
              apiError(
                400,
                'validation_error',
              ).having((e) => e.details.keys, 'fields', ['amount']),
            ),
          );
          final p1 = await f.repos.payout.pay(
            req(4),
            proofFilePath: proofFile.path,
          );
          final p2 = await f.repos.payout.pay(
            req(4),
            proofFilePath: proofFile.path,
          );
          expect((p1.replayed, p2.replayed), (false, true));
          expect(p2.toJson(), p1.toJson());
          expect(p1.proofUrl, isNotNull);
          // A signature may come without a file.
          final second = (await f.repos.payout.payout(site, today)).lines[1];
          final sig = await f.repos.payout.pay(
            PaymentCreateRequest(
              clientUuid: uuid(0xb010),
              payLineId: second.payLineId,
              amount: second.net,
              proofType: ProofType.signature,
            ),
          );
          expect((sig.proofType, sig.proofUrl), (ProofType.signature, null));
          await expectLater(
            f.repos.payout.pay(req(5), proofFilePath: proofFile.path),
            throwsA(
              apiError(
                409,
                'conflict',
              ).having((e) => e.details['pay_line_id'], 'line', line.payLineId),
            ),
          );
          await expectLater(
            f.repos.payout.pay(
              PaymentCreateRequest(
                clientUuid: uuid(0xb006),
                payLineId: 424242,
                amount: 1,
                proofType: ProofType.signature,
              ),
            ),
            throwsA(apiError(404, 'not_found')),
          );
          final sheet = await f.repos.payout.payout(site, today);
          expect(sheet.paidCount, 2);
          expect(sheet.leftToPay, sheet.totalToPay - sheet.paidTotal);
        },
      );

      test(
        'sync carries site_id and date on records; logout; offline',
        () async {
          await f.signIn();
          await f.repos.attendance.putRecords(site, today, [
            RecordUpsert(
              clientUuid: uuid(44),
              workerId: 44,
              status: AttendanceStatus.present,
            ),
          ]);
          final s = await f.repos.sync.sync();
          expect(s.serverTime, '2026-10-31T07:30:00Z');
          expect(s.records.single.siteId, site);
          expect(s.records.single.date, today);
          expect(await f.repos.sync.health(), isTrue);
          await f.repos.auth.logout();
          await expectLater(
            f.repos.sync.sync(since: s.serverTime),
            throwsA(apiError(401, 'not_authenticated')),
          );
          f.backend.offline = true;
          await expectLater(
            f.repos.sync.health(),
            throwsA(isA<NetworkException>()),
          );
        },
      );
    });
  }

  test('both implementations tell the same story', () async {
    final mock = await story(implementations['mock']!());
    final http = implementations['http']!();
    final viaHttp = await story(http);
    expect(viaHttp.length, mock.length);
    for (var i = 0; i < mock.length; i++) {
      expect(viaHttp[i], mock[i], reason: 'step $i');
    }
    // And every answer of the HTTP run matches its schema in the contract.
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
    // Every endpoint was exercised.
    expect(
      http.exchanges!.map((x) => '${x.method} ${x.template}').toSet(),
      containsAll([
        'POST /auth/pin/',
        'POST /auth/logout/',
        'GET /me/',
        'GET /health/',
        'GET /sites/{site_id}/crew/',
        'GET /sites/{site_id}/days/',
        'GET /sites/{site_id}/days/{date}/',
        'PUT /sites/{site_id}/days/{date}/records/',
        'POST /sites/{site_id}/days/{date}/submit/',
        'POST /records/{record_id}/corrections/',
        'GET /sites/{site_id}/payouts/{date}/',
        'POST /payments/',
        'GET /sync/',
      ]),
    );
  }, skip: specSkip);
}
