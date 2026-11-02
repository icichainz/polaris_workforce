/// Check-in (contract 1.2, tag `checkin`): QR badges, kiosk phones, check-ins
/// with GPS and photo, roll-call suggestions. See `docs/api/README.md`,
/// "Check-in (1.2)".
library;

import 'enums.dart';

T _parse<T extends Enum>(List<T> values, String wire, String Function(T) w) {
  for (final v in values) {
    if (w(v) == wire) return v;
  }
  throw FormatException('Unknown ${T.toString()} value: $wire');
}

double? _double(Object? v) => (v as num?)?.toDouble();

/// A company setting: who records attendance.
enum AttendanceMode {
  /// Roll call as before; check-in screens are optional.
  foremanRollCall('foreman_roll_call'),

  /// Workers scan; each counted check-in writes the worker's draft record on
  /// the server. The foreman reviews and submits.
  selfCheckIn('self_check_in'),

  /// Badge scans pre-fill the roll call (suggestions); the foreman confirms.
  both('both');

  const AttendanceMode(this.wire);
  final String wire;

  static AttendanceMode fromWire(String v) => _parse(values, v, (e) => e.wire);

  /// The server writes draft records from check-ins (refresh the day).
  bool get serverWritesDrafts => this == selfCheckIn;
}

enum CheckInDirection {
  checkIn('in'),
  checkOut('out');

  const CheckInDirection(this.wire);
  final String wire;

  static CheckInDirection fromWire(String v) =>
      _parse(values, v, (e) => e.wire);
}

enum CheckInSource {
  foremanScan('foreman_scan'),
  kiosk('kiosk'),
  manual('manual');

  const CheckInSource(this.wire);
  final String wire;

  static CheckInSource fromWire(String v) => _parse(values, v, (e) => e.wire);
}

/// What the server noticed about a check-in. A check-in is never refused for
/// what it says: these tell the phone what to show.
enum CheckInFlag {
  /// Not a badge of this company (made up, damaged, another company's).
  /// Not counted.
  unknownBadge('unknown_badge'),

  /// A revoked badge (a newer one exists, or it was reported lost). Not
  /// counted.
  revokedBadge('revoked_badge'),

  /// The worker is not on the site's crew that day. Counted.
  notOnCrew('not_on_crew'),

  /// GPS position outside the site's circle. Counted.
  outsideGeofence('outside_geofence'),

  /// Same worker and direction less than 5 minutes after another. Counted.
  duplicate('duplicate');

  const CheckInFlag(this.wire);
  final String wire;

  static CheckInFlag fromWire(String v) => _parse(values, v, (e) => e.wire);
}

/// `GET /checkin/device/` → `kiosk`.
class Kiosk {
  const Kiosk({
    required this.siteId,
    required this.siteName,
    required this.since,
  });

  factory Kiosk.fromJson(Map<String, dynamic> json) => Kiosk(
    siteId: json['site_id'] as int,
    siteName: json['site_name'] as String,
    since: json['since'] as String,
  );

  final int siteId;
  final String siteName;
  final String since;

  Map<String, dynamic> toJson() => {
    'site_id': siteId,
    'site_name': siteName,
    'since': since,
  };
}

/// `GET /checkin/device/`: this phone's check-in role.
class CheckInDevice {
  const CheckInDevice({required this.attendanceMode, required this.kiosk});

  factory CheckInDevice.fromJson(Map<String, dynamic> json) => CheckInDevice(
    attendanceMode: AttendanceMode.fromWire(json['attendance_mode'] as String),
    kiosk: json['kiosk'] == null
        ? null
        : Kiosk.fromJson(json['kiosk'] as Map<String, dynamic>),
  );

  final AttendanceMode attendanceMode;

  /// Set when the office switched this phone to kiosk mode for one site.
  final Kiosk? kiosk;

  bool get isKiosk => kiosk != null;

