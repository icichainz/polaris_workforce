import 'dart:async';
import 'dart:convert';

import 'package:uuid/uuid.dart';

import '../core/dates.dart';
import '../core/site_clock.dart';
import '../data/local/local_store.dart';
import '../data/models/models.dart';
import 'observations.dart';
import 'sensors.dart';
import 'tracking_api.dart';
import 'tracking_models.dart';
import 'tracking_token_store.dart';
import 'uploader.dart';

/// Where tracking stands for the signed-in foreman.
enum TrackingPhase {
  /// Signed out (or not started).
  off,

  /// Signed in, no answer from the server yet and no saved token (first
  /// sign-in offline): nothing is collected.
  unknown,

  /// The server wants the consent notice answered: show it.
  consentRequired,

  /// Accepted on the phone; the answer has not reached the server yet.
  consentPending,

  /// Off by settings, or refused: no tracking UI at all.
  disabled,

  /// A token (possibly expired while offline) and its settings: collect
  /// inside tracking hours.
  active,
}

/// The small status shown on Today.
enum TrackingIndicator {
  hidden,

  /// Recording (inside tracking hours, location allowed).
  on,

  /// Outside tracking hours: nothing is recorded.
  paused,

  /// Inside tracking hours but the phone's location is refused or off.
  off,
}

class TrackingState {
  const TrackingState({
    this.phase = TrackingPhase.off,
    this.token,
    this.insideHours = false,
    this.gps = LocationAccess.unknown,
    this.recordingGps = false,
    this.scanningSiteId,
    this.refused = false,
    this.queued = 0,
    this.mockedFixes = 0,
    this.consentContext = const ConsentContext(),
  });

  final TrackingPhase phase;
  final TrackingToken? token;
  final bool insideHours;
  final LocationAccess gps;
  final bool recordingGps;

  /// The site whose badges are being scanned, if any.
  final int? scanningSiteId;

  /// The foreman's last answer on this phone was a refusal (the notice can
  /// be opened again from the menu).
  final bool refused;

  /// Observations on the phone not sent yet.
  final int queued;

  /// Fixes thrown away because they came from a mock-location provider.
  final int mockedFixes;

  /// What the server said with `consent_required` (for the notice).
  final ConsentContext consentContext;

  bool get consentNeeded => phase == TrackingPhase.consentRequired;

  TrackingIndicator get indicator {
    if (phase != TrackingPhase.active || token == null) {
      return TrackingIndicator.hidden;
    }
    if (!insideHours) return TrackingIndicator.paused;
    return gps == LocationAccess.denied || gps == LocationAccess.serviceOff
        ? TrackingIndicator.off
        : TrackingIndicator.on;
  }

  TrackingState copyWith({
    TrackingPhase? phase,
    TrackingToken? Function()? token,
    bool? insideHours,
    LocationAccess? gps,
    bool? recordingGps,
    int? Function()? scanningSiteId,
    bool? refused,
    int? queued,
    int? mockedFixes,
    ConsentContext? consentContext,
  }) => TrackingState(
    phase: phase ?? this.phase,
    token: token != null ? token() : this.token,
    insideHours: insideHours ?? this.insideHours,
    gps: gps ?? this.gps,
    recordingGps: recordingGps ?? this.recordingGps,
    scanningSiteId: scanningSiteId != null
        ? scanningSiteId()
        : this.scanningSiteId,
    refused: refused ?? this.refused,
    queued: queued ?? this.queued,
    mockedFixes: mockedFixes ?? this.mockedFixes,
    consentContext: consentContext ?? this.consentContext,
  );
}

/// Keys of the phone's key/value store used by tracking.
abstract final class TrackingKeys {
  /// The foreman's last consent answer on this phone (JSON `{accepted,
  /// text_version, at}`).
  static String consentAnswer(String owner) => 'tracking_consent|$owner';
}

