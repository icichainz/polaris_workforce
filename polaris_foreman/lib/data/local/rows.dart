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

  /// A record from the server. Every record carries its day's `site_id` and
  /// `date` (contract 1.1), so it can be stored without its day.
  factory LocalRecord.fromServer(AttendanceRecord r) => LocalRecord(
    siteId: r.siteId,
    date: r.date,
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

/// A check-in made on this phone (badge scan or manual), kept for the day.
/// [server] is set once the server has stored it; until then the phone shows
/// what it could work out offline ([workerId] from the badge hashes of
/// `checkin-setup`).
class LocalCheckIn {
  const LocalCheckIn({
    required this.clientUuid,
    required this.siteId,
    required this.date,
    required this.direction,
    required this.at,
    this.badge,
    this.workerId,
    this.latitude,
    this.longitude,
    this.accuracyM,
    this.photoPath,
    this.kiosk = false,
    this.server,
  });

  factory LocalCheckIn.fromJson(Map<String, dynamic> json) => LocalCheckIn(
    clientUuid: json['client_uuid'] as String,
    siteId: json['site_id'] as int,
    date: json['date'] as String,
    direction: CheckInDirection.fromWire(json['direction'] as String),
    at: json['at'] as String,
    badge: json['badge'] as String?,
    workerId: json['worker_id'] as int?,
    latitude: (json['latitude'] as num?)?.toDouble(),
    longitude: (json['longitude'] as num?)?.toDouble(),
    accuracyM: json['accuracy_m'] as int?,
    photoPath: json['photo_path'] as String?,
    kiosk: (json['kiosk'] as bool?) ?? false,
    server: json['server'] == null
        ? null
        : CheckIn.fromJson(json['server'] as Map<String, dynamic>),
  );

  final String clientUuid;
  final int siteId;

  /// Work day on the site (company time zone).
  final String date;
  final CheckInDirection direction;

  /// ISO timestamp of the scan (phone clock).
  final String at;

  /// The scanned QR text; null for a manual check-in.
  final String? badge;

  /// Recognised offline (badge hash) or picked by hand; the server's answer
  /// wins once there is one ([knownWorkerId]).
  final int? workerId;
  final double? latitude;
  final double? longitude;
  final int? accuracyM;

  /// The compressed photo, kept until the server has the check-in.
  final String? photoPath;

  /// Scanned on a kiosk phone.
  final bool kiosk;

  /// The server's answer (flags, `counted`, its worker id).
  final CheckIn? server;

  bool get sent => server != null;

  /// The worker as best known: the server's answer, else the offline match.
  int? get knownWorkerId => server != null ? server!.workerId : workerId;

  DateTime get instant => DateTime.parse(at);

  CheckInCreateRequest toRequest() => CheckInCreateRequest(
    clientUuid: clientUuid,
    siteId: siteId,
    direction: direction,
    at: at,
    badge: badge,
    workerId: badge == null ? workerId : null,
    latitude: latitude,
    longitude: longitude,
    accuracyM: accuracyM,
  );

  /// The server's answer; the photo file is dropped once it is stored.
  LocalCheckIn withServer(CheckIn checkIn) => LocalCheckIn(
    clientUuid: clientUuid,
    siteId: siteId,
    date: date,
    direction: direction,
    at: at,
    badge: badge,
    workerId: workerId,
    latitude: latitude,
    longitude: longitude,
    accuracyM: accuracyM,
    kiosk: kiosk,
    server: checkIn,
  );

  Map<String, dynamic> toJson() => {
    'client_uuid': clientUuid,
    'site_id': siteId,
    'date': date,
    'direction': direction.wire,
    'at': at,
    'badge': badge,
    'worker_id': workerId,
    'latitude': latitude,
    'longitude': longitude,
    'accuracy_m': accuracyM,
    'photo_path': photoPath,
    'kiosk': kiosk,
    'server': server?.toJson(),
  };
}
