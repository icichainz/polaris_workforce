import 'dart:async';
import 'dart:convert';
import 'dart:io' show File, IOException;
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:http_parser/http_parser.dart' show MediaType;

import '../../models/models.dart';
import '../repositories.dart';

/// A successful answer: status, headers (lower-case names) and the body.
class ApiResponse {
  const ApiResponse(this.status, this.headers, this.bytes);

  final int status;
  final Map<String, String> headers;
  final Uint8List bytes;

  /// `Idempotent-Replayed: true` (a create repeated with the same
  /// `client_uuid`: the server returned the original object).
  bool get replayed => headers['idempotent-replayed']?.toLowerCase() == 'true';

  /// The body as a JSON object. A body that is not the API's JSON (a captive
  /// portal's HTML page on a "successful" status, for example) means the
  /// server was not really reached: [NetworkException], so writes are kept
  /// and retried.
  Map<String, dynamic> json() {
    final decoded = _decode(bytes);
    if (decoded is Map<String, dynamic>) return decoded;
    throw const NetworkException('Unexpected response (not the API)');
  }
}

Object? _decode(Uint8List bytes) {
  if (bytes.isEmpty) return null;
  try {
    return jsonDecode(utf8.decode(bytes));
  } on FormatException {
    return null;
  }
}

/// The transport under the HTTP repositories: base URL, headers, timeouts
/// and the mapping of failures to [ApiException] / [NetworkException].
///
/// * Paths are relative to [baseUri] and end with a slash (`me/`,
///   `sites/3/crew/`); the base ends with `/api/v1/`.
/// * Every call sends `Accept: application/json` and `Accept-Language`; calls
///   with [auth] send `Authorization: Device <token>`.
/// * An error status with the API's `{code, message, details}` body becomes
///   `ApiException(status, ApiError)`, with `Retry-After` kept for 429. Any
///   other error body (a proxy's HTML page) becomes an [ApiException] whose
///   code comes from the status (`not_found` for 404, `server_error` for
///   5xx, …; see [ErrorCodes.forStatus]).
/// * Socket errors, TLS handshake failures, timeouts and client exceptions
///   become [NetworkException].
/// * A 401 on an authenticated call also calls [onUnauthorized] (the app
///   goes back to the PIN screen; the outbox is kept).
class ApiClient {
  ApiClient({
    required String baseUrl,
    required this.token,
    required this.language,
    http.Client? client,
    this.timeout = const Duration(seconds: 30),
    this.uploadTimeout = const Duration(minutes: 3),
    this.onUnauthorized,
  }) : baseUri = Uri.parse(baseUrl.endsWith('/') ? baseUrl : '$baseUrl/'),
       _client = client ?? http.Client();

  final Uri baseUri;
  final TokenReader token;

  /// `en` or `fr`: the app's current language (translated `message`s and
  /// `worked` labels).
  final String Function() language;

  /// For each request: until the headers arrive, then until the body is read.
  final Duration timeout;

  /// For uploads (`POST /payments/` with a proof file, `POST /checkins/`
  /// with a photo) and file downloads (the network may be ~15 KB/s).
  final Duration uploadTimeout;

  void Function()? onUnauthorized;

  final http.Client _client;

  void close() => _client.close();

  Uri uri(String path, [Map<String, String?> query = const {}]) {
    assert(!path.startsWith('/'), 'Paths are relative to the base URL');
    assert(path.endsWith('/'), 'Every API path ends with a slash: $path');
    final u = baseUri.resolve(path);
    final q = {
      for (final e in query.entries)
        if (e.value != null) e.key: e.value!,
    };
    return q.isEmpty ? u : u.replace(queryParameters: q);
  }

  Future<Map<String, String>> _headers({required bool auth}) async {
    final headers = {
      'Accept': 'application/json',
      'Accept-Language': language(),
    };
    if (auth) {
      final t = await token();
      if (t != null) headers['Authorization'] = 'Device $t';
    }
    return headers;
  }

  /// A JSON request ([body] is encoded when not null).
  Future<ApiResponse> send(
    String method,
    String path, {
    Map<String, String?> query = const {},
    Object? body,
    bool auth = true,
    bool notifyUnauthorized = true,
  }) async {
    final request = http.Request(method, uri(path, query))
      ..headers.addAll(await _headers(auth: auth));
    if (body != null) {
      request.headers['Content-Type'] = 'application/json; charset=utf-8';
      request.bodyBytes = utf8.encode(jsonEncode(body));
    }
    return _run(
      request,
      timeout,
      notifyUnauthorized: auth && notifyUnauthorized,
    );
  }

