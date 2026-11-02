import 'dart:async';
import 'dart:convert';
import 'dart:io' show File;

import '../core/site_clock.dart';
import '../data/local/local_store.dart';
import '../data/models/models.dart';
import '../data/repositories/repositories.dart';

/// What the sync banner shows.
class SyncStatus {
  const SyncStatus({
    this.online,
    this.syncing = false,
    this.waiting = 0,
    this.failed = 0,
    this.lastSyncAt,
  });

  /// Null until the first `/health/` check.
  final bool? online;
  final bool syncing;

  /// Writes on the phone not sent yet (each record of a batch counts).
  final int waiting;

  /// Writes the server refused (not retried; shown to the foreman).
  final int failed;
  final DateTime? lastSyncAt;

  SyncStatus copyWith({
    bool? online,
    bool? syncing,
    int? waiting,
    int? failed,
    DateTime? lastSyncAt,
  }) => SyncStatus(
    online: online ?? this.online,
    syncing: syncing ?? this.syncing,
    waiting: waiting ?? this.waiting,
    failed: failed ?? this.failed,
    lastSyncAt: lastSyncAt ?? this.lastSyncAt,
  );
}

enum DrainResult {
  done,
  waiting,
  signedOut,

  /// The outbox belongs to another foreman than the one signed in: nothing
  /// is sent until they sign in again.
  held,
}

/// Error code stored on an entry that failed for lack of network.
const networkErrorCode = 'network';

int _weight(OutboxEntry e) => e.kind == OutboxKind.records
    ? ((e.payload['records'] as Map?)?.length ?? 1)
    : 1;

/// Moves the outbox to the server, one write at a time and in order
/// (check-ins → records → submit → corrections → payments), then pulls
/// changes.
///
/// * Checks `GET /health/` first.
/// * Network errors, 5xx and 429: keep the entry, retry later with backoff.
/// * 401: stop, keep the outbox, call [onSignedOut] (PIN screen).
/// * 400/403/404/409/423: mark the entry failed and continue.
class SyncService {
  SyncService({
    required this.store,
    required this.repos,
    required this.clock,
    this.onSignedOut,
    this.onDeviceRole,
    this.onSuggestions,
    this.pollInterval = const Duration(seconds: 20),
    DateTime Function()? now,
  }) : _now = now ?? DateTime.now {
    _sub = store.changes.listen((_) => _refreshCounts());
    _refreshCounts();
  }

  final LocalStore store;
  final Repositories repos;
  SiteClock clock;
  void Function()? onSignedOut;

  /// `GET /checkin/device/` answered (kiosk mode on or off).
  void Function(CheckInDevice device)? onDeviceRole;

  /// New suggestions for a site's day are on the phone (pre-fill them).
  Future<void> Function(int siteId, String date)? onSuggestions;

  /// Presence tracking is on for the site (it is in the tracker token's
  /// `sites`): its suggestions are fetched even without a scan (1.5).
  bool Function(int siteId)? isTracked;

  /// Sends a queued tracking consent answer (`POST /tracking/consent/`;
  /// `lib/tracking/`). Unset: the entry waits, as for a server without the
  /// endpoint.
  Future<void> Function(OutboxEntry entry)? sendConsent;
  final Duration pollInterval;

  /// Days whose draft the server wrote from check-ins (`self_check_in`,
  /// `draft_updated: true`): fetched again after the drain.
  final Set<(int, String)> _draftDays = {};
  final DateTime Function() _now;

  final _status = StreamController<SyncStatus>.broadcast();
  SyncStatus _current = const SyncStatus();
  StreamSubscription<void>? _sub;
  Timer? _poll;
  Timer? _retry;
  Future<void>? _running;
  bool _again = false;
  bool _paused = false;

  /// Last unexpected error of a sync cycle (for debugging).
  Object? lastError;

  SyncStatus get status => _current;
  Stream<SyncStatus> get statusStream => _status.stream;

  void _emit(SyncStatus s) {
    _current = s;
    if (!_status.isClosed) _status.add(s);
  }

  Future<void> _refreshCounts() async {
    final outbox = await store.loadOutbox();
    var waiting = 0, failed = 0;
    for (final e in outbox) {
      if (e.state == OutboxState.pending) {
        waiting += _weight(e);
      } else {
        failed += _weight(e);
      }
    }
    if (waiting != _current.waiting || failed != _current.failed) {
      _emit(_current.copyWith(waiting: waiting, failed: failed));
    }
  }

