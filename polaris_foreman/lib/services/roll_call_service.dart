import 'package:uuid/uuid.dart';

import '../core/dates.dart';
import '../core/site_clock.dart';
import '../data/local/local_store.dart';
import '../data/models/models.dart';

String _uuid() => const Uuid().v4();

/// Default half-day departure (the artboards' "leaves 12:00").
const halfDayDeparture = '12:00';

/// Submitting with workers left unmarked (the server would answer 409).
class UnmarkedWorkersException implements Exception {
  const UnmarkedWorkersException(this.workerIds);
  final List<int> workerIds;
}

/// The day can no longer be changed this way (submitted → use a correction;
/// locked → read only).
class DayNotEditableException implements Exception {
  const DayNotEditableException(this.status);
  final DayStatus status;
}

/// Day status as the foreman should see it: a submit still in the outbox
/// counts as submitted.
DayStatus effectiveDayStatus(
  AttendanceDaySummary? day,
  Iterable<OutboxEntry> outbox,
) {
  final status = day?.status ?? DayStatus.notStarted;
  if (!status.editable) return status;
  final pendingSubmit = outbox.any(
    (e) =>
        e.kind == OutboxKind.submit &&
        e.state == OutboxState.pending &&
        day != null &&
        e.siteId == day.siteId &&
        e.date == day.date,
  );
  return pendingSubmit ? DayStatus.submitted : status;
}

/// Morning roll call: every change goes to the local store and the outbox
/// first, then [onWrite] wakes the sync service.
class RollCallService {
  RollCallService(
    this.store, {
    required this.clock,
    String Function()? newUuid,
    this.onWrite,
  }) : _newUuid = newUuid ?? _uuid;

  final LocalStore store;
  final SiteClock clock;
  final String Function() _newUuid;
  final void Function()? onWrite;

  Future<DayStatus> _status(int siteId, String date) async =>
      effectiveDayStatus(
        await store.loadDay(siteId, date) ??
            _blankDay(siteId, date, DayStatus.notStarted),
        await store.loadOutbox(),
      );

  /// Default arrival/departure for a status: present and half day arrive at
  /// [workStart], late arrives now, half day leaves at 12:00.
  ({String? arrival, String? departure}) defaultTimes(
    AttendanceStatus status,
    String? workStart,
  ) {
    switch (status) {
      case AttendanceStatus.present:
        return (arrival: workStart, departure: null);
      case AttendanceStatus.halfDay:
        return (arrival: workStart, departure: halfDayDeparture);
      case AttendanceStatus.late:
        final now = clock.nowHm();
        final arrival = workStart != null && now.compareTo(workStart) <= 0
            ? workStart
            : now;
        return (arrival: arrival, departure: null);
      case AttendanceStatus.absent:
        return (arrival: null, departure: null);
    }
  }

  /// Marks one worker. Reuses the worker's `client_uuid` for the day.
  Future<LocalRecord> mark({
    required int siteId,
    required String date,
    required int workerId,
    required AttendanceStatus status,
    String? workStart,
  }) async {
    final out = await markMany(
      siteId: siteId,
      date: date,
      marks: {workerId: status},
      workStart: workStart,
    );
    return out.single;
  }

  /// Marks several workers in one write ("All P").
  Future<List<LocalRecord>> markMany({
    required int siteId,
    required String date,
    required Map<int, AttendanceStatus> marks,
    String? workStart,
  }) async {
    final status = await _status(siteId, date);
    if (!status.editable) throw DayNotEditableException(status);
    final records = <LocalRecord>[];
    for (final m in marks.entries) {
      final existing = await store.loadRecord(siteId, date, m.key);
      final keepTimes = existing != null && existing.status == m.value;
      final times = defaultTimes(m.value, workStart);
      records.add(
        LocalRecord(
          siteId: siteId,
          date: date,
          workerId: m.key,
          clientUuid: existing?.clientUuid ?? _newUuid(),
          serverId: existing?.serverId,
          dayId: existing?.dayId,
          status: m.value,
          arrival: keepTimes ? existing.arrival : times.arrival,
          departure: keepTimes ? existing.departure : times.departure,
          note: existing?.note ?? '',
          corrected: existing?.corrected ?? false,
          updatedAt: existing?.updatedAt,
        ),
      );
    }
    await store.upsertRecords(records);
    await _queueRecords(siteId, date, records);
    onWrite?.call();
    return records;
  }

  /// Changes the arrival or departure time of a marked worker.
  Future<void> setTimes({
    required int siteId,
    required String date,
    required int workerId,
    String? Function()? arrival,
    String? Function()? departure,
  }) async {
    final status = await _status(siteId, date);
    if (!status.editable) throw DayNotEditableException(status);
    final existing = await store.loadRecord(siteId, date, workerId);
    if (existing == null) return;
    final r = existing.copyWith(arrival: arrival, departure: departure);
    await store.upsertRecords([r]);
    await _queueRecords(siteId, date, [r]);
    onWrite?.call();
  }

