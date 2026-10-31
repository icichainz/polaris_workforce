import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/l10n/gen/app_localizations.dart';
import 'package:polaris_foreman/ui/widgets.dart';

Map<String, dynamic> _arb(String locale) =>
    jsonDecode(File('lib/l10n/app_$locale.arb').readAsStringSync())
        as Map<String, dynamic>;

Iterable<String> _messageKeys(Map<String, dynamic> arb) =>
    arb.keys.where((k) => !k.startsWith('@'));

void main() {
  final en = _arb('en');
  final fr = _arb('fr');

  test('every key is in both ARB files, in the same order', () {
    expect(_messageKeys(fr).toList(), _messageKeys(en).toList());
    for (final key in _messageKeys(fr)) {
      expect((fr[key] as String).trim(), isNotEmpty, reason: key);
    }
  });

  test('French keeps every placeholder of the English message', () {
    for (final key in _messageKeys(en)) {
      final meta = en['@$key'] as Map<String, dynamic>?;
      final placeholders =
          (meta?['placeholders'] as Map<String, dynamic>?)?.keys ?? const [];
      for (final name in placeholders) {
        expect(fr[key] as String, contains('{$name'), reason: '$key: {$name}');
      }
    }
  });

  test('status letters follow the language: late is R (retard) in French', () {
    final english = lookupAppLocalizations(const Locale('en'));
    final french = lookupAppLocalizations(const Locale('fr'));
    String letters(AppLocalizations l) =>
        AttendanceStatus.values.map((s) => statusLetter(l, s)).join(' ');

    expect(letters(english), 'P A ½ L');
    expect(letters(french), 'P A ½ R');
    expect(statusWord(french, AttendanceStatus.late), 'Retard');
    expect(statusWord(french, AttendanceStatus.halfDay), 'Demi-journée');
  });

  for (final (locale, letter, word) in [
    ('en', 'L', 'Late'),
    ('fr', 'R', 'Retard'),
  ]) {
    testWidgets('status button shows "$letter $word" in $locale', (
      tester,
    ) async {
      await tester.pumpWidget(
        MaterialApp(
          locale: Locale(locale),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(
            body: Row(
              children: [
                Expanded(
                  child: StatusButton(
                    status: AttendanceStatus.late,
                    selected: true,
                    onTap: () {},
                  ),
                ),
                const StatusBadge(AttendanceStatus.late),
              ],
            ),
          ),
        ),
      );

      expect(find.text(letter), findsNWidgets(2));
      expect(find.text(word), findsOneWidget);
      expect(
        tester.getSemantics(find.byType(StatusButton)).label,
        contains('$letter $word'),
      );
    });
  }
}
