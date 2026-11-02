import 'dart:async';
import 'dart:convert';

import 'package:crypto/crypto.dart';

import '../../../core/checkin_rules.dart';
import '../../../core/dates.dart';
import '../../models/models.dart';
import 'mock_seed.dart';

/// An in-memory stand-in for the Django API (contract 1.2), following the
/// rules of `docs/api/README.md`: idempotent writes by `client_uuid` (replays
/// flagged like `Idempotent-Replayed`), `{code, message, details}` errors with
/// list-shaped item errors, `not_started` days, records with `site_id` and
/// `date`, payment proof rules, payout invariants, sync by `since`, and the
/// check-in rules of 1.2 (badges, flags, kiosk restriction, `self_check_in`
/// drafts, suggestions; see `core/checkin_rules.dart`).
///
/// Debug switches: [offline] (every call fails with [NetworkException]),
/// [failNext] (queue errors for the next calls), [revokeToken] (401),
/// [approvalDelay] (the "site manager" approves a submitted day after it),
/// [attendanceMode], [setKiosk], [revokeBadge], [setWorkerPhoto].
class MockBackend {
  MockBackend({
    this.latency = const Duration(milliseconds: 350),
    this.approvalDelay = const Duration(seconds: 12),
    DateTime Function()? now,
  }) : _now = now ?? DateTime.now {
    _seed();
  }

  Duration latency;
  Duration? approvalDelay;
  final DateTime Function() _now;

  /// Simulates no network at all.
  bool offline = false;

  /// Errors thrown, one per call, before anything else happens.
  final List<Exception> failNext = [];

  String language = 'en';

  // --- Auth state
  bool _revoked = false;
  int _attemptsLeft = 5;
  DateTime? _lockedUntil;
  int _tokenSeq = 0;

  // --- Data
  final Map<int, List<SeedWorker>> _crews = {
    MockSeed.siteId: MockSeed.workers,
    MockSeed.site2.id: MockSeed.site2Workers,
  };
  final Map<String, AttendanceDaySummary> _days = {};
  final Map<String, Map<int, AttendanceRecord>> _records = {};
  final Map<String, String> _submitUuids = {};
  final Map<String, Correction> _corrections = {};
  final Map<String, Payment> _payments = {};
  final Map<String, List<PayoutLine>> _lines = {};
  final Map<String, int> _cashIssued = {};
  final List<Timer> _timers = [];
  int _dayId = 300;
  int _recordId = 9500;
  int _correctionId = 100;
  int _paymentId = 3400;

  // --- Check-in (1.2)
  /// Base of the protected file URLs in responses (`photo_url`).
  String publicBaseUrl = 'https://polaris.example.com/api/v1/';

  /// The company setting (`GET /checkin/device/`, setup, suggestions).
  AttendanceMode attendanceMode = AttendanceMode.both;

  /// `late_after` per site (the site's, else the company's).
  final Map<int, String> lateAfter = {MockSeed.siteId: '07:15'};
  String companyLateAfter = '07:30';

  /// The sites' circles (the second site has none).
  final Map<int, Geofence> geofences = {
    MockSeed.siteId: const Geofence(
      latitude: 6.131944,
      longitude: 1.222778,
      radiusM: 150,
    ),
  };
  int? _kioskSiteId;
  String? _kioskSince;
  final Map<String, _MockBadge> _badges = {};
  final Map<String, _MockCheckIn> _checkins = {};
  final Map<String, Map<int, Map<String, String?>>> _checkinWrote = {};
  final Map<int, _MockFile> _workerPhotos = {};
  int _checkinId = 5120;

  String get today => isoDate(_now().toUtc()); // Africa/Lome is UTC+0.
  String get _ts => isoTimestamp(_now());
  String _key(int siteId, String date) => '$siteId|$date';

  int callCount = 0;

  void dispose() {
    for (final t in _timers) {
      t.cancel();
    }
  }

  // ---------------------------------------------------------------- helpers

  Future<void> _call({bool auth = true, String? token}) async {
    callCount++;
    if (latency > Duration.zero) await Future<void>.delayed(latency);
    if (offline) throw const NetworkException('Mock offline');
    if (failNext.isNotEmpty) throw failNext.removeAt(0);
    // Any token this mock issued stays valid across app restarts until revoked.
    if (auth &&
        (token == null || !token.startsWith('mock-token-') || _revoked)) {
      throw _err(
        401,
        ErrorCodes.notAuthenticated,
        'Invalid or revoked device token.',
      );
    }
  }

  ApiException _err(
    int status,
    String code,
    String message, [
    Map<String, dynamic> details = const {},
  ]) => ApiException(
    status,
    ApiError(code: code, message: message, details: details),
  );

  void _checkSite(int siteId) {
    if (!_crews.containsKey(siteId)) {
      throw _err(
        403,
        ErrorCodes.permissionDenied,
        'You do not have permission to perform this action.',
      );
    }
  }

  void revokeToken() => _revoked = true;

  AttendanceDaySummary _summary(int siteId, String date) {
    final existing = _days[_key(siteId, date)];
    final counts = _counts(siteId, date);
    if (existing != null) return existing.copyWith(counts: counts);
    return AttendanceDaySummary(
      id: null,
      siteId: siteId,
      date: date,
      status: DayStatus.notStarted,
      foremanNote: '',
      submittedAt: null,
      submittedBy: null,
      approvedAt: null,
      approvedBy: null,
      rejectionNote: '',
      counts: counts,
      payoutStatus: PayoutStatus.notOpen,
      updatedAt: null,
    );
  }

  DayCounts _counts(int siteId, String date) {
    final recs = _records[_key(siteId, date)];
    final expected = _crews[siteId]!.length;
    final stored = _days[_key(siteId, date)]?.counts;
    if ((recs == null || recs.isEmpty) && stored != null) return stored;
    return DayCounts.of(expected, (recs ?? {}).values.map((r) => r.status));
  }

