/// Money on the wire is always an integer in the currency's smallest unit, next
/// to a `currency {code, decimals}` object. Never a float.
class Currency {
  const Currency({required this.code, required this.decimals});

  factory Currency.fromJson(Map<String, dynamic> json) =>
      Currency(code: json['code'] as String, decimals: json['decimals'] as int);

  static const xof = Currency(code: 'XOF', decimals: 0);

  final String code;

  /// Decimal places of the smallest unit (0–3).
  final int decimals;

  Map<String, dynamic> toJson() => {'code': code, 'decimals': decimals};

  @override
  bool operator ==(Object other) =>
      other is Currency && other.code == code && other.decimals == decimals;

  @override
  int get hashCode => Object.hash(code, decimals);
}

class Money {
  const Money(this.minor, this.currency);

  /// Amount in the smallest unit (e.g. cents, or francs for XOF).
  final int minor;
  final Currency currency;

  Money operator +(Money other) {
    _check(other);
    return Money(minor + other.minor, currency);
  }

  Money operator -(Money other) {
    _check(other);
    return Money(minor - other.minor, currency);
  }

  void _check(Money other) {
    if (other.currency != currency) {
      throw ArgumentError('Currency mismatch: $currency vs ${other.currency}');
    }
  }

  String format({String decimalSeparator = '.'}) =>
      formatMoney(minor, currency, decimalSeparator: decimalSeparator);

  @override
  bool operator ==(Object other) =>
      other is Money && other.minor == minor && other.currency == currency;

  @override
  int get hashCode => Object.hash(minor, currency);

  @override
  String toString() => format();
}

/// Formats an amount in minor units: `3500` XOF → `3 500 XOF`,
/// `123450` EUR (2 decimals) → `1 234.50 EUR`. Integer maths only.
String formatMoney(
  int minor,
  Currency currency, {
  String groupSeparator = ' ',
  String decimalSeparator = '.',
}) {
  final negative = minor < 0;
  final abs = minor.abs();
  var factor = 1;
  for (var i = 0; i < currency.decimals; i++) {
    factor *= 10;
  }
  final whole = abs ~/ factor;
  final fraction = abs % factor;

  final digits = whole.toString();
  final grouped = StringBuffer();
  for (var i = 0; i < digits.length; i++) {
    if (i > 0 && (digits.length - i) % 3 == 0) grouped.write(groupSeparator);
    grouped.write(digits[i]);
  }
  final number = currency.decimals == 0
      ? grouped.toString()
      : '$grouped$decimalSeparator'
            '${fraction.toString().padLeft(currency.decimals, '0')}';
  return '${negative ? '-' : ''}$number ${currency.code}';
}