/// Presence tracking on the phone: the tracker token, the consent answer,
/// GPS and badge collection inside tracking hours, and the upload of the
/// observation queue. Plain Dart; `tracking_providers.dart` wires it to the
/// session, the current site and the app's lifecycle.
///
/// * **Token:** fetched after sign-in, kept in secure storage, refreshed
///   before `expires_at` (and on a tracker `401`), dropped on sign-out.
///   Offline, a saved token keeps collection going with its settings; the
///   queue waits until a fresh token can send it.
/// * **`403 consent_required`:** [TrackingState.consentNeeded] (the app
///   shows the notice). The answer goes through the outbox
///   ([OutboxKind.consent]), so it works offline.
/// * **`403 tracking_disabled`** (or a refusal): no tracking at all; the
///   token and the queue are dropped.
/// * **Collection:** only while signed in, not on a kiosk phone, inside
///   `tracking_hours` on the site's clock. GPS every
///   `gps_interval_seconds` (mock locations dropped); badges only while the
///   app is in the foreground on a site of the token, each at most once per
///   30 s.
class TrackingClient {
  TrackingClient({
    required this.api,
    required this.tokens,
    required this.observations,
    required this.store,
    required this.transport,
    required this.location,
    required this.scanner,
    required SiteClock Function() clock,
    DateTime Function()? now,
    this.onChange,
    this.kickSync,
    BackgroundNotice Function()? notice,
    this.uploadDelay = const Duration(seconds: 30),
    this.keepFor = const Duration(days: 7),
    this.keepAtMost = 50000,
  }) : _clock = clock,
       _now = now ?? DateTime.now,
       _notice =
           notice ??
           (() => const BackgroundNotice(
             title: 'Polaris',
             text: 'Recording your position during tracking hours.',
           )) {
    uploader = ObservationUploader(
      store: observations,
      transport: transport,
      currentToken: () async => _state.token,
      refreshToken: refreshToken,
      now: _now,
    );
  }

  final TrackingRepository api;
  final TrackingTokenStore tokens;
  final ObservationStore observations;
  final LocalStore store;
  final TrackerTransport transport;
  final TrackingLocationSource location;
  final BadgeScanner scanner;
  final SiteClock Function() _clock;
  final DateTime Function() _now;
  final BackgroundNotice Function() _notice;

  /// Called after every state change.
  void Function(TrackingState state)? onChange;

  /// Asks the sync service to send the outbox (a consent answer).
  void Function()? kickSync;

  /// Wait after a new observation before sending, so a few go together.
  final Duration uploadDelay;

  /// Observations older than this are dropped unsent (the tracker would
  /// store them in the track only, and the queue must not grow forever).
  final Duration keepFor;
  final int keepAtMost;

  late final ObservationUploader uploader;

  TrackingState _state = const TrackingState();
  TrackingState get state => _state;

  String? _owner;
  String? get owner => _owner;

  bool _foreground = true;
  int? _siteId;
  bool _kiosk = false;

  Timer? _refreshTimer;
  Timer? _hoursTimer;
  Timer? _uploadTimer;
  DateTime? _uploadDue;

  /// After a failed upload: new observations do not make the phone retry
  /// before the uploader's backoff is over (only the network coming back,
  /// a new token or the app reopening do).
  DateTime? _uploadNotBefore;
  int _tokenFailures = 0;
  DateTime? _lastAnswer;
  Future<TrackingToken?>? _refreshing;

  StreamSubscription<TrackingFix>? _gps;
  bool _gpsStarting = false;
  Duration? _gpsInterval;
  bool _askedPermission = false;
  final _gpsFilter = GpsFilter(const Duration(seconds: 60));

  StreamSubscription<BadgeSighting>? _ble;
  final _deduper = BleDeduper();

  bool _disposed = false;

  void _emit(TrackingState s) {
    _state = s;
    if (!_disposed) onChange?.call(s);
  }

  // ------------------------------------------------------------ session

  /// After PIN sign-in (or at start-up with a saved session).
  Future<void> signedIn(String owner) async {
    if (_owner == owner && _state.phase != TrackingPhase.off) return;
    if (_owner != null && _owner != owner) await signedOut();
    _owner = owner;
    final saved = await tokens.read(owner);
    final refused = await _refusedHere(owner);
    if (_owner != owner) return;
    _emit(
      TrackingState(
        phase: saved != null ? TrackingPhase.active : TrackingPhase.unknown,
        token: saved,
        refused: refused,
        queued: await observations.countObservations(owner),
      ),
    );
    _apply();
    await refreshToken();
  }

  /// Sign-out or a `401`: stop, drop the token. Queued observations stay
  /// with their owner and go once the same foreman signs in again.
  Future<void> signedOut() async {
    _owner = null;
    _stopCollecting();
    _cancelTimers();
    _tokenFailures = 0;
    _askedPermission = false;
    _emit(const TrackingState());
    await tokens.clear();
  }

  // -------------------------------------------------------------- token

  /// `POST /tracking/token/` now (one call at a time). Returns the new
  /// token, or null when there is none (consent, disabled, offline…).
  Future<TrackingToken?> refreshToken() =>
      _refreshing ??= _refresh().whenComplete(() => _refreshing = null);