  AttendanceDay _day(int siteId, String date) {
    final recs = (_records[_key(siteId, date)] ?? {}).values.toList()
      ..sort((a, b) => a.id.compareTo(b.id));
    return AttendanceDay(summary: _summary(siteId, date), records: recs);
  }

  void _seed() {
    // One printed badge per worker; Kodjo Atsu (58) lost his first one.
    for (final w in [...MockSeed.workers, ...MockSeed.site2Workers]) {
      _issueBadge(w.id);
    }
    revokeBadge(58);
    // Previous days: yesterday approved and paid, older days locked.
    final t = today;
    const onSite = [40, 39, 41, 42, 40, 38];
    for (var i = 1; i <= 6; i++) {
      final date = addDays(t, -i);
      final present = onSite[i - 1];
      final ts = isoTimestamp(
        parseIsoDate(date).add(const Duration(hours: 7, minutes: 42)),
      );
      final approved = isoTimestamp(
        parseIsoDate(date).add(const Duration(hours: 16, minutes: 10)),
      );
      _days[_key(MockSeed.siteId, date)] = AttendanceDaySummary(
        id: 200 + 10 - i,
        siteId: MockSeed.siteId,
        date: date,
        status: i == 1 ? DayStatus.approved : DayStatus.locked,
        foremanNote: '',
        submittedAt: ts,
        submittedBy: MockSeed.foreman,
        approvedAt: approved,
        approvedBy: MockSeed.manager,
        rejectionNote: '',
        counts: DayCounts(
          expected: 42,
          present: present,
          absent: 42 - present,
          halfDay: 0,
          late: 0,
          unmarked: 0,
        ),
        payoutStatus: i == 1 ? PayoutStatus.paid : PayoutStatus.closed,
        updatedAt: approved,
      );
    }
  }

  // ------------------------------------------------------------------- auth

  Future<PinLoginResponse> signIn(PinLoginRequest req) async {
    await _call(auth: false);
    final now = _now();
    // Unknown phone (or no PIN, or a role without the phone app): the same
    // plain 401 for all, and no attempt is counted.
    if (req.phone.replaceAll(' ', '') != MockSeed.foremanPhone) {
      throw _err(
        401,
        ErrorCodes.notAuthenticated,
        'Wrong phone number or PIN.',
      );
    }
    if (_lockedUntil != null) {
      if (now.isBefore(_lockedUntil!)) {
        throw _err(
          423,
          ErrorCodes.locked,
          'Too many wrong PINs. Try again later.',
          {'locked_until': isoTimestamp(_lockedUntil!)},
        );
      }
      _lockedUntil = null; // The lockout is over: count again.
      _attemptsLeft = 5;
    }
    if (req.pin != MockSeed.foremanPin) {
      _attemptsLeft--;
      if (_attemptsLeft <= 0) {
        // The 5th wrong PIN: still 401, with the lock time.
        _lockedUntil = now.add(const Duration(minutes: 15));
        throw _err(401, ErrorCodes.notAuthenticated, 'Wrong PIN.', {
          'attempts_left': 0,
          'locked_until': isoTimestamp(_lockedUntil!),
        });
      }
      throw _err(401, ErrorCodes.notAuthenticated, 'Wrong PIN.', {
        'attempts_left': _attemptsLeft,
      });
    }
    _attemptsLeft = 5;
    _revoked = false;
    final token = 'mock-token-${++_tokenSeq}';
    final me = MockSeed.me();
    return PinLoginResponse(
      token: token,
      device: Device(
        id: 12,
        deviceId: req.deviceId,
        name: req.deviceName ?? '',
      ),
      user: UserProfile(
        id: me.id,
        name: me.name,
        role: me.role,
        phone: me.phone,
        language: me.language,
      ),
      sites: me.sites,
    );
  }

  Future<Me> me(String? token) async {
    await _call(token: token);
    return MockSeed.me();
  }

  Future<void> logout(String? token) async {
    await _call(token: token);
    _revoked = true;
  }

  Future<bool> health() async {
    await _call(auth: false);
    return true;
  }

  // ------------------------------------------------------------------- crew

  Future<Crew> crew(String? token, int siteId, String? date) async {
    await _call(token: token);
    _checkSite(siteId);
    final workers = [
      for (final w in _crews[siteId]!) _withPhoto(MockSeed.crewWorker(w)),
    ]..sort((a, b) => a.lastName.compareTo(b.lastName));
    return Crew(
      siteId: siteId,
      date: date ?? today,
      count: workers.length,
      workers: workers,
    );
  }

  // ------------------------------------------------------------- attendance

  Future<AttendanceDayList> days(
    String? token,
    int siteId,
    String? from,
    String? to,
  ) async {
    await _call(token: token);
    _checkSite(siteId);
    final end = to ?? today;
    final start = from ?? addDays(end, -6);
    final out = <AttendanceDaySummary>[];
    for (var d = end; d.compareTo(start) >= 0; d = addDays(d, -1)) {
      out.add(_summary(siteId, d));
    }
    return AttendanceDayList(siteId: siteId, days: out);
  }

  Future<AttendanceDay> day(String? token, int siteId, String date) async {
    await _call(token: token);
    _checkSite(siteId);
    return _day(siteId, date);
  }

  /// Per site and day (`"$siteId|$date"`), the sources a changed record was
  /// last written from (`from_suggestion`, 1.5), as the server audits them.
  final Map<String, Map<int, List<SuggestionSource>>> suggestionAudit = {};

