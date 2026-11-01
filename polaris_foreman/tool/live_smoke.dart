// Live smoke test of the HTTP repositories against a running Django server.
//
//   dart run tool/live_smoke.dart roll-call \
//     --base http://127.0.0.1:8765/api/v1/ --phone +22890112233 --pin 4821
//   # approve the day in the office (or with manage.py shell), then:
//   dart run tool/live_smoke.dart payout --base ... --phone ... --pin ...
//
// `roll-call`: health, PIN sign-in, me, crew, the day, records PUT twice (the
// second changes nothing), submit (twice: a replay), sync (records carry
// site_id and date), a correction (twice: Idempotent-Replayed), the payout
// preview. `payout`: sign in again, the open sheet, a payment with a photo
// (multipart) and its replay, a signature payment without a file (JSON),
// logout, then 401.
//
// Every JSON answer is also checked against its schema in
// ../docs/api/openapi.yaml. Exit code 0 when every step passed.
//
// `--tracking` (either phase) adds presence tracking: the tracker token,
// the consent when asked (and its replay), then one GPS fix to the tracker
// twice (`--tracker-url http://127.0.0.1:4100` to override the token's
// URL). It answers 404 until the Django presence app is merged.
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/http/http_repositories.dart';
import 'package:polaris_foreman/data/repositories/repositories.dart';
import 'package:polaris_foreman/tracking/observations.dart';
import 'package:polaris_foreman/tracking/tracking_api.dart';
import 'package:polaris_foreman/tracking/tracking_models.dart';
import 'package:uuid/uuid.dart';

import '../test/support/openapi.dart';

/// Keeps every answer to check it against the contract.
class RecordingClient extends http.BaseClient {
  RecordingClient(this._inner);
  final http.Client _inner;
  final exchanges = <(http.BaseRequest, int, String)>[];

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final r = await _inner.send(request);
    final bytes = await r.stream.toBytes();
    exchanges.add((request, r.statusCode, utf8.decode(bytes)));
    return http.StreamedResponse(
      Stream.value(bytes),
      r.statusCode,
      headers: r.headers,
      request: r.request,
      reasonPhrase: r.reasonPhrase,
    );
  }
}

/// `/api/v1/sites/1/days/2026-10-31/records/` → `/sites/{site_id}/days/{date}/records/`.
String? template(String path) {
  const rules = [
    (
      r'^/api/v1/sites/\d+/days/\d{4}-\d{2}-\d{2}/records/$',
      '/sites/{site_id}/days/{date}/records/',
    ),
    (
      r'^/api/v1/sites/\d+/days/\d{4}-\d{2}-\d{2}/submit/$',
      '/sites/{site_id}/days/{date}/submit/',
    ),
    (
      r'^/api/v1/sites/\d+/days/\d{4}-\d{2}-\d{2}/$',
      '/sites/{site_id}/days/{date}/',
    ),
    (r'^/api/v1/sites/\d+/days/$', '/sites/{site_id}/days/'),
    (r'^/api/v1/sites/\d+/crew/$', '/sites/{site_id}/crew/'),
    (
      r'^/api/v1/sites/\d+/payouts/\d{4}-\d{2}-\d{2}/$',
      '/sites/{site_id}/payouts/{date}/',
    ),
    (
      r'^/api/v1/records/\d+/corrections/$',
      '/records/{record_id}/corrections/',
    ),
  ];
  for (final (pattern, t) in rules) {
    if (RegExp(pattern).hasMatch(path)) return t;
  }
  return path.startsWith('/api/v1/') ? path.substring('/api/v1'.length) : null;
}

var failures = 0;

void check(bool ok, String what) {
  stdout.writeln('${ok ? 'PASS' : 'FAIL'}  $what');
  if (!ok) failures++;
}

Future<T?> step<T>(String what, Future<T> Function() f) async {
  try {
    final v = await f();
    check(true, what);
    return v;
  } catch (e) {
    check(false, '$what: $e');
    return null;
  }
}

Future<ApiException?> expectError(
  String what,
  Future<Object?> Function() f,
) async {
  try {
    await f();
    check(false, '$what: no error');
  } on ApiException catch (e) {
    check(true, '$what → ${e.statusCode} ${e.code} ${jsonEncode(e.details)}');
    return e;
  } catch (e) {
    check(false, '$what: $e');
  }
  return null;
}