  /// One records entry per site and day; later edits merge into it.
  Future<void> _queueRecords(
    int siteId,
    String date,
    List<LocalRecord> records,
  ) async {
    final outbox = await store.loadOutbox();
    final existing = outbox
        .where(
          (e) =>
              e.kind == OutboxKind.records &&
              e.siteId == siteId &&
              e.date == date,
        )
        .firstOrNull;
    final merged = <String, dynamic>{
      ...?(existing?.payload['records'] as Map<String, dynamic>?),
      for (final r in records) '${r.workerId}': r.toUpsert().toJson(),
    };
    if (existing == null) {
      await store.insertOutbox(
        OutboxEntry(
          kind: OutboxKind.records,
          siteId: siteId,
          date: date,
          clientUuid: '',
          payload: {'records': merged},
          createdAt: isoTimestamp(clock.instant()),
        ),
      );
    } else {
      await store.updateOutbox(
        existing.copyWith(
          payload: {'records': merged},
          version: existing.version + 1,
          state: OutboxState.pending,
          errorCode: () => null,
          errorMessage: () => null,
        ),
      );
    }
  }

  /// Submits the roll call. Every crew member must be marked.
  Future<void> submit({
    required int siteId,
    required String date,
    required Iterable<int> crewWorkerIds,
    String foremanNote = '',
    PersonRef? foreman,
  }) async {
    final status = await _status(siteId, date);
    if (!status.editable) throw DayNotEditableException(status);
    final marked = (await store.loadRecords(
      siteId,
      date,
    )).map((r) => r.workerId).toSet();
    final unmarked = crewWorkerIds.where((id) => !marked.contains(id)).toList();
    if (unmarked.isNotEmpty) throw UnmarkedWorkersException(unmarked);

    final outbox = await store.loadOutbox();
    final previous = outbox
        .where(
          (e) =>
              e.kind == OutboxKind.submit &&
              e.siteId == siteId &&
              e.date == date,
        )
        .firstOrNull;
    // When Submit was tapped, by the phone clock (1.1 `submitted_at`): the
    // write may reach the server hours later.
    final tappedAt = isoTimestamp(clock.instant());
    // A submit still waiting keeps its client_uuid (only the note changes);
    // a rejected day is submitted again with a new one.
    if (previous != null && previous.state == OutboxState.pending) {
      final payload = SubmitDayRequest(
        clientUuid: previous.clientUuid,
        foremanNote: foremanNote,
        submittedAt: tappedAt,
      ).toJson();
      await store.updateOutbox(
        previous.copyWith(payload: payload, version: previous.version + 1),
      );
    } else {
      if (previous != null) await store.deleteOutbox(previous.id!);
      final fresh = SubmitDayRequest(
        clientUuid: _newUuid(),
        foremanNote: foremanNote,
        submittedAt: tappedAt,
      );
      await store.insertOutbox(
        OutboxEntry(
          kind: OutboxKind.submit,
          siteId: siteId,
          date: date,
          clientUuid: fresh.clientUuid,
          payload: fresh.toJson(),
          createdAt: tappedAt,
        ),
      );
    }

    final day =
        await store.loadDay(siteId, date) ??
        _blankDay(siteId, date, DayStatus.draft);
    await store.upsertDays([
      day.copyWith(
        status: DayStatus.submitted,
        foremanNote: foremanNote,
        submittedAt: tappedAt,
        submittedBy: foreman,
      ),
    ]);
    onWrite?.call();
  }

  /// Changes a record after submission; [reason] is required.
  Future<void> correct({
    required int siteId,
    required String date,
    required int workerId,
    required CorrectableField field,
    required String? newValue,
    required String reason,
  }) async {
    if (reason.trim().isEmpty) {
      throw ArgumentError.value(reason, 'reason', 'A reason is required');
    }
    final status = await _status(siteId, date);
    if (status.editable || status == DayStatus.locked) {
      throw DayNotEditableException(status);
    }
    final existing = await store.loadRecord(siteId, date, workerId);
    if (existing == null) throw StateError('No record for worker $workerId');
    final updated = switch (field) {
      CorrectableField.status => existing.copyWith(
        status: AttendanceStatus.fromWire(newValue!),
      ),
      CorrectableField.arrival => existing.copyWith(arrival: () => newValue),
      CorrectableField.departure => existing.copyWith(
        departure: () => newValue,
      ),
      CorrectableField.note => existing.copyWith(note: newValue ?? ''),
    };
    await store.upsertRecords([updated.copyWith(corrected: true)]);
    final uuid = _newUuid();
    await store.insertOutbox(
      OutboxEntry(
        kind: OutboxKind.correction,
        siteId: siteId,
        date: date,
        clientUuid: uuid,
        payload: {
          ...CorrectionCreateRequest(
            clientUuid: uuid,
            field: field,
            newValue: newValue,
            reason: reason.trim(),
          ).toJson(),
          'worker_id': workerId,
        },
        createdAt: isoTimestamp(clock.instant()),
      ),
    );
    onWrite?.call();
  }

  static AttendanceDaySummary _blankDay(
    int siteId,
    String date,
    DayStatus status,
  ) => AttendanceDaySummary(
    id: null,
    siteId: siteId,
    date: date,
    status: status,
    foremanNote: '',
    submittedAt: null,
    submittedBy: null,
    approvedAt: null,
    approvedBy: null,
    rejectionNote: '',
    counts: const DayCounts(
      expected: 0,
      present: 0,
      absent: 0,
      halfDay: 0,
      late: 0,
      unmarked: 0,
    ),
    payoutStatus: PayoutStatus.notOpen,
    updatedAt: null,
  );
}