  /// A `multipart/form-data` POST with text [fields] and one file.
  Future<ApiResponse> multipart(
    String path, {
    required Map<String, String> fields,
    required String fileField,
    required String filePath,
  }) async {
    final request = http.MultipartRequest('POST', uri(path))
      ..headers.addAll(await _headers(auth: true))
      ..fields.addAll(fields);
    try {
      request.files.add(
        await http.MultipartFile.fromPath(
          fileField,
          filePath,
          contentType: mediaTypeFor(filePath),
        ),
      );
    } on IOException {
      throw ProofFileMissingException(filePath);
    }
    return _run(request, uploadTimeout, notifyUnauthorized: true);
  }

  /// `GET` of a protected file (`photo_url`, `proof_url`): [url] comes from
  /// a response (absolute; a relative one is resolved against [baseUri]).
  ///
  /// The `Authorization: Device <token>` header goes only to the API's own
  /// origin, never to another host. With [etag], sends `If-None-Match`; a
  /// `304 Not Modified` is returned as a response (no body), not an error.
  Future<ApiResponse> download(String url, {String? etag}) async {
    final target = baseUri.resolve(url);
    final sameOrigin =
        target.scheme == baseUri.scheme &&
        target.host == baseUri.host &&
        target.port == baseUri.port;
    final headers = await _headers(auth: sameOrigin);
    headers['Accept'] = 'image/*, application/pdf, application/json';
    final request = http.Request('GET', target)..headers.addAll(headers);
    if (etag != null) request.headers['If-None-Match'] = etag;
    return _run(
      request,
      uploadTimeout,
      notifyUnauthorized: sameOrigin,
      notModifiedOk: true,
    );
  }

  Future<ApiResponse> _run(
    http.BaseRequest request,
    Duration limit, {
    required bool notifyUnauthorized,
    bool notModifiedOk = false,
  }) async {
    final ApiResponse response;
    try {
      final streamed = await _client.send(request).timeout(limit);
      final bytes = await streamed.stream.toBytes().timeout(limit);
      response = ApiResponse(streamed.statusCode, {
        for (final h in streamed.headers.entries) h.key.toLowerCase(): h.value,
      }, bytes);
    } on TimeoutException {
      throw NetworkException(
        'Timed out: ${request.method} ${request.url.path}',
      );
    } on IOException catch (e) {
      // SocketException, HandshakeException/TlsException, HttpException.
      throw NetworkException('${e.runtimeType}: $e');
    } on http.ClientException catch (e) {
      throw NetworkException('ClientException: ${e.message}');
    }
    if (response.status >= 200 && response.status < 300) return response;
    if (notModifiedOk && response.status == 304) return response;
    final error = errorFrom(response);
    if (error.statusCode == 401 && notifyUnauthorized) {
      onUnauthorized?.call();
    }
    throw error;
  }

  /// Maps an error response to an [ApiException] (see the class comment).
  static ApiException errorFrom(ApiResponse r) {
    final retryAfter = int.tryParse(r.headers['retry-after']?.trim() ?? '');
    final body = _decode(r.bytes);
    if (body is Map<String, dynamic> && body['code'] is String) {
      final details = body['details'];
      return ApiException(
        r.status,
        ApiError(
          code: body['code'] as String,
          message: (body['message'] as String?) ?? '',
          details: details is Map<String, dynamic> ? details : const {},
        ),
        retryAfterHeader: retryAfter,
      );
    }
    return ApiException(
      r.status,
      ApiError(
        code: ErrorCodes.forStatus(r.status),
        message: 'HTTP ${r.status}',
      ),
      retryAfterHeader: retryAfter,
    );
  }

  /// Content type of an uploaded file from its extension (jpg, jpeg, png,
  /// webp, pdf are what the server accepts).
  static MediaType mediaTypeFor(String path) {
    final dot = path.lastIndexOf('.');
    final ext = dot < 0 ? '' : path.substring(dot + 1).toLowerCase();
    return switch (ext) {
      'jpg' || 'jpeg' => MediaType('image', 'jpeg'),
      'png' => MediaType('image', 'png'),
      'webp' => MediaType('image', 'webp'),
      'pdf' => MediaType('application', 'pdf'),
      _ => MediaType('application', 'octet-stream'),
    };
  }
}

/// The proof file of a queued payment is no longer on the phone.
class ProofFileMissingException implements Exception {
  const ProofFileMissingException(this.path);
  final String path;

  @override
  String toString() => 'ProofFileMissingException($path)';
}

/// Whether [path] names a file that exists (proof files live in the app's
/// documents directory and may be cleared by the OS or the user).
bool proofFileExists(String path) {
  try {
    return File(path).existsSync();
  } on IOException {
    return false;
  }
}
