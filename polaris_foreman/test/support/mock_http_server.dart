// The mock server behind HTTP: a `MockClient` handler that routes requests to
// a [MockBackend] the way Django routes them (paths with a trailing slash,
// `Authorization: Device <token>`, JSON or multipart bodies, `{code, message,
// details}` errors, 201 vs 200 + `Idempotent-Replayed`). The HTTP
// repositories running against it must behave exactly like the mock
// repositories (test/repository_suite_test.dart).
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_backend.dart';

const mockBaseUrl = 'http://mock.test/api/v1/';

/// One request and its answer, with the contract path it matched.
class Exchange {
  Exchange(this.method, this.template, this.request, this.status, this.body);

  final String method;

  /// The path in `openapi.yaml` without `/api/v1`, e.g. `/sync/`. Null for
  /// an unknown URL.
  final String? template;
  final http.Request request;
  final int status;

  /// Decoded JSON body (null for 204).
  final Object? body;
}

/// A part of a `multipart/form-data` body.
class FormPart {
  FormPart(this.name, this.bytes, {this.filename, this.contentType});
  final String name;
  final Uint8List bytes;
  final String? filename;
  final String? contentType;

  String get text => utf8.decode(bytes);
}

/// Splits a multipart body (enough for what `http.MultipartRequest` sends).
List<FormPart> parseMultipart(http.Request request) {
  final type = request.headers['content-type'] ?? '';
  final boundary = RegExp(r'boundary=(.+)$').firstMatch(type)?.group(1);
  if (boundary == null) throw const FormatException('No boundary');
  final body = latin1.decode(request.bodyBytes);
  final parts = <FormPart>[];
  for (final chunk in body.split('--$boundary')) {
    if (chunk.isEmpty || chunk.startsWith('--')) continue;
    final split = chunk.indexOf('\r\n\r\n');
    if (split < 0) continue;
    final head = chunk.substring(0, split);
    var content = chunk.substring(split + 4);
    if (content.endsWith('\r\n')) {
      content = content.substring(0, content.length - 2);
    }
    final name = RegExp(r'name="([^"]*)"').firstMatch(head)?.group(1);
    if (name == null) continue;
    parts.add(
      FormPart(
        name,
        Uint8List.fromList(latin1.encode(content)),
        filename: RegExp(r'filename="([^"]*)"').firstMatch(head)?.group(1),
        contentType: RegExp(
          r'content-type:\s*(\S+)',
          caseSensitive: false,
        ).firstMatch(head)?.group(1),
      ),
    );
  }
  return parts;
}

typedef _Handler =
    Future<_Reply> Function(List<String> args, http.Request req, String? token);

class _Reply {
  const _Reply(this.status, this.body, [this.headers = const {}]);
  final int status;
  final Object? body;
  final Map<String, String> headers;
}

class _Route {
  _Route(this.method, this.template, String pattern, this.handler)
    : regex = RegExp('^$pattern\$');
  final String method;
  final String template;
  final RegExp regex;
  final _Handler handler;
}

const _date = r'(\d{4}-\d{2}-\d{2})';

Map<String, dynamic> _json(http.Request r) =>
    jsonDecode(r.body) as Map<String, dynamic>;

