import 'auth.dart';
import 'enums.dart';

class DayCounts {
  const DayCounts({
    required this.expected,
    required this.present,
    required this.absent,
    required this.halfDay,
    required this.late,
    required this.unmarked,
  });

  factory DayCounts.fromJson(Map<String, dynamic> json) => DayCounts(
    expected: json['expected'] as int,
    present: json['present'] as int,
    absent: json['absent'] as int,
    halfDay: json['half_day'] as int,
    late: json['late'] as int,
    unmarked: json['unmarked'] as int,
  );

  /// Counts for [statuses] out of [expected] workers.
  factory DayCounts.of(int expected, Iterable<AttendanceStatus> statuses) {
    var p = 0, a = 0, h = 0, l = 0;
    for (final s in statuses) {
      switch (s) {
        case AttendanceStatus.present:
          p++;
        case AttendanceStatus.absent:
          a++;
        case AttendanceStatus.halfDay:
          h++;
        case AttendanceStatus.late:
          l++;
      }
    }
    final marked = p + a + h + l;
    return DayCounts(
      expected: expected,
      present: p,
      absent: a,
      halfDay: h,
      late: l,
      unmarked: expected > marked ? expected - marked : 0,
    );
  }

  final int expected;
  final int present;
  final int absent;
  final int halfDay;
  final int late;
  final int unmarked;

  /// Workers on site at all (present, half day or late).
  int get onSite => present + halfDay + late;

  Map<String, dynamic> toJson() => {
    'expected': expected,
    'present': present,
    'absent': absent,
    'half_day': halfDay,
    'late': late,
    'unmarked': unmarked,
  };
}

class AttendanceRecord {
  const AttendanceRecord({
    required this.id,
    required this.clientUuid,
    required this.dayId,
    required this.siteId,
    required this.date,
    required this.workerId,
    required this.status,
    required this.arrival,
    required this.departure,
    required this.note,
    required this.corrected,
    required this.updatedAt,
  });

  factory AttendanceRecord.fromJson(Map<String, dynamic> json) =>
      AttendanceRecord(
        id: json['id'] as int,
        clientUuid: json['client_uuid'] as String,
        dayId: json['day_id'] as int,
        siteId: json['site_id'] as int,
        date: json['date'] as String,
        workerId: json['worker_id'] as int,
        status: AttendanceStatus.fromWire(json['status'] as String),
        arrival: json['arrival'] as String?,
        departure: json['departure'] as String?,
        note: json['note'] as String,
        corrected: json['corrected'] as bool,
        updatedAt: json['updated_at'] as String,
      );

  final int id;
  final String clientUuid;
  final int dayId;

  /// The day's site and date (1.1): a record can be stored without its day.
  final int siteId;
  final String date;
  final int workerId;
  final AttendanceStatus status;
  final String? arrival;
  final String? departure;
  final String note;
  final bool corrected;
  final String updatedAt;

  AttendanceRecord copyWith({
    String? clientUuid,
    AttendanceStatus? status,
    String? Function()? arrival,
    String? Function()? departure,
    String? note,
    bool? corrected,
    String? updatedAt,
  }) => AttendanceRecord(
    id: id,
    clientUuid: clientUuid ?? this.clientUuid,
    dayId: dayId,
    siteId: siteId,
    date: date,
    workerId: workerId,
    status: status ?? this.status,
    arrival: arrival != null ? arrival() : this.arrival,
    departure: departure != null ? departure() : this.departure,
    note: note ?? this.note,
    corrected: corrected ?? this.corrected,
    updatedAt: updatedAt ?? this.updatedAt,
  );

  Map<String, dynamic> toJson() => {
    'id': id,
    'client_uuid': clientUuid,
    'day_id': dayId,
    'site_id': siteId,
    'date': date,
    'worker_id': workerId,
    'status': status.wire,
    'arrival': arrival,
    'departure': departure,
    'note': note,
    'corrected': corrected,
    'updated_at': updatedAt,
  };
}

/// A day without its records (`GET /sites/{id}/days/`, `GET /sync/`).
class AttendanceDaySummary {
  const AttendanceDaySummary({
    required this.id,
    required this.siteId,
    required this.date,
    required this.status,
    required this.foremanNote,
    required this.submittedAt,
    required this.submittedBy,
    required this.approvedAt,
    required this.approvedBy,
    required this.rejectionNote,
    required this.counts,
    required this.payoutStatus,
    required this.updatedAt,
  });

