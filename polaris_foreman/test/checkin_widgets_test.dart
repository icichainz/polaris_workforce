import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:geolocator/geolocator.dart' show LocationPermission;
import 'package:polaris_foreman/app.dart';
import 'package:polaris_foreman/data/local/local_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/state/providers.dart';
import 'package:polaris_foreman/state/session.dart';
import 'package:polaris_foreman/ui/qr_scanner.dart';

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
}