  Future<AttendanceDay> putRecords(
    String? token,
    int siteId,
    String date,
    List<RecordUpsert> batch,
  ) async {
    await _call(token: token);
    _checkSite(siteId);
    if (batch.length > 200) {
      // An error on the field itself, not on an item.
      throw _err(400, ErrorCodes.validationError, 'Invalid input.', {
        'records': {
          'non_field_errors': [
            'Ensure this field has no more than 200 elements.',
          ],
        },
      });
    }
    final summary = _summary(siteId, date);
    final crewIds = _crews[siteId]!.map((w) => w.id).toSet();
    final errors = <Map<String, dynamic>>[];
    for (final r in batch) {
      errors.add(
        crewIds.contains(r.workerId)
            ? {}
            : {
                'worker_id': [
                  'This worker is not on the crew of this site that day.',
                ],
              },
      );
    }
    if (errors.any((e) => e.isNotEmpty)) {
      // A JSON list, one entry per item (`{}` without errors), like the
      // server's `upsert_records`.
      throw _err(400, ErrorCodes.validationError, 'Invalid input.', {
        'records': errors,
      });
    }
    final key = _key(siteId, date);
    // A client_uuid already used for another worker or another day.
    for (final r in batch) {
      for (final e in _records.entries) {
        for (final x in e.value.values) {
          if (x.clientUuid == r.clientUuid &&
              (e.key != key || x.workerId != r.workerId)) {
            throw _err(
              409,
              ErrorCodes.conflict,
              'This client_uuid was already used for another record.',
              {'client_uuid': r.clientUuid},
            );
          }
        }
      }
    }
    // A batch that changes nothing writes nothing, whatever the day's status
    // (a replay); one that changes something needs an editable day.
    final current = _records[key] ?? const <int, AttendanceRecord>{};
    final changes = batch.any((r) {
      final x =
          current.values
              .where((x) => x.clientUuid == r.clientUuid)
              .firstOrNull ??
          current[r.workerId];
      return x == null ||
          x.status != r.status ||
          x.arrival != r.arrival ||
          x.departure != r.departure ||
          x.note != r.note;
    });
    if (!summary.status.editable) {
      if (!changes) return _day(siteId, date);
      throw _err(
        423,
        ErrorCodes.locked,
        summary.status == DayStatus.locked
            ? 'This day is locked and cannot be changed.'
            : 'This day was already submitted: change records with a '
                  'correction.',
        {'status': summary.status.wire},
      );
    }
    final dayId = summary.id ?? ++_dayId;
    final recs = _records.putIfAbsent(key, () => {});
    final ts = _ts;
    var changed = false;
    for (final r in batch) {
      final existing =
          recs.values.where((x) => x.clientUuid == r.clientUuid).firstOrNull ??
          recs[r.workerId];
      if (existing == null) {
        recs[r.workerId] = AttendanceRecord(
          id: ++_recordId,
          clientUuid: r.clientUuid,
          dayId: dayId,
          siteId: siteId,
          date: date,
          workerId: r.workerId,
          status: r.status,
          arrival: r.arrival,
          departure: r.departure,
          note: r.note,
          corrected: false,
          updatedAt: ts,
        );
        changed = true;
      } else if (existing.status != r.status ||
          existing.arrival != r.arrival ||
          existing.departure != r.departure ||
          existing.note != r.note) {
        // Keeps the server's client_uuid: the phone adopts it.
        recs[r.workerId] = existing.copyWith(
          status: r.status,
          arrival: () => r.arrival,
          departure: () => r.departure,
          note: r.note,
          updatedAt: ts,
        );
        changed = true;
      } else {
        continue; // Unchanged: no audit entry.
      }
      // The server's audit entry (`after.from_suggestion`), per changed record.
      final audit = suggestionAudit.putIfAbsent(key, () => {});
      if (r.fromSuggestion.isEmpty) {
        audit.remove(r.workerId);
      } else {
        audit[r.workerId] = r.fromSuggestion;
      }
    }
    if (changed || summary.id == null || summary.status == DayStatus.rejected) {
      _days[key] = summary.copyWith(
        id: dayId,
        status: DayStatus.draft,
        updatedAt: ts,
        counts: _counts(siteId, date),
      );
    }
    return _day(siteId, date);
  }

  Future<AttendanceDay> submit(
    String? token,
    int siteId,
    String date,
    SubmitDayRequest req,
  ) async {
    await _call(token: token);
    _checkSite(siteId);
    final key = _key(siteId, date);
    final summary = _summary(siteId, date);
    if (_submitUuids[key] == req.clientUuid) return _day(siteId, date);
    if (summary.status == DayStatus.locked) {
      throw _err(
        423,
        ErrorCodes.locked,
        'This day is locked and cannot be changed.',
        {'status': 'locked'},
      );
    }
    if (summary.status == DayStatus.submitted ||
        summary.status == DayStatus.approved) {
      throw _err(409, ErrorCodes.conflict, 'This day was already submitted.', {
        'status': summary.status.wire,
      });
    }
    final recs = _records[key] ?? {};
    final unmarked = _crews[siteId]!
        .map((w) => w.id)
        .where((id) => !recs.containsKey(id))
        .toList();
    if (unmarked.isNotEmpty) {
      throw _err(
        409,
        ErrorCodes.conflict,
        'Mark every worker on the crew before submitting.',
        {'unmarked_worker_ids': unmarked},
      );
    }
    _submitUuids[key] = req.clientUuid;
    _days[key] = summary.copyWith(
      status: DayStatus.submitted,
      foremanNote: req.foremanNote,
      submittedAt: _ts,
      submittedBy: MockSeed.foreman,
      updatedAt: _ts,
    );
    _scheduleApproval(siteId, date);
    return _day(siteId, date);
  }

  void _scheduleApproval(int siteId, String date) {
    final delay = approvalDelay;
    if (delay == null) return;
    _timers.add(Timer(delay, () => approve(siteId, date)));
  }

  /// What the site manager does in the office.
  void approve(int siteId, String date) {
    final key = _key(siteId, date);
    final s = _days[key];
    if (s == null || s.status != DayStatus.submitted) return;
    _days[key] = s.copyWith(
      status: DayStatus.approved,
      approvedAt: _ts,
      approvedBy: MockSeed.manager,
      payoutStatus: PayoutStatus.open,
      updatedAt: _ts,
    );
    _buildLines(siteId, date);
  }

