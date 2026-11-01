import 'dart:async';
import 'dart:convert';
import 'dart:io' show IOException;

import 'package:http/http.dart' as http;

import '../data/models/models.dart';
import '../data/repositories/http/api_client.dart';
import 'tracking_models.dart';

/// The Django side of tracking (`docs/api/presence-django-openapi.yaml`),
/// with the phone's device token. Pure Dart (used by `tool/live_smoke.dart`).
abstract interface class TrackingRepository {
  /// `POST /tracking/token/`. `403 consent_required` until the foreman
  /// accepted the notice; `403 tracking_disabled` when tracking is off.
  Future<TrackingToken> token();

  /// `POST /tracking/consent/`, idempotent by `client_uuid`.
  Future<ConsentResult> consent(ConsentRequest request);
}

/// [TrackingRepository] over the app's [ApiClient] (same base URL, device
/// token, language, timeouts and error mapping as every other call; a 401
/// sends the foreman back to the PIN screen like any other call).
class HttpTrackingApi implements TrackingRepository {
  HttpTrackingApi(this.client);

  final ApiClient client;

  @override
  Future<TrackingToken> token() async {
    final r = await client.send('POST', 'tracking/token/', body: const {});
    return TrackingToken.fromJson(r.json());
  }

  @override
  Future<ConsentResult> consent(ConsentRequest request) async {
    final r = await client.send(
      'POST',
      'tracking/consent/',
      body: request.toJson(),
    );
    return ConsentResult.fromJson(r.json());
  }
}

/// Used when nothing else is configured (and in tests): tracking is off.
class DisabledTrackingRepository implements TrackingRepository {
  const DisabledTrackingRepository();

  static const _off = ApiException(
    403,
    ApiError(
      code: TrackingErrors.trackingDisabled,
      message: 'Tracking is not enabled for you.',
    ),
  );

  @override
  Future<TrackingToken> token() async => throw _off;

  @override
  Future<ConsentResult> consent(ConsentRequest request) async => throw _off;
}

/// Sends observations to the tracker service (`docs/api/tracker-openapi.yaml`).
abstract interface class TrackerTransport {
  /// `POST {trackerUrl}/v1/observations` with the token as
  /// `Authorization: Bearer` header. Throws [ApiException] for an error status and
  /// [NetworkException] when the tracker cannot be reached.
  Future<IngestResult> postObservations({
    required String trackerUrl,
    required String token,
    required List<Map<String, dynamic>> observations,
  });
}

/// [TrackerTransport] over HTTP. The tracker uses the same error shape as
/// the Django API, so failures map the same way ([ApiClient.errorFrom]).
class HttpTrackerTransport implements TrackerTransport {
  HttpTrackerTransport({
    http.Client? client,
    this.timeout = const Duration(seconds: 60),
  }) : _client = client ?? http.Client();

  final http.Client _client;

  /// A full batch is ~80 KB: at ~15 KB/s that is several seconds.
  final Duration timeout;

  static Uri observationsUri(String trackerUrl) {
    final base = trackerUrl.endsWith('/') ? trackerUrl : '$trackerUrl/';
    return Uri.parse(base).resolve('v1/observations');
  }

  @override
  Future<IngestResult> postObservations({
    required String trackerUrl,
    required String token,
    required List<Map<String, dynamic>> observations,
  }) async {
    final request = http.Request('POST', observationsUri(trackerUrl))
      ..headers.addAll({
        'Accept': 'application/json',
        'Content-Type': 'application/json; charset=utf-8',
        'Authorization': 'Bearer $token',
      })
      ..bodyBytes = utf8.encode(jsonEncode({'observations': observations}));
    final ApiResponse response;
    try {
      final streamed = await _client.send(request).timeout(timeout);
      final bytes = await streamed.stream.toBytes().timeout(timeout);
      response = ApiResponse(streamed.statusCode, {
        for (final h in streamed.headers.entries) h.key.toLowerCase(): h.value,
      }, bytes);
    } on TimeoutException {
      throw const NetworkException('Timed out: POST /v1/observations');
    } on IOException catch (e) {
      // SocketException, HandshakeException/TlsException, HttpException.
      throw NetworkException('${e.runtimeType}: $e');
    } on http.ClientException catch (e) {
      throw NetworkException('ClientException: ${e.message}');
    }
    if (response.status >= 200 && response.status < 300) {
      return IngestResult.fromJson(response.json());
    }
    throw ApiClient.errorFrom(response);
  }
}
