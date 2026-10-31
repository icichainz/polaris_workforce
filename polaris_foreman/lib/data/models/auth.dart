import 'enums.dart';
import 'money.dart';

List<T> _list<T>(Object? raw, T Function(Map<String, dynamic>) f) =>
    (raw as List<dynamic>).map((e) => f(e as Map<String, dynamic>)).toList();

class PersonRef {
  const PersonRef({required this.id, required this.name});

  factory PersonRef.fromJson(Map<String, dynamic> json) =>
      PersonRef(id: json['id'] as int, name: json['name'] as String);

  static PersonRef? maybe(Object? json) =>
      json == null ? null : PersonRef.fromJson(json as Map<String, dynamic>);

  final int id;
  final String name;

  Map<String, dynamic> toJson() => {'id': id, 'name': name};
}

class AssignedSite {
  const AssignedSite({
    required this.id,
    required this.name,
    required this.address,
    required this.manager,
    required this.workStart,
    required this.rollCallDue,
  });

  factory AssignedSite.fromJson(Map<String, dynamic> json) => AssignedSite(
    id: json['id'] as int,
    name: json['name'] as String,
    address: json['address'] as String,
    manager: PersonRef.maybe(json['manager']),
    workStart: json['work_start'] as String?,
    rollCallDue: json['roll_call_due'] as String?,
  );

  final int id;
  final String name;
  final String address;
  final PersonRef? manager;

  /// `HH:MM`, site local time.
  final String? workStart;
  final String? rollCallDue;

  Map<String, dynamic> toJson() => {
    'id': id,
    'name': name,
    'address': address,
    'manager': manager?.toJson(),
    'work_start': workStart,
    'roll_call_due': rollCallDue,
  };
}

class Company {
  const Company({
    required this.country,
    required this.timeZone,
    required this.currency,
  });

  factory Company.fromJson(Map<String, dynamic> json) => Company(
    country: json['country'] as String,
    timeZone: json['time_zone'] as String,
    currency: Currency.fromJson(json['currency'] as Map<String, dynamic>),
  );

  final String country;
  final String timeZone;
  final Currency currency;

  Map<String, dynamic> toJson() => {
    'country': country,
    'time_zone': timeZone,
    'currency': currency.toJson(),
  };
}

class UserProfile {
  const UserProfile({
    required this.id,
    required this.name,
    required this.role,
    required this.phone,
    required this.language,
  });

  factory UserProfile.fromJson(Map<String, dynamic> json) => UserProfile(
    id: json['id'] as int,
    name: json['name'] as String,
    role: UserRole.fromWire(json['role'] as String),
    phone: json['phone'] as String,
    language: Language.fromWire(json['language'] as String),
  );

  final int id;
  final String name;
  final UserRole role;
  final String phone;
  final Language language;

  Map<String, dynamic> toJson() => {
    'id': id,
    'name': name,
    'role': role.wire,
    'phone': phone,
    'language': language.wire,
  };
}

/// `GET /me/`.
class Me extends UserProfile {
  const Me({
    required super.id,
    required super.name,
    required super.role,
    required super.phone,
    required super.language,
    required this.company,
    required this.sites,
  });

  factory Me.fromJson(Map<String, dynamic> json) {
    final p = UserProfile.fromJson(json);
    return Me(
      id: p.id,
      name: p.name,
      role: p.role,
      phone: p.phone,
      language: p.language,
      company: Company.fromJson(json['company'] as Map<String, dynamic>),
      sites: _list(json['sites'], AssignedSite.fromJson),
    );
  }

  final Company company;
  final List<AssignedSite> sites;

  @override
  Map<String, dynamic> toJson() => {
    ...super.toJson(),
    'company': company.toJson(),
    'sites': sites.map((s) => s.toJson()).toList(),
  };
}

class Device {
  const Device({required this.id, required this.deviceId, required this.name});

  factory Device.fromJson(Map<String, dynamic> json) => Device(
    id: json['id'] as int,
    deviceId: json['device_id'] as String,
    name: json['name'] as String,
  );

  final int id;
  final String deviceId;
  final String name;

  Map<String, dynamic> toJson() => {
    'id': id,
    'device_id': deviceId,
    'name': name,
  };
}

/// `POST /auth/pin/` body.
class PinLoginRequest {
  const PinLoginRequest({
    required this.phone,
    required this.pin,
    required this.deviceId,
    this.deviceName,
  });

  factory PinLoginRequest.fromJson(Map<String, dynamic> json) =>
      PinLoginRequest(
        phone: json['phone'] as String,
        pin: json['pin'] as String,
        deviceId: json['device_id'] as String,
        deviceName: json['device_name'] as String?,
      );

  final String phone;
  final String pin;
  final String deviceId;
  final String? deviceName;

  Map<String, dynamic> toJson() => {
    'phone': phone,
    'pin': pin,
    'device_id': deviceId,
    if (deviceName != null) 'device_name': deviceName,
  };
}

class PinLoginResponse {
  const PinLoginResponse({
    required this.token,
    required this.device,
    required this.user,
    required this.sites,
  });

  factory PinLoginResponse.fromJson(Map<String, dynamic> json) =>
      PinLoginResponse(
        token: json['token'] as String,
        device: Device.fromJson(json['device'] as Map<String, dynamic>),
        user: UserProfile.fromJson(json['user'] as Map<String, dynamic>),
        sites: _list(json['sites'], AssignedSite.fromJson),
      );

  final String token;
  final Device device;
  final UserProfile user;
  final List<AssignedSite> sites;

  Map<String, dynamic> toJson() => {
    'token': token,
    'device': device.toJson(),
    'user': user.toJson(),
    'sites': sites.map((s) => s.toJson()).toList(),
  };
}
