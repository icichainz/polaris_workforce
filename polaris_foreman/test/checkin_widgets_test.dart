import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:geolocator/geolocator.dart' show LocationPermission;
import 'package:polaris_foreman/app.dart';
import 'package:polaris_foreman/data/local/local_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/l10n/gen/app_localizations.dart';
import 'package:polaris_foreman/state/providers.dart';
import 'package:polaris_foreman/state/session.dart';
import 'package:polaris_foreman/ui/qr_scanner.dart';
import 'package:polaris_foreman/ui/screens/roll_call_screen.dart';
import 'package:polaris_foreman/ui/widgets.dart';

import 'support/app_harness.dart';
import 'support/fakes.dart';

const site = 3;

/// No camera in tests: one button per QR text.
Override fakeScanner(List<String> codes) =>
    scannerBuilderProvider.overrideWithValue(
      (context, {required onScan, required frontCamera}) => ListView(
        children: [
          for (var i = 0; i < codes.length; i++)
            TextButton(
              key: Key('fakeScan-$i'),
              onPressed: () => onScan(codes[i]),
              child: Text('scan $i'),
            ),
        ],
      ),
    );

Future<void> saveSetup(Harness h) async {
  final setup = await h.backend.checkInSetup(await h.token(), site);
  await h.store.setValue(
    StoreKeys.checkinSetup(site),
    jsonEncode({'date': today, 'setup': setup.toJson()}),
  );
}

Finder inFeedback(String text) => find.descendant(
  of: find.byKey(const Key('scanFeedback')),
  matching: find.text(text),
);

/// A 1×1 PNG.
final png = base64Decode(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
);

