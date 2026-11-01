// Presence tracking: the contract shapes (`POST /api/v1/tracking/token/`,
// `POST /api/v1/tracking/consent/`, the tracker's `POST /v1/observations`).
// Pure Dart: `tool/live_smoke.dart` uses these too.

/// Version of the consent notice shown on the phone (`text_version` of
/// `POST /tracking/consent/`). The English and French texts are the same
/// notice; change this when the wording changes, so the server can ask
/// again.
const consentTextVersion = '2026-11-01';

/// Error codes of the tracking endpoints.
abstract final class TrackingErrors {
  /// `403` from the token endpoint until the foreman accepted the notice.
  static const consentRequired = 'consent_required';

  /// `403`: tracking is off for this foreman (settings, or a refusal).
  static const trackingDisabled = 'tracking_disabled';
}

/// A wall-clock time of day, `HH:MM`.
class TimeOfDay24 {
  const TimeOfDay24(this.hour, this.minute);

  /// Parses `HH:MM` (or `HH:MM:SS`; seconds are ignored).
  factory TimeOfDay24.parse(String text) {
    final parts = text.trim().split(':');
    final h = parts.isNotEmpty ? int.tryParse(parts[0]) : null;
    final m = parts.length > 1 ? int.tryParse(parts[1]) : null;
    if (h == null || m == null || h < 0 || h > 24 || m < 0 || m > 59) {
      throw FormatException('Not a time of day (HH:MM): $text');
    }
    if (h == 24 && m != 0) throw FormatException('Past 24:00: $text');
    return TimeOfDay24(h, m);
  }

  final int hour;
  final int minute;

  int get minutes => hour * 60 + minute;

  String get text =>
      '${hour.toString().padLeft(2, '0')}:${minute.toString().padLeft(2, '0')}';

  @override
  bool operator ==(Object other) =>
      other is TimeOfDay24 && other.minutes == minutes;

  @override
  int get hashCode => minutes;

  @override
  String toString() => text;
}

/// `tracking_hours` from the token's settings, on the site's clock (the
/// company's time zone, as everywhere in the app).
///
/// * `start < end`: inside from `start` (included) to `end` (excluded).
/// * `end < start`: a night window over midnight (`22:00`–`06:00`).
/// * `start == end`: an empty window, never inside (no tracking rather
///   than tracking all day by mistake).
class TrackingHours {
  const TrackingHours(this.start, this.end);

  factory TrackingHours.fromJson(Map<String, dynamic> json) => TrackingHours(
    TimeOfDay24.parse(json['start'] as String),
    TimeOfDay24.parse(json['end'] as String),
  );

  final TimeOfDay24 start;
  final TimeOfDay24 end;

  Map<String, dynamic> toJson() => {'start': start.text, 'end': end.text};

  /// Whether [siteLocal] (wall-clock fields on the site) is inside.
  bool contains(DateTime siteLocal) {
    final m = siteLocal.hour * 60 + siteLocal.minute;
    final s = start.minutes, e = end.minutes;
    if (s == e) return false;
    if (s < e) return m >= s && m < e;
    return m >= s || m < e;
  }

  /// Time from [siteLocal] to the next start or end (at least one second),
  /// so a timer can switch collection on or off on time. Null for an empty
  /// window.
  Duration? untilNextChange(DateTime siteLocal) {
    if (start.minutes == end.minutes) return null;
    final secondsNow =
        siteLocal.hour * 3600 + siteLocal.minute * 60 + siteLocal.second;
    int until(TimeOfDay24 t) {
      final d = t.minutes * 60 - secondsNow;
      return d > 0 ? d : d + 24 * 3600;
    }

    final a = until(start), b = until(end);
    final next = a < b ? a : b;
    return Duration(seconds: next < 1 ? 1 : next);
  }

  @override
  String toString() => '$start–$end';
}

/// `POST /tracking/token/` → a tracker token and the settings the phone
/// needs to collect.
class TrackingToken {
  const TrackingToken({
    required this.token,
    required this.expiresAt,
    required this.trackerUrl,
    required this.sites,
    required this.gpsIntervalSeconds,
    required this.hours,
  });

  factory TrackingToken.fromJson(Map<String, dynamic> json) {
    final settings = json['settings'] as Map<String, dynamic>;
    return TrackingToken(
      token: json['token'] as String,
      expiresAt: DateTime.parse(json['expires_at'] as String).toUtc(),
      trackerUrl: json['tracker_url'] as String,
      sites: [for (final s in json['sites'] as List) (s as num).toInt()],
      gpsIntervalSeconds: (settings['gps_interval_seconds'] as num).toInt(),
      hours: TrackingHours.fromJson(
        settings['tracking_hours'] as Map<String, dynamic>,
      ),
    );
  }