  void reject(int siteId, String date, String note) {
    final key = _key(siteId, date);
    final s = _days[key];
    if (s == null || s.status != DayStatus.submitted) return;
    _submitUuids.remove(key);
    _days[key] = s.copyWith(
      status: DayStatus.rejected,
      rejectionNote: note,
      updatedAt: _ts,
    );
  }

  Future<Correction> correct(
    String? token,
    int recordId,
    CorrectionCreateRequest req,
  ) async {
    await _call(token: token);
    final seen = _corrections[req.clientUuid];
    if (seen != null) {
      if (seen.recordId != recordId) {
        throw _err(
          409,
          ErrorCodes.conflict,
          'This client_uuid was already used for a different request.',
          {'client_uuid': req.clientUuid},
        );
      }
      return seen.asReplayed(); // 200 + Idempotent-Replayed: true.
    }
    String? key;
    AttendanceRecord? rec;
    for (final e in _records.entries) {
      for (final r in e.value.values) {
        if (r.id == recordId) {
          key = e.key;
          rec = r;
        }
      }
    }
    if (rec == null || key == null) {
      throw _err(404, ErrorCodes.notFound, 'Not found.');
    }
    final day = _days[key]!;
    if (day.status == DayStatus.locked) {
      throw _err(
        423,
        ErrorCodes.locked,
        'This resource is locked and cannot be changed.',
        {'status': 'locked'},
      );
    }
    if (day.status.editable) {
      throw _err(
        409,
        ErrorCodes.conflict,
        'The day is not submitted: change the records instead.',
        {'status': day.status.wire},
      );
    }
    final ts = _ts;
    String? old;
    AttendanceRecord updated;
    switch (req.field) {
      case CorrectableField.status:
        old = rec.status.wire;
        updated = rec.copyWith(
          status: AttendanceStatus.fromWire(req.newValue!),
        );
      case CorrectableField.arrival:
        old = rec.arrival;
        updated = rec.copyWith(arrival: () => req.newValue);
      case CorrectableField.departure:
        old = rec.departure;
        updated = rec.copyWith(departure: () => req.newValue);
      case CorrectableField.note:
        old = rec.note;
        updated = rec.copyWith(note: req.newValue ?? '');
    }
    updated = updated.copyWith(corrected: true, updatedAt: ts);
    _records[key]![rec.workerId] = updated;
    if (day.status == DayStatus.approved) {
      // An approved day goes back to the site manager.
      _days[key] = day.copyWith(
        status: DayStatus.submitted,
        payoutStatus: PayoutStatus.notOpen,
        updatedAt: ts,
      );
      final parts = key.split('|');
      _lines.remove(key);
      _scheduleApproval(int.parse(parts[0]), parts[1]);
    } else {
      _days[key] = day.copyWith(updatedAt: ts);
    }
    final c = Correction(
      id: ++_correctionId,
      clientUuid: req.clientUuid,
      recordId: recordId,
      field: req.field,
      oldValue: old,
      newValue: req.newValue,
      reason: req.reason,
      createdBy: MockSeed.foreman,
      createdAt: ts,
      record: updated,
    );
    _corrections[req.clientUuid] = c;
    return c;
  }

  // ----------------------------------------------------------------- payout

  SeedWorker _seedWorker(int siteId, int workerId) =>
      _crews[siteId]!.firstWhere((w) => w.id == workerId);

  List<PayoutLine> _computeLines(int siteId, String date) {
    final recs = (_records[_key(siteId, date)] ?? {}).values.toList()
      ..sort((a, b) => a.workerId.compareTo(b.workerId));
    final fr = language == 'fr';
    final dayId = _days[_key(siteId, date)]?.id ?? 0;
    final out = <PayoutLine>[];
    for (final r in recs) {
      if (r.status == AttendanceStatus.absent) continue;
      final w = _seedWorker(siteId, r.workerId);
      final half = r.status == AttendanceStatus.halfDay;
      final hourly = w.payType == PayType.hourly;
      // Units as a decimal string; gross in integer maths (hundredths).
      final hundredths = hourly ? (half ? 400 : 800) : (half ? 50 : 100);
      final units =
          '${hundredths ~/ 100}.${(hundredths % 100).toString().padLeft(2, '0')}';
      final gross = w.rate * hundredths ~/ 100;
      final deduction = w.advance > gross ~/ 2 ? gross ~/ 2 : w.advance;
      final worked = hourly
          ? '${hundredths ~/ 100} h'
          : half
          ? (fr ? '½ journée' : '½ day')
          : r.status == AttendanceStatus.late
          ? (fr ? '1 jour · retard' : '1 day · late')
          : (fr ? '1 jour' : '1 day');
      out.add(
        PayoutLine(
          payLineId: dayId * 1000 + w.id,
          worker: PayoutWorker(
            id: w.id,
            name: '${w.firstName} ${w.lastName}',
            trade: w.trade,
            photoUrl: null,
          ),
          worked: worked,
          units: units,
          gross: gross,
          advanceDeduction: deduction,
          net: gross - deduction,
          payment: null,
        ),
      );
    }
    return out;
  }

  void _buildLines(int siteId, String date) {
    final key = _key(siteId, date);
    final lines = _computeLines(siteId, date);
    _lines[key] = lines;
    final total = lines.fold<int>(0, (s, l) => s + l.net);
    // The office hands over the total rounded up to the next 5 000.
    _cashIssued[key] = ((total + 4999) ~/ 5000) * 5000;
  }

  Payout _payout(int siteId, String date) {
    final key = _key(siteId, date);
    final summary = _summary(siteId, date);
    final lines = _lines[key] ?? _computeLines(siteId, date);
    final total = lines.fold<int>(0, (s, l) => s + l.net);
    return Payout.fromLines(
      siteId: siteId,
      date: date,
      status: summary.payoutStatus,
      dayStatus: summary.status,
      approvedAt: summary.approvedAt,
      currency: Currency.xof,
      cashIssued: _cashIssued[key] ?? ((total + 4999) ~/ 5000) * 5000,
      lines: lines,
    );
  }

