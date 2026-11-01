import 'dart:async';

import 'package:polaris_foreman/core/site_clock.dart';
import 'package:polaris_foreman/data/local/memory_local_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/tracking/observations.dart';
import 'package:polaris_foreman/tracking/sensors.dart';
import 'package:polaris_foreman/tracking/tracking_api.dart';
import 'package:polaris_foreman/tracking/tracking_client.dart';
import 'package:polaris_foreman/tracking/tracking_models.dart';
import 'package:polaris_foreman/tracking/tracking_token_store.dart';

/// Friday 31 October 2026, 07:30 in Lomé (UTC+0), as in `test/support`.
final trackingNow = DateTime.utc(2026, 10, 31, 7, 30);

TrackingToken tokenFor({
  String token = 'tracker-jwt-1',
  DateTime? expiresAt,
  List<int> sites = const [1],
  String start = '06:00',
  String end = '19:00',
  int interval = 60,
}) => TrackingToken(
  token: token,
  expiresAt: expiresAt ?? trackingNow.add(const Duration(hours: 12)),
  trackerUrl: 'https://tracker.test',
  sites: sites,
  gpsIntervalSeconds: interval,
  hours: TrackingHours(TimeOfDay24.parse(start), TimeOfDay24.parse(end)),
);

ApiException apiError(int status, String code) =>
    ApiException(status, ApiError(code: code, message: code));

final consentRequired = apiError(403, TrackingErrors.consentRequired);
final trackingDisabled = apiError(403, TrackingErrors.trackingDisabled);

/// Django's tracking endpoints: answers queued per call, then [fallback].
class FakeTrackingRepository implements TrackingRepository {
  FakeTrackingRepository({this.fallback});

  /// Each item is a [TrackingToken] to return or an error to throw.
  final List<Object> answers = [];
  Object? fallback;
  int tokenCalls = 0;
  final List<ConsentRequest> consents = [];
  Object? consentError;

  @override
  Future<TrackingToken> token() async {
    tokenCalls++;
    final a = answers.isNotEmpty ? answers.removeAt(0) : fallback;
    if (a is TrackingToken) return a;
    if (a == null) throw const NetworkException('no answer configured');
    throw a;
  }

  @override
  Future<ConsentResult> consent(ConsentRequest request) async {
    final e = consentError;
    if (e != null) throw e;
    consents.add(request);
    return ConsentResult(
      accepted: request.accepted,
      at: '2026-10-31T07:30:00Z',
      textVersion: request.textVersion,
    );
  }
}

/// The tracker: [handler] decides each answer (default: accept all,
/// remembering `client_uuid`s to count duplicates).
class FakeTrackerTransport implements TrackerTransport {
  FakeTrackerTransport({this.handler});

  Future<IngestResult> Function(List<Map<String, dynamic>> batch, String token)?
  handler;
  final List<List<Map<String, dynamic>>> batches = [];
  final List<String> tokens = [];
  final Set<String> received = {};

  /// Airplane mode: every call fails with [NetworkException].
  bool offline = false;
  int calls = 0;

  @override
  Future<IngestResult> postObservations({
    required String trackerUrl,
    required String token,
    required List<Map<String, dynamic>> observations,
  }) async {
    calls++;
    if (offline) throw const NetworkException('airplane mode');
    batches.add(observations);
    tokens.add(token);
    final h = handler;
    if (h != null) return h(observations, token);
    var accepted = 0, duplicates = 0;
    for (final o in observations) {
      received.add(o['client_uuid'] as String) ? accepted++ : duplicates++;
    }
    return IngestResult(accepted: accepted, duplicates: duplicates, late: 0);
  }
}

class FakeLocationSource implements TrackingLocationSource {
  FakeLocationSource({this.answer = LocationAccess.ready});

  LocationAccess answer;
  int requests = 0;
  Duration? interval;
  BackgroundNotice? notice;
  StreamController<TrackingFix>? _fixes;

  bool get listening => _fixes != null;

  @override
  Future<LocationAccess> access({bool request = false}) async {
    if (request) requests++;
    return answer;
  }

  @override
  Stream<TrackingFix> positions({
    required Duration interval,
    required BackgroundNotice notice,
  }) {
    this.interval = interval;
    this.notice = notice;
    late StreamController<TrackingFix> c;
    c = StreamController<TrackingFix>(
      onCancel: () {
        if (identical(_fixes, c)) _fixes = null;
      },
    );
    _fixes = c;
    return c.stream;
  }

  void add(TrackingFix fix) => _fixes!.add(fix);
}

class FakeBadgeScanner implements BadgeScanner {
  StreamController<BadgeSighting>? _c;
  int starts = 0;

  bool get listening => _c != null;

  @override
  Stream<BadgeSighting> sightings() {
    starts++;
    late StreamController<BadgeSighting> c;
    c = StreamController<BadgeSighting>(
      onCancel: () {
        if (identical(_c, c)) _c = null;
      },
    );
    _c = c;
    return c.stream;
  }

  void see(String badgeId, {int rssi = -70, required DateTime at}) =>
      _c!.add(BadgeSighting(badgeId: badgeId, rssi: rssi, at: at));
}

TrackingFix fixAt(
  DateTime at, {
  double lat = 6.132,
  double lng = 1.2228,
  double accuracy = 12,
  bool mocked = false,
}) => TrackingFix(
  latitude: lat,
  longitude: lng,
  accuracyM: accuracy,
  at: at,
  isMocked: mocked,
);

/// Lets queued microtasks and zero timers run.
Future<void> settle() async {
  for (var i = 0; i < 30; i++) {
    await Future<void>.delayed(Duration.zero);
  }
}

/// A [TrackingClient] on fakes, with a movable clock.
class TrackingRig {
  TrackingRig({
    FakeTrackingRepository? api,
    DateTime? now,
    Duration uploadDelay = const Duration(hours: 1),
  }) : api = api ?? FakeTrackingRepository(fallback: tokenFor()),
       now = now ?? trackingNow {
    client = TrackingClient(
      api: this.api,
      tokens: tokens,
      observations: store,
      store: store,
      transport: transport,
      location: location,
      scanner: scanner,
      clock: () => SiteClock(now: () => this.now, timeZone: 'Africa/Lome'),
      now: () => this.now,
      // By default, sends only when the test says so.
      uploadDelay: uploadDelay,
      kickSync: () => kicks++,
    );
  }

  final FakeTrackingRepository api;
  final tokens = MemoryTrackingTokenStore();
  final store = MemoryLocalStore();
  final transport = FakeTrackerTransport();
  final location = FakeLocationSource();
  final scanner = FakeBadgeScanner();
  DateTime now;
  int kicks = 0;
  late final TrackingClient client;

  TrackingState get state => client.state;

  Future<void> signIn([String owner = '7']) async {
    await client.signedIn(owner);
    await settle();
  }

  Future<int> queued([String owner = '7']) => store.countObservations(owner);

  void dispose() => client.dispose();
}
