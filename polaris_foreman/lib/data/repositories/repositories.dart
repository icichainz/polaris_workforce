import '../models/models.dart';

/// Reads the device token for `Authorization: Device <token>`.
typedef TokenReader = Future<String?> Function();

/// Remote side of the app: one method per endpoint of `docs/api/openapi.yaml`.
///
/// Implementations throw [ApiException] for error responses and
/// [NetworkException] when the server cannot be reached. The UI never calls
/// these directly for writes: writes go through the local store and the outbox
/// (see `SyncService`).

abstract interface class AuthRepository {
  /// `POST /auth/pin/`. Wrong PIN: 401 with `attempts_left`; locked: 423.
  Future<PinLoginResponse> signIn(PinLoginRequest request);

  /// `GET /me/`.
  Future<Me> me();

  /// `POST /auth/logout/`.
  Future<void> logout();
}

abstract interface class CrewRepository {
  /// `GET /sites/{siteId}/crew/?date=`.
  Future<Crew> crew(int siteId, {String? date});
}

abstract interface class AttendanceRepository {
  /// `GET /sites/{siteId}/days/?from=&to=`.
  Future<AttendanceDayList> days(int siteId, {String? from, String? to});

  /// `GET /sites/{siteId}/days/{date}/`.
  Future<AttendanceDay> day(int siteId, String date);

  /// `PUT /sites/{siteId}/days/{date}/records/` (at most 200 records).
  Future<AttendanceDay> putRecords(
    int siteId,
    String date,
    List<RecordUpsert> records,
  );

  /// `POST /sites/{siteId}/days/{date}/submit/`.
  Future<AttendanceDay> submit(
    int siteId,
    String date,
    SubmitDayRequest request,
  );

  /// `POST /records/{recordId}/corrections/`.
  Future<Correction> correct(int recordId, CorrectionCreateRequest request);
}

abstract interface class PayoutRepository {
  /// `GET /sites/{siteId}/payouts/{date}/`.
  Future<Payout> payout(int siteId, String date);

  /// `POST /payments/` (multipart when [proofFilePath] is set).
  Future<Payment> pay(PaymentCreateRequest request, {String? proofFilePath});
}

abstract interface class SyncRepository {
  /// `GET /health/`: true when the server answers.
  Future<bool> health();

  /// `GET /sync/?since=`.
  Future<SyncResponse> sync({String? since});
}

/// Check-in (contract 1.2): badges, kiosk phones, check-ins, suggestions.
abstract interface class CheckInRepository {
  /// `GET /checkin/device/`: attendance mode and kiosk role of this phone.
  Future<CheckInDevice> device();

  /// `GET /sites/{siteId}/checkin-setup/`: mode, `late_after`, geofence and
  /// the badge hashes of today's crew.
  Future<CheckInSetup> setup(int siteId);

  /// `POST /checkins/` (multipart when [photoPath] names a file on the
  /// phone). `201` the first time; `200` with [CheckIn.replayed] for a
  /// repeated `client_uuid`.
  Future<CheckIn> checkIn(CheckInCreateRequest request, {String? photoPath});

  /// `GET /sites/{siteId}/checkins/{date}/`, oldest first.
  Future<CheckInList> checkins(int siteId, String date);

  /// `GET /sites/{siteId}/days/{date}/suggestions/`.
  Future<RollCallSuggestions> suggestions(int siteId, String date);
}

/// Protected downloads under `/files/` (`photo_url`, `proof_url`): sent with
/// the `Authorization` header and revalidated with `If-None-Match`.
abstract interface class FilesRepository {
  /// [url] is an absolute URL from a response. With [etag], a `304` answers
  /// [FileDownload.notModified].
  Future<FileDownload> download(String url, {String? etag});
}

/// The set of repositories the app runs against (mock or real).
class Repositories {
  const Repositories({
    required this.auth,
    required this.crew,
    required this.attendance,
    required this.payout,
    required this.sync,
    required this.checkin,
    required this.files,
  });

  final AuthRepository auth;
  final CrewRepository crew;
  final AttendanceRepository attendance;
  final PayoutRepository payout;
  final SyncRepository sync;
  final CheckInRepository checkin;
  final FilesRepository files;
}
