import 'package:http/http.dart' as http;

import '../../models/models.dart';
import '../repositories.dart';
import 'api_client.dart';

export 'api_client.dart' show ApiClient, ApiResponse;

/// The real API (`docs/api/openapi.yaml`, contract 1.2) over HTTP.
///
/// [baseUrl] ends with `/api/v1/`: `http://10.0.2.2:8000/api/v1/` from the
/// Android emulator, `http://127.0.0.1:8000/api/v1/` from the iOS simulator,
/// `https://<server>/api/v1/` for a real phone. Pass [client] to use another
/// `http.Client` (tests use `MockClient`).
Repositories httpRepositories({
  required String baseUrl,
  required TokenReader token,
  required String Function() language,
  http.Client? client,
  void Function()? onUnauthorized,
  Duration timeout = const Duration(seconds: 30),
}) {
  final api = HttpApi(
    ApiClient(
      baseUrl: baseUrl,
      token: token,
      language: language,
      client: client,
      timeout: timeout,
      onUnauthorized: onUnauthorized,
    ),
  );
  return Repositories(
    auth: api,
    crew: api,
    attendance: api,
    payout: api,
    sync: api,
    checkin: api,
    files: api,
  );
}

/// One class for every repository interface: each method is one endpoint.
class HttpApi
    implements
        AuthRepository,
        CrewRepository,
        AttendanceRepository,
        PayoutRepository,
        SyncRepository,
        CheckInRepository,
        FilesRepository {
  HttpApi(this.client);

  final ApiClient client;

  Future<Map<String, dynamic>> _get(
    String path, [
    Map<String, String?> query = const {},
  ]) async => (await client.send('GET', path, query: query)).json();

  // ------------------------------------------------------------------ auth

  @override
  Future<PinLoginResponse> signIn(PinLoginRequest request) async {
    final r = await client.send(
      'POST',
      'auth/pin/',
      body: request.toJson(),
      auth: false,
    );
    return PinLoginResponse.fromJson(r.json());
  }

  @override
  Future<Me> me() async => Me.fromJson(await _get('me/'));

  @override
  Future<void> logout() async {
    // A 401 here (token already revoked) is not a reason to show the PIN
    // screen from the logout itself: the caller signs out anyway.
    await client.send('POST', 'auth/logout/', notifyUnauthorized: false);
  }

  // ------------------------------------------------------------------ crew

  @override
  Future<Crew> crew(int siteId, {String? date}) async =>
      Crew.fromJson(await _get('sites/$siteId/crew/', {'date': date}));

  // ------------------------------------------------------------ attendance

  @override
  Future<AttendanceDayList> days(
    int siteId, {
    String? from,
    String? to,
  }) async => AttendanceDayList.fromJson(
    await _get('sites/$siteId/days/', {'from': from, 'to': to}),
  );

  @override
  Future<AttendanceDay> day(int siteId, String date) async =>
      AttendanceDay.fromJson(await _get('sites/$siteId/days/$date/'));

  @override
  Future<AttendanceDay> putRecords(
    int siteId,
    String date,
    List<RecordUpsert> records,
  ) async {
    final r = await client.send(
      'PUT',
      'sites/$siteId/days/$date/records/',
      body: {'records': records.map((x) => x.toJson()).toList()},
    );
    return AttendanceDay.fromJson(r.json());
  }

  @override
  Future<AttendanceDay> submit(
    int siteId,
    String date,
    SubmitDayRequest request,
  ) async {
    final r = await client.send(
      'POST',
      'sites/$siteId/days/$date/submit/',
      body: request.toJson(),
    );
    return AttendanceDay.fromJson(r.json());
  }

  @override
  Future<Correction> correct(
    int recordId,
    CorrectionCreateRequest request,
  ) async {
    final r = await client.send(
      'POST',
      'records/$recordId/corrections/',
      body: request.toJson(),
    );
    return Correction.fromJson(r.json(), replayed: r.replayed);
  }

  // ---------------------------------------------------------------- payout

  @override
  Future<Payout> payout(int siteId, String date) async =>
      Payout.fromJson(await _get('sites/$siteId/payouts/$date/'));

  /// Multipart with the proof file when there is one on the phone, JSON
  /// otherwise. A proof file that has disappeared from the phone is left
  /// out: the server answers `400` on `proof_file` if the type needs one,
  /// which the outbox shows to the foreman.
  @override
  Future<Payment> pay(
    PaymentCreateRequest request, {
    String? proofFilePath,
  }) async {
    ApiResponse? r;
    if (proofFilePath != null && proofFileExists(proofFilePath)) {
      try {
        r = await client.multipart(
          'payments/',
          fields: {
            for (final e in request.toJson().entries) e.key: '${e.value}',
          },
          fileField: 'proof_file',
          filePath: proofFilePath,
        );
      } on ProofFileMissingException {
        r = null;
      }
    }
    r ??= await client.send('POST', 'payments/', body: request.toJson());
    return Payment.fromJson(r.json(), replayed: r.replayed);
  }

  // ------------------------------------------------------------------ sync

  @override
  Future<bool> health() async {
    final r = await client.send('GET', 'health/', auth: false);
    return r.json()['status'] == 'ok';
  }

  @override
  Future<SyncResponse> sync({String? since}) async =>
      SyncResponse.fromJson(await _get('sync/', {'since': since}));

  // --------------------------------------------------------------- checkin

  @override
  Future<CheckInDevice> device() async =>
      CheckInDevice.fromJson(await _get('checkin/device/'));

  @override
  Future<CheckInSetup> setup(int siteId) async =>
      CheckInSetup.fromJson(await _get('sites/$siteId/checkin-setup/'));

  /// Multipart with the photo when it is still on the phone, JSON otherwise
  /// (the photo is optional: a check-in whose photo is gone is still sent).
  @override
  Future<CheckIn> checkIn(
    CheckInCreateRequest request, {
    String? photoPath,
  }) async {
    ApiResponse? r;
    if (photoPath != null && proofFileExists(photoPath)) {
      try {
        r = await client.multipart(
          'checkins/',
          fields: {
            for (final e in request.toJson().entries) e.key: '${e.value}',
          },
          fileField: 'photo',
          filePath: photoPath,
        );
      } on ProofFileMissingException {
        r = null;
      }
    }
    r ??= await client.send('POST', 'checkins/', body: request.toJson());
    return CheckIn.fromJson(r.json(), replayed: r.replayed);
  }

  @override
  Future<CheckInList> checkins(int siteId, String date) async =>
      CheckInList.fromJson(await _get('sites/$siteId/checkins/$date/'));

  @override
  Future<RollCallSuggestions> suggestions(int siteId, String date) async =>
      RollCallSuggestions.fromJson(
        await _get('sites/$siteId/days/$date/suggestions/'),
      );

  // ----------------------------------------------------------------- files

  @override
  Future<FileDownload> download(String url, {String? etag}) async {
    final r = await client.download(url, etag: etag);
    if (r.status == 304) {
      return FileDownload.notModified(etag: r.headers['etag'] ?? etag);
    }
    return FileDownload.ok(
      r.bytes,
      etag: r.headers['etag'],
      contentType: r.headers['content-type'],
    );
  }
}