  Map<String, dynamic> toJson() => {
    'attendance_mode': attendanceMode.wire,
    'kiosk': kiosk?.toJson(),
  };
}

/// A site's circle (WGS 84).
class Geofence {
  const Geofence({
    required this.latitude,
    required this.longitude,
    required this.radiusM,
  });

  factory Geofence.fromJson(Map<String, dynamic> json) => Geofence(
    latitude: _double(json['latitude'])!,
    longitude: _double(json['longitude'])!,
    radiusM: json['radius_m'] as int,
  );

  final double latitude;
  final double longitude;
  final int radiusM;

  Map<String, dynamic> toJson() => {
    'latitude': latitude,
    'longitude': longitude,
    'radius_m': radiusM,
  };
}

/// An active badge of today's crew: the SHA-256 (hex) of its QR text.
class BadgeHash {
  const BadgeHash({
    required this.workerId,
    required this.payloadSha256,
    required this.issuedAt,
  });

  factory BadgeHash.fromJson(Map<String, dynamic> json) => BadgeHash(
    workerId: json['worker_id'] as int,
    payloadSha256: json['payload_sha256'] as String,
    issuedAt: json['issued_at'] as String,
  );

  final int workerId;
  final String payloadSha256;
  final String issuedAt;

  Map<String, dynamic> toJson() => {
    'worker_id': workerId,
    'payload_sha256': payloadSha256,
    'issued_at': issuedAt,
  };
}

/// `GET /sites/{id}/checkin-setup/`: what the phone needs to scan offline.
class CheckInSetup {
  const CheckInSetup({
    required this.siteId,
    required this.attendanceMode,
    required this.lateAfter,
    required this.kiosk,
    required this.geofence,
    required this.badges,
  });

  factory CheckInSetup.fromJson(Map<String, dynamic> json) => CheckInSetup(
    siteId: json['site_id'] as int,
    attendanceMode: AttendanceMode.fromWire(json['attendance_mode'] as String),
    lateAfter: json['late_after'] as String,
    kiosk: json['kiosk'] as bool,
    geofence: json['geofence'] == null
        ? null
        : Geofence.fromJson(json['geofence'] as Map<String, dynamic>),
    badges: [
      for (final b in json['badges'] as List<dynamic>)
        BadgeHash.fromJson(b as Map<String, dynamic>),
    ],
  );

  final int siteId;
  final AttendanceMode attendanceMode;

  /// `HH:MM`, site local time: a first `in` after it is late.
  final String lateAfter;

  /// This phone is the site's kiosk.
  final bool kiosk;
  final Geofence? geofence;
  final List<BadgeHash> badges;

  Map<String, dynamic> toJson() => {
    'site_id': siteId,
    'attendance_mode': attendanceMode.wire,
    'late_after': lateAfter,
    'kiosk': kiosk,
    'geofence': geofence?.toJson(),
    'badges': badges.map((b) => b.toJson()).toList(),
  };
}

/// `POST /checkins/` body (JSON, or multipart with a `photo`).
class CheckInCreateRequest {
  const CheckInCreateRequest({
    required this.clientUuid,
    required this.siteId,
    required this.direction,
    required this.at,
    this.badge,
    this.workerId,
    this.latitude,
    this.longitude,
    this.accuracyM,
  });

  factory CheckInCreateRequest.fromJson(Map<String, dynamic> json) =>
      CheckInCreateRequest(
        clientUuid: json['client_uuid'] as String,
        siteId: json['site_id'] as int,
        direction: CheckInDirection.fromWire(json['direction'] as String),
        at: json['at'] as String,
        badge: json['badge'] as String?,
        workerId: json['worker_id'] as int?,
        latitude: _double(json['latitude']),
        longitude: _double(json['longitude']),
        accuracyM: json['accuracy_m'] as int?,
      );

  final String clientUuid;
  final int siteId;
  final CheckInDirection direction;