  /// Starts polling (health + outbox + pull).
  void start() {
    _paused = false;
    _poll?.cancel();
    _poll = Timer.periodic(pollInterval, (_) => kick());
    kick();
  }

  /// Stops sending until [start] (signed out).
  void pause() {
    _paused = true;
    _poll?.cancel();
    _retry?.cancel();
  }

  void dispose() {
    pause();
    _sub?.cancel();
    _status.close();
  }

  /// Asks for a sync soon; coalesces with one already running.
  void kick() {
    if (_paused) return;
    if (_running != null) {
      _again = true;
      return;
    }
    unawaited(runOnce());
  }

  /// One full cycle. Returns when done (handy in tests).
  Future<void> runOnce() async {
    if (_running != null) {
      _again = true;
      return _running;
    }
    final run = _cycle();
    _running = run;
    try {
      await run;
    } finally {
      _running = null;
    }
    if (_again && !_paused) {
      _again = false;
      await runOnce();
    }
  }

  Future<void> _cycle() async {
    _emit(_current.copyWith(syncing: true));
    try {
      final reachable = await _health();
      _emit(_current.copyWith(online: reachable));
      if (!reachable) return;
      final result = await drain();
      if (result == DrainResult.done || result == DrainResult.held) {
        await pull();
        _emit(_current.copyWith(lastSyncAt: _now()));
      }
    } on NetworkException {
      _emit(_current.copyWith(online: false));
    } on ApiException catch (e) {
      // A pull that fails (other than 401) is retried on the next poll.
      if (e.statusCode == 401) _signedOut();
    } catch (e) {
      // Never let a bug in a background sync crash the app; the outbox is
      // untouched and the next poll tries again.
      lastError = e;
    } finally {
      _emit(_current.copyWith(syncing: false));
      await _refreshCounts();
    }
  }

  Future<bool> _health() async {
    try {
      return await repos.sync.health();
    } catch (_) {
      return false;
    }
  }

  void _signedOut() {
    pause();
    onSignedOut?.call();
  }

  void _scheduleRetry(Duration delay) {
    _retry?.cancel();
    _retry = Timer(delay, kick);
  }

  /// Sends pending entries in order until the outbox is empty or a retryable
  /// error stops it.
  Future<DrainResult> drain() async {
    if (await outboxHeld()) return DrainResult.held;
    while (true) {
      final pending = sendingOrder(
        (await store.loadOutbox()).where((e) => e.state == OutboxState.pending),
      );
      if (pending.isEmpty) return DrainResult.done;
      final entry = pending.first;
      final wait = entry.nextAttemptAt;
      if (wait != null &&
          entry.errorCode != networkErrorCode &&
          _now().isBefore(wait)) {
        _scheduleRetry(wait.difference(_now()));
        return DrainResult.waiting;
      }
      try {
        await _send(entry);
      } catch (error) {
        final attempts = entry.attempts + 1;
        switch (retryDecision(error, attempts)) {
          case RetryLater(:final delay):
            await store.updateOutbox(
              entry.copyWith(
                attempts: attempts,
                nextAttemptAt: () => _now().add(delay),
                errorCode: () =>
                    error is ApiException ? error.code : networkErrorCode,
                errorMessage: () =>
                    error is ApiException ? error.error.message : null,
              ),
            );
            if (error is NetworkException) {
              _emit(_current.copyWith(online: false));
            }
            _scheduleRetry(delay);
            return DrainResult.waiting;
          case SignInAgain():
            _signedOut();
            return DrainResult.signedOut;
          case GiveUp():
            await store.updateOutbox(
              entry.copyWith(
                attempts: attempts,
                state: OutboxState.failed,
                errorCode: () =>
                    error is ApiException ? error.code : 'app_error',
                errorMessage: () => error is ApiException
                    ? error.error.message
                    : error.toString(),
              ),
            );
        }
      }
    }
  }