/// A `MockClient` serving [backend]. [onExchange] sees every answer.
MockClient mockHttpClient(
  MockBackend backend, {
  void Function(Exchange)? onExchange,
}) {
  final routes = <_Route>[
    _Route(
      'POST',
      '/auth/pin/',
      'auth/pin/',
      (a, r, t) async => _Reply(
        200,
        (await backend.signIn(PinLoginRequest.fromJson(_json(r)))).toJson(),
      ),
    ),
    _Route('POST', '/auth/logout/', 'auth/logout/', (a, r, t) async {
      await backend.logout(t);
      return const _Reply(204, null);
    }),
    _Route(
      'GET',
      '/me/',
      'me/',
      (a, r, t) async => _Reply(200, (await backend.me(t)).toJson()),
    ),
    _Route('GET', '/health/', 'health/', (a, r, t) async {
      await backend.health();
      return const _Reply(200, {'status': 'ok'});
    }),
    _Route(
      'GET',
      '/sites/{site_id}/crew/',
      r'sites/(\d+)/crew/',
      (a, r, t) async => _Reply(
        200,
        (await backend.crew(
          t,
          int.parse(a[0]),
          r.url.queryParameters['date'],
        )).toJson(),
      ),
    ),
    _Route(
      'GET',
      '/sites/{site_id}/days/',
      r'sites/(\d+)/days/',
      (a, r, t) async => _Reply(
        200,
        (await backend.days(
          t,
          int.parse(a[0]),
          r.url.queryParameters['from'],
          r.url.queryParameters['to'],
        )).toJson(),
      ),
    ),
    _Route(
      'GET',
      '/sites/{site_id}/days/{date}/',
      'sites/(\\d+)/days/$_date/',
      (a, r, t) async =>
          _Reply(200, (await backend.day(t, int.parse(a[0]), a[1])).toJson()),
    ),
    _Route(
      'PUT',
      '/sites/{site_id}/days/{date}/records/',
      'sites/(\\d+)/days/$_date/records/',
      (a, r, t) async {
        final items = (_json(r)['records'] as List)
            .map((e) => RecordUpsert.fromJson(e as Map<String, dynamic>))
            .toList();
        final day = await backend.putRecords(t, int.parse(a[0]), a[1], items);
        return _Reply(200, day.toJson());
      },
    ),
    _Route(
      'POST',
      '/sites/{site_id}/days/{date}/submit/',
      'sites/(\\d+)/days/$_date/submit/',
      (a, r, t) async => _Reply(
        200,
        (await backend.submit(
          t,
          int.parse(a[0]),
          a[1],
          SubmitDayRequest.fromJson(_json(r)),
        )).toJson(),
      ),
    ),
    _Route(
      'POST',
      '/records/{record_id}/corrections/',
      r'records/(\d+)/corrections/',
      (a, r, t) async {
        final c = await backend.correct(
          t,
          int.parse(a[0]),
          CorrectionCreateRequest.fromJson(_json(r)),
        );
        return c.replayed
            ? _Reply(200, c.toJson(), const {'idempotent-replayed': 'true'})
            : _Reply(201, c.toJson());
      },
    ),
    _Route(
      'GET',
      '/sites/{site_id}/payouts/{date}/',
      'sites/(\\d+)/payouts/$_date/',
      (a, r, t) async => _Reply(
        200,
        (await backend.payout(t, int.parse(a[0]), a[1])).toJson(),
      ),
    ),
    _Route('POST', '/payments/', 'payments/', (a, r, t) async {
      final PaymentCreateRequest req;
      String? file;
      if ((r.headers['content-type'] ?? '').startsWith('multipart/')) {
        final parts = parseMultipart(r);
        String? field(String n) =>
            parts.where((p) => p.name == n).firstOrNull?.text;
        final upload = parts.where((p) => p.name == 'proof_file').firstOrNull;
        if (upload != null) file = 'upload:${upload.filename}';
        req = PaymentCreateRequest(
          clientUuid: field('client_uuid')!,
          payLineId: int.parse(field('pay_line_id')!),
          amount: int.parse(field('amount')!),
          proofType: ProofType.fromWire(field('proof_type')!),
          paidAt: field('paid_at'),
        );
      } else {
        req = PaymentCreateRequest.fromJson(_json(r));
      }
      final p = await backend.pay(t, req, file);
      return p.replayed
          ? _Reply(200, p.toJson(), const {'idempotent-replayed': 'true'})
          : _Reply(201, p.toJson());
    }),
    _Route(
      'GET',
      '/sync/',
      'sync/',
      (a, r, t) async => _Reply(
        200,
        (await backend.sync(t, r.url.queryParameters['since'])).toJson(),
      ),
    ),
  ];

  _Reply error(int status, String code, String message) => _Reply(status, {
    'code': code,
    'message': message,
    'details': <String, dynamic>{},
  });

  return MockClient((request) async {
    const prefix = '/api/v1/';
    final path = request.url.path;
    String? template;
    _Reply reply;
    if (!path.startsWith(prefix)) {
      reply = const _Reply(404, '<html><body>Not Found</body></html>');
    } else if (!path.endsWith('/')) {
      // Django's APPEND_SLASH.
      reply = _Reply(301, '', {'location': '$path/'});
    } else {
      final rest = path.substring(prefix.length);
      final matching = routes.where((r) => r.regex.hasMatch(rest)).toList();
      final route = matching.where((r) => r.method == request.method);
      if (matching.isEmpty) {
        reply = error(404, ErrorCodes.notFound, 'Not found.');
      } else if (route.isEmpty) {
        reply = error(
          405,
          ErrorCodes.methodNotAllowed,
          'Method "${request.method}" not allowed.',
        );
      } else {
        final r = route.first;
        template = r.template;
        final auth = request.headers['authorization'];
        final token = auth != null && auth.startsWith('Device ')
            ? auth.substring(7)
            : null;
        final lang = request.headers['accept-language'];
        if (lang == 'en' || lang == 'fr') backend.language = lang!;
        final m = r.regex.firstMatch(rest)!;
        final args = [for (var i = 1; i <= m.groupCount; i++) m.group(i)!];
        try {
          reply = await r.handler(args, request, token);
        } on ApiException catch (e) {
          final retry = e.retryAfterSeconds;
          reply = _Reply(e.statusCode, e.error.toJson(), {
            if (retry != null) 'retry-after': '$retry',
          });
        } on NetworkException catch (e) {
          throw http.ClientException(e.message, request.url);
        } on FormatException catch (e) {
          reply = _Reply(400, {
            'code': ErrorCodes.validationError,
            'message': 'Invalid input.',
            'details': {
              'non_field_errors': [e.message],
            },
          });
        }
      }
    }
    final body = reply.body;
    onExchange?.call(
      Exchange(request.method, template, request, reply.status, body),
    );
    final text = body == null
        ? ''
        : body is String
        ? body
        : jsonEncode(body);
    return http.Response.bytes(
      utf8.encode(text),
      reply.status,
      headers: {
        if (body is Map) 'content-type': 'application/json',
        if (body is String && body.isNotEmpty) 'content-type': 'text/html',
        ...reply.headers,
      },
      request: request,
    );
  });
}