  Future<TrackingToken?> _refresh() async {
    final owner = _owner;
    if (owner == null) return null;
    _refreshTimer?.cancel();
    try {
      final token = await api.token();
      if (_owner != owner) return null;
      _lastAnswer = _now();
      _tokenFailures = 0;
      await tokens.write(owner, token);
      _emit(_state.copyWith(phase: TrackingPhase.active, token: () => token));
      _scheduleRefresh(token);
      _apply();
      _scheduleUpload(Duration.zero, now: true);
      return token;
    } on ApiException catch (e) {
      if (_owner != owner) return null;
      if (e.statusCode == 403 && e.code == TrackingErrors.consentRequired) {
        _lastAnswer = _now();
        await _dropToken();
        _emit(
          _state.copyWith(
            phase: await _consentPhase(),
            token: () => null,
            consentContext: ConsentContext.fromDetails(e.details),
          ),
        );
        _apply();
        return null;
      }
      if (e.statusCode == 403 && e.code == TrackingErrors.trackingDisabled) {
        _lastAnswer = _now();
        await _turnOff(refused: _state.refused);
        return null;
      }
      // 401: the session goes back to the PIN screen (ApiClient). Others
      // (404 before the server has tracking, 5xx…): like offline.
      if (e.statusCode != 401) _retryToken();
      return null;
    } on NetworkException {
      if (_owner == owner) _retryToken();
      return null;
    }
  }

  /// Offline or server trouble: keep what we have, ask again later (30 s,
  /// 1 min, 2 min… up to 15 min).
  void _retryToken() {
    _tokenFailures++;
    final n = _tokenFailures > 6 ? 6 : _tokenFailures;
    final wait = Duration(seconds: 15 << n); // 30 s … 16 min
    _refreshTimer?.cancel();
    _refreshTimer = Timer(
      wait > const Duration(minutes: 15) ? const Duration(minutes: 15) : wait,
      () => unawaited(refreshToken()),
    );
  }

  void _scheduleRefresh(TrackingToken token) {
    _refreshTimer?.cancel();
    final now = _now();
    final at = token.refreshAt(now);
    final wait = at.isAfter(now) ? at.difference(now) : Duration.zero;
    _refreshTimer = Timer(wait, () => unawaited(refreshToken()));
  }

  Future<void> _dropToken() async {
    _stopCollecting();
    await tokens.clear();
  }

  /// Off by settings or refused: no token, no collection, nothing queued.
  Future<void> _turnOff({required bool refused}) async {
    final owner = _owner;
    await _dropToken();
    if (owner != null) await observations.clearObservations(owner);
    _cancelUpload();
    _emit(
      _state.copyWith(
        phase: TrackingPhase.disabled,
        token: () => null,
        refused: refused,
        queued: 0,
        insideHours: false,
      ),
    );
    _apply();
  }

  // ------------------------------------------------------------ consent

  Future<List<OutboxEntry>> _pendingConsents() async => [
    for (final e in await store.loadOutbox())
      if (e.kind == OutboxKind.consent && e.state == OutboxState.pending) e,
  ];

  /// `consent_required`: is an answer already on its way?
  Future<TrackingPhase> _consentPhase() async {
    final pending = await _pendingConsents();
    if (pending.isEmpty) return TrackingPhase.consentRequired;
    return pending.last.payload['accepted'] == true
        ? TrackingPhase.consentPending
        : TrackingPhase.disabled;
  }

  Future<bool> _refusedHere(String owner) async {
    final raw = await store.getValue(TrackingKeys.consentAnswer(owner));
    if (raw == null) return false;
    try {
      return (jsonDecode(raw) as Map<String, dynamic>)['accepted'] == false;
    } catch (_) {
      return false;
    }
  }

  /// The foreman answered the notice. Queued in the outbox (works offline);
  /// a newer answer replaces one not sent yet.
  Future<void> answerConsent({required bool accepted}) async {
    final owner = _owner;
    if (owner == null) return;
    for (final e in await _pendingConsents()) {
      await store.deleteOutbox(e.id!);
    }
    final now = _now();
    final request = ConsentRequest(
      clientUuid: const Uuid().v4(),
      accepted: accepted,
      textVersion: consentTextVersion,
    );
    await store.insertOutbox(
      OutboxEntry(
        kind: OutboxKind.consent,
        siteId: 0,
        date: _clock().today(),
        clientUuid: request.clientUuid,
        payload: request.toJson(),
        createdAt: isoTimestamp(now),
      ),
    );
    await store.setValue(
      TrackingKeys.consentAnswer(owner),
      jsonEncode({
        'accepted': accepted,
        'text_version': consentTextVersion,
        'at': isoTimestamp(now),
      }),
    );
    if (accepted) {
      // Accepting again while tracking (the notice reopened from the menu)
      // changes nothing on the phone.
      _emit(
        _state.copyWith(
          phase: _state.phase == TrackingPhase.active
              ? TrackingPhase.active
              : TrackingPhase.consentPending,
          refused: false,
        ),
      );
    } else {
      await _turnOff(refused: true);
    }
    kickSync?.call();
  }