  /// True when the queued writes belong to another user than the one signed
  /// in ([StoreKeys.outboxOwner]). Writes go out under the user who made them
  /// only: the server would refuse another foreman's payment (`409`) and save
  /// records and submissions under the wrong name.
  Future<bool> outboxHeld() async {
    final owner = await store.getValue(StoreKeys.outboxOwner);
    final raw = await store.getValue(StoreKeys.me);
    if (owner == null || raw == null) return false;
    final me = jsonDecode(raw) as Map<String, dynamic>;
    return '${me['id']}' != owner;
  }

  // ----------------------------------------------------------------- send

  Future<void> _send(OutboxEntry e) async {
    switch (e.kind) {
      case OutboxKind.consent:
        final send = sendConsent;
        if (send == null) {
          throw const ApiException(
            501,
            ApiError(
              code: ErrorCodes.notImplemented,
              message: 'Tracking is not available.',
            ),
          );
        }
        await send(e);
        await store.deleteOutbox(e.id!);
      case OutboxKind.checkin:
        await _sendCheckIn(e);
      case OutboxKind.records:
        await _sendRecords(e);
      case OutboxKind.submit:
        final day = await repos.attendance.submit(
          e.siteId,
          e.date,
          SubmitDayRequest.fromJson(e.payload),
        );
        await store.deleteOutbox(e.id!);
        await _applyDay(day, overwrite: true);
      case OutboxKind.correction:
        final workerId = e.payload['worker_id'] as int;
        final local = await store.loadRecord(e.siteId, e.date, workerId);
        final recordId = local?.serverId;
        if (recordId == null) {
          throw const ApiException(
            404,
            ApiError(
              code: 'record_not_sent',
              message: 'The record has not reached the server yet.',
            ),
          );
        }
        final c = await repos.attendance.correct(
          recordId,
          CorrectionCreateRequest.fromJson(e.payload),
        );
        await store.deleteOutbox(e.id!);
        await store.upsertRecords([LocalRecord.fromServer(c.record)]);
      case OutboxKind.payment:
        final payment = await repos.payout.pay(
          PaymentCreateRequest.fromJson(e.payload),
          proofFilePath: e.payload['proof_path'] as String?,
        );
        await store.deleteOutbox(e.id!);
        await _applyPayment(e, payment);
    }
  }

  Future<void> _sendCheckIn(OutboxEntry e) async {
    final photo = e.payload['photo_path'] as String?;
    final checkIn = await repos.checkin.checkIn(
      CheckInCreateRequest.fromJson(e.payload),
      photoPath: photo,
    );
    await store.deleteOutbox(e.id!);
    final local = (await store.loadCheckIns(
      e.siteId,
      e.date,
    )).where((c) => c.clientUuid == e.clientUuid).firstOrNull;
    if (local != null) await store.upsertCheckIn(local.withServer(checkIn));
    // The server has the photo: the phone's copy can go.
    if (photo != null) _deleteQuietly(photo);
    if (checkIn.draftUpdated) _draftDays.add((checkIn.siteId, checkIn.date));
  }

  static void _deleteQuietly(String path) {
    try {
      final f = File(path);
      if (f.existsSync()) f.deleteSync();
    } catch (_) {
      // A file the OS already cleared.
    }
  }

  Future<void> _sendRecords(OutboxEntry e) async {
    final map = (e.payload['records'] as Map<String, dynamic>);
    final upserts = map.values
        .map((j) => RecordUpsert.fromJson(j as Map<String, dynamic>))
        .toList();
    AttendanceDay? last;
    for (var i = 0; i < upserts.length; i += 200) {
      final chunk = upserts.sublist(
        i,
        i + 200 > upserts.length ? upserts.length : i + 200,
      );
      last = await repos.attendance.putRecords(e.siteId, e.date, chunk);
    }
    // Delete only if nothing changed while the request was in flight;
    // otherwise the newer version is sent next time (idempotent).
    final settled = await store.deleteOutbox(e.id!, ifVersion: e.version);
    if (last != null) await _applyDay(last, overwrite: settled);
  }