  Future<Payout> payout(String? token, int siteId, String date) async {
    await _call(token: token);
    _checkSite(siteId);
    return _payout(siteId, date);
  }

  /// Proof rules of `POST /payments/` (`company.payments`).
  PaymentRules paymentRules = MockSeed.payments;

  Future<Payment> pay(
    String? token,
    PaymentCreateRequest req,
    String? proofFilePath,
  ) async {
    await _call(token: token);
    final seen = _payments[req.clientUuid];
    if (seen != null) {
      if (seen.payLineId != req.payLineId) {
        throw _err(
          409,
          ErrorCodes.conflict,
          'This client_uuid was already used for a different request.',
          {'client_uuid': req.clientUuid},
        );
      }
      return seen.asReplayed(); // 200 + Idempotent-Replayed: true.
    }
    if (!paymentRules.accepts(req.proofType)) {
      throw _err(400, ErrorCodes.validationError, 'Invalid input.', {
        'proof_type': ['This proof type is not accepted.'],
      });
    }
    if (paymentRules.needsFile(req.proofType) && proofFilePath == null) {
      throw _err(400, ErrorCodes.validationError, 'Invalid input.', {
        'proof_file': ['Attach the photo of the worker receiving the cash.'],
      });
    }
    for (final entry in _lines.entries) {
      final idx = entry.value.indexWhere((l) => l.payLineId == req.payLineId);
      if (idx < 0) continue;
      final day = _days[entry.key]!;
      if (day.payoutStatus == PayoutStatus.closed ||
          day.status == DayStatus.locked) {
        throw _err(423, ErrorCodes.locked, 'This pay period is locked.', {
          'status': 'locked',
        });
      }
      if (day.payoutStatus == PayoutStatus.notOpen) {
        throw _notOpen();
      }
      final line = entry.value[idx];
      if (line.payment != null) {
        throw _err(
          409,
          ErrorCodes.conflict,
          'This worker has already been paid.',
          {'pay_line_id': line.payLineId},
        );
      }
      if (req.amount != line.net) {
        throw _err(400, ErrorCodes.validationError, 'Invalid input.', {
          'amount': ['The amount must be the net left to pay: ${line.net}.'],
        });
      }
      final payment = Payment(
        id: ++_paymentId,
        clientUuid: req.clientUuid,
        payLineId: req.payLineId,
        amount: req.amount,
        currency: Currency.xof,
        proofType: req.proofType,
        proofUrl: proofFilePath == null
            ? null
            : 'https://polaris.example.com/media/proofs/$_paymentId.jpg',
        paidAt: req.paidAt ?? _ts,
        paidBy: MockSeed.foreman,
      );
      _payments[req.clientUuid] = payment;
      entry.value[idx] = line.withPayment(payment.summary);
      if (entry.value.every((l) => l.payment != null)) {
        _days[entry.key] = day.copyWith(
          payoutStatus: PayoutStatus.paid,
          updatedAt: _ts,
        );
      }
      return payment;
    }
    // A line of a sheet that is still a preview (roll call not approved).
    for (final d in _days.values) {
      if (_computeLines(
        d.siteId,
        d.date,
      ).any((l) => l.payLineId == req.payLineId)) {
        throw _notOpen();
      }
    }
    throw _err(404, ErrorCodes.notFound, 'Not found.');
  }

  ApiException _notOpen() => _err(
    409,
    ErrorCodes.conflict,
    'The cash sheet is not open: the roll call is not approved yet.',
    {'status': 'not_open'},
  );

  // ---------------------------------------------------------------- checkin

  static const _base32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

  /// The contract's example badge (worker 63, Delali Agbo).
  static const exampleBadge = 'PW1:K7Q2M4XR5TBN3WCA.D6HZP2LQ4V7NYE3S';

  static String _b32(List<int> bytes) =>
      bytes.map((b) => _base32[b % 32]).join();

  /// `PW1:<public id>.<signature>`, 37 characters of QR alphanumeric mode.
  static String _payload(int workerId, int version) {
    if (workerId == 63 && version == 1) return exampleBadge;
    final d = sha256
        .convert(utf8.encode('mock-badge-$workerId-$version'))
        .bytes;
    return 'PW1:${_b32(d.sublist(0, 16))}.${_b32(d.sublist(16, 32))}';
  }

  /// What the server compares: no `PW1:` prefix, no spaces, upper case.
  static String _normalise(String scanned) {
    var t = scanned.trim().replaceAll(' ', '').toUpperCase();
    if (t.startsWith('PW1:')) t = t.substring(4);
    return t;
  }

  void _issueBadge(int workerId) {
    final version =
        _badges.values.where((b) => b.workerId == workerId).length + 1;
    final payload = _payload(workerId, version);
    _badges[_normalise(payload)] = _MockBadge(
      workerId,
      payload,
      isoTimestamp(
        parseIsoDate(
          today,
        ).subtract(Duration(days: 11 - version)).add(const Duration(hours: 9)),
      ),
    );
  }

  /// The QR text of [workerId]'s active badge (what a scan reads).
  String badgeOf(int workerId) => _badges.values
      .lastWhere((b) => b.workerId == workerId && !b.revoked)
      .payload;

  /// The QR text of a revoked badge of [workerId], if any.
  String? revokedBadgeOf(int workerId) => _badges.values
      .where((b) => b.workerId == workerId && b.revoked)
      .firstOrNull
      ?.payload;

  /// The office reissues a badge (lost, damaged): the old one stops counting.
  void revokeBadge(int workerId) {
    for (final b in _badges.values) {
      if (b.workerId == workerId) b.revoked = true;
    }
    _issueBadge(workerId);
  }

