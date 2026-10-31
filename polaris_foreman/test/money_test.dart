import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/data/models/money.dart';

void main() {
  const eur = Currency(code: 'EUR', decimals: 2);
  const kwd = Currency(code: 'KWD', decimals: 3);

  group('formatMoney', () {
    test('0 decimals groups thousands with spaces', () {
      expect(formatMoney(3500, Currency.xof), '3 500 XOF');
      expect(formatMoney(171500, Currency.xof), '171 500 XOF');
      expect(formatMoney(1234567, Currency.xof), '1 234 567 XOF');
      expect(formatMoney(500, Currency.xof), '500 XOF');
      expect(formatMoney(0, Currency.xof), '0 XOF');
    });

    test('2 decimals splits the minor units', () {
      expect(formatMoney(123450, eur), '1 234.50 EUR');
      expect(formatMoney(1250, eur), '12.50 EUR');
      expect(formatMoney(5, eur), '0.05 EUR');
      expect(formatMoney(100, eur), '1.00 EUR');
    });

    test('3 decimals and a custom decimal separator', () {
      expect(formatMoney(1234567, kwd), '1 234.567 KWD');
      expect(formatMoney(1250, eur, decimalSeparator: ','), '12,50 EUR');
    });

    test('negative amounts', () {
      expect(formatMoney(-2000, Currency.xof), '-2 000 XOF');
      expect(formatMoney(-123450, eur), '-1 234.50 EUR');
    });
  });

  group('Money', () {
    test('integer arithmetic in one currency', () {
      const a = Money(6000, Currency.xof);
      const b = Money(2000, Currency.xof);
      expect((a - b).minor, 4000);
      expect((a + b).format(), '8 000 XOF');
    });

    test('mixing currencies is an error', () {
      expect(
        () => const Money(1, Currency.xof) + const Money(1, eur),
        throwsArgumentError,
      );
    });

    test('Currency JSON', () {
      expect(Currency.fromJson({'code': 'XOF', 'decimals': 0}), Currency.xof);
      expect(eur.toJson(), {'code': 'EUR', 'decimals': 2});
    });
  });
}
