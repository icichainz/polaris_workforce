import '../models/models.dart';

/// An attendance record as kept on the phone. [clientUuid] is made the first
/// time the worker is marked that day and reused for every later change.
class LocalRecord {
  const LocalRecord({
    required this.siteId,
    required this.date,
    required this.workerId,
    required this.clientUuid,
    required this.status,
    this.serverId,
    this.dayId,
    this.arrival,
    this.departure,
    this.note = '',
    this.corrected = false,
    this.updatedAt,
  });

  factory LocalRecord.fromServer(
    AttendanceRecord r, {
    required int siteId,
    required String date,
  }) => LocalRecord(
    siteId: siteId,
    date: date,
    workerId: r.workerId,
    clientUuid: r.clientUuid,
    serverId: r.id,
    dayId: r.dayId,
    status: r.status,
    arrival: r.arrival,
    departure: r.departure,
    note: r.note,
    corrected: r.corrected,
    updatedAt: r.updatedAt,
  );

  final int siteId;
  final String date;
  final int workerId;
  final String clientUuid;
  final int? serverId;
  final int? dayId;
  final AttendanceStatus status;
  final String? arrival;
  final String? departure;
  final String note;
  final bool corrected;
  final String? updatedAt;

  LocalRecord copyWith({
    String? clientUuid,
    int? serverId,
    int? dayId,
    AttendanceStatus? status,
    String? Function()? arrival,
    String? Function()? departure,
    String? note,
    bool? corrected,
    String? updatedAt,
  }) => LocalRecord(
    siteId: siteId,
    date: date,
    workerId: workerId,
    clientUuid: clientUuid ?? this.clientUuid,
    serverId: serverId ?? this.serverId,
    dayId: dayId ?? this.dayId,
    status: status ?? this.status,
    arrival: arrival != null ? arrival() : this.arrival,
    departure: departure != null ? departure() : this.departure,
    note: note ?? this.note,
    corrected: corrected ?? this.corrected,
    updatedAt: updatedAt ?? this.updatedAt,
  );

  RecordUpsert toUpsert() => RecordUpsert(
    clientUuid: clientUuid,
    workerId: workerId,
    status: status,
    arrival: arrival,
    departure: departure,
    note: note,
  );
}

/// A payment made on the phone (possibly not sent yet).
class LocalPayment {
  const LocalPayment({
    required this.clientUuid,
    required this.siteId,
    required this.date,
    required this.payLineId,
    required this.workerId,
    required this.amount,
    required this.proofType,
    required this.paidAt,
    this.proofPath,
    this.serverId,
  });

  final String clientUuid;
  final int siteId;
  final String date;
  final int payLineId;
  final int workerId;
  final int amount;
  final ProofType proofType;
  final String? proofPath;

  /// ISO timestamp of the moment the cash was handed over.
  final String paidAt;

  /// Set once the server has stored the payment.
  final int? serverId;

  bool get sent => serverId != null;

  LocalPayment withServerId(int id) => LocalPayment(
    clientUuid: clientUuid,
    siteId: siteId,
    date: date,
    payLineId: payLineId,
    workerId: workerId,
    amount: amount,
    proofType: proofType,
    proofPath: proofPath,
    paidAt: paidAt,
    serverId: id,
  );
}
