import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/app.dart';
import 'package:polaris_foreman/data/local/local_store.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_seed.dart';
import 'package:polaris_foreman/state/providers.dart';
import 'package:polaris_foreman/state/session.dart';
import 'package:polaris_foreman/tracking/consent_screen.dart';
import 'package:polaris_foreman/tracking/tracking_client.dart';
import 'package:polaris_foreman/tracking/tracking_providers.dart';

import '../support/app_harness.dart';
import '../support/fakes.dart';
import 'tracking_fakes.dart';

List<Override> _tracking(FakeTrackingRepository api) => [
  trackingRepositoryProvider.overrideWithValue(api),
  trackerTransportProvider.overrideWithValue(FakeTrackerTransport()),
  trackingLocationProvider.overrideWithValue(FakeLocationSource()),
  badgeScannerProvider.overrideWithValue(FakeBadgeScanner()),
];

/// Ends a test with no timer left (token refresh, tracking-hours switch).
Future<void> _signOut(WidgetTester tester, ProviderContainer c) async {
  await c.read(sessionProvider.notifier).signOut();
  await tester.pumpAndSettle();
  expect(c.read(trackingProvider).phase, TrackingPhase.off);
}

void main() {
  test('Today opens the notice only when it must be answered', () {
    String? go(String at, {bool consent = true, bool kiosk = false}) =>
        appRedirect(
          signedIn: true,
          kiosk: kiosk,
          location: at,
          consent: consent,
        );
    expect(go('/today'), trackingConsentRoute);
    expect(go('/pin'), trackingConsentRoute);
    // Never in the middle of a roll call, a scan or a payout.
    for (final at in ['/roll-call', '/review', '/payout', '/scan']) {
      expect(go(at), isNull, reason: at);
    }
    expect(go(trackingConsentRoute), isNull);
    expect(go(trackingConsentRoute, consent: false), isNull);
    expect(go('/today', consent: false), isNull);
    expect(go('/today', kiosk: true), '/kiosk');
    expect(
      appRedirect(signedIn: false, kiosk: false, location: '/today'),
      '/pin',
    );
  });

  testWidgets('first use: the notice opens, accepting starts tracking', (
    tester,
  ) async {
    final h = await Harness.create();
    final api = FakeTrackingRepository(fallback: consentRequired);
    final c = await pumpAt(
      tester,
      h,
      location: '/today',
      appRouter: true,
      overrides: _tracking(api),
    );
    expect(find.byKey(const Key('consentAccept')), findsOneWidget);
    expect(find.text('Location tracking'), findsOneWidget);
    final notice = find.descendant(
      of: find.byKey(const Key('consentText')),
      matching: find.byType(Scrollable),
    );
    for (final text in [
      'When',
      'What is recorded',
      'How long it is kept',
      'Who sees it',
      'What it is used for',
      'not used to discipline',
      'You can refuse',
      'Notice version',
    ]) {
      await tester.scrollUntilVisible(
        find.textContaining(text),
        100,
        scrollable: notice,
      );
      expect(find.textContaining(text), findsOneWidget, reason: text);
    }

    api.fallback = tokenFor(sites: [MockSeed.site.id]);
    await tester.tap(find.byKey(const Key('consentAccept')));
    await tester.pumpAndSettle();
    await tester.pump(const Duration(seconds: 2)); // sent, then a token
    await tester.pump(const Duration(seconds: 5)); // the snack bar goes
    await tester.pumpAndSettle();

    expect(api.consents.single.accepted, isTrue);
    expect(
      (await h.store.loadOutbox()).where((e) => e.kind == OutboxKind.consent),
      isEmpty,
    );
    expect(find.byKey(const Key('openRollCall')), findsOneWidget);
    expect(find.text('Location on'), findsOneWidget);
    expect(c.read(trackingProvider).phase, TrackingPhase.active);
    await _signOut(tester, c);
  });

  testWidgets('refusing: back to Today, no tracking UI, notice in the menu', (
    tester,
  ) async {
    final h = await Harness.create();
    final api = FakeTrackingRepository(fallback: consentRequired);
    final c = await pumpAt(
      tester,
      h,
      location: '/today',
      appRouter: true,
      overrides: _tracking(api),
    );
    api.fallback = trackingDisabled; // what the server says after a refusal
    await tester.tap(find.byKey(const Key('consentRefuse')));
    await tester.pumpAndSettle();
    await tester.pump(const Duration(seconds: 2));
    await tester.pump(const Duration(seconds: 5));
    await tester.pumpAndSettle();

    expect(api.consents.single.accepted, isFalse);
    expect(find.byKey(const Key('openRollCall')), findsOneWidget);
    expect(find.byKey(const Key('trackingIndicator')), findsNothing);
    expect(c.read(trackingProvider).refused, isTrue);

    // The menu offers the notice again (to change one's mind).
    await tester.tap(find.byType(PopupMenuButton<String>));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Location tracking'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('consentAccept')), findsOneWidget);
    await _signOut(tester, c);
  });

  testWidgets('the notice is in French too', (tester) async {
    final h = await Harness.create();
    final api = FakeTrackingRepository(fallback: consentRequired);
    final c = await pumpAt(
      tester,
      h,
      location: '/today',
      appRouter: true,
      overrides: _tracking(api),
    );
    c.read(localeProvider.notifier).set('fr', persist: false);
    await tester.pumpAndSettle();
    expect(find.text('Suivi de la position'), findsOneWidget);
    expect(find.text('Quand'), findsOneWidget);
    expect(find.text('Accepter'), findsOneWidget);
    expect(find.text('Refuser'), findsOneWidget);

    api.fallback = trackingDisabled;
    await tester.tap(find.text('Refuser'));
    await tester.pumpAndSettle();
    await tester.pump(const Duration(seconds: 7));
    await tester.pumpAndSettle();
    await _signOut(tester, c);
  });

  testWidgets('tracking off by settings: no notice, no pill, no menu item', (
    tester,
  ) async {
    final h = await Harness.create();
    final c = await pumpAt(
      tester,
      h,
      location: '/today',
      appRouter: true,
      overrides: _tracking(FakeTrackingRepository(fallback: trackingDisabled)),
    );
    expect(find.byKey(const Key('openRollCall')), findsOneWidget);
    expect(find.byKey(const Key('trackingIndicator')), findsNothing);
    await tester.tap(find.byType(PopupMenuButton<String>));
    await tester.pumpAndSettle();
    expect(find.text('Location tracking'), findsNothing);
    expect(c.read(trackingProvider).phase, TrackingPhase.disabled);
  });

  testWidgets('outside tracking hours the pill says paused', (tester) async {
    final h = await Harness.create();
    // 07:30 on the site; tracking only 18:00–19:00.
    final api = FakeTrackingRepository(
      fallback: tokenFor(sites: [MockSeed.site.id], start: '18:00'),
    );
    final c = await pumpAt(
      tester,
      h,
      location: '/today',
      appRouter: true,
      overrides: _tracking(api),
    );
    expect(find.text('Location paused'), findsOneWidget);
    await tester.tap(find.byKey(const Key('trackingIndicator')));
    await tester.pumpAndSettle();
    expect(
      find.textContaining('Outside tracking hours (18:00–19:00'),
      findsOne,
    );
    expect(find.text('Everything is sent.'), findsOneWidget);
    await tester.tap(find.byKey(const Key('trackingReadNotice')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('consentRefuse')), findsOneWidget);
    // The notice shows the real hours once a token is known.
    expect(find.textContaining('18:00–19:00 (site time)'), findsOneWidget);
    await _signOut(tester, c);
  });
}
