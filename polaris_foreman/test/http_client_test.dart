// Contract tests of the HTTP client: for every endpoint, the request it
// sends (method, path, headers, body) and how it parses the answers, using
// the examples of docs/api/openapi.yaml; then the error mapping.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/http/http_repositories.dart';
import 'package:polaris_foreman/data/repositories/repositories.dart';

import 'support/mock_http_server.dart' show parseMultipart;
import 'support/openapi.dart';

const base = 'https://polaris.example.com/api/v1/';

/// A fake server: answers every request with [answer] and keeps them.
class FakeServer {
  FakeServer(this.answer);

  FutureOr<http.Response> Function(http.Request) answer;
  final requests = <http.Request>[];
  int unauthorized = 0;
  String language = 'fr';

  http.Request get last => requests.last;

  Repositories repos({
    String? token = 'q9V3rM2xQk7f0bU8p1sZcW4yL6tN5hJaDgE_oRiK-Yw',
    Duration timeout = const Duration(seconds: 30),
  }) => httpRepositories(
    baseUrl: base,
    token: () async => token,
    language: () => language,
    timeout: timeout,
    onUnauthorized: () => unauthorized++,
    client: MockClient((r) async {
      requests.add(r);
      return answer(r);
    }),
  );
}

http.Response json(
  Object? body, {
  int status = 200,
  Map<String, String> headers = const {},
}) => http.Response(
  jsonEncode(body),
  status,
  headers: {'content-type': 'application/json', ...headers},
);

http.Response html(int status, {Map<String, String> headers = const {}}) =>
    http.Response(
      '<html><body><h1>$status</h1></body></html>',
      status,
      headers: {'content-type': 'text/html', ...headers},
    );