  /// The office switches this phone to kiosk mode for [siteId] (null ends
  /// it). Kiosk mode belongs to the device: it survives signing in again.
  void setKiosk(int? siteId) {
    _kioskSiteId = siteId;
    _kioskSince = siteId == null ? null : _ts;
  }

  /// A worker photo behind `files/workers/{id}/photo/` (a new photo, a new
  /// `ETag`).
  void setWorkerPhoto(int workerId, List<int> bytes) {
    final n = (_workerPhotos[workerId]?.version ?? 0) + 1;
    _workerPhotos[workerId] = _MockFile(
      bytes,
      '"w$workerId-$n"',
      'image/jpeg',
      n,
    );
  }

  CrewWorker _withPhoto(CrewWorker w) => !_workerPhotos.containsKey(w.id)
      ? w
      : CrewWorker(
          id: w.id,
          number: w.number,
          firstName: w.firstName,
          lastName: w.lastName,
          trade: w.trade,
          photoUrl: '${publicBaseUrl}files/workers/${w.id}/photo/',
          payType: w.payType,
          absentStreak: w.absentStreak,
        );

  ApiException _validation(Map<String, dynamic> details) =>
      _err(400, ErrorCodes.validationError, 'Invalid input.', details);

  /// Check-in endpoints of a site: assigned sites only; a kiosk phone only
  /// its own site.
  void _checkCheckInSite(int siteId) {
    final kiosk = _kioskSiteId;
    if (kiosk != null && kiosk != siteId) {
      throw _err(
        403,
        ErrorCodes.permissionDenied,
        'This phone is a kiosk for another site.',
      );
    }
    _checkSite(siteId);
  }

  String _lateAfter(int siteId) => lateAfter[siteId] ?? companyLateAfter;

  Future<CheckInDevice> checkInDevice(String? token) async {
    await _call(token: token);
    final site = _kioskSiteId;
    return CheckInDevice(
      attendanceMode: attendanceMode,
      kiosk: site == null
          ? null
          : Kiosk(
              siteId: site,
              siteName: site == MockSeed.site2.id
                  ? MockSeed.site2.name
                  : MockSeed.site.name,
              since: _kioskSince!,
            ),
    );
  }

  Future<CheckInSetup> checkInSetup(String? token, int siteId) async {
    await _call(token: token);
    _checkCheckInSite(siteId);
    final crew = _crews[siteId]!.map((w) => w.id).toSet();
    final badges = [
      for (final b in _badges.values)
        if (!b.revoked && crew.contains(b.workerId))
          BadgeHash(
            workerId: b.workerId,
            payloadSha256: badgeHash(b.payload),
            issuedAt: b.issuedAt,
          ),
    ]..sort((a, b) => a.workerId.compareTo(b.workerId));
    return CheckInSetup(
      siteId: siteId,
      attendanceMode: attendanceMode,
      lateAfter: _lateAfter(siteId),
      kiosk: _kioskSiteId == siteId,
      geofence: geofences[siteId],
      badges: badges,
    );
  }