  /// Phone clock at the scan (ISO 8601 with offset). Decides the work day.
  final String at;

  /// The scanned QR text exactly as read. Send this or [workerId].
  final String? badge;

  /// Manual check-in by the foreman (not on a kiosk).
  final int? workerId;

  /// WGS 84, 6 decimals; together or not at all.
  final double? latitude;
  final double? longitude;
  final int? accuracyM;

  /// Only the fields that are set (the server takes `badge` or `worker_id`).
  Map<String, dynamic> toJson() => {
    'client_uuid': clientUuid,
    'site_id': siteId,
    'direction': direction.wire,
    'at': at,
    if (badge != null) 'badge': badge,
    if (workerId != null) 'worker_id': workerId,
    if (latitude != null) 'latitude': latitude,
    if (longitude != null) 'longitude': longitude,
    if (accuracyM != null) 'accuracy_m': accuracyM,
  };
}

/// A recorded check-in or check-out.
class CheckIn {
  const CheckIn({
    required this.id,
    required this.clientUuid,
    required this.siteId,
    required this.workerId,
    required this.date,
    required this.direction,
    required this.at,
    required this.receivedAt,
    required this.source,
    required this.latitude,
    required this.longitude,
    required this.accuracyM,
    required this.distanceM,
    required this.insideGeofence,
    required this.counted,
    required this.draftUpdated,
    required this.flags,
    required this.photoUrl,
    this.replayed = false,
  });

  factory CheckIn.fromJson(
    Map<String, dynamic> json, {
    bool replayed = false,
  }) => CheckIn(
    id: json['id'] as int,
    clientUuid: json['client_uuid'] as String,
    siteId: json['site_id'] as int,
    workerId: json['worker_id'] as int?,
    date: json['date'] as String,
    direction: CheckInDirection.fromWire(json['direction'] as String),
    at: json['at'] as String,
    receivedAt: json['received_at'] as String,
    source: CheckInSource.fromWire(json['source'] as String),
    latitude: _double(json['latitude']),
    longitude: _double(json['longitude']),
    accuracyM: json['accuracy_m'] as int?,
    distanceM: json['distance_m'] as int?,
    insideGeofence: json['inside_geofence'] as bool?,
    counted: json['counted'] as bool,
    draftUpdated: json['draft_updated'] as bool,
    flags: [
      for (final f in json['flags'] as List<dynamic>)
        CheckInFlag.fromWire(f as String),
    ],
    photoUrl: json['photo_url'] as String?,
    replayed: replayed,
  );

  final int id;
  final String clientUuid;
  final int siteId;

  /// Null for a badge the server does not know.
  final int? workerId;

  /// Work day of [at] in the company time zone.
  final String date;
  final CheckInDirection direction;
  final String at;
  final String receivedAt;
  final CheckInSource source;
  final double? latitude;
  final double? longitude;
  final int? accuracyM;

  /// Distance from the geofence centre (null without GPS or geofence).
  final int? distanceM;
  final bool? insideGeofence;

  /// Counts towards the roll call (known worker, badge not revoked).
  final bool counted;

  /// `self_check_in`: this check-in wrote the worker's draft record.
  final bool draftUpdated;
  final List<CheckInFlag> flags;
  final String? photoUrl;

  /// `200` + `Idempotent-Replayed: true`. Not part of the JSON.
  final bool replayed;

  CheckIn asReplayed() => CheckIn.fromJson(toJson(), replayed: true);

  Map<String, dynamic> toJson() => {
    'id': id,
    'client_uuid': clientUuid,
    'site_id': siteId,
    'worker_id': workerId,
    'date': date,
    'direction': direction.wire,
    'at': at,
    'received_at': receivedAt,
    'source': source.wire,
    'latitude': latitude,
    'longitude': longitude,
    'accuracy_m': accuracyM,
    'distance_m': distanceM,
    'inside_geofence': insideGeofence,
    'counted': counted,
    'draft_updated': draftUpdated,
    'flags': flags.map((f) => f.wire).toList(),
    'photo_url': photoUrl,
  };
}

