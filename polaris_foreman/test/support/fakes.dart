import 'dart:typed_data';

import 'package:polaris_foreman/core/site_clock.dart';
import 'package:polaris_foreman/data/local/memory_local_store.dart';
import 'package:polaris_foreman/data/local/token_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_backend.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_repositories.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_seed.dart';
import 'package:polaris_foreman/data/repositories/repositories.dart';
import 'package:geolocator/geolocator.dart' show LocationPermission;
import 'package:polaris_foreman/services/checkin_photo.dart';
import 'package:polaris_foreman/services/location_service.dart';
import 'package:polaris_foreman/services/proof_capture.dart';

/// Friday 31 October 2026, 07:30 in Lomé (UTC+0).
final fixedNow = DateTime.utc(2026, 10, 31, 7, 30);
const today = '2026-10-31';

/// Wraps repositories: logs every call and can fail chosen calls.
class RecordingRepos
    implements
        AuthRepository,
        CrewRepository,
        AttendanceRepository,
        PayoutRepository,
        SyncRepository,
        CheckInRepository,
        FilesRepository {
  RecordingRepos(this.inner);

  final Repositories inner;
  final List<String> log = [];

  /// Errors to throw, per method name, before calling through.
  final Map<String, List<Object>> failures = {};

  /// Called once, inside the next `putRecords`, before it returns.
  Future<void> Function()? duringPut;

  Repositories get repos => Repositories(
    auth: this,
    crew: this,
    attendance: this,
    payout: this,
    sync: this,
    checkin: this,
    files: this,
  );

  Future<T> _call<T>(String name, Future<T> Function() f) async {
    log.add(name);
    final queue = failures[name];
    if (queue != null && queue.isNotEmpty) throw queue.removeAt(0);
    return f();
  }

  @override
  Future<PinLoginResponse> signIn(PinLoginRequest r) =>
      _call('signIn', () => inner.auth.signIn(r));

  @override
  Future<Me> me() => _call('me', inner.auth.me);

  @override
  Future<void> logout() => _call('logout', inner.auth.logout);

  @override
  Future<Crew> crew(int siteId, {String? date}) =>
      _call('crew', () => inner.crew.crew(siteId, date: date));

  @override
  Future<AttendanceDayList> days(int siteId, {String? from, String? to}) =>
      _call('days', () => inner.attendance.days(siteId, from: from, to: to));

  @override
  Future<AttendanceDay> day(int siteId, String date) =>
      _call('day', () => inner.attendance.day(siteId, date));

  @override
  Future<AttendanceDay> putRecords(
    int siteId,
    String date,
    List<RecordUpsert> records,
  ) => _call('putRecords', () async {
    final out = await inner.attendance.putRecords(siteId, date, records);
    final hook = duringPut;
    duringPut = null;
    if (hook != null) await hook();
    return out;
  });

  @override
  Future<AttendanceDay> submit(
    int siteId,
    String date,
    SubmitDayRequest request,
  ) => _call('submit', () => inner.attendance.submit(siteId, date, request));

  @override
  Future<Correction> correct(int recordId, CorrectionCreateRequest request) =>
      _call('correct', () => inner.attendance.correct(recordId, request));

  @override
  Future<Payout> payout(int siteId, String date) =>
      _call('payout', () => inner.payout.payout(siteId, date));

  @override
  Future<Payment> pay(PaymentCreateRequest request, {String? proofFilePath}) =>
      _call(
        'pay',
        () => inner.payout.pay(request, proofFilePath: proofFilePath),
      );

  @override
  Future<bool> health() => _call('health', inner.sync.health);

  @override
  Future<SyncResponse> sync({String? since}) =>
      _call('sync', () => inner.sync.sync(since: since));

  @override
  Future<CheckInDevice> device() => _call('device', inner.checkin.device);

  @override
  Future<CheckInSetup> setup(int siteId) =>
      _call('setup', () => inner.checkin.setup(siteId));

  @override
  Future<CheckIn> checkIn(CheckInCreateRequest request, {String? photoPath}) =>
      _call(
        'checkIn',
        () => inner.checkin.checkIn(request, photoPath: photoPath),
      );

  @override
  Future<CheckInList> checkins(int siteId, String date) =>
      _call('checkins', () => inner.checkin.checkins(siteId, date));

  @override
  Future<RollCallSuggestions> suggestions(int siteId, String date) =>
      _call('suggestions', () => inner.checkin.suggestions(siteId, date));

  @override
  Future<FileDownload> download(String url, {String? etag}) =>
      _call('download', () => inner.files.download(url, etag: etag));
}

/// A mock server, a signed-in token, an empty phone.
class Harness {
  Harness._(this.backend, this.tokens, this.store, this.recording, this.clock);

  static Future<Harness> create({DateTime Function()? now}) async {
    final clockNow = now ?? () => fixedNow;
    final backend = MockBackend(
      latency: Duration.zero,
      approvalDelay: null,
      now: clockNow,
    );
    final tokens = MemoryTokenStore();
    final login = await backend.signIn(
      const PinLoginRequest(
        phone: MockSeed.foremanPhone,
        pin: MockSeed.foremanPin,
        deviceId: 'test-device',
      ),
    );
    await tokens.write(login.token);
    final recording = RecordingRepos(mockRepositories(backend, tokens.read));
    return Harness._(
      backend,
      tokens,
      MemoryLocalStore(),
      recording,
      SiteClock(now: clockNow, timeZone: 'Africa/Lome'),
    );
  }

  final MockBackend backend;
  final MemoryTokenStore tokens;
  final MemoryLocalStore store;
  final RecordingRepos recording;
  final SiteClock clock;

  Repositories get repos => recording.repos;
  Future<String?> token() => tokens.read();
}

class FakeProofCapture implements ProofCapture {
  int photos = 0;

  @override
  Future<String?> takePhoto() async {
    photos++;
    return '/tmp/proof-$photos.jpg';
  }

  @override
  Future<String> savePng(Uint8List bytes) async => '/tmp/signature.png';
}

/// GPS for tests: a permission answer and an optional fix.
class FakeLocationPlatform implements LocationPlatform {
  FakeLocationPlatform({
    this.enabled = true,
    this.permission = LocationPermission.whileInUse,
    this.onRequest,
    this.fix,
  });

  bool enabled;
  LocationPermission permission;

  /// What the permission prompt answers (null: same as [permission]).
  LocationPermission? onRequest;
  GpsFix? fix;
  int requests = 0;

  @override
  Future<bool> serviceEnabled() async => enabled;

  @override
  Future<LocationPermission> checkPermission() async => permission;

  @override
  Future<LocationPermission> requestPermission() async {
    requests++;
    return permission = onRequest ?? permission;
  }

  @override
  Future<GpsFix> currentPosition({required Duration timeLimit}) async {
    final f = fix;
    if (f == null) throw StateError('No fix');
    return f;
  }
}

class FakeCheckInPhoto implements CheckInPhotoCapture {
  int taken = 0;

  @override
  Future<String?> take() async => '/tmp/checkin-${++taken}.jpg';
}
