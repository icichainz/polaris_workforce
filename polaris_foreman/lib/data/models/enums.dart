/// Enums use words on the wire (`present`, `half_day`…). The app shows letters.
library;

T _parse<T extends Enum>(List<T> values, String wire, String Function(T) w) {
  for (final v in values) {
    if (w(v) == wire) return v;
  }
  throw FormatException('Unknown ${T.toString()} value: $wire');
}

enum AttendanceStatus {
  present('present', 'P'),
  absent('absent', 'A'),
  halfDay('half_day', '½'),
  late('late', 'L');

  const AttendanceStatus(this.wire, this.letter);
  final String wire;
  final String letter;

  static AttendanceStatus fromWire(String v) =>
      _parse(values, v, (e) => e.wire);
}

enum DayStatus {
  notStarted('not_started'),
  draft('draft'),
  submitted('submitted'),
  approved('approved'),
  rejected('rejected'),
  locked('locked');

  const DayStatus(this.wire);
  final String wire;

  static DayStatus fromWire(String v) => _parse(values, v, (e) => e.wire);

  /// The foreman can still change records with the batch PUT.
  bool get editable => this == notStarted || this == draft || this == rejected;
}

enum PayoutStatus {
  notOpen('not_open'),
  open('open'),
  paid('paid'),
  closed('closed');

  const PayoutStatus(this.wire);
  final String wire;

  static PayoutStatus fromWire(String v) => _parse(values, v, (e) => e.wire);
}

enum ProofType {
  photo('photo'),
  signature('signature'),
  thumbMark('thumb_mark'),
  none('none');

  const ProofType(this.wire);
  final String wire;

  static ProofType fromWire(String v) => _parse(values, v, (e) => e.wire);
}

enum CorrectableField {
  status('status'),
  arrival('arrival'),
  departure('departure'),
  note('note');

  const CorrectableField(this.wire);
  final String wire;

  static CorrectableField fromWire(String v) =>
      _parse(values, v, (e) => e.wire);
}

enum UserRole {
  admin('admin'),
  siteManager('site_manager'),
  foreman('foreman'),
  accountant('accountant');

  const UserRole(this.wire);
  final String wire;

  static UserRole fromWire(String v) => _parse(values, v, (e) => e.wire);
}

enum PayType {
  daily('daily'),
  hourly('hourly');

  const PayType(this.wire);
  final String wire;

  static PayType fromWire(String v) => _parse(values, v, (e) => e.wire);
}

enum Language {
  en('en'),
  fr('fr');

  const Language(this.wire);
  final String wire;

  static Language fromWire(String v) => _parse(values, v, (e) => e.wire);
}