  Future<CheckIn> checkIn(
    String? token,
    CheckInCreateRequest req, {
    List<int>? photo,
    String photoType = 'image/jpeg',
  }) async {
    await _call(token: token);
    // Field checks first (400), like the serializer.
    final badge = req.badge;
    if ((badge == null) == (req.workerId == null)) {
      throw _validation({
        'non_field_errors': ['Send either badge or worker_id.'],
      });
    }
    if (badge != null && (badge.isEmpty || badge.length > 200)) {
      throw _validation({
        'badge': ['Ensure this field has no more than 200 characters.'],
      });
    }
    if ((req.latitude == null) != (req.longitude == null)) {
      throw _validation({
        req.latitude == null ? 'latitude' : 'longitude': [
          'Send latitude and longitude together.',
        ],
      });
    }
    if (req.latitude != null && req.latitude!.abs() > 90) {
      throw _validation({
        'latitude': ['Ensure this value is between -90 and 90.'],
      });
    }
    if (req.longitude != null && req.longitude!.abs() > 180) {
      throw _validation({
        'longitude': ['Ensure this value is between -180 and 180.'],
      });
    }
    final accuracy = req.accuracyM;
    if (accuracy != null && (accuracy < 0 || accuracy > 100000)) {
      throw _validation({
        'accuracy_m': ['Ensure this value is between 0 and 100000.'],
      });
    }
    final at = DateTime.tryParse(req.at);
    if (at == null || !req.at.contains('T')) {
      throw _validation({
        'at': ['Datetime has wrong format.'],
      });
    }
    // Then the site (403), the kiosk rule and the replay.
    _checkCheckInSite(req.siteId);
    final kiosk = _kioskSiteId != null;
    if (kiosk && req.workerId != null) {
      throw _validation({
        'badge': ['A kiosk only records badge scans.'],
      });
    }
    final seen = _checkins[req.clientUuid];
    if (seen != null) {
      final same =
          seen.checkIn.siteId == req.siteId &&
          seen.checkIn.direction == req.direction &&
          DateTime.parse(seen.checkIn.at).isAtSameMomentAs(at) &&
          (badge != null
              ? seen.badgeScan
              : !seen.badgeScan && seen.checkIn.workerId == req.workerId);
      if (!same) {
        throw _err(
          409,
          ErrorCodes.conflict,
          'This client_uuid was already used for a different request.',
          {'client_uuid': req.clientUuid},
        );
      }
      return seen.checkIn.asReplayed(); // 200 + Idempotent-Replayed: true.
    }
    final now = _now();
    if (at.isAfter(now.add(const Duration(minutes: 10)))) {
      throw _validation({
        'at': ["This time is in the future. Check the phone's clock."],
      });
    }
    if (at.isBefore(now.subtract(const Duration(days: 31)))) {
      throw _validation({
        'at': ['This time is more than 31 days old.'],
      });
    }
    final companyWorkers = {
      for (final crew in _crews.values) ...crew.map((w) => w.id),
    };
    if (req.workerId != null && !companyWorkers.contains(req.workerId)) {
      throw _validation({
        'worker_id': ['Unknown worker.'],
      });
    }

    final flags = <CheckInFlag>[];
    int? workerId = req.workerId;
    if (badge != null) {
      final b = _badges[_normalise(badge)];
      if (b == null) {
        flags.add(CheckInFlag.unknownBadge);
      } else {
        workerId = b.workerId;
        if (b.revoked) flags.add(CheckInFlag.revokedBadge);
      }
    }
    final counted =
        workerId != null && !flags.contains(CheckInFlag.revokedBadge);
    final crew = _crews[req.siteId]!.map((w) => w.id).toSet();
    if (workerId != null && !crew.contains(workerId)) {
      flags.add(CheckInFlag.notOnCrew);
    }
    final fence = geofences[req.siteId];
    int? distance;
    bool? inside;
    if (fence != null && req.latitude != null) {
      distance = distanceMetres(
        fence.latitude,
        fence.longitude,
        req.latitude!,
        req.longitude!,
      ).round();
      inside = insideGeofence(fence, req.latitude!, req.longitude!, accuracy);
      if (!inside) flags.add(CheckInFlag.outsideGeofence);
    }
    final date = isoDate(at.toUtc()); // Africa/Lome is UTC+0.
    if (workerId != null &&
        _checkins.values.any(
          (c) =>
              c.checkIn.workerId == workerId &&
              c.checkIn.siteId == req.siteId &&
              c.checkIn.direction == req.direction &&
              !at.isBefore(DateTime.parse(c.checkIn.at)) &&
              at.difference(DateTime.parse(c.checkIn.at)) <
                  const Duration(minutes: 5),
        )) {
      flags.add(CheckInFlag.duplicate);
    }
    final id = ++_checkinId;
    var checkIn = CheckIn(
      id: id,
      clientUuid: req.clientUuid,
      siteId: req.siteId,
      workerId: workerId,
      date: date,
      direction: req.direction,
      at: req.at,
      receivedAt: _ts,
      source: kiosk
          ? CheckInSource.kiosk
          : badge != null
          ? CheckInSource.foremanScan
          : CheckInSource.manual,
      latitude: req.latitude,
      longitude: req.longitude,
      accuracyM: accuracy,
      distanceM: distance,
      insideGeofence: inside,
      counted: counted,
      draftUpdated: false,
      flags: flags,
      photoUrl: photo == null
          ? null
          : '${publicBaseUrl}files/checkins/$id/photo/',
    );
    final stored = _MockCheckIn(
      checkIn,
      badge != null,
      photo == null ? null : _MockFile(photo, '"c$id"', photoType, 1),
    );
    _checkins[req.clientUuid] = stored;
    if (attendanceMode.serverWritesDrafts &&
        counted &&
        crew.contains(workerId)) {
      if (_writeDraft(req.siteId, date, workerId)) {
        checkIn = CheckIn.fromJson({
          ...checkIn.toJson(),
          'draft_updated': true,
        });
        stored.checkIn = checkIn;
      }
    }
    return checkIn;
  }

  /// Counted check-ins of crew workers of a site and day.
  List<CheckInEvent> _events(int siteId, String date, {int? workerId}) {
    final crew = _crews[siteId]!.map((w) => w.id).toSet();
    return [
      for (final c in _checkins.values)
        if (c.checkIn.siteId == siteId &&
            c.checkIn.date == date &&
            c.checkIn.counted &&
            crew.contains(c.checkIn.workerId) &&
            (workerId == null || c.checkIn.workerId == workerId))
          CheckInEvent(
            c.checkIn.workerId!,
            c.checkIn.direction,
            DateTime.parse(c.checkIn.at),
          ),
    ];
  }

  /// `self_check_in`: the worker's draft record follows the check-ins. A
  /// field is written when the record is new, the time is empty, or it
  /// still holds what earlier check-ins suggested (the foreman's hand
  /// changes win). Never once the day was submitted.
  bool _writeDraft(int siteId, String date, int workerId) {
    final summary = _summary(siteId, date);
    if (!summary.status.editable) return false;
    final s = suggestFromEvents(
      _events(siteId, date, workerId: workerId),
      lateAfter: _lateAfter(siteId),
      localOf: (t) => t.toUtc(),
    ).single;
    final key = _key(siteId, date);
    final wrote = _checkinWrote.putIfAbsent(key, () => {})[workerId] ??= {};
    final recs = _records.putIfAbsent(key, () => {});
    final existing = recs[workerId];
    if (existing == null && s.status == null) return false;
    bool mine(String field, String? current) =>
        existing == null ||
        current == null ||
        (wrote.containsKey(field) && wrote[field] == current);
    var status = existing?.status ?? s.status!;
    var arrival = existing?.arrival;
    var departure = existing?.departure;
    if (s.status != null && mine('status', existing?.status.wire)) {
      status = s.status!;
      wrote['status'] = status.wire;
    }
    if (s.arrival != null && mine('arrival', existing?.arrival)) {
      arrival = s.arrival;
      wrote['arrival'] = arrival;
    }
    if (s.departure != null && mine('departure', existing?.departure)) {
      departure = s.departure;
      wrote['departure'] = departure;
    }
    if (existing != null &&
        existing.status == status &&
        existing.arrival == arrival &&
        existing.departure == departure) {
      return false;
    }
    final dayId = summary.id ?? ++_dayId;
    final ts = _ts;
    final newId = existing == null ? ++_recordId : existing.id;
    recs[workerId] = existing == null
        ? AttendanceRecord(
            id: newId,
            // Made by the server (no phone wrote it): stable for tests.
            clientUuid:
                '00000000-0000-4000-9000-${newId.toString().padLeft(12, '0')}',
            dayId: dayId,
            siteId: siteId,
            date: date,
            workerId: workerId,
            status: status,
            arrival: arrival,
            departure: departure,
            note: '',
            corrected: false,
            updatedAt: ts,
          )
        : existing.copyWith(
            status: status,
            arrival: () => arrival,
            departure: () => departure,
            updatedAt: ts,
          );
    _days[key] = summary.copyWith(
      id: dayId,
      status: DayStatus.draft,
      updatedAt: ts,
      counts: _counts(siteId, date),
    );
    return true;
  }