/// `GET /sites/{id}/checkins/{date}/`: oldest first.
class CheckInList {
  const CheckInList({
    required this.siteId,
    required this.date,
    required this.checkins,
  });

  factory CheckInList.fromJson(Map<String, dynamic> json) => CheckInList(
    siteId: json['site_id'] as int,
    date: json['date'] as String,
    checkins: [
      for (final c in json['checkins'] as List<dynamic>)
        CheckIn.fromJson(c as Map<String, dynamic>),
    ],
  );

  final int siteId;
  final String date;
  final List<CheckIn> checkins;

  Map<String, dynamic> toJson() => {
    'site_id': siteId,
    'date': date,
    'checkins': checkins.map((c) => c.toJson()).toList(),
  };
}

/// Where a roll-call suggestion comes from (contract 1.5, `sources`); also
/// what a record write says it was taken from (`from_suggestion`).
enum SuggestionSource {
  /// Counted check-ins of the worker.
  checkin('checkin'),

  /// Presence tracking (badge or phone seen on site).
  presence('presence');

  const SuggestionSource(this.wire);
  final String wire;

  static SuggestionSource fromWire(String v) =>
      _parse(values, v, (e) => e.wire);
}

/// What presence tracking saw of a worker on the site that day (1.5,
/// `suggestions[].presence`). Presence never marks attendance: the foreman
/// accepts it.
class SuggestionPresence {
  const SuggestionPresence({
    required this.firstSeenAt,
    required this.lastSeenAt,
    required this.open,
    required this.seenFrom,
    required this.seenUntil,
    required this.zones,
    required this.visits,
    required this.status,
  });

  factory SuggestionPresence.fromJson(Map<String, dynamic> json) =>
      SuggestionPresence(
        firstSeenAt: json['first_seen_at'] as String,
        lastSeenAt: json['last_seen_at'] as String?,
        open: json['open'] as bool,
        seenFrom: json['seen_from'] as String,
        seenUntil: json['seen_until'] as String?,
        zones: [for (final z in json['zones'] as List<dynamic>) z as String],
        visits: json['visits'] as int,
        status: AttendanceStatus.fromWire(json['status'] as String),
      );

  final String firstSeenAt;

  /// Null while the worker is still on site ([open]).
  final String? lastSeenAt;
  final bool open;

  /// `HH:MM`, company time zone.
  final String seenFrom;
  final String? seenUntil;

  /// Zone codes visited, in order (may be empty).
  final List<String> zones;
  final int visits;

  /// What the presence alone suggests.
  final AttendanceStatus status;

  Map<String, dynamic> toJson() => {
    'first_seen_at': firstSeenAt,
    'last_seen_at': lastSeenAt,
    'open': open,
    'seen_from': seenFrom,
    'seen_until': seenUntil,
    'zones': zones,
    'visits': visits,
    'status': status.wire,
  };
}

/// One crew worker's suggested mark, from their counted check-ins, their
/// presence on site, or both (the check-ins win; the presence is shown).
class RollCallSuggestion {
  const RollCallSuggestion({
    required this.workerId,
    required this.status,
    required this.arrival,
    required this.departure,
    required this.firstInAt,
    required this.lastOutAt,
    required this.checkinCount,
    this.sources = const [SuggestionSource.checkin],
    this.presence,
  });