  /// Stores a day returned by the server. Records adopt the server's id and
  /// `client_uuid`; their values are replaced only when no newer local edit
  /// is waiting ([overwrite]).
  Future<void> _applyDay(AttendanceDay day, {required bool overwrite}) async {
    final s = day.summary;
    await store.upsertDays([s]);
    final pendingWorkers = await _pendingWorkers(s.siteId, s.date);
    final out = <LocalRecord>[];
    for (final r in day.records) {
      final local = await store.loadRecord(s.siteId, s.date, r.workerId);
      if (overwrite || local == null || !pendingWorkers.contains(r.workerId)) {
        out.add(LocalRecord.fromServer(r));
      } else {
        out.add(
          local.copyWith(
            serverId: r.id,
            dayId: r.dayId,
            clientUuid: r.clientUuid,
          ),
        );
        if (local.clientUuid != r.clientUuid) {
          await _adoptUuid(s.siteId, s.date, r.workerId, r.clientUuid);
        }
      }
    }
    await store.upsertRecords(out);
  }

  Future<Set<int>> _pendingWorkers(int siteId, String date) async {
    final outbox = await store.loadOutbox();
    return {
      for (final e in outbox)
        if (e.kind == OutboxKind.records &&
            e.state == OutboxState.pending &&
            e.siteId == siteId &&
            e.date == date)
          ...(e.payload['records'] as Map<String, dynamic>).keys.map(int.parse),
    };
  }

  /// Same worker marked from two phones: use the server's `client_uuid`.
  Future<void> _adoptUuid(
    int siteId,
    String date,
    int workerId,
    String uuid,
  ) async {
    for (final e in await store.loadOutbox()) {
      if (e.kind != OutboxKind.records ||
          e.siteId != siteId ||
          e.date != date) {
        continue;
      }
      final map = Map<String, dynamic>.from(
        e.payload['records'] as Map<String, dynamic>,
      );
      final item = map['$workerId'] as Map<String, dynamic>?;
      if (item == null) continue;
      map['$workerId'] = {...item, 'client_uuid': uuid};
      await store.updateOutbox(e.copyWith(payload: {'records': map}));
    }
  }

  Future<void> _applyPayment(OutboxEntry e, Payment payment) async {
    final local = (await store.loadPayments(
      e.siteId,
      e.date,
    )).where((p) => p.clientUuid == e.clientUuid).firstOrNull;
    if (local != null) {
      await store.upsertPayment(local.withServerId(payment.id));
    }
    final sheet = await store.loadPayout(e.siteId, e.date);
    if (sheet != null) {
      await store.savePayout(
        Payout.fromLines(
          siteId: sheet.siteId,
          date: sheet.date,
          status: sheet.status,
          dayStatus: sheet.dayStatus,
          approvedAt: sheet.approvedAt,
          currency: sheet.currency,
          cashIssued: sheet.cashIssued,
          cashToPay: sheet.cashToPay,
          lines: [
            for (final l in sheet.lines)
              l.payLineId == payment.payLineId
                  ? l.withPayment(payment.summary)
                  : l,
          ],
        ),
      );
    }
  }

  // ----------------------------------------------------------------- pull

  /// `GET /sync/` (server wins), then refreshes crews, the week and payouts.
  Future<void> pull() async {
    final since = await store.getValue(StoreKeys.syncSince);
    final resp = await repos.sync.sync(since: since);
    await store.upsertDays(resp.days);
    // The same record may come in `records` and in a correction: keep the
    // newest copy. Records carry `site_id` and `date` (1.1), so they are
    // stored even when their day is not on the phone yet.
    final newest = <int, AttendanceRecord>{};
    for (final r in [
      ...resp.records,
      ...resp.corrections.map((c) => c.record),
    ]) {
      final seen = newest[r.id];
      if (seen == null || r.updatedAt.compareTo(seen.updatedAt) >= 0) {
        newest[r.id] = r;
      }
    }
    for (final r in newest.values) {
      final pending = await _pendingWorkers(r.siteId, r.date);
      if (pending.contains(r.workerId)) continue; // Unsent write: send it.
      await store.upsertRecords([LocalRecord.fromServer(r)]);
    }
    await store.setValue(StoreKeys.syncSince, resp.serverTime);

    final device = await _refreshDevice();
    final kioskSite = device?.kiosk?.siteId;
    for (final site in await _sites()) {
      // A kiosk phone only takes its own site's check-ins (others: 403).
      if (kioskSite != null && site.id != kioskSite) continue;
      await refreshSite(site.id);
      await refreshCheckIns(site.id, device);
    }
    await refreshDraftDays();
  }

