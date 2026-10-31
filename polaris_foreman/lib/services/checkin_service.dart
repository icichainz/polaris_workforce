import 'dart:convert';

import 'package:uuid/uuid.dart';

import '../core/checkin_rules.dart';
import '../core/dates.dart';
import '../core/site_clock.dart';
import '../data/local/local_store.dart';
import '../data/models/models.dart';
import 'location_service.dart';
import 'roll_call_service.dart';

/// What the phone can tell right after a scan, before the server answers.
class ScanResult {
  const ScanResult({
    required this.checkIn,
    required this.workerId,
    required this.flags,
  });

  final LocalCheckIn checkIn;

  /// The worker whose badge it is (offline match), or null.
  final int? workerId;

  /// What the phone already sees: `unknown_badge` (not in today's badge
  /// list; the server decides), `not_on_crew`, `outside_geofence`,
  /// `duplicate`. The server's own flags replace these once it answers.
  final List<CheckInFlag> flags;

  bool get recognised => workerId != null;
}

/// Badge scans and check-ins: every check-in is saved on the phone and queued
/// in the outbox (`client_uuid` and `at` taken at the scan, before any
/// network), then [onWrite] wakes the sync service. Suggestions from the
/// check-ins pre-fill the roll call; marks the foreman changed by hand win.
class CheckInService {
  CheckInService(
    this.store, {
    required this.clock,
    required this.rollCall,
    String Function()? newUuid,
    this.onWrite,
  }) : _newUuid = newUuid ?? (() => const Uuid().v4());

  final LocalStore store;
  final SiteClock clock;
  final RollCallService rollCall;
  final String Function() _newUuid;
  final void Function()? onWrite;

  /// Two scans of the same worker and direction closer than this are
  /// duplicates (the server flags them too).
  static const duplicateWindow = Duration(minutes: 5);

  // ------------------------------------------------------------------ cache

  /// `GET /checkin/device/` as last seen (null before the first sync).
  Future<CheckInDevice?> cachedDevice() async {
    final raw = await store.getValue(StoreKeys.checkinDevice);
    if (raw == null) return null;
    return CheckInDevice.fromJson(jsonDecode(raw) as Map<String, dynamic>);
  }

  /// The site's setup as last fetched (any day: a stale badge list still
  /// names most workers; the server decides anyway).
  Future<CheckInSetup?> cachedSetup(int siteId) async {
    final raw = await store.getValue(StoreKeys.checkinSetup(siteId));
    if (raw == null) return null;
    final json = jsonDecode(raw) as Map<String, dynamic>;
    return CheckInSetup.fromJson(json['setup'] as Map<String, dynamic>);
  }

  /// The day the cached setup was fetched for.
  Future<String?> cachedSetupDate(int siteId) async {
    final raw = await store.getValue(StoreKeys.checkinSetup(siteId));
    if (raw == null) return null;
    return (jsonDecode(raw) as Map<String, dynamic>)['date'] as String?;
  }

  Future<void> saveSetup(CheckInSetup setup, String date) => store.setValue(
    StoreKeys.checkinSetup(setup.siteId),
    jsonEncode({'date': date, 'setup': setup.toJson()}),
  );

  Future<RollCallSuggestions?> cachedSuggestions(
    int siteId,
    String date,
  ) async {
    final raw = await store.getValue(StoreKeys.suggestions(siteId, date));
    if (raw == null) return null;
    return RollCallSuggestions.fromJson(
      jsonDecode(raw) as Map<String, dynamic>,
    );
  }

  /// The attendance mode as last seen (device role, else the site setup).
  Future<AttendanceMode?> mode(int siteId) async =>
      (await cachedDevice())?.attendanceMode ??
      (await cachedSetup(siteId))?.attendanceMode;

  // ------------------------------------------------------------------ scans

  /// Records a badge scan ([text] exactly as read).
  Future<ScanResult> scan({
    required int siteId,
    required String text,
    required CheckInDirection direction,
    GpsFix? fix,
    String? photoPath,
    bool kiosk = false,
  }) async {
    final setup = await cachedSetup(siteId);
    final workerId = recogniseBadge(text, setup?.badges ?? const []);
    return _record(
      siteId: siteId,
      direction: direction,
      badge: text,
      workerId: workerId,
      fix: fix,
      photoPath: photoPath,
      kiosk: kiosk,
      setup: setup,
    );
  }

  /// A check-in of a worker picked by hand (no badge; not on a kiosk).
  Future<ScanResult> manual({
    required int siteId,
    required int workerId,
    required CheckInDirection direction,
    GpsFix? fix,
    String? photoPath,
  }) async => _record(
    siteId: siteId,
    direction: direction,
    workerId: workerId,
    fix: fix,
    photoPath: photoPath,
    setup: await cachedSetup(siteId),
  );