  final String token;
  final DateTime expiresAt;
  final String trackerUrl;
  final List<int> sites;
  final int gpsIntervalSeconds;
  final TrackingHours hours;

  /// The interval used on the phone: never below 10 s (battery), whatever
  /// the setting says.
  Duration get gpsInterval =>
      Duration(seconds: gpsIntervalSeconds < 10 ? 10 : gpsIntervalSeconds);

  bool expiredAt(DateTime now) => !now.isBefore(expiresAt);

  /// When to ask for a new token: 30 minutes before `expires_at`, or half
  /// way for a short-lived one.
  DateTime refreshAt(DateTime issuedOrNow) {
    final left = expiresAt.difference(issuedOrNow);
    final margin = left > const Duration(hours: 1)
        ? const Duration(minutes: 30)
        : left ~/ 2;
    return expiresAt.subtract(margin);
  }

  Map<String, dynamic> toJson() => {
    'token': token,
    'expires_at': expiresAt.toUtc().toIso8601String(),
    'tracker_url': trackerUrl,
    'sites': sites,
    'settings': {
      'gps_interval_seconds': gpsIntervalSeconds,
      'tracking_hours': hours.toJson(),
    },
  };
}

/// `POST /tracking/consent/` body.
class ConsentRequest {
  const ConsentRequest({
    required this.clientUuid,
    required this.accepted,
    required this.textVersion,
  });

  factory ConsentRequest.fromJson(Map<String, dynamic> json) => ConsentRequest(
    clientUuid: json['client_uuid'] as String,
    accepted: json['accepted'] as bool,
    textVersion: json['text_version'] as String,
  );

  final String clientUuid;
  final bool accepted;
  final String textVersion;

  Map<String, dynamic> toJson() => {
    'client_uuid': clientUuid,
    'accepted': accepted,
    'text_version': textVersion,
  };
}

/// `POST /tracking/consent/` → `{accepted, at, text_version}`.
class ConsentResult {
  const ConsentResult({
    required this.accepted,
    required this.at,
    required this.textVersion,
  });

  factory ConsentResult.fromJson(Map<String, dynamic> json) => ConsentResult(
    accepted: json['accepted'] as bool,
    at: json['at'] as String,
    textVersion: json['text_version'] as String,
  );

  final bool accepted;
  final String at;
  final String textVersion;
}

/// The tracker's answer to `POST /v1/observations`.
class IngestResult {
  const IngestResult({
    required this.accepted,
    required this.duplicates,
    required this.late,
    this.rejected = const [],
  });

  factory IngestResult.fromJson(Map<String, dynamic> json) => IngestResult(
    accepted: (json['accepted'] as num).toInt(),
    duplicates: (json['duplicates'] as num).toInt(),
    late: (json['late'] as num).toInt(),
    rejected: [
      for (final r in (json['rejected'] as List?) ?? const [])
        IngestRejection.fromJson(r as Map<String, dynamic>),
    ],
  );

  final int accepted;
  final int duplicates;
  final int late;
  final List<IngestRejection> rejected;
}

/// What the consent notice can say precisely. Read from the `details` of
/// `403 consent_required` when the server sends them (`tracking_hours`,
/// `gps_interval_seconds`, `retention_days`; not in the contract yet), else
/// the notice uses general wording.
class ConsentContext {
  const ConsentContext({
    this.hours,
    this.gpsIntervalSeconds,
    this.retentionDays,
  });

  factory ConsentContext.fromDetails(Map<String, dynamic> details) {
    TrackingHours? hours;
    try {
      final h = details['tracking_hours'];
      if (h is Map<String, dynamic>) hours = TrackingHours.fromJson(h);
    } on Object {
      hours = null;
    }
    int? positive(Object? v) => v is num && v > 0 ? v.toInt() : null;
    return ConsentContext(
      hours: hours,
      gpsIntervalSeconds: positive(details['gps_interval_seconds']),
      retentionDays: positive(details['retention_days']),
    );
  }

  final TrackingHours? hours;
  final int? gpsIntervalSeconds;
  final int? retentionDays;
}

class IngestRejection {
  const IngestRejection({
    required this.ref,
    required this.code,
    required this.message,
  });

  factory IngestRejection.fromJson(Map<String, dynamic> json) =>
      IngestRejection(
        ref: json['ref'] as String,
        code: json['code'] as String,
        message: (json['message'] as String?) ?? '',
      );

  final String ref;
  final String code;
  final String message;
}
