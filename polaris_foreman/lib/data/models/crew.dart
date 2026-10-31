import 'enums.dart';

class CrewWorker {
  const CrewWorker({
    required this.id,
    required this.number,
    required this.firstName,
    required this.lastName,
    required this.trade,
    required this.photoUrl,
    required this.payType,
    required this.absentStreak,
  });

  factory CrewWorker.fromJson(Map<String, dynamic> json) => CrewWorker(
    id: json['id'] as int,
    number: json['number'] as String,
    firstName: json['first_name'] as String,
    lastName: json['last_name'] as String,
    trade: json['trade'] as String?,
    photoUrl: json['photo_url'] as String?,
    payType: PayType.fromWire(json['pay_type'] as String),
    absentStreak: json['absent_streak'] as int,
  );

  final int id;
  final String number;
  final String firstName;
  final String lastName;
  final String? trade;
  final String? photoUrl;
  final PayType payType;

  /// Consecutive working days absent right before this date.
  final int absentStreak;

  String get name => '$firstName $lastName';

  Map<String, dynamic> toJson() => {
    'id': id,
    'number': number,
    'first_name': firstName,
    'last_name': lastName,
    'trade': trade,
    'photo_url': photoUrl,
    'pay_type': payType.wire,
    'absent_streak': absentStreak,
  };
}

/// `GET /sites/{id}/crew/?date=`.
class Crew {
  const Crew({
    required this.siteId,
    required this.date,
    required this.count,
    required this.workers,
  });

  factory Crew.fromJson(Map<String, dynamic> json) => Crew(
    siteId: json['site_id'] as int,
    date: json['date'] as String,
    count: json['count'] as int,
    workers: (json['workers'] as List<dynamic>)
        .map((e) => CrewWorker.fromJson(e as Map<String, dynamic>))
        .toList(),
  );

  final int siteId;
  final String date;
  final int count;
  final List<CrewWorker> workers;

  Map<String, dynamic> toJson() => {
    'site_id': siteId,
    'date': date,
    'count': count,
    'workers': workers.map((w) => w.toJson()).toList(),
  };
}
