import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/data/local/local_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_seed.dart';
import 'package:polaris_foreman/state/providers.dart';
import 'package:polaris_foreman/state/session.dart';
import 'package:polaris_foreman/ui/widgets.dart';

import 'support/app_harness.dart';
import 'support/fakes.dart';

StatusButton statusButton(WidgetTester tester, int workerId, String wire) =>
    tester.widget<StatusButton>(find.byKey(Key('status-$workerId-$wire')));

bool enabled(WidgetTester tester, Key key) =>
    tester.widget<ButtonStyleButton>(find.byKey(key)).onPressed != null;

void main() {
  testWidgets('roll call: a row shows letter + word and keeps one selection', (
    tester,
  ) async {
    final h = await Harness.create();
    final crew = await seedCrew(h, MockSeed.siteId);
    await pumpAt(tester, h, location: '/roll-call');

    final w = crew.workers.first;
    expect(find.text(w.name), findsOneWidget);
    expect(find.text('0/42'), findsOneWidget);
    expect(find.text('Review · 42 not marked'), findsOneWidget);

    // Every button shows its letter and its word.
    final card = find.byKey(ValueKey('worker-${w.id}'));
    for (final (letter, word) in [
      ('P', 'Present'),
      ('A', 'Absent'),
      ('½', 'Half day'),
      ('L', 'Late'),
    ]) {
      expect(
        find.descendant(of: card, matching: find.text(letter)),
        findsOneWidget,
      );
      expect(
        find.descendant(of: card, matching: find.text(word)),
        findsOneWidget,
      );
    }

    await tester.tap(find.byKey(Key('status-${w.id}-late')));
    await tester.pumpAndSettle();
    expect(statusButton(tester, w.id, 'late').selected, isTrue);
    expect(statusButton(tester, w.id, 'present').selected, isFalse);
    expect(find.text('1/42'), findsOneWidget);
    expect(
      find.descendant(of: card, matching: find.text('07:30')),
      findsOneWidget,
    );
    expect(
      tester.getSemantics(find.byKey(Key('status-${w.id}-late'))),
      containsSemantics(
        label: 'L Late',
        isButton: true,
        isSelected: true,
        hasTapAction: true,
      ),
    );

    await tester.tap(find.byKey(Key('status-${w.id}-present')));
    await tester.pumpAndSettle();
    expect(statusButton(tester, w.id, 'present').selected, isTrue);
    expect(statusButton(tester, w.id, 'late').selected, isFalse);
    final rec = await h.store.loadRecord(MockSeed.siteId, today, w.id);
    expect(rec!.status, AttendanceStatus.present);
    expect(rec.arrival, '07:00');

    // The marked worker stays visible under "To mark" until the tab reopens.
    expect(card, findsOneWidget);

    await tester.tap(find.byKey(const Key('allPresent')));
    await tester.pumpAndSettle();
    expect(find.text('42/42'), findsOneWidget);
    expect(find.text('Review & submit'), findsOneWidget);
  });

  testWidgets('review: submit is enabled only when everyone is marked', (
    tester,
  ) async {
    final h = await Harness.create();
    final siteId = MockSeed.site2.id;
    final crew = await seedCrew(h, siteId);
    final container = await pumpAt(
      tester,
      h,
      location: '/review',
      siteId: siteId,
    );

    expect(find.text('Submit 6 workers'), findsOneWidget);
    expect(enabled(tester, const Key('submit')), isFalse);
    expect(find.text('6 workers are not marked yet.'), findsOneWidget);

    final service = container.read(rollCallServiceProvider);
    await service.markMany(
      siteId: siteId,
      date: today,
      marks: {
        for (final w in crew.workers.skip(1)) w.id: AttendanceStatus.present,
      },
      workStart: '07:30',
    );
    await tester.pumpAndSettle();
    expect(enabled(tester, const Key('submit')), isFalse);
    expect(find.text('1 worker is not marked yet.'), findsOneWidget);

    await service.mark(
      siteId: siteId,
      date: today,
      workerId: crew.workers.first.id,
      status: AttendanceStatus.absent,
    );
    await tester.pumpAndSettle();
    expect(enabled(tester, const Key('submit')), isTrue);
    expect(find.text('Absent today'), findsOneWidget);

    await tester.enterText(find.byKey(const Key('note')), 'Rain at 10:00.');
    await tester.tap(find.byKey(const Key('submit')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('submittedPanel')), findsOneWidget);
    expect(find.text('Back to today'), findsOneWidget);
    expect(find.byKey(const Key('submit')), findsNothing);

    // The submit went out through the outbox to the (mock) server.
    final day = await h.backend.day(await h.token(), siteId, today);
    expect(day.summary.status, DayStatus.submitted);
    expect(day.summary.foremanNote, 'Rain at 10:00.');
    expect(await h.store.loadOutbox(), isEmpty);
  });

  testWidgets('payout: confirming a payment moves to the next worker', (
    tester,
  ) async {
    final h = await Harness.create();
    final siteId = MockSeed.site2.id;
    final crew = await seedCrew(h, siteId);
    // The day is submitted and approved on the server; the sheet is open.
    final token = await h.token();
    await h.backend.putRecords(token, siteId, today, [
      for (final w in crew.workers)
        RecordUpsert(
          clientUuid: 'uuid-${w.id}',
          workerId: w.id,
          status: AttendanceStatus.present,
        ),
    ]);
    await h.backend.submit(
      token,
      siteId,
      today,
      const SubmitDayRequest(clientUuid: 'submit-1'),
    );
    h.backend.approve(siteId, today);
    final sheet = await h.backend.payout(token, siteId, today);
    await h.store.savePayout(sheet);
    final proofs = FakeProofCapture();

    await pumpAt(
      tester,
      h,
      location: '/payout',
      siteId: siteId,
      proofs: proofs,
    );
    final first = sheet.lines[0], second = sheet.lines[1];
    expect(find.byKey(Key('current-${first.payLineId}')), findsOneWidget);
    expect(find.text('0/6'), findsOneWidget);
    expect(find.text(formatMoney(first.net, Currency.xof)), findsWidgets);

    // Proof first, then confirm.
    expect(enabled(tester, const Key('confirmPaid')), isFalse);
    await tester.tap(find.byKey(const Key('proof-photo')));
    await tester.pumpAndSettle();
    expect(proofs.photos, 1);
    expect(find.byKey(const Key('proofReady')), findsOneWidget);
    expect(enabled(tester, const Key('confirmPaid')), isTrue);
    expect(
      find.text('Confirm paid · ${formatMoney(first.net, Currency.xof)}'),
      findsOneWidget,
    );

    await tester.tap(find.byKey(const Key('confirmPaid')));
    await tester.pumpAndSettle();
    expect(find.byKey(Key('current-${second.payLineId}')), findsOneWidget);
    expect(find.text('1/6'), findsOneWidget);
    expect(find.byKey(const Key('proofReady')), findsNothing);
    final cashLeft = sheet.cashIssued - first.net;
    expect(
      tester.widget<Text>(find.byKey(const Key('inHand'))).data,
      formatMoney(cashLeft, Currency.xof),
    );

    // Pay everyone else.
    for (var i = 1; i < sheet.lines.length; i++) {
      await tester.tap(find.byKey(const Key('proof-photo')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('confirmPaid')));
      await tester.pumpAndSettle();
    }
    expect(find.byKey(const Key('everyonePaid')), findsOneWidget);
    expect(find.text('Everyone is paid'), findsOneWidget);
    expect(find.text('6/6'), findsOneWidget);
    final server = await h.backend.payout(token, siteId, today);
    expect(server.paidCount, 6);
    expect(server.status, PayoutStatus.paid);
  });

  testWidgets('PIN: the button enables at 4 digits; wrong PIN, then sign-in', (
    tester,
  ) async {
    final h = await Harness.create();
    await h.store.setValue(StoreKeys.language, 'en');
    final container = await pumpAt(
      tester,
      h,
      location: '/pin',
      session: const SignedOut(
        remembered: RememberedUser(
          phone: MockSeed.foremanPhone,
          name: 'Kossi Amegah',
          site: 'Riverside Apartments',
        ),
      ),
    );
    expect(find.text('Kossi Amegah'), findsOneWidget);
    expect(find.text('Foreman · Riverside Apartments'), findsOneWidget);
    expect(find.text('Enter your 4-digit PIN'), findsOneWidget);

    Future<void> press(String label) async {
      await tester.tap(find.byKey(Key('key-$label')));
      await tester.pump();
    }

    bool signInEnabled() => enabled(tester, const Key('signIn'));

    expect(signInEnabled(), isFalse);
    for (final k in ['1', '2', '3']) {
      await press(k);
    }
    expect(signInEnabled(), isFalse);
    await press('⌫');
    await press('C');
    expect(signInEnabled(), isFalse);
    for (final k in ['1', '2', '3', '9']) {
      await press(k);
    }
    expect(find.text('PIN entered'), findsOneWidget);
    expect(signInEnabled(), isTrue);
    await press('5'); // A fifth digit is ignored.
    expect(find.text('PIN entered'), findsOneWidget);

    await tester.tap(find.byKey(const Key('signIn')));
    await tester.pumpAndSettle();
    expect(find.text('Wrong PIN. 4 tries left.'), findsOneWidget);
    expect(signInEnabled(), isFalse, reason: 'PIN is cleared');

    for (final k in ['1', '2', '3', '4']) {
      await press(k);
    }
    await tester.tap(find.byKey(const Key('signIn')));
    await tester.pumpAndSettle();
    expect(container.read(sessionProvider), isA<SignedIn>());
    expect(await h.tokens.read(), startsWith('mock-token-'));
    container.read(syncServiceProvider).pause();
  });

  testWidgets('PIN: locked after too many tries', (tester) async {
    final h = await Harness.create();
    h.recording.failures['signIn'] = [
      const ApiException(
        423,
        ApiError(
          code: 'locked',
          message: 'Locked',
          details: {'status': 'locked', 'locked_until': '2026-10-31T07:45:00Z'},
        ),
      ),
    ];
    await pumpAt(
      tester,
      h,
      location: '/pin',
      session: const SignedOut(
        remembered: RememberedUser(
          phone: MockSeed.foremanPhone,
          name: 'Kossi Amegah',
        ),
      ),
    );
    for (final k in ['1', '2', '3', '4']) {
      await tester.tap(find.byKey(Key('key-$k')));
      await tester.pump();
    }
    await tester.tap(find.byKey(const Key('signIn')));
    await tester.pumpAndSettle();
    expect(
      find.text('Too many wrong PINs. Try again at 07:45.'),
      findsOneWidget,
    );
  });

  testWidgets('today: cards, sync banner and this week from the phone cache', (
    tester,
  ) async {
    final h = await Harness.create();
    await seedCrew(h, MockSeed.siteId);
    final week = await h.backend.days(
      await h.token(),
      MockSeed.siteId,
      null,
      null,
    );
    await h.store.upsertDays(week.days);
    await pumpAt(tester, h, location: '/today');

    // 31 October 2026 is a Saturday.
    expect(find.text('Saturday 31 October'), findsOneWidget);
    expect(find.text('Riverside Apartments'), findsOneWidget);
    expect(find.text('MORNING · DUE 08:00'), findsOneWidget);
    expect(find.text('42 workers on your crew list'), findsOneWidget);
    expect(find.text('Start roll call'), findsOneWidget);
    expect(find.text('Preview tonight’s payout'), findsOneWidget);
    expect(find.text('Fri 30'), findsOneWidget);
    expect(find.text('40 / 42'), findsOneWidget);
    expect(find.text('Paid'), findsOneWidget);
    expect(find.text('Locked'), findsWidgets);
    expect(find.text('0 waiting'), findsOneWidget);

    await tester.tap(find.byKey(const Key('openRollCall')));
    await tester.pumpAndSettle();
    expect(find.text('0/42'), findsOneWidget);
  });

  testWidgets('PIN screen switches to French', (tester) async {
    final h = await Harness.create();
    await pumpAt(
      tester,
      h,
      location: '/pin',
      session: const SignedOut(
        remembered: RememberedUser(
          phone: MockSeed.foremanPhone,
          name: 'Kossi Amegah',
        ),
      ),
    );
    expect(find.text('FR'), findsOneWidget);
    await tester.tap(find.byKey(const Key('language')));
    await tester.pumpAndSettle();
    expect(find.text('Entrez votre code à 4 chiffres'), findsOneWidget);
    expect(find.text('EN'), findsOneWidget);
  });

  testWidgets('payout: only the accepted proofs; `none` needs no proof', (
    tester,
  ) async {
    final h = await Harness.create();
    const rules = PaymentRules(
      acceptedProofs: [ProofType.signature, ProofType.none],
      fileRequiredFor: [],
    );
    h.backend.paymentRules = rules;
    final siteId = MockSeed.site2.id;
    final crew = await seedCrew(h, siteId);
    final token = await h.token();
    await h.backend.putRecords(token, siteId, today, [
      for (final w in crew.workers)
        RecordUpsert(
          clientUuid: 'uuid-${w.id}',
          workerId: w.id,
          status: AttendanceStatus.present,
        ),
    ]);
    await h.backend.submit(
      token,
      siteId,
      today,
      const SubmitDayRequest(clientUuid: 'submit-1'),
    );
    h.backend.approve(siteId, today);
    final sheet = await h.backend.payout(token, siteId, today);
    await h.store.savePayout(sheet);
    final me = MockSeed.me(language: Language.en);
    await pumpAt(
      tester,
      h,
      location: '/payout',
      siteId: siteId,
      session: SignedIn(
        Me(
          id: me.id,
          name: me.name,
          role: me.role,
          phone: me.phone,
          language: me.language,
          company: const Company(
            country: 'TG',
            timeZone: 'Africa/Lome',
            currency: Currency.xof,
            payments: rules,
          ),
          sites: me.sites,
        ),
      ),
    );
    expect(find.byKey(const Key('proof-photo')), findsNothing);
    expect(find.byKey(const Key('proof-thumb_mark')), findsNothing);
    expect(find.byKey(const Key('proof-signature')), findsOneWidget);
    expect(find.text('No proof'), findsOneWidget);
    // A signature needs no file here (it may be on the paper sheet).
    expect(enabled(tester, const Key('confirmPaid')), isTrue);

    await tester.tap(find.byKey(const Key('proof-none')));
    await tester.pumpAndSettle();
    expect(enabled(tester, const Key('confirmPaid')), isTrue);
    await tester.tap(find.byKey(const Key('confirmPaid')));
    await tester.pumpAndSettle();
    expect(find.text('1/6'), findsOneWidget);
    final server = await h.backend.payout(token, siteId, today);
    final paid = server.lines.firstWhere((l) => l.payment != null);
    expect(paid.payment!.proofType, ProofType.none);
  });

  testWidgets('PIN: the 5th wrong PIN (401 + locked_until) locks', (
    tester,
  ) async {
    final h = await Harness.create();
    for (var i = 0; i < 4; i++) {
      await h.backend
          .signIn(
            const PinLoginRequest(
              phone: MockSeed.foremanPhone,
              pin: '0000',
              deviceId: 'd',
            ),
          )
          .then<void>((_) {}, onError: (Object _) {});
    }
    await pumpAt(
      tester,
      h,
      location: '/pin',
      session: const SignedOut(
        remembered: RememberedUser(
          phone: MockSeed.foremanPhone,
          name: 'Kossi Amegah',
        ),
      ),
    );
    for (final k in ['9', '9', '9', '9']) {
      await tester.tap(find.byKey(Key('key-$k')));
      await tester.pump();
    }
    await tester.tap(find.byKey(const Key('signIn')));
    await tester.pumpAndSettle();
    expect(
      find.text('Too many wrong PINs. Try again at 07:45.'),
      findsOneWidget,
    );
  });
}