  Future<ScanResult> _record({
    required int siteId,
    required CheckInDirection direction,
    String? badge,
    int? workerId,
    GpsFix? fix,
    String? photoPath,
    bool kiosk = false,
    CheckInSetup? setup,
  }) async {
    final instant = clock.instant();
    final date = isoDate(clock.localOf(instant));
    final checkIn = LocalCheckIn(
      clientUuid: _newUuid(),
      siteId: siteId,
      date: date,
      direction: direction,
      at: isoTimestamp(instant),
      badge: badge,
      workerId: workerId,
      latitude: fix?.latitude,
      longitude: fix?.longitude,
      accuracyM: fix?.accuracyM,
      photoPath: photoPath,
      kiosk: kiosk,
    );
    final flags = <CheckInFlag>[];
    if (workerId == null) {
      flags.add(CheckInFlag.unknownBadge);
    } else {
      final crew = await store.loadCrew(siteId);
      if (crew != null && !crew.workers.any((w) => w.id == workerId)) {
        flags.add(CheckInFlag.notOnCrew);
      }
      final earlier = await store.loadCheckIns(siteId, date);
      if (earlier.any(
        (c) =>
            c.knownWorkerId == workerId &&
            c.direction == direction &&
            !instant.isBefore(c.instant) &&
            instant.difference(c.instant) < duplicateWindow,
      )) {
        flags.add(CheckInFlag.duplicate);
      }
    }
    final fence = setup?.geofence;
    if (fence != null &&
        fix != null &&
        !insideGeofence(fence, fix.latitude, fix.longitude, fix.accuracyM)) {
      flags.add(CheckInFlag.outsideGeofence);
    }

    await store.upsertCheckIn(checkIn);
    await store.insertOutbox(
      OutboxEntry(
        kind: OutboxKind.checkin,
        siteId: siteId,
        date: date,
        clientUuid: checkIn.clientUuid,
        payload: {
          ...checkIn.toRequest().toJson(),
          if (photoPath != null) 'photo_path': photoPath,
        },
        createdAt: checkIn.at,
      ),
    );
    if (!kiosk) await applySuggestions(siteId, date);
    onWrite?.call();
    return ScanResult(checkIn: checkIn, workerId: workerId, flags: flags);
  }

  // ------------------------------------------------------------ suggestions

  /// The server's suggestions (cached) joined with this phone's own scans,
  /// for the crew workers.
  Future<List<RollCallSuggestion>> suggestions(int siteId, String date) async {
    final server = await cachedSuggestions(siteId, date);
    final setup = await cachedSetup(siteId);
    final crew = await store.loadCrew(siteId);
    final crewIds = crew?.workers.map((w) => w.id).toSet();
    final local = [
      for (final c in await store.loadCheckIns(siteId, date))
        if (c.knownWorkerId != null &&
            (c.server?.counted ?? true) &&
            (crewIds == null || crewIds.contains(c.knownWorkerId)))
          CheckInEvent(c.knownWorkerId!, c.direction, c.instant),
    ];
    final merged = mergeSuggestions(
      server?.suggestions ?? const [],
      local,
      lateAfter: setup?.lateAfter ?? server?.lateAfter,
      localOf: clock.localOf,
    );
    return [
      for (final s in merged)
        if (crewIds == null || crewIds.contains(s.workerId)) s,
    ];
  }

  /// What was pre-filled per worker (a mark still equal to it was not
  /// changed by hand).
  Future<Map<int, SuggestedMark>> prefilled(int siteId, String date) async {
    final raw = await store.getValue(StoreKeys.prefilled(siteId, date));
    if (raw == null) return {};
    final json = jsonDecode(raw) as Map<String, dynamic>;
    return {
      for (final e in json.entries)
        int.parse(e.key): SuggestedMark.fromJson(
          e.value as Map<String, dynamic>,
        ),
    };
  }

  /// Pre-fills the roll call from the suggestions: a worker's mark takes the
  /// suggested status, arrival and departure unless the foreman already
  /// marked them (a mark still holding what was pre-filled earlier follows
  /// newer check-ins; one changed by hand is left alone). Workers without a
  /// suggestion stay unmarked. Returns the number of marks written.
  ///
  /// Not on a kiosk phone (no roll call there) and not in `self_check_in`
  /// mode (the server writes the drafts; the phone refreshes the day).
  Future<int> applySuggestions(int siteId, String date) async {
    final device = await cachedDevice();
    if (device?.isKiosk ?? false) return 0;
    if (await mode(siteId) == AttendanceMode.selfCheckIn) return 0;
    final status = effectiveDayStatus(
      await store.loadDay(siteId, date),
      await store.loadOutbox(),
    );
    if (!status.editable) return 0;
    final before = await prefilled(siteId, date);
    final after = {...before};
    final marks = <int, SuggestedMark>{};
    for (final s in await suggestions(siteId, date)) {
      final existing = await store.loadRecord(siteId, date, s.workerId);
      final previous = before[s.workerId];
      final untouched =
          existing == null || (previous != null && previous.matches(existing));
      if (!untouched) continue;
      final st = s.status ?? existing?.status;
      if (st == null) continue; // Only a check-out: the foreman decides.
      final mark = SuggestedMark(
        st,
        s.arrival ?? existing?.arrival,
        s.departure ?? existing?.departure,
      );
      after[s.workerId] = mark;
      if (existing != null && mark.matches(existing)) continue;
      marks[s.workerId] = mark;
    }
    if (marks.isNotEmpty) {
      await rollCall.prefill(siteId: siteId, date: date, marks: marks);
    }
    if (after.length != before.length || marks.isNotEmpty) {
      await store.setValue(
        StoreKeys.prefilled(siteId, date),
        jsonEncode({
          for (final e in after.entries) '${e.key}': e.value.toJson(),
        }),
      );
    }
    return marks.length;
  }
}