Future<void> main(List<String> args) async {
  String arg(String name, String fallback) {
    final i = args.indexOf('--$name');
    return i >= 0 && i + 1 < args.length ? args[i + 1] : fallback;
  }

  final phase = args.isEmpty || args.first.startsWith('--')
      ? 'roll-call'
      : args.first;
  final base = arg('base', 'http://127.0.0.1:8765/api/v1/');
  final phone = arg('phone', '+22890112233');
  final pin = arg('pin', '4821');
  const deviceId = '5a1b2c3d-0000-4000-8000-00000000f001';
  String? token;
  final client = RecordingClient(http.Client());
  final Repositories repos = httpRepositories(
    baseUrl: base,
    token: () async => token,
    language: () => arg('lang', 'en'),
    client: client,
    onUnauthorized: () =>
        stdout.writeln('      (onUnauthorized: back to the PIN screen)'),
  );

  stdout.writeln('Live smoke ($phase) against $base');
  check(await repos.sync.health(), 'GET health/');
  await expectError('GET me/ without a token', repos.auth.me);
  await expectError(
    'POST auth/pin/ wrong PIN',
    () => repos.auth.signIn(
      PinLoginRequest(
        phone: phone,
        pin: pin == '0000' ? '1111' : '0000',
        deviceId: deviceId,
      ),
    ),
  );
  final login = await step(
    'POST auth/pin/',
    () => repos.auth.signIn(
      PinLoginRequest(
        phone: phone,
        pin: pin,
        deviceId: deviceId,
        deviceName: 'Live smoke (Dart)',
      ),
    ),
  );
  if (login == null) exit(1);
  token = login.token;
  final me = (await step('GET me/', repos.auth.me))!;
  stdout.writeln(
    '      ${me.name}, ${me.company.timeZone}, ${me.company.currency.code}, '
    'proofs ${me.company.payments.acceptedProofs.map((p) => p.wire)} '
    '(file for ${me.company.payments.fileRequiredFor.map((p) => p.wire)})',
  );
  if (args.contains('--tracking')) {
    await trackingSmoke(base, () async => token, arg);
  }
  final site = me.sites.first;
  final crew = (await step(
    'GET sites/${site.id}/crew/',
    () => repos.crew.crew(site.id),
  ))!;
  final date = crew.date;
  stdout.writeln('      ${crew.count} workers on $date');

  if (phase == 'roll-call') {
    await step(
      'GET sites/${site.id}/days/',
      () => repos.attendance.days(site.id),
    );
    final blank = (await step(
      'GET sites/${site.id}/days/$date/',
      () => repos.attendance.day(site.id, date),
    ))!;
    stdout.writeln('      status ${blank.summary.status.wire}');
    const statuses = [
      AttendanceStatus.present,
      AttendanceStatus.halfDay,
      AttendanceStatus.late,
      AttendanceStatus.absent,
    ];
    final marks = [
      for (var i = 0; i < crew.workers.length; i++)
        RecordUpsert(
          clientUuid: const Uuid().v4(),
          workerId: crew.workers[i].id,
          status: statuses[i % statuses.length],
          arrival: statuses[i % statuses.length] == AttendanceStatus.absent
              ? null
              : statuses[i % statuses.length] == AttendanceStatus.late
              ? '07:35'
              : '07:00',
          departure: statuses[i % statuses.length] == AttendanceStatus.halfDay
              ? '12:00'
              : null,
        ),
    ];
    final first = (await step(
      'PUT records/ (${marks.length})',
      () => repos.attendance.putRecords(site.id, date, marks),
    ))!;
    final second = (await step(
      'PUT records/ again',
      () => repos.attendance.putRecords(site.id, date, marks),
    ))!;
    check(
      jsonEncode(first.toJson()) == jsonEncode(second.toJson()),
      'the second PUT changed nothing',
    );
    check(
      first.records.every(
        (r) =>
            r.siteId == site.id &&
            r.date == date &&
            r.dayId == first.summary.id,
      ),
      'records carry site_id and date',
    );
    final bad = await expectError(
      'PUT records/ with a worker not on the crew',
      () => repos.attendance.putRecords(site.id, date, [
        marks.first,
        RecordUpsert(
          clientUuid: const Uuid().v4(),
          workerId: 999999,
          status: AttendanceStatus.present,
        ),
      ]),
    );
    final items = bad?.error.itemErrors('records');
    check(
      items != null &&
          items.length == 2 &&
          items[0].isEmpty &&
          items[1].isNotEmpty,
      'item errors are a JSON list: ${jsonEncode(bad?.details)}',
    );

    final submit = SubmitDayRequest(
      clientUuid: const Uuid().v4(),
      foremanNote: 'Live smoke from the Dart client.',
      submittedAt: DateTime.now().toUtc().toIso8601String(),
    );
    final submitted = (await step(
      'POST submit/',
      () => repos.attendance.submit(site.id, date, submit),
    ))!;
    check(submitted.summary.status == DayStatus.submitted, 'day is submitted');
    final replay = (await step(
      'POST submit/ again (same client_uuid)',
      () => repos.attendance.submit(site.id, date, submit),
    ))!;
    check(
      replay.summary.id == submitted.summary.id,
      'submit replay returns the day',
    );
    final unchanged = await step(
      'PUT records/ after submit, unchanged (a replay)',
      () => repos.attendance.putRecords(site.id, date, marks.sublist(0, 1)),
    );
    check(
      unchanged?.summary.status == DayStatus.submitted,
      'an unchanged batch is a 200 replay after submit',
    );
    final m = marks.first;
    await expectError(
      'PUT records/ after submit, changed',
      () => repos.attendance.putRecords(site.id, date, [
        RecordUpsert(
          clientUuid: m.clientUuid,
          workerId: m.workerId,
          status: m.status == AttendanceStatus.late
              ? AttendanceStatus.present
              : AttendanceStatus.late,
          arrival: '07:50',
        ),
      ]),
    );

    final sync = (await step('GET sync/', () => repos.sync.sync()))!;
    stdout.writeln(
      '      ${sync.days.length} days, ${sync.records.length} records, server_time ${sync.serverTime}',
    );
    check(
      sync.records
          .where((r) => r.dayId == submitted.summary.id)
          .every((r) => r.siteId == site.id && r.date == date),
      'synced records carry site_id and date',
    );
    final since = (await step(
      'GET sync/?since=server_time',
      () => repos.sync.sync(since: sync.serverTime),
    ))!;
    stdout.writeln(
      '      since: ${since.days.length} days, ${since.records.length} records',
    );

    final rec = submitted.records.first;
    final fix = CorrectionCreateRequest(
      clientUuid: const Uuid().v4(),
      field: CorrectableField.note,
      newValue: 'Corrected from the live smoke test.',
      reason: 'Smoke test',
    );
    final c1 = await step(
      'POST corrections/',
      () => repos.attendance.correct(rec.id, fix),
    );
    final c2 = await step(
      'POST corrections/ again',
      () => repos.attendance.correct(rec.id, fix),
    );
    check(
      c1 != null && !c1.replayed && c2 != null && c2.replayed && c1.id == c2.id,
      'correction replay: Idempotent-Replayed (${c1?.replayed}, ${c2?.replayed})',
    );
    final preview = await step(
      'GET payouts/$date/ (preview)',
      () => repos.payout.payout(site.id, date),
    );
    stdout.writeln(
      '      payout ${preview?.status.wire}, ${preview?.lineCount} lines, total ${preview?.totalToPay}',
    );
  } else {
    final sheet = (await step(
      'GET payouts/$date/',
      () => repos.payout.payout(site.id, date),
    ))!;
    stdout.writeln(
      '      payout ${sheet.status.wire}, ${sheet.lineCount} lines, total ${sheet.totalToPay}',
    );
    check(sheet.status == PayoutStatus.open, 'the sheet is open');
    final unpaid = sheet.lines.where((l) => l.payment == null).toList();
    if (unpaid.length >= 2) {
      final photo = File(
        '${Directory.systemTemp.createTempSync('smoke').path}/proof.jpg',
      )..writeAsBytesSync([0xff, 0xd8, 0xff, 0xe0, ...List.filled(512, 0x30)]);
      final req = PaymentCreateRequest(
        clientUuid: const Uuid().v4(),
        payLineId: unpaid[0].payLineId,
        amount: unpaid[0].net,
        proofType: ProofType.photo,
        paidAt: DateTime.now().toUtc().toIso8601String(),
      );
      await expectError(
        'POST payments/ photo without the file',
        () => repos.payout.pay(req),
      );
      final p1 = await step(
        'POST payments/ (multipart, photo)',
        () => repos.payout.pay(req, proofFilePath: photo.path),
      );
      final p2 = await step(
        'POST payments/ again',
        () => repos.payout.pay(req, proofFilePath: photo.path),
      );
      check(
        p1 != null &&
            !p1.replayed &&
            p1.proofUrl != null &&
            p2 != null &&
            p2.replayed &&
            p2.id == p1.id,
        'payment replay: Idempotent-Replayed; proof_url ${p1?.proofUrl}',
      );
      await step(
        'POST payments/ (JSON, signature without a file)',
        () => repos.payout.pay(
          PaymentCreateRequest(
            clientUuid: const Uuid().v4(),
            payLineId: unpaid[1].payLineId,
            amount: unpaid[1].net,
            proofType: ProofType.signature,
          ),
        ),
      );
      final after = (await step(
        'GET payouts/$date/ after',
        () => repos.payout.payout(site.id, date),
      ))!;
      check(
        after.paidCount == sheet.paidCount + 2 &&
            after.leftToPay == after.totalToPay - after.paidTotal,
        'the sheet adds up (${after.paidCount}/${after.lineCount} paid)',
      );
    } else {
      check(false, 'need two unpaid lines, found ${unpaid.length}');
    }
    await step('POST auth/logout/', repos.auth.logout);
    await expectError('GET me/ after logout', repos.auth.me);
  }

  // Every JSON answer against the contract.
  final api = OpenApi.load();
  var checked = 0;
  for (final (req, status, body) in client.exchanges) {
    final t = template(req.url.path);
    if (t == null) continue;
    final what = '${req.method} $t → $status';
    if (!api.statuses(t, req.method.toLowerCase()).contains('$status')) {
      check(false, '$what: status not in the contract');
      continue;
    }
    final schema = api.responseSchema(t, req.method.toLowerCase(), '$status');
    if (schema == null) continue;
    final errors = api.check(jsonDecode(body), schema);
    checked++;
    if (errors.isNotEmpty) check(false, '$what: ${errors.join('; ')}');
  }
  check(true, '$checked answers checked against openapi.yaml');
  stdout.writeln(failures == 0 ? 'ALL PASSED' : '$failures FAILED');
  exit(failures == 0 ? 0 : 1);
}