  /// Sends a queued answer (called by the sync service). Throws like any
  /// API call; the sync service keeps the entry and retries.
  Future<void> sendConsent(OutboxEntry entry) async {
    await api.consent(ConsentRequest.fromJson(entry.payload));
    // The entry leaves the outbox when this returns; then ask for a token
    // (a refusal gets `tracking_disabled`).
    _refreshTimer?.cancel();
    _refreshTimer = Timer(
      const Duration(seconds: 1),
      () => unawaited(refreshToken()),
    );
  }

  // --------------------------------------------------------- app inputs

  /// The app came to the foreground (or went to the background).
  void setForeground(bool foreground) {
    if (_foreground == foreground) return;
    _foreground = foreground;
    _apply();
    if (!foreground || _owner == null) return;
    final last = _lastAnswer;
    final stale =
        last == null || _now().difference(last) > const Duration(minutes: 30);
    if (_state.phase != TrackingPhase.active && stale) {
      unawaited(refreshToken());
    } else if (_state.phase == TrackingPhase.active) {
      _scheduleUpload(Duration.zero, now: true);
    }
  }

  /// The site on screen (badges are scanned for it).
  void setSite(int? siteId) {
    if (_siteId == siteId) return;
    _siteId = siteId;
    _apply();
  }

  /// A kiosk phone (shared, at the gate) records nothing.
  void setKiosk(bool kiosk) {
    if (_kiosk == kiosk) return;
    _kiosk = kiosk;
    _apply();
  }

  /// The network is back (the sync service reached the server).
  void online() {
    if (_owner == null) return;
    if (_state.phase == TrackingPhase.active) {
      _scheduleUpload(Duration.zero, now: true);
    } else if (_state.phase == TrackingPhase.unknown ||
        _state.phase == TrackingPhase.consentPending) {
      unawaited(refreshToken());
    }
  }

  // --------------------------------------------------------- collection

  /// Starts or stops GPS and badge scanning to match the state, the clock
  /// and the screen. Called on every change and at each window boundary.
  void _apply() {
    if (_disposed) return;
    _hoursTimer?.cancel();
    final token = _state.token;
    final collecting =
        _owner != null &&
        _state.phase == TrackingPhase.active &&
        token != null &&
        !_kiosk;
    final local = _clock().local();
    final inside = collecting && token.hours.contains(local);
    if (collecting) {
      final next = token.hours.untilNextChange(local);
      if (next != null) _hoursTimer = Timer(next, _apply);
    }
    if (inside != _state.insideHours) {
      _emit(_state.copyWith(insideHours: inside));
    }

    // GPS: started in the foreground, kept in the background.
    if (inside) {
      if (_gps != null && _gpsInterval != token.gpsInterval) _stopGps();
      if (_gps == null && !_gpsStarting && _foreground) {
        unawaited(_startGps(token.gpsInterval));
      }
    } else {
      _stopGps();
    }

    // Badges: in the foreground, on a site of the token.
    final site = _siteId;
    final bleSite =
        inside && _foreground && site != null && token.sites.contains(site)
        ? site
        : null;
    if (bleSite != _state.scanningSiteId) {
      _stopBle();
      if (bleSite != null) _startBle(bleSite);
    }
  }

  Future<void> _startGps(Duration interval) async {
    _gpsStarting = true;
    try {
      final access = await location.access(request: !_askedPermission);
      _askedPermission = true;
      if (_state.gps != access) _emit(_state.copyWith(gps: access));
      if (access != LocationAccess.ready) return;
      // Still wanted after the permission dialog?
      final token = _state.token;
      if (_owner == null ||
          _kiosk ||
          !_state.insideHours ||
          token == null ||
          _gps != null) {
        return;
      }
      _gpsFilter
        ..interval = interval
        ..reset();
      _gpsInterval = interval;
      _gps = location
          .positions(interval: interval, notice: _notice())
          .listen(
            (fix) => unawaited(_onFix(fix)),
            onError: (Object _) => _stopGps(),
            onDone: _stopGps,
          );
      _emit(_state.copyWith(recordingGps: true));
    } finally {
      _gpsStarting = false;
    }
  }