  factory AttendanceDaySummary.fromJson(Map<String, dynamic> json) =>
      AttendanceDaySummary(
        id: json['id'] as int?,
        siteId: json['site_id'] as int,
        date: json['date'] as String,
        status: DayStatus.fromWire(json['status'] as String),
        foremanNote: json['foreman_note'] as String,
        submittedAt: json['submitted_at'] as String?,
        submittedBy: PersonRef.maybe(json['submitted_by']),
        approvedAt: json['approved_at'] as String?,
        approvedBy: PersonRef.maybe(json['approved_by']),
        rejectionNote: json['rejection_note'] as String,
        counts: DayCounts.fromJson(json['counts'] as Map<String, dynamic>),
        payoutStatus: PayoutStatus.fromWire(json['payout_status'] as String),
        updatedAt: json['updated_at'] as String?,
      );

  /// Null while the day is `not_started`.
  final int? id;
  final int siteId;
  final String date;
  final DayStatus status;
  final String foremanNote;
  final String? submittedAt;
  final PersonRef? submittedBy;
  final String? approvedAt;
  final PersonRef? approvedBy;
  final String rejectionNote;
  final DayCounts counts;
  final PayoutStatus payoutStatus;
  final String? updatedAt;

  AttendanceDaySummary copyWith({
    int? id,
    DayStatus? status,
    String? foremanNote,
    String? submittedAt,
    PersonRef? submittedBy,
    String? approvedAt,
    PersonRef? approvedBy,
    String? rejectionNote,
    DayCounts? counts,
    PayoutStatus? payoutStatus,
    String? updatedAt,
  }) => AttendanceDaySummary(
    id: id ?? this.id,
    siteId: siteId,
    date: date,
    status: status ?? this.status,
    foremanNote: foremanNote ?? this.foremanNote,
    submittedAt: submittedAt ?? this.submittedAt,
    submittedBy: submittedBy ?? this.submittedBy,
    approvedAt: approvedAt ?? this.approvedAt,
    approvedBy: approvedBy ?? this.approvedBy,
    rejectionNote: rejectionNote ?? this.rejectionNote,
    counts: counts ?? this.counts,
    payoutStatus: payoutStatus ?? this.payoutStatus,
    updatedAt: updatedAt ?? this.updatedAt,
  );

  Map<String, dynamic> toJson() => {
    'id': id,
    'site_id': siteId,
    'date': date,
    'status': status.wire,
    'foreman_note': foremanNote,
    'submitted_at': submittedAt,
    'submitted_by': submittedBy?.toJson(),
    'approved_at': approvedAt,
    'approved_by': approvedBy?.toJson(),
    'rejection_note': rejectionNote,
    'counts': counts.toJson(),
    'payout_status': payoutStatus.wire,
    'updated_at': updatedAt,
  };
}

/// `GET /sites/{id}/days/{date}/` and the records/submit responses.
class AttendanceDay {
  const AttendanceDay({required this.summary, required this.records});

  factory AttendanceDay.fromJson(Map<String, dynamic> json) => AttendanceDay(
    summary: AttendanceDaySummary.fromJson(json),
    records: (json['records'] as List<dynamic>)
        .map((e) => AttendanceRecord.fromJson(e as Map<String, dynamic>))
        .toList(),
  );

  final AttendanceDaySummary summary;
  final List<AttendanceRecord> records;

  Map<String, dynamic> toJson() => {
    ...summary.toJson(),
    'records': records.map((r) => r.toJson()).toList(),
  };
}

class AttendanceDayList {
  const AttendanceDayList({required this.siteId, required this.days});

  factory AttendanceDayList.fromJson(Map<String, dynamic> json) =>
      AttendanceDayList(
        siteId: json['site_id'] as int,
        days: (json['days'] as List<dynamic>)
            .map(
              (e) => AttendanceDaySummary.fromJson(e as Map<String, dynamic>),
            )
            .toList(),
      );

  final int siteId;
  final List<AttendanceDaySummary> days;

  Map<String, dynamic> toJson() => {
    'site_id': siteId,
    'days': days.map((d) => d.toJson()).toList(),
  };
}

/// One item of `PUT /sites/{id}/days/{date}/records/`.
class RecordUpsert {
  const RecordUpsert({
    required this.clientUuid,
    required this.workerId,
    required this.status,
    this.arrival,
    this.departure,
    this.note = '',
  });

  factory RecordUpsert.fromJson(Map<String, dynamic> json) => RecordUpsert(
    clientUuid: json['client_uuid'] as String,
    workerId: json['worker_id'] as int,
    status: AttendanceStatus.fromWire(json['status'] as String),
    arrival: json['arrival'] as String?,
    departure: json['departure'] as String?,
    note: (json['note'] as String?) ?? '',
  );

  final String clientUuid;
  final int workerId;
  final AttendanceStatus status;
  final String? arrival;
  final String? departure;
  final String note;

  Map<String, dynamic> toJson() => {
    'client_uuid': clientUuid,
    'worker_id': workerId,
    'status': status.wire,
    'arrival': arrival,
    'departure': departure,
    'note': note,
  };
}

