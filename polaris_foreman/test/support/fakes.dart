import 'dart:typed_data';

import 'package:polaris_foreman/core/site_clock.dart';
import 'package:polaris_foreman/data/local/memory_local_store.dart';
import 'package:polaris_foreman/data/local/token_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_backend.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_repositories.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_seed.dart';
import 'package:polaris_foreman/data/repositories/repositories.dart';
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
        SyncRepository {
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