  Future<CheckInList> siteCheckIns(
    String? token,
    int siteId,
    String date,
  ) async {
    await _call(token: token);
    _checkCheckInSite(siteId);
    final list = [
      for (final c in _checkins.values)
        if (c.checkIn.siteId == siteId && c.checkIn.date == date) c.checkIn,
    ]..sort((a, b) => DateTime.parse(a.at).compareTo(DateTime.parse(b.at)));
    return CheckInList(siteId: siteId, date: date, checkins: list);
  }

  Future<RollCallSuggestions> suggestions(
    String? token,
    int siteId,
    String date,
  ) async {
    await _call(token: token);
    _checkCheckInSite(siteId);
    final fromCheckIns = suggestFromEvents(
      _events(siteId, date),
      lateAfter: _lateAfter(siteId),
      localOf: (t) => t.toUtc(),
    );
    // Presence (1.5): what [seePresence] put there, crew only. The server
    // works the presence status out from the settings; here it is given.
    final crew = _crews[siteId]!.map((w) => w.id).toSet();
    final seen = {
      for (final e in (_presence[_key(siteId, date)] ?? const {}).entries)
        if (crew.contains(e.key)) e.key: e.value,
    };
    final byWorker = {for (final s in fromCheckIns) s.workerId: s};
    final ids = {...byWorker.keys, ...seen.keys}.toList()..sort();
    return RollCallSuggestions(
      siteId: siteId,
      date: date,
      attendanceMode: attendanceMode,
      lateAfter: _lateAfter(siteId),
      dayStatus: _summary(siteId, date).status,
      suggestions: [
        for (final id in ids)
          if (byWorker[id] case final s?)
            seen[id] == null
                ? s
                : s.withPresence(const [
                    SuggestionSource.checkin,
                    SuggestionSource.presence,
                  ], seen[id])
          else
            RollCallSuggestion(
              workerId: id,
              status: seen[id]!.status,
              arrival: seen[id]!.seenFrom,
              departure: seen[id]!.seenUntil,
              firstInAt: null,
              lastOutAt: null,
              checkinCount: 0,
              sources: const [SuggestionSource.presence],
              presence: seen[id],
            ),
      ],
    );
  }

  final Map<String, Map<int, SuggestionPresence>> _presence = {};

  /// Tests and the demo: presence tracking saw [workerId] on the site that
  /// day (the suggestions show it).
  void seePresence(
    int siteId,
    String date,
    int workerId,
    SuggestionPresence presence,
  ) => _presence.putIfAbsent(_key(siteId, date), () => {})[workerId] = presence;

  // ------------------------------------------------------------------ files

  static final _workerPhotoPath = RegExp(r'/files/workers/(\d+)/photo/$');
  static final _checkinPhotoPath = RegExp(r'/files/checkins/(\d+)/photo/$');

  /// `GET /files/workers/{id}/photo/` and `GET /files/checkins/{id}/photo/`:
  /// your sites only (`404` otherwise, or without a file), `ETag` and `304`.
  Future<FileDownload> download(String? token, String url, String? etag) async {
    await _call(token: token);
    final path = Uri.parse(url).path;
    _MockFile? file;
    final w = _workerPhotoPath.firstMatch(path);
    final c = _checkinPhotoPath.firstMatch(path);
    if (w != null) {
      final id = int.parse(w.group(1)!);
      final mine = _crews.values.any((crew) => crew.any((x) => x.id == id));
      if (mine) file = _workerPhotos[id];
    } else if (c != null) {
      final id = int.parse(c.group(1)!);
      file = _checkins.values
          .where((x) => x.checkIn.id == id)
          .firstOrNull
          ?.photo;
    }
    if (file == null) throw _err(404, ErrorCodes.notFound, 'Not found.');
    if (etag != null && etag == file.etag) {
      return FileDownload.notModified(etag: file.etag);
    }
    return FileDownload.ok(
      file.bytes,
      etag: file.etag,
      contentType: file.contentType,
    );
  }

  // ------------------------------------------------------------------- sync

  Future<SyncResponse> sync(String? token, String? since) async {
    await _call(token: token);
    final serverTime = _ts;
    bool after(String? ts) =>
        since == null || (ts != null && ts.compareTo(since) >= 0);
    final floor = addDays(today, -14);
    final days = <AttendanceDaySummary>[];
    final records = <AttendanceRecord>[];
    for (final e in _days.entries) {
      final s = e.value;
      if (s.date.compareTo(floor) < 0) continue;
      if (after(s.updatedAt)) days.add(_summary(s.siteId, s.date));
      for (final r in (_records[e.key] ?? {}).values) {
        if (after(r.updatedAt)) records.add(r);
      }
    }
    final corrections = _corrections.values
        .where((c) => after(c.createdAt))
        .toList();
    return SyncResponse(
      serverTime: serverTime,
      since: since,
      days: days,
      records: records,
      corrections: corrections,
    );
  }
}

class _MockBadge {
  _MockBadge(this.workerId, this.payload, this.issuedAt);
  final int workerId;
  final String payload;
  final String issuedAt;
  bool revoked = false;
}

class _MockCheckIn {
  _MockCheckIn(this.checkIn, this.badgeScan, this.photo);
  CheckIn checkIn;

  /// Sent with `badge` (else `worker_id`): part of the replay check.
  final bool badgeScan;
  final _MockFile? photo;
}

class _MockFile {
  const _MockFile(this.bytes, this.etag, this.contentType, this.version);
  final List<int> bytes;
  final String etag;
  final String contentType;
  final int version;
}