  /// `GET /checkin/device/` (after sign-in, at start-up and with every
  /// sync). Null when the server has no check-in endpoints (a 1.1 server).
  Future<CheckInDevice?> _refreshDevice() async {
    try {
      final device = await repos.checkin.device();
      await store.setValue(
        StoreKeys.checkinDevice,
        jsonEncode(device.toJson()),
      );
      onDeviceRole?.call(device);
      return device;
    } on ApiException catch (e) {
      if (e.statusCode == 401) rethrow;
      final raw = await store.getValue(StoreKeys.checkinDevice);
      return raw == null
          ? null
          : CheckInDevice.fromJson(jsonDecode(raw) as Map<String, dynamic>);
    }
  }

  /// The site's check-in setup (badge hashes; once a day, with the crew)
  /// and today's suggestions. Errors other than 401 leave the cache as it
  /// is (a 1.1 server answers 404).
  Future<void> refreshCheckIns(int siteId, CheckInDevice? device) async {
    final today = clock.today();
    try {
      final raw = await store.getValue(StoreKeys.checkinSetup(siteId));
      final cachedDate = raw == null
          ? null
          : (jsonDecode(raw) as Map<String, dynamic>)['date'] as String?;
      if (cachedDate != today) {
        final setup = await repos.checkin.setup(siteId);
        await store.setValue(
          StoreKeys.checkinSetup(siteId),
          jsonEncode({'date': today, 'setup': setup.toJson()}),
        );
      }
      if (device?.isKiosk ?? false) return;
      final mode = device?.attendanceMode;
      final scanned = (await store.loadCheckIns(siteId, today)).isNotEmpty;
      final tracked = isTracked?.call(siteId) ?? false;
      if (mode == null ||
          (mode == AttendanceMode.foremanRollCall && !scanned && !tracked)) {
        return;
      }
      final suggestions = await repos.checkin.suggestions(siteId, today);
      await store.setValue(
        StoreKeys.suggestions(siteId, today),
        jsonEncode(suggestions.toJson()),
      );
      if (mode.serverWritesDrafts) {
        _draftDays.add((siteId, today));
      } else {
        await onSuggestions?.call(siteId, today);
      }
    } on ApiException catch (e) {
      if (e.statusCode == 401) rethrow;
    }
  }

  /// `self_check_in`: the server wrote draft records from the check-ins;
  /// fetch those days again (local edits still waiting are kept).
  Future<void> refreshDraftDays() async {
    final days = _draftDays.toList();
    _draftDays.clear();
    for (final (siteId, date) in days) {
      try {
        final day = await repos.attendance.day(siteId, date);
        await _applyDay(day, overwrite: false);
      } on ApiException catch (e) {
        if (e.statusCode == 401) rethrow;
      }
    }
  }

  Future<List<AssignedSite>> _sites() async {
    final raw = await store.getValue(StoreKeys.me);
    if (raw == null) return const [];
    return Me.fromJson(jsonDecode(raw) as Map<String, dynamic>).sites;
  }

  /// Crew (once a day), the last 7 days, and today's payout sheet.
  Future<void> refreshSite(int siteId) async {
    final today = clock.today();
    final crew = await store.loadCrew(siteId);
    if (crew == null || crew.date != today) {
      await store.saveCrew(await repos.crew.crew(siteId, date: today));
    }
    final week = await repos.attendance.days(siteId);
    await store.upsertDays(week.days);
    final day = week.days.where((d) => d.date == today).firstOrNull;
    final cached = await store.loadPayout(siteId, today);
    if (cached != null ||
        (day != null && day.payoutStatus != PayoutStatus.notOpen)) {
      await refreshPayout(siteId, today);
    }
  }

  /// Fetches a payout sheet (also the `not_open` preview).
  Future<void> refreshPayout(int siteId, String date) async {
    final sheet = await repos.payout.payout(siteId, date);
    await store.savePayout(sheet);
    // Payments the server already has are no longer "unsent".
    final local = await store.loadPayments(siteId, date);
    for (final line in sheet.lines) {
      final p = line.payment;
      if (p == null) continue;
      for (final l in local) {
        if (l.clientUuid == p.clientUuid && !l.sent) {
          await store.upsertPayment(l.withServerId(p.id));
        }
      }
    }
  }
}