  factory RollCallSuggestion.fromJson(Map<String, dynamic> json) =>
      RollCallSuggestion(
        workerId: json['worker_id'] as int,
        status: json['status'] == null
            ? null
            : AttendanceStatus.fromWire(json['status'] as String),
        arrival: json['arrival'] as String?,
        departure: json['departure'] as String?,
        firstInAt: json['first_in_at'] as String?,
        lastOutAt: json['last_out_at'] as String?,
        checkinCount: json['checkin_count'] as int,
        // A 1.4 server (or a cached answer of one) only had check-ins.
        sources: json['sources'] == null
            ? const [SuggestionSource.checkin]
            : [
                for (final s in json['sources'] as List<dynamic>)
                  SuggestionSource.fromWire(s as String),
              ],
        presence: json['presence'] == null
            ? null
            : SuggestionPresence.fromJson(
                json['presence'] as Map<String, dynamic>,
              ),
      );

  final int workerId;

  /// Never `absent`. Null when the worker only checked out: the foreman
  /// decides.
  final AttendanceStatus? status;

  /// `HH:MM`, site local time.
  final String? arrival;
  final String? departure;
  final String? firstInAt;
  final String? lastOutAt;
  final int checkinCount;
  final List<SuggestionSource> sources;
  final SuggestionPresence? presence;

  /// From check-ins: the phone pre-fills it (modes `both` and
  /// `foreman_roll_call`).
  bool get fromCheckIns => sources.contains(SuggestionSource.checkin);

  /// Presence only: shown, and written only when the foreman accepts it.
  bool get presenceOnly => !fromCheckIns && presence != null;

  RollCallSuggestion withPresence(
    List<SuggestionSource> sources,
    SuggestionPresence? presence,
  ) => RollCallSuggestion(
    workerId: workerId,
    status: status,
    arrival: arrival,
    departure: departure,
    firstInAt: firstInAt,
    lastOutAt: lastOutAt,
    checkinCount: checkinCount,
    sources: sources,
    presence: presence,
  );

  Map<String, dynamic> toJson() => {
    'worker_id': workerId,
    'status': status?.wire,
    'arrival': arrival,
    'departure': departure,
    'first_in_at': firstInAt,
    'last_out_at': lastOutAt,
    'checkin_count': checkinCount,
    'sources': sources.map((s) => s.wire).toList(),
    'presence': presence?.toJson(),
  };
}

/// `GET /sites/{id}/days/{date}/suggestions/`.
class RollCallSuggestions {
  const RollCallSuggestions({
    required this.siteId,
    required this.date,
    required this.attendanceMode,
    required this.lateAfter,
    required this.dayStatus,
    required this.suggestions,
  });

  factory RollCallSuggestions.fromJson(Map<String, dynamic> json) =>
      RollCallSuggestions(
        siteId: json['site_id'] as int,
        date: json['date'] as String,
        attendanceMode: AttendanceMode.fromWire(
          json['attendance_mode'] as String,
        ),
        lateAfter: json['late_after'] as String,
        dayStatus: DayStatus.fromWire(json['day_status'] as String),
        suggestions: [
          for (final s in json['suggestions'] as List<dynamic>)
            RollCallSuggestion.fromJson(s as Map<String, dynamic>),
        ],
      );

  final int siteId;
  final String date;
  final AttendanceMode attendanceMode;
  final String lateAfter;
  final DayStatus dayStatus;
  final List<RollCallSuggestion> suggestions;

  Map<String, dynamic> toJson() => {
    'site_id': siteId,
    'date': date,
    'attendance_mode': attendanceMode.wire,
    'late_after': lateAfter,
    'day_status': dayStatus.wire,
    'suggestions': suggestions.map((s) => s.toJson()).toList(),
  };
}

/// A protected file download (`GET /files/…`): the bytes with their `ETag`,
/// or `304 Not Modified` for the copy the phone already has.
class FileDownload {
  const FileDownload.ok(this.bytes, {this.etag, this.contentType})
    : notModified = false;

  const FileDownload.notModified({this.etag})
    : bytes = null,
      contentType = null,
      notModified = true;

  final List<int>? bytes;
  final String? etag;
  final String? contentType;

  /// `304`: keep the cached copy.
  final bool notModified;
}