void main() {
  late OpenApi api;
  setUpAll(() {
    if (specSkip == null) api = OpenApi.load();
  });

  void expectHeaders(http.Request r, {bool auth = true, bool body = false}) {
    expect(r.headers['Accept'], 'application/json');
    expect(r.headers['Accept-Language'], 'fr');
    expect(
      r.headers['Authorization'],
      auth ? 'Device q9V3rM2xQk7f0bU8p1sZcW4yL6tN5hJaDgE_oRiK-Yw' : isNull,
    );
    if (body) {
      expect(r.headers['Content-Type'], startsWith('application/json'));
    }
  }

  group('requests and responses (openapi examples)', skip: specSkip, () {
    test('POST auth/pin/: no token, JSON body, SignedIn parsed', () async {
      final ok = api.response('/auth/pin/', 'post', '200', 'SignedIn');
      final s = FakeServer((_) => json(ok));
      final body = api.request('/auth/pin/', 'post', 'SignIn');
      final resp = await s.repos().auth.signIn(PinLoginRequest.fromJson(body));
      expect(s.last.method, 'POST');
      expect(s.last.url.toString(), '${base}auth/pin/');
      expectHeaders(s.last, auth: false, body: true);
      expect(jsonDecode(s.last.body), body);
      expect(resp.toJson(), ok);
    });

    test('GET me/ with payment rules', () async {
      final ok = api.response('/me/', 'get', '200', 'Foreman');
      final s = FakeServer((_) => json(ok));
      final me = await s.repos().auth.me();
      expect((s.last.method, s.last.url.toString()), ('GET', '${base}me/'));
      expectHeaders(s.last);
      expect(me.toJson(), ok);
      expect(me.company.payments.fileRequiredFor, [ProofType.photo]);
      expect(me.company.payments.accepts(ProofType.none), isFalse);
    });

    test('POST auth/logout/: 204, no body', () async {
      final s = FakeServer((_) => http.Response('', 204));
      await s.repos().auth.logout();
      expect(
        (s.last.method, s.last.url.toString(), s.last.body),
        ('POST', '${base}auth/logout/', ''),
      );
      expectHeaders(s.last);
    });

    test('GET sites/3/crew/?date=', () async {
      final ok = api.response('/sites/{site_id}/crew/', 'get', '200', 'Crew');
      final s = FakeServer((_) => json(ok));
      final crew = await s.repos().crew.crew(3, date: '2026-10-31');
      expect(s.last.url.toString(), '${base}sites/3/crew/?date=2026-10-31');
      expectHeaders(s.last);
      expect(crew.toJson(), ok);
      await s.repos().crew.crew(3);
      expect(s.last.url.toString(), '${base}sites/3/crew/');
    });

    test('GET sites/3/days/?from=&to=', () async {
      final ok = api.response(
        '/sites/{site_id}/days/',
        'get',
        '200',
        'ThisWeek',
      );
      final s = FakeServer((_) => json(ok));
      final list = await s.repos().attendance.days(
        3,
        from: '2026-10-25',
        to: '2026-10-31',
      );
      expect(s.last.url.path, '/api/v1/sites/3/days/');
      expect(s.last.url.queryParameters, {
        'from': '2026-10-25',
        'to': '2026-10-31',
      });
      expect(list.toJson(), ok);
    });

    test('GET sites/3/days/2026-10-31/ (draft and not_started)', () async {
      for (final name in ['Draft', 'NotStarted']) {
        final ok = api.response(
          '/sites/{site_id}/days/{date}/',
          'get',
          '200',
          name,
        );
        final s = FakeServer((_) => json(ok));
        final day = await s.repos().attendance.day(3, '2026-10-31');
        expect(s.last.url.toString(), '${base}sites/3/days/2026-10-31/');
        expect(day.toJson(), ok);
      }
    });

    test('PUT sites/3/days/2026-10-31/records/', () async {
      const path = '/sites/{site_id}/days/{date}/records/';
      final ok = api.response(path, 'put', '200', 'Saved');
      final body = api.request(path, 'put', 'Marks');
      final s = FakeServer((_) => json(ok));
      final day = await s.repos().attendance.putRecords(3, '2026-10-31', [
        for (final r in body['records'] as List)
          RecordUpsert.fromJson(r as Map<String, dynamic>),
      ]);
      expect(s.last.method, 'PUT');
      expect(s.last.url.toString(), '${base}sites/3/days/2026-10-31/records/');
      expectHeaders(s.last, body: true);
      expect(jsonDecode(s.last.body), body);
      expect(day.toJson(), ok);
      expect(
        (day.records.first.siteId, day.records.first.date),
        (3, '2026-10-31'),
      );
    });

    test('POST sites/3/days/2026-10-31/submit/ with submitted_at', () async {
      const path = '/sites/{site_id}/days/{date}/submit/';
      final ok = api.response(path, 'post', '200', 'Submitted');
      final body = api.request(path, 'post', 'Submit');
      expect(body['submitted_at'], isNotNull);
      final s = FakeServer((_) => json(ok));
      final day = await s.repos().attendance.submit(
        3,
        '2026-10-31',
        SubmitDayRequest.fromJson(body),
      );
      expect(s.last.method, 'POST');
      expect(s.last.url.toString(), '${base}sites/3/days/2026-10-31/submit/');
      expectHeaders(s.last, body: true);
      expect(jsonDecode(s.last.body), body);
      expect(day.toJson(), ok);
    });

    test('POST records/9003/corrections/: 201, then a 200 replay', () async {
      const path = '/records/{record_id}/corrections/';
      final created = api.response(path, 'post', '201', 'Created');
      final body = api.request(path, 'post', 'CorrectArrival');
      final s = FakeServer((_) => json(created, status: 201));
      final req = CorrectionCreateRequest.fromJson(body);
      final c = await s.repos().attendance.correct(9003, req);
      expect(s.last.url.toString(), '${base}records/9003/corrections/');
      expectHeaders(s.last, body: true);
      expect(jsonDecode(s.last.body), body);
      expect(c.toJson(), created);
      expect(c.replayed, isFalse);

      s.answer = (_) => json(
        api.response(path, 'post', '200', 'Created'),
        headers: {'Idempotent-Replayed': 'true'},
      );
      final again = await s.repos().attendance.correct(9003, req);
      expect(again.toJson(), created);
      expect(again.replayed, isTrue);
    });

    test('GET sites/3/payouts/2026-10-31/', () async {
      final ok = api.response(
        '/sites/{site_id}/payouts/{date}/',
        'get',
        '200',
        'OpenSheet',
      );
      final s = FakeServer((_) => json(ok));
      final p = await s.repos().payout.payout(3, '2026-10-31');
      expect(s.last.url.toString(), '${base}sites/3/payouts/2026-10-31/');
      expect(p.toJson(), ok);
    });

    test('POST payments/: JSON without a file; 200 replay flagged', () async {
      final paid = api.response('/payments/', 'post', '201', 'Paid');
      final body = api.request('/payments/', 'post', 'PayWithPhotoProof');
      final s = FakeServer((_) => json(paid, status: 201));
      final req = PaymentCreateRequest.fromJson(body);
      final p = await s.repos().payout.pay(req);
      expect(s.last.url.toString(), '${base}payments/');
      expectHeaders(s.last, body: true);
      expect(jsonDecode(s.last.body), body);
      expect(p.toJson(), paid);
      expect(p.replayed, isFalse);

      s.answer = (_) =>
          json(paid, headers: {'Idempotent-Replayed': 'true'}); // 200
      final again = await s.repos().payout.pay(
        req,
        proofFilePath: '/no/such/file.jpg', // Gone from the phone: JSON.
      );
      expect(again.replayed, isTrue);
      expect(s.last.headers['Content-Type'], startsWith('application/json'));
    });

    test('POST payments/: multipart with the proof file', () async {
      final dir = Directory.systemTemp.createTempSync('polaris_http');
      final file = File('${dir.path}/receipt.jpg')
        ..writeAsBytesSync([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]);
      final paid = api.response('/payments/', 'post', '201', 'Paid');
      final body = api.request('/payments/', 'post', 'PayWithPhotoProof');
      final s = FakeServer((_) => json(paid, status: 201));
      final p = await s.repos().payout.pay(
        PaymentCreateRequest.fromJson(body),
        proofFilePath: file.path,
      );
      expect(p.toJson(), paid);
      final r = s.last;
      expect((r.method, r.url.toString()), ('POST', '${base}payments/'));
      expect(r.headers['content-type'], startsWith('multipart/form-data'));
      expect(r.headers['Authorization'], startsWith('Device '));
      expect(r.headers['Accept-Language'], 'fr');
      final parts = {for (final x in parseMultipart(r)) x.name: x};
      expect(parts.keys, {
        'client_uuid',
        'pay_line_id',
        'amount',
        'proof_type',
        'paid_at',
        'proof_file',
      });
      for (final e in body.entries) {
        expect(parts[e.key]!.text, '${e.value}', reason: e.key);
      }
      final f = parts['proof_file']!;
      expect((f.filename, f.contentType), ('receipt.jpg', 'image/jpeg'));
      expect(f.bytes, [0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]);
    });

    test('GET health/ (no token) and sync/?since= (URL-encoded +)', () async {
      final s = FakeServer((r) {
        if (r.url.path.endsWith('/health/')) return json({'status': 'ok'});
        return json(api.response('/sync/', 'get', '200', 'Changes'));
      });
      expect(await s.repos().sync.health(), isTrue);
      expect(s.last.url.toString(), '${base}health/');
      expectHeaders(s.last, auth: false);

      final sync = await s.repos().sync.sync(
        since: '2026-10-31T07:45:00+00:00',
      );
      expect(s.last.url.query, 'since=2026-10-31T07%3A45%3A00%2B00%3A00');
      expect(s.last.url.queryParameters['since'], '2026-10-31T07:45:00+00:00');
      expect(sync.toJson(), api.response('/sync/', 'get', '200', 'Changes'));
      await s.repos().sync.sync();
      expect(s.last.url.toString(), '${base}sync/');
    });

    test('every path ends with a slash; the base URL may lack one', () async {
      final s = FakeServer((_) => json({'status': 'ok'}));
      final repos = httpRepositories(
        baseUrl: 'http://10.0.2.2:8000/api/v1',
        token: () async => null,
        language: () => 'en',
        client: MockClient((r) async {
          s.requests.add(r);
          return json({'status': 'ok'});
        }),
      );
      await repos.sync.health();
      expect(s.last.url.toString(), 'http://10.0.2.2:8000/api/v1/health/');
      expect(s.last.headers['Authorization'], isNull);
    });
  });

  group('errors', skip: specSkip, () {
    Future<ApiException> apiError(
      FakeServer s,
      Future<Object?> Function(Repositories r) call,
    ) async {
      try {
        await call(s.repos());
      } on ApiException catch (e) {
        return e;
      }
      fail('No ApiException');
    }

    test('401 on any call: not_authenticated, back to the PIN', () async {
      final body = api.response('/me/', 'get', '401', 'NotAuthenticated');
      final s = FakeServer((_) => json(body, status: 401));
      final e = await apiError(s, (r) => r.attendance.day(3, '2026-10-31'));
      expect(
        (e.status, e.code, e.message),
        (401, 'not_authenticated', 'Invalid or revoked device token.'),
      );
      expect(s.unauthorized, 1);
      // Not from the PIN sign-in or the logout itself.
      final pin = api.response('/auth/pin/', 'post', '401', 'WrongPIN');
      s.answer = (_) => json(pin, status: 401);
      final wrong = await apiError(
        s,
        (r) => r.auth.signIn(
          const PinLoginRequest(phone: '+228', pin: '0000', deviceId: 'd'),
        ),
      );
      expect(wrong.attemptsLeft, 2);
      s.answer = (_) => json(body, status: 401);
      await apiError(s, (r) => r.auth.logout());
      expect(s.unauthorized, 1);
    });

    test('403, 404 (JSON) and 404 (HTML)', () async {
      final forbidden = api.response(
        '/sites/{site_id}/crew/',
        'get',
        '403',
        'PermissionDenied',
      );
      final s = FakeServer((_) => json(forbidden, status: 403));
      final e403 = await apiError(s, (r) => r.crew.crew(9));
      expect((e403.status, e403.code), (403, 'permission_denied'));
      expect(e403.error.toJson(), forbidden);

      final notFound = api.response('/payments/', 'post', '404', 'NotFound');
      s.answer = (_) => json(notFound, status: 404);
      final e404 = await apiError(s, (r) => r.payout.payout(3, '2026-10-31'));
      expect(e404.error.toJson(), notFound);

      s.answer = (_) => html(404);
      final h404 = await apiError(s, (r) => r.payout.payout(3, '2026-10-31'));
      expect((h404.status, h404.code), (404, 'not_found'));
      expect(h404.details, isEmpty);
    });

    test('400 with list-shaped item errors', () async {
      final bad = api.response(
        '/sites/{site_id}/days/{date}/records/',
        'put',
        '400',
        'WorkerNotOnTheCrew',
      );
      final s = FakeServer((_) => json(bad, status: 400));
      final e = await apiError(
        s,
        (r) => r.attendance.putRecords(3, '2026-10-31', const []),
      );
      expect(e.code, 'validation_error');
      final items = e.error.itemErrors('records')!;
      expect(items, hasLength(2));
      expect(items[0], isEmpty);
      expect(items[1]['worker_id'], isNotEmpty);
    });

    test('409 with unmarked_worker_ids; 409 already paid', () async {
      const path = '/sites/{site_id}/days/{date}/submit/';
      final s = FakeServer(
        (_) => json(
          api.response(path, 'post', '409', 'UnmarkedWorkers'),
          status: 409,
        ),
      );
      final e = await apiError(
        s,
        (r) => r.attendance.submit(
          3,
          '2026-10-31',
          const SubmitDayRequest(clientUuid: 'c'),
        ),
      );
      expect((e.status, e.code), (409, 'conflict'));
      expect(e.unmarkedWorkerIds, [58]);

      s.answer = (_) => json(
        api.response('/payments/', 'post', '409', 'AlreadyPaid'),
        status: 409,
      );
      final paid = await apiError(
        s,
        (r) => r.payout.pay(
          const PaymentCreateRequest(
            clientUuid: 'u',
            payLineId: 801,
            amount: 4000,
            proofType: ProofType.signature,
          ),
        ),
      );
      expect(paid.details['pay_line_id'], 801);
    });

    test('423 with locked_until (PIN) and status (day)', () async {
      final s = FakeServer(
        (_) => json(
          api.response('/auth/pin/', 'post', '423', 'LockedAfterWrongPINs'),
          status: 423,
        ),
      );
      final e = await apiError(
        s,
        (r) => r.auth.signIn(
          const PinLoginRequest(phone: '+228', pin: '1234', deviceId: 'd'),
        ),
      );
      expect(
        (e.status, e.code, e.lockedUntil),
        (423, 'locked', '2026-10-31T06:57:00Z'),
      );
      s.answer = (_) => json(
        api.response(
          '/sites/{site_id}/days/{date}/records/',
          'put',
          '423',
          'AlreadySubmitted',
        ),
        status: 423,
      );
      final day = await apiError(
        s,
        (r) => r.attendance.putRecords(3, '2026-10-31', const []),
      );
      expect(day.details['status'], 'submitted');
    });

    test('429: retry_after from details, else Retry-After', () async {
      final throttled = api.response('/auth/pin/', 'post', '429', 'Throttled');
      final s = FakeServer(
        (_) => json(throttled, status: 429, headers: {'Retry-After': '42'}),
      );
      final e = await apiError(s, (r) => r.auth.me());
      expect((e.code, e.retryAfterSeconds), ('throttled', 42));

      s.answer = (_) => html(429, headers: {'Retry-After': '17'});
      final proxy = await apiError(s, (r) => r.auth.me());
      expect((proxy.code, proxy.retryAfterSeconds), ('throttled', 17));
    });

    test('5xx: JSON server_error, and a proxy HTML 502', () async {
      final s = FakeServer(
        (_) => json({
          'code': 'server_error',
          'message': 'A server error occurred.',
          'details': {},
        }, status: 500),
      );
      final e = await apiError(s, (r) => r.sync.sync());
      expect((e.status, e.code), (500, 'server_error'));
      s.answer = (_) => html(502);
      final bad = await apiError(s, (r) => r.sync.sync());
      expect(
        (bad.status, bad.code, bad.message),
        (502, 'server_error', 'HTTP 502'),
      );
      s.answer = (_) => html(418);
      expect((await apiError(s, (r) => r.sync.sync())).code, 'http_error');
    });

    test('network: timeout, socket, TLS, client errors; HTML on 200', () async {
      Future<void> expectNetwork(FakeServer s, {Duration? timeout}) =>
          expectLater(
            s.repos(timeout: timeout ?? const Duration(seconds: 30)).auth.me(),
            throwsA(isA<NetworkException>()),
          );

      final never = Completer<http.Response>();
      await expectNetwork(
        FakeServer((_) => never.future),
        timeout: const Duration(milliseconds: 50),
      );
      await expectNetwork(
        FakeServer((_) => throw const SocketException('Connection refused')),
      );
      await expectNetwork(
        FakeServer((_) => throw const HandshakeException('bad certificate')),
      );
      await expectNetwork(
        FakeServer((r) => throw http.ClientException('reset', r.url)),
      );
      // A captive portal answering 200 with its own page.
      await expectNetwork(FakeServer((_) => html(200)));
    });
  });
}