class SubmitDayRequest {
  const SubmitDayRequest({
    required this.clientUuid,
    this.foremanNote = '',
    this.submittedAt,
  });

  factory SubmitDayRequest.fromJson(Map<String, dynamic> json) =>
      SubmitDayRequest(
        clientUuid: json['client_uuid'] as String,
        foremanNote: (json['foreman_note'] as String?) ?? '',
        submittedAt: json['submitted_at'] as String?,
      );

  final String clientUuid;
  final String foremanNote;

  /// Phone clock when the foreman tapped Submit (1.1, optional): the office
  /// flags a roll call that reached the server an hour or more later.
  final String? submittedAt;

  Map<String, dynamic> toJson() => {
    'client_uuid': clientUuid,
    'foreman_note': foremanNote,
    if (submittedAt != null) 'submitted_at': submittedAt,
  };
}

class Correction {
  const Correction({
    required this.id,
    required this.clientUuid,
    required this.recordId,
    required this.field,
    required this.oldValue,
    required this.newValue,
    required this.reason,
    required this.createdBy,
    required this.createdAt,
    required this.record,
    this.replayed = false,
  });

  factory Correction.fromJson(
    Map<String, dynamic> json, {
    bool replayed = false,
  }) => Correction(
    id: json['id'] as int,
    clientUuid: json['client_uuid'] as String,
    recordId: json['record_id'] as int,
    field: CorrectableField.fromWire(json['field'] as String),
    oldValue: json['old_value'] as String?,
    newValue: json['new_value'] as String?,
    reason: json['reason'] as String,
    createdBy: PersonRef.fromJson(json['created_by'] as Map<String, dynamic>),
    createdAt: json['created_at'] as String,
    record: AttendanceRecord.fromJson(json['record'] as Map<String, dynamic>),
    replayed: replayed,
  );

  final int id;
  final String clientUuid;
  final int recordId;
  final CorrectableField field;
  final String? oldValue;
  final String? newValue;
  final String reason;
  final PersonRef createdBy;
  final String createdAt;

  /// The record after the correction.
  final AttendanceRecord record;

  /// `200` + `Idempotent-Replayed: true`: the server already had this
  /// `client_uuid` and returned the original correction. Not part of the JSON.
  final bool replayed;

  Correction asReplayed() => Correction.fromJson(toJson(), replayed: true);

  Map<String, dynamic> toJson() => {
    'id': id,
    'client_uuid': clientUuid,
    'record_id': recordId,
    'field': field.wire,
    'old_value': oldValue,
    'new_value': newValue,
    'reason': reason,
    'created_by': createdBy.toJson(),
    'created_at': createdAt,
    'record': record.toJson(),
  };
}

class CorrectionCreateRequest {
  const CorrectionCreateRequest({
    required this.clientUuid,
    required this.field,
    required this.newValue,
    required this.reason,
  });

  factory CorrectionCreateRequest.fromJson(Map<String, dynamic> json) =>
      CorrectionCreateRequest(
        clientUuid: json['client_uuid'] as String,
        field: CorrectableField.fromWire(json['field'] as String),
        newValue: json['new_value'] as String?,
        reason: json['reason'] as String,
      );

  final String clientUuid;
  final CorrectableField field;
  final String? newValue;
  final String reason;

  Map<String, dynamic> toJson() => {
    'client_uuid': clientUuid,
    'field': field.wire,
    'new_value': newValue,
    'reason': reason,
  };
}

/// `GET /sync/?since=`.
class SyncResponse {
  const SyncResponse({
    required this.serverTime,
    required this.since,
    required this.days,
    required this.records,
    required this.corrections,
  });

  factory SyncResponse.fromJson(Map<String, dynamic> json) => SyncResponse(
    serverTime: json['server_time'] as String,
    since: json['since'] as String?,
    days: (json['days'] as List<dynamic>)
        .map((e) => AttendanceDaySummary.fromJson(e as Map<String, dynamic>))
        .toList(),
    records: (json['records'] as List<dynamic>)
        .map((e) => AttendanceRecord.fromJson(e as Map<String, dynamic>))
        .toList(),
    corrections: (json['corrections'] as List<dynamic>)
        .map((e) => Correction.fromJson(e as Map<String, dynamic>))
        .toList(),
  );

  /// Store exactly as received; send back as `since`.
  final String serverTime;
  final String? since;
  final List<AttendanceDaySummary> days;
  final List<AttendanceRecord> records;
  final List<Correction> corrections;

  Map<String, dynamic> toJson() => {
    'server_time': serverTime,
    'since': since,
    'days': days.map((d) => d.toJson()).toList(),
    'records': records.map((r) => r.toJson()).toList(),
    'corrections': corrections.map((c) => c.toJson()).toList(),
  };
}
