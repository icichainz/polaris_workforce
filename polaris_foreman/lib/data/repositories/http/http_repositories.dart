import '../../models/models.dart';
import '../repositories.dart';

/// Placeholder for the real API client (FL2).
///
/// FL2 replaces these stubs with an HTTP client (dio per the plan) that:
/// - prefixes [baseUrl] (`https://<server>/api/v1/`, every path ends with `/`);
/// - sends `Authorization: Device <token>` (from [token]) and
///   `Accept-Language: en|fr`;
/// - parses error bodies with [ApiError.fromJson] into [ApiException], and
///   maps socket errors and timeouts to [NetworkException];
/// - treats any 404 (also Django's plain HTML 404) as `not_found`;
/// - sends `POST /payments/` as multipart when there is a proof file.
Repositories httpRepositories({
  required String baseUrl,
  required TokenReader token,
  required String Function() language,
}) {
  final stub = _HttpStub(baseUrl);
  return Repositories(
    auth: stub,
    crew: stub,
    attendance: stub,
    payout: stub,
    sync: stub,
  );
}

class _HttpStub
    implements
        AuthRepository,
        CrewRepository,
        AttendanceRepository,
        PayoutRepository,
        SyncRepository {
  _HttpStub(this.baseUrl);

  final String baseUrl;

  Never _todo(String endpoint) =>
      throw UnimplementedError('FL2: $endpoint against $baseUrl');

  @override
  Future<PinLoginResponse> signIn(PinLoginRequest request) =>
      _todo('POST /auth/pin/');

  @override
  Future<Me> me() => _todo('GET /me/');

  @override
  Future<void> logout() => _todo('POST /auth/logout/');

  @override
  Future<Crew> crew(int siteId, {String? date}) =>
      _todo('GET /sites/$siteId/crew/');

  @override
  Future<AttendanceDayList> days(int siteId, {String? from, String? to}) =>
      _todo('GET /sites/$siteId/days/');

  @override
  Future<AttendanceDay> day(int siteId, String date) =>
      _todo('GET /sites/$siteId/days/$date/');

  @override
  Future<AttendanceDay> putRecords(
    int siteId,
    String date,
    List<RecordUpsert> records,
  ) => _todo('PUT /sites/$siteId/days/$date/records/');

  @override
  Future<AttendanceDay> submit(
    int siteId,
    String date,
    SubmitDayRequest request,
  ) => _todo('POST /sites/$siteId/days/$date/submit/');

  @override
  Future<Correction> correct(int recordId, CorrectionCreateRequest request) =>
      _todo('POST /records/$recordId/corrections/');

  @override
  Future<Payout> payout(int siteId, String date) =>
      _todo('GET /sites/$siteId/payouts/$date/');

  @override
  Future<Payment> pay(PaymentCreateRequest request, {String? proofFilePath}) =>
      _todo('POST /payments/');

  @override
  Future<bool> health() => _todo('GET /health/');

  @override
  Future<SyncResponse> sync({String? since}) => _todo('GET /sync/');
}
