import '../data/models/models.dart';
import '../data/repositories/mock/mock_backend.dart';
import '../data/repositories/repositories.dart' show TokenReader;
import 'tracking_api.dart';
import 'tracking_models.dart';

/// Tracking against the in-memory server (`USE_MOCK_API` builds): the first
/// token request asks for consent; after an acceptance, tokens cover the
/// foreman's sites for 12 hours, tracking hours 06:00–19:00, a fix a
/// minute. Follows the mock's offline switch (long-press the sync banner).
class MockTrackingRepository implements TrackingRepository {
  MockTrackingRepository(this.backend, this.deviceToken);

  final MockBackend backend;
  final TokenReader deviceToken;

  /// Null: never answered; then the last answer.
  bool? accepted;
  final Map<String, ConsentResult> _answers = {};
  int _issued = 0;

  @override
  Future<TrackingToken> token() async {
    // Same checks as every mock call (offline, device token), and the sites.
    final me = await backend.me(await deviceToken());
    if (accepted == null) {
      throw const ApiException(
        403,
        ApiError(
          code: TrackingErrors.consentRequired,
          message: 'Accept the tracking notice first.',
        ),
      );
    }
    if (accepted == false) {
      throw const ApiException(
        403,
        ApiError(
          code: TrackingErrors.trackingDisabled,
          message: 'Tracking is not enabled for you.',
        ),
      );
    }
    _issued++;
    return TrackingToken(
      token: 'mock-tracker-token-$_issued',
      expiresAt: DateTime.now().toUtc().add(const Duration(hours: 12)),
      trackerUrl: 'https://tracker.mock.invalid',
      sites: [for (final s in me.sites) s.id],
      gpsIntervalSeconds: 60,
      hours: TrackingHours(const TimeOfDay24(6, 0), const TimeOfDay24(19, 0)),
    );
  }

  @override
  Future<ConsentResult> consent(ConsentRequest request) async {
    await backend.me(await deviceToken());
    final replay = _answers[request.clientUuid];
    if (replay != null) return replay;
    accepted = request.accepted;
    return _answers[request.clientUuid] = ConsentResult(
      accepted: request.accepted,
      at: DateTime.now().toUtc().toIso8601String(),
      textVersion: request.textVersion,
    );
  }
}

/// A tracker that accepts everything (duplicates counted by `client_uuid`),
/// offline when the mock server is.
class MockTrackerTransport implements TrackerTransport {
  MockTrackerTransport(this.backend);

  final MockBackend backend;
  final Set<String> received = {};

  @override
  Future<IngestResult> postObservations({
    required String trackerUrl,
    required String token,
    required List<Map<String, dynamic>> observations,
  }) async {
    if (backend.offline) throw const NetworkException('Mock offline');
    var accepted = 0, duplicates = 0;
    for (final o in observations) {
      if (received.add(o['client_uuid'] as String)) {
        accepted++;
      } else {
        duplicates++;
      }
    }
    return IngestResult(accepted: accepted, duplicates: duplicates, late: 0);
  }
}