  void _stopGps() {
    final sub = _gps;
    _gps = null;
    _gpsInterval = null;
    if (sub != null) unawaited(sub.cancel());
    if (_state.recordingGps) _emit(_state.copyWith(recordingGps: false));
  }

  Future<void> _onFix(TrackingFix fix) async {
    final owner = _owner, token = _state.token;
    if (owner == null || token == null) return;
    // The hours timer may fire late (the phone asleep): check the fix's
    // own time.
    if (!token.hours.contains(_clock().localOf(fix.at))) return;
    switch (_gpsFilter.check(fix)) {
      case FixRejection.mocked:
        _emit(_state.copyWith(mockedFixes: _state.mockedFixes + 1));
      case FixRejection.invalid || FixRejection.tooSoon:
        break;
      case null:
        await record(gpsObservation(owner: owner, fix: fix));
    }
  }

  void _startBle(int siteId) {
    _ble = scanner.sightings().listen(
      (s) => unawaited(_onSighting(siteId, s)),
      onError: (Object _) {},
    );
    _emit(_state.copyWith(scanningSiteId: () => siteId));
  }

  void _stopBle() {
    final sub = _ble;
    _ble = null;
    if (sub != null) unawaited(sub.cancel());
    if (_state.scanningSiteId != null) {
      _emit(_state.copyWith(scanningSiteId: () => null));
    }
  }

  Future<void> _onSighting(int siteId, BadgeSighting s) async {
    final owner = _owner, token = _state.token;
    if (owner == null || token == null) return;
    if (!token.hours.contains(_clock().localOf(s.at))) return;
    if (!_deduper.accept(siteId, s.badgeId, s.at)) return;
    await record(
      bleObservation(
        owner: owner,
        siteId: siteId,
        badgeId: s.badgeId,
        rssi: s.rssi,
        at: s.at,
      ),
    );
  }

  void _stopCollecting() {
    _stopGps();
    _stopBle();
    _deduper.clear();
  }

  // ------------------------------------------------------------- upload

  /// Queues an observation (the phone's outbox) and schedules a send.
  Future<void> record(QueuedObservation o) async {
    await observations.addObservations([o]);
    _emit(_state.copyWith(queued: _state.queued + 1));
    _scheduleUpload(uploadDelay);
  }

  void _scheduleUpload(Duration wait, {bool now = false}) {
    if (_owner == null || _disposed) return;
    if (now) _uploadNotBefore = null;
    final notBefore = _uploadNotBefore;
    final at = _now();
    if (notBefore != null && at.add(wait).isBefore(notBefore)) {
      wait = notBefore.difference(at);
    }
    final due = at.add(wait);
    final current = _uploadDue;
    if (_uploadTimer != null && current != null && !due.isBefore(current)) {
      return;
    }
    _uploadTimer?.cancel();
    _uploadDue = due;
    _uploadTimer = Timer(wait, () {
      _uploadTimer = null;
      _uploadDue = null;
      unawaited(uploadNow());
    });
  }

  void _cancelUpload() {
    _uploadTimer?.cancel();
    _uploadTimer = null;
    _uploadDue = null;
    _uploadNotBefore = null;
  }

  /// Sends the queue now. Returns how it went (handy in tests).
  Future<UploadReport?> uploadNow() async {
    final owner = _owner;
    if (owner == null) return null;
    if (_state.phase != TrackingPhase.active) {
      return const UploadReport(UploadOutcome.noToken);
    }
    await observations.pruneObservations(
      before: isoTimestamp(_now().subtract(keepFor)),
      keepAtMost: keepAtMost,
    );
    final report = await uploader.drain(owner);
    if (_owner != owner) return report;
    _emit(_state.copyWith(queued: await observations.countObservations(owner)));
    if (report.outcome == UploadOutcome.retryLater) {
      final wait = report.retryIn ?? uploadDelay;
      _uploadNotBefore = _now().add(wait);
      _scheduleUpload(wait);
    } else {
      _uploadNotBefore = null;
    }
    return report;
  }

  // ------------------------------------------------------------ cleanup

  void _cancelTimers() {
    _refreshTimer?.cancel();
    _hoursTimer?.cancel();
    _cancelUpload();
  }

  void dispose() {
    _disposed = true;
    _stopCollecting();
    _cancelTimers();
  }
}