/// `--tracking`: presence tracking (docs/api/presence-django-openapi.yaml).
/// Until the Django `presence` app (feat/presence-django) is merged the
/// server answers 404 here: expected, and reported as such. With a token,
/// one GPS fix goes to the tracker twice (`--tracker-url` overrides the
/// token's `tracker_url`, e.g. `http://127.0.0.1:4100` for a local
/// `TRACKER_DEBUG=1 gleam run`); the second is a duplicate. Not checked
/// against openapi.yaml (the presence endpoints are not merged into it yet).
Future<void> trackingSmoke(
  String base,
  Future<String?> Function() token,
  String Function(String, String) arg,
) async {
  final api = HttpTrackingApi(
    ApiClient(baseUrl: base, token: token, language: () => 'en'),
  );

  /// The token, or the error code (`not_found` for a server without the
  /// endpoint).
  Future<(TrackingToken?, String?)> fetch() async {
    try {
      final t = await api.token();
      check(
        true,
        'POST tracking/token/ → sites ${t.sites}, ${t.trackerUrl}, '
        'every ${t.gpsIntervalSeconds} s, ${t.hours}, until ${t.expiresAt}',
      );
      return (t, null);
    } on ApiException catch (e) {
      return (null, e.statusCode == 404 ? 'not_found' : e.code);
    } catch (e) {
      check(false, 'POST tracking/token/: $e');
      return (null, 'error');
    }
  }

  var (t, code) = await fetch();
  if (code == TrackingErrors.consentRequired) {
    check(true, 'POST tracking/token/ → 403 consent_required');
    final request = ConsentRequest(
      clientUuid: const Uuid().v4(),
      accepted: true,
      textVersion: consentTextVersion,
    );
    final first = await step(
      'POST tracking/consent/',
      () => api.consent(request),
    );
    final again = await step(
      'POST tracking/consent/ again (same client_uuid)',
      () => api.consent(request),
    );
    check(
      first != null && again != null && first.at == again.at,
      'the consent replay returns the original answer',
    );
    (t, code) = await fetch();
  }
  switch (code) {
    case null || 'error':
      break;
    case 'not_found':
      check(
        false,
        'POST tracking/token/ → 404: the Django tracking endpoints are not '
        'on this server yet (feat/presence-django not merged)',
      );
    case TrackingErrors.trackingDisabled:
      check(true, 'POST tracking/token/ → 403 tracking_disabled (settings)');
    default:
      check(false, 'POST tracking/token/ → $code');
  }
  if (t == null) return;

  final tracker = HttpTrackerTransport();
  final url = arg('tracker-url', t.trackerUrl);
  final fix = gpsObservation(
    owner: 'smoke',
    fix: TrackingFix(
      latitude: 6.132,
      longitude: 1.2228,
      accuracyM: 12,
      at: DateTime.now(),
    ),
  );
  for (final label in ['', ' again (duplicate)']) {
    await step('POST $url/v1/observations$label', () async {
      final r = await tracker.postObservations(
        trackerUrl: url,
        token: t!.token,
        observations: [fix.json],
      );
      stdout.writeln(
        '      accepted ${r.accepted}, duplicates ${r.duplicates}, '
        'late ${r.late}, rejected ${r.rejected.map((x) => x.code)}',
      );
      return r;
    });
  }
}