void main() {
  testWidgets('kiosk phone: only the kiosk; big feedback; PIN to leave', (
    tester,
  ) async {
    final h = await Harness.create();
    h.backend.setKiosk(site);
    await seedCrew(h, site);
    await saveSetup(h);
    await h.tokens.writeDeviceId('test-device');
    await h.tokens.writePinCheck(
      SessionController.pinCheck('test-device', '+22890112233', '1234'),
    );
    final device = await h.backend.checkInDevice(await h.token());
    final c = await pumpAt(
      tester,
      h,
      location: '/today',
      appRouter: true,
      overrides: [
        initialCheckInDeviceProvider.overrideWithValue(device),
        fakeScanner([h.backend.badgeOf(63), 'SOMETHING-ELSE']),
      ],
    );
    expect(find.byKey(const Key('kioskSite')), findsOneWidget);
    expect(find.text('Riverside Apartments'), findsOneWidget);
    expect(find.byKey(const Key('openRollCall')), findsNothing);

    // Foreman screens stay out of reach.
    c.read(routerProvider).go('/roll-call');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('kioskSite')), findsOneWidget);

    await tester.tap(find.byKey(const Key('fakeScan-0')));
    await tester.pumpAndSettle();
    expect(inFeedback('Delali Agbo'), findsOneWidget);
    expect(inFeedback('In'), findsOneWidget);
    final sent = await h.backend.siteCheckIns(await h.token(), site, today);
    expect(sent.checkins.single.source, CheckInSource.kiosk);

    // The answer clears after a few seconds, ready for the next worker.
    await tester.pump(const Duration(seconds: 7));
    expect(find.byKey(const Key('scanFeedback')), findsNothing);
    await tester.tap(find.byKey(const Key('fakeScan-1')));
    await tester.pumpAndSettle();
    expect(
      inFeedback('Badge not recognised — see the foreman'),
      findsOneWidget,
    );

    // Leaving needs the PIN.
    await tester.tap(find.byKey(const Key('kioskExit')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('kioskPin')), '0000');
    await tester.tap(find.byKey(const Key('kioskPinOk')));
    await tester.pumpAndSettle();
    expect(find.text('Wrong PIN'), findsOneWidget);
    expect(c.read(sessionProvider), isA<SignedIn>());
    await tester.enterText(find.byKey(const Key('kioskPin')), '1234');
    await tester.tap(find.byKey(const Key('kioskPinOk')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('kioskSignOut')));
    await tester.pumpAndSettle();
    expect(c.read(sessionProvider), isA<SignedOut>());
    expect(find.byKey(const Key('kioskSite')), findsNothing);
  });

  testWidgets('kiosk mode ended by the office: back to Today', (tester) async {
    final h = await Harness.create();
    h.backend.setKiosk(site);
    await seedCrew(h, site);
    final device = await h.backend.checkInDevice(await h.token());
    final c = await pumpAt(
      tester,
      h,
      location: '/today',
      appRouter: true,
      overrides: [
        initialCheckInDeviceProvider.overrideWithValue(device),
        fakeScanner(const []),
      ],
    );
    expect(find.byKey(const Key('kioskSite')), findsOneWidget);
    c
        .read(checkInDeviceProvider.notifier)
        .set(
          const CheckInDevice(attendanceMode: AttendanceMode.both, kiosk: null),
        );
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('kioskSite')), findsNothing);
    expect(find.byKey(const Key('openRollCall')), findsOneWidget);
  });

  testWidgets('demo build: long-press the site name to try kiosk mode', (
    tester,
  ) async {
    final h = await Harness.create();
    await seedCrew(h, site);
    await pumpAt(
      tester,
      h,
      location: '/today',
      appRouter: true,
      overrides: [
        mockBackendProvider.overrideWithValue(h.backend),
        fakeScanner(const []),
      ],
    );
    expect(find.byKey(const Key('openRollCall')), findsOneWidget);

    await tester.longPress(find.byKey(const Key('todaySite')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('kioskSite')), findsOneWidget);
    expect(
      (await h.backend.checkInDevice(await h.token())).kiosk?.siteId,
      site,
    );

    // …and the same gesture on the kiosk screen ends it.
    await tester.longPress(find.byKey(const Key('kioskSite')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('kioskSite')), findsNothing);
    expect(find.byKey(const Key('openRollCall')), findsOneWidget);
    expect((await h.backend.checkInDevice(await h.token())).kiosk, isNull);
  });

  testWidgets('real build: the long-press does nothing', (tester) async {
    final h = await Harness.create();
    await seedCrew(h, site);
    await pumpAt(
      tester,
      h,
      location: '/today',
      appRouter: true,
      overrides: [fakeScanner(const [])],
    );

    await tester.longPress(find.byKey(const Key('todaySite')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('kioskSite')), findsNothing);
    expect((await h.backend.checkInDevice(await h.token())).kiosk, isNull);
  });

  testWidgets('roll call → scan badges → the scan pre-fills the mark', (
    tester,
  ) async {
    final h = await Harness.create();
    await seedCrew(h, site);
    await saveSetup(h);
    await pumpAt(
      tester,
      h,
      location: '/roll-call',
      overrides: [
        fakeScanner([h.backend.badgeOf(63)]),
        locationPlatformProvider.overrideWithValue(
          FakeLocationPlatform(
            permission: LocationPermission.denied,
            onRequest: LocationPermission.denied,
          ),
        ),
      ],
    );
    await tester.tap(find.byKey(const Key('scanBadges')));
    await tester.pumpAndSettle();
    expect(
      find.text('No GPS permission: scans are sent without position'),
      findsOneWidget,
    );
    await tester.tap(find.byKey(const Key('fakeScan-0')));
    await tester.pumpAndSettle();
    expect(inFeedback('Delali Agbo'), findsOneWidget);
    expect(inFeedback('In'), findsOneWidget);
    await tester.tap(find.byKey(const Key('backToRollCall')));
    await tester.pumpAndSettle();

    final card = find.byKey(const ValueKey('worker-63'));
    await tester.scrollUntilVisible(
      card,
      200,
      scrollable: find.byType(Scrollable).last,
    );
    expect(card, findsOneWidget);
    expect(find.byKey(const Key('fromCheckIn-63')), findsOneWidget);
    expect(find.byKey(const Key('rollCallNote')), findsOneWidget);
    expect(
      (await h.store.loadRecord(site, today, 63))!.status,
      AttendanceStatus.late,
    );
    // The foreman's tap wins.
    await tester.ensureVisible(find.byKey(const Key('status-63-present')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('status-63-present')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('fromCheckIn-63')), findsNothing);
    expect(
      (await h.store.loadRecord(site, today, 63))!.status,
      AttendanceStatus.present,
    );
  });

  testWidgets('crew list shows the worker photo (initials without one)', (
    tester,
  ) async {
    final h = await Harness.create();
    h.backend.setWorkerPhoto(63, png);
    await seedCrew(h, site);
    await pumpAt(tester, h, location: '/roll-call');
    await tester.scrollUntilVisible(
      find.byKey(const ValueKey('worker-63')),
      200,
      scrollable: find.byType(Scrollable).last,
    );
    await tester.pumpAndSettle();
    final withPhoto = find.descendant(
      of: find.byKey(const ValueKey('worker-63')),
      matching: find.byType(Image),
    );
    expect(withPhoto, findsOneWidget);
    expect(h.recording.log, contains('download'));
    await tester.scrollUntilVisible(
      find.byKey(const ValueKey('worker-44')),
      400,
      scrollable: find.byType(Scrollable).last,
    );
    final initials = find.descendant(
      of: find.byKey(const ValueKey('worker-44')),
      matching: find.text('YM'),
    );
    expect(initials, findsOneWidget);
  });

  group('presence suggestions (1.5)', () {
    const open = SuggestionPresence(
      firstSeenAt: '2026-10-31T07:31:10Z',
      lastSeenAt: null,
      open: true,
      seenFrom: '07:31',
      seenUntil: null,
      zones: ['A'],
      visits: 1,
      status: AttendanceStatus.late,
    );
    const closed = SuggestionPresence(
      firstSeenAt: '2026-10-31T07:12:00Z',
      lastSeenAt: '2026-10-31T16:48:00Z',
      open: false,
      seenFrom: '07:12',
      seenUntil: '16:48',
      zones: ['A', 'B'],
      visits: 1,
      status: AttendanceStatus.present,
    );

    Future<void> cacheSuggestions(Harness h) async {
      final s = await h.backend.suggestions(await h.token(), site, today);
      await h.store.setValue(
        StoreKeys.suggestions(site, today),
        jsonEncode(s.toJson()),
      );
    }

    Future<Finder> card(WidgetTester tester, int id) async {
      final f = find.byKey(ValueKey('worker-$id'));
      await tester.scrollUntilVisible(
        f,
        200,
        scrollable: find.byType(Scrollable).last,
      );
      await tester.pumpAndSettle();
      return f;
    }

    testWidgets('seen on site: shown, accepted with one tap', (tester) async {
      final h = await Harness.create();
      await seedCrew(h, site);
      await saveSetup(h);
      h.backend.seePresence(site, today, 58, open);
      await cacheSuggestions(h);
      await pumpAt(tester, h, location: '/roll-call');
      await tester.pumpAndSettle();

      // Presence never marks anyone by itself.
      expect(await h.store.loadRecord(site, today, 58), isNull);
      expect(find.byKey(const Key('presenceNote')), findsOneWidget);
      await card(tester, 58);
      expect(
        find.descendant(
          of: find.byKey(const Key('presence-58')),
          matching: find.text('Seen since 07:31, still on site · zone A'),
        ),
        findsOneWidget,
      );
      final accept = find.byKey(const Key('accept-58'));
      await tester.ensureVisible(accept);
      await tester.pumpAndSettle();
      expect(
        find.descendant(of: accept, matching: find.text('Accept: Late')),
        findsOneWidget,
      );

      await tester.tap(accept);
      await tester.pumpAndSettle();

      final r = (await h.store.loadRecord(site, today, 58))!;
      expect((r.status, r.arrival), (AttendanceStatus.late, '07:31'));
      // Sent at once (the sync kicks after a write): audited as accepted
      // from presence.
      expect(await h.store.loadOutbox(), isEmpty);
      expect(h.backend.suggestionAudit['$site|$today'], {
        58: [SuggestionSource.presence],
      });
      expect(find.byKey(const Key('accept-58')), findsNothing);
      expect(find.byKey(const Key('presence-58')), findsOneWidget);
      expect(find.byKey(const Key('presenceNote')), findsNothing);
    });

    testWidgets('check-ins and presence: the scan wins, presence is shown', (
      tester,
    ) async {
      final h = await Harness.create();
      await seedCrew(h, site);
      await saveSetup(h);
      await h.backend.checkIn(
        await h.token(),
        const CheckInCreateRequest(
          clientUuid: '22222222-2222-4222-8222-222222222222',
          siteId: site,
          direction: CheckInDirection.checkIn,
          at: '2026-10-31T07:40:00Z',
          workerId: 63,
        ),
      );
      h.backend.seePresence(site, today, 63, closed);
      await cacheSuggestions(h);
      await pumpAt(tester, h, location: '/roll-call');
      await tester.pumpAndSettle();

      await card(tester, 63);
      final r = (await h.store.loadRecord(site, today, 63))!;
      expect((r.status, r.arrival), (AttendanceStatus.late, '07:40'));
      expect(find.byKey(const Key('fromCheckIn-63')), findsOneWidget);
      expect(
        find.descendant(
          of: find.byKey(const Key('presence-63')),
          matching: find.text('Seen 07:12–16:48 · zones A, B'),
        ),
        findsOneWidget,
      );
      expect(find.byKey(const Key('accept-63')), findsNothing);
    });

    testWidgets('a submitted day shows presence but offers no accept', (
      tester,
    ) async {
      final h = await Harness.create();
      await seedCrew(h, site);
      await saveSetup(h);
      h.backend.seePresence(site, today, 58, open);
      await cacheSuggestions(h);
      await h.store.upsertDays([
        (await h.backend.day(
          await h.token(),
          site,
          today,
        )).summary.copyWith(status: DayStatus.submitted),
      ]);
      await pumpAt(tester, h, location: '/roll-call');
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('tabAll')));
      await tester.pumpAndSettle();

      await card(tester, 58);
      expect(find.byKey(const Key('presence-58')), findsOneWidget);
      expect(find.byKey(const Key('accept-58')), findsNothing);
    });

    test('the presence line in French', () {
      final fr = lookupAppLocalizations(const Locale('fr'));
      expect(
        presenceLine(fr, open),
        'Vu depuis 07:31, toujours sur le chantier · zone A',
      );
      expect(presenceLine(fr, closed), 'Vu 07:12–16:48 · zones A, B');
      expect(
        fr.acceptSuggestion(statusWord(fr, AttendanceStatus.late)),
        'Accepter : Retard',
      );
    });
  });
}
