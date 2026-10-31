import 'dart:io' show File, IOException;

import '../../models/models.dart';
import '../repositories.dart';
import 'mock_backend.dart';

/// Repositories backed by [MockBackend]. Responses are built from the same
/// model classes the real client parses, so they have the contract's shapes.
Repositories mockRepositories(MockBackend backend, TokenReader token) {
  return Repositories(
    auth: _MockAuth(backend, token),
    crew: _MockCrew(backend, token),
    attendance: _MockAttendance(backend, token),
    payout: _MockPayout(backend, token),
    sync: _MockSync(backend, token),
    checkin: _MockCheckIn(backend, token),
    files: _MockFiles(backend, token),
  );
}

class _MockAuth implements AuthRepository {
  _MockAuth(this.b, this.token);
  final MockBackend b;
  final TokenReader token;

  @override
  Future<PinLoginResponse> signIn(PinLoginRequest request) => b.signIn(request);

  @override
  Future<Me> me() async => b.me(await token());

  @override
  Future<void> logout() async => b.logout(await token());
}

class _MockCrew implements CrewRepository {
  _MockCrew(this.b, this.token);
  final MockBackend b;
  final TokenReader token;

  @override
  Future<Crew> crew(int siteId, {String? date}) async =>
      b.crew(await token(), siteId, date);
}

class _MockAttendance implements AttendanceRepository {
  _MockAttendance(this.b, this.token);
  final MockBackend b;
  final TokenReader token;

  @override
  Future<AttendanceDayList> days(
    int siteId, {
    String? from,
    String? to,
  }) async => b.days(await token(), siteId, from, to);

  @override
  Future<AttendanceDay> day(int siteId, String date) async =>
      b.day(await token(), siteId, date);

  @override
  Future<AttendanceDay> putRecords(
    int siteId,
    String date,
    List<RecordUpsert> records,
  ) async => b.putRecords(await token(), siteId, date, records);

  @override
  Future<AttendanceDay> submit(
    int siteId,
    String date,
    SubmitDayRequest request,
  ) async => b.submit(await token(), siteId, date, request);

  @override
  Future<Correction> correct(
    int recordId,
    CorrectionCreateRequest request,
  ) async => b.correct(await token(), recordId, request);
}

class _MockPayout implements PayoutRepository {
  _MockPayout(this.b, this.token);
  final MockBackend b;
  final TokenReader token;

  @override
  Future<Payout> payout(int siteId, String date) async =>
      b.payout(await token(), siteId, date);

  @override
  Future<Payment> pay(
    PaymentCreateRequest request, {
    String? proofFilePath,
  }) async => b.pay(await token(), request, proofFilePath);
}

class _MockSync implements SyncRepository {
  _MockSync(this.b, this.token);
  final MockBackend b;
  final TokenReader token;

  @override
  Future<bool> health() => b.health();

  @override
  Future<SyncResponse> sync({String? since}) async =>
      b.sync(await token(), since);
}

class _MockCheckIn implements CheckInRepository {
  _MockCheckIn(this.b, this.token);
  final MockBackend b;
  final TokenReader token;

  @override
  Future<CheckInDevice> device() async => b.checkInDevice(await token());

  @override
  Future<CheckInSetup> setup(int siteId) async =>
      b.checkInSetup(await token(), siteId);

  /// Like the HTTP client: the photo goes with the check-in while the file
  /// is on the phone; a photo that is gone is left out.
  @override
  Future<CheckIn> checkIn(
    CheckInCreateRequest request, {
    String? photoPath,
  }) async {
    List<int>? photo;
    if (photoPath != null) {
      try {
        final f = File(photoPath);
        if (f.existsSync()) photo = await f.readAsBytes();
      } on IOException {
        photo = null;
      }
    }
    return b.checkIn(await token(), request, photo: photo);
  }

  @override
  Future<CheckInList> checkins(int siteId, String date) async =>
      b.siteCheckIns(await token(), siteId, date);

  @override
  Future<RollCallSuggestions> suggestions(int siteId, String date) async =>
      b.suggestions(await token(), siteId, date);
}

class _MockFiles implements FilesRepository {
  _MockFiles(this.b, this.token);
  final MockBackend b;
  final TokenReader token;

  @override
  Future<FileDownload> download(String url, {String? etag}) async =>
      b.download(await token(), url, etag);
}
