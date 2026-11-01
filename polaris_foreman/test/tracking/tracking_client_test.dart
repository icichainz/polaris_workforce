import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/core/site_clock.dart';
import 'package:polaris_foreman/data/local/local_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/services/sync_service.dart';
import 'package:polaris_foreman/tracking/observations.dart';
import 'package:polaris_foreman/tracking/sensors.dart';
import 'package:polaris_foreman/tracking/tracking_client.dart';
import 'package:polaris_foreman/tracking/tracking_models.dart';
import 'package:polaris_foreman/tracking/uploader.dart';

import '../support/fakes.dart' show Harness;
import 'tracking_fakes.dart';

void main() {
  late TrackingRig rig;

  setUp(() => rig = TrackingRig());
  tearDown(() => rig.dispose());

  group('token lifecycle', () {
    test('after sign-in: fetched, kept for this foreman', () async {
      await rig.signIn('7');
      expect(rig.api.tokenCalls, 1);
      expect(rig.state.phase, TrackingPhase.active);
      expect(rig.state.token?.token, 'tracker-jwt-1');
      expect((await rig.tokens.read('7'))?.token, 'tracker-jwt-1');
      expect(await rig.tokens.read('8'), isNull);
    });

    test('sign-out drops the token and stops everything', () async {
      await rig.signIn();
      expect(rig.location.listening, isTrue);
      await rig.client.signedOut();
      expect(rig.state.phase, TrackingPhase.off);
      expect(rig.state.indicator, TrackingIndicator.hidden);
      expect(await rig.tokens.read('7'), isNull);
      expect(rig.location.listening, isFalse);
    });

    test('a saved token keeps collecting offline (airplane mode)', () async {
      await rig.tokens.write('7', tokenFor(token: 'saved'));
      rig.api.fallback = const NetworkException('airplane mode');
      await rig.signIn();
      expect(rig.state.phase, TrackingPhase.active);
      expect(rig.state.token?.token, 'saved');
      expect(rig.location.listening, isTrue);
    });

    test('no token and offline: nothing collected until it comes', () async {
      rig.api.fallback = const NetworkException('airplane mode');
      await rig.signIn();
      expect(rig.state.phase, TrackingPhase.unknown);
      expect(rig.location.listening, isFalse);
      rig.api.fallback = tokenFor();
      rig.client.online();
      await settle();
      expect(rig.state.phase, TrackingPhase.active);
      expect(rig.location.listening, isTrue);
    });

    test('a token saved for another foreman is not used', () async {
      await rig.tokens.write('8', tokenFor(token: 'theirs'));
      rig.api.fallback = const NetworkException('offline');
      await rig.signIn('7');
      expect(rig.state.token, isNull);
    });

    test(
      'tracking_disabled: no tracking UI, token and queue dropped',
      () async {
        await rig.signIn();
        await rig.client.record(
          bleObservation7(siteId: 1, badge: 'b', at: trackingNow),
        );
        rig.api.fallback = trackingDisabled;
        await rig.client.refreshToken();
        expect(rig.state.phase, TrackingPhase.disabled);
        expect(rig.state.indicator, TrackingIndicator.hidden);
        expect(rig.state.consentNeeded, isFalse);
        expect(await rig.tokens.read('7'), isNull);
        expect(await rig.queued(), 0);
        expect(rig.location.listening, isFalse);
      },
    );

    test('401 from Django: left to the session (back to the PIN)', () async {
      rig.api.fallback = apiError(401, 'not_authenticated');
      await rig.signIn();
      expect(rig.state.phase, TrackingPhase.unknown);
      expect(rig.api.tokenCalls, 1);
    });

    test(
      'a 404 (server without tracking yet) is retried like offline',
      () async {
        await rig.tokens.write('7', tokenFor(token: 'saved'));
        rig.api.fallback = apiError(404, 'not_found');
        await rig.signIn();
        expect(rig.state.phase, TrackingPhase.active);
        expect(rig.state.token?.token, 'saved');
      },
    );

    test('refreshed before expires_at', () async {
      // A 2-second token: refreshed half way, after one second.
      rig.api.answers.add(
        tokenFor(
          token: 'short',
          expiresAt: trackingNow.add(const Duration(seconds: 2)),
        ),
      );
      rig.api.fallback = tokenFor(token: 'next');
      await rig.signIn();
      expect(rig.state.token?.token, 'short');
      await Future<void>.delayed(const Duration(milliseconds: 1300));
      await settle();
      expect(rig.api.tokenCalls, 2);
      expect(rig.state.token?.token, 'next');
    });

    test('refreshed on a tracker 401', () async {
      rig.api.answers.addAll([tokenFor(token: 't1'), tokenFor(token: 't2')]);
      await rig.signIn();
      rig.transport.handler = (batch, token) async {
        if (token == 't1') throw apiError(401, 'invalid_token');
        return IngestResult(accepted: batch.length, duplicates: 0, late: 0);
      };
      await rig.client.record(
        bleObservation7(siteId: 1, badge: 'b', at: trackingNow),
      );
      final r = await rig.client.uploadNow();
      expect(r?.outcome, UploadOutcome.done);
      expect(rig.transport.tokens, ['t1', 't2']);
      expect(rig.state.token?.token, 't2');
      expect((await rig.tokens.read('7'))?.token, 't2');
    });
  });

  group('consent', () {
    test('consent_required: the notice is needed, nothing collected', () async {
      rig.api.fallback = const ApiException(
        403,
        ApiError(
          code: TrackingErrors.consentRequired,
          message: '',
          details: {'retention_days': 30},
        ),
      );
      await rig.signIn();
      expect(rig.state.consentNeeded, isTrue);
      expect(rig.state.consentContext.retentionDays, 30);
      expect(rig.state.indicator, TrackingIndicator.hidden);
      expect(rig.location.listening, isFalse);
    });

    test('accepting queues the answer in the outbox', () async {
      rig.api.fallback = consentRequired;
      await rig.signIn();
      await rig.client.answerConsent(accepted: true);
      expect(rig.state.phase, TrackingPhase.consentPending);
      expect(rig.kicks, 1);
      final entry = (await rig.store.loadOutbox()).single;
      expect(entry.kind, OutboxKind.consent);
      expect(entry.payload['accepted'], isTrue);
      expect(entry.payload['text_version'], consentTextVersion);
      expect(entry.payload['client_uuid'], entry.clientUuid);
      final saved = jsonDecode(
        (await rig.store.getValue(TrackingKeys.consentAnswer('7')))!,
      );
      expect(saved['accepted'], isTrue);
    });

    test('an answer still on its way is not asked again', () async {
      rig.api.fallback = consentRequired;
      await rig.signIn();
      await rig.client.answerConsent(accepted: true);
      await rig.client.refreshToken(); // still consent_required: not sent
      expect(rig.state.phase, TrackingPhase.consentPending);
      expect(rig.state.consentNeeded, isFalse);
    });

    test('accepting again while tracking keeps tracking', () async {
      await rig.signIn();
      await rig.client.answerConsent(accepted: true);
      expect(rig.state.phase, TrackingPhase.active);
      expect(rig.location.listening, isTrue);
      expect((await rig.store.loadOutbox()).single.kind, OutboxKind.consent);
    });

    test('a new answer replaces one not sent yet', () async {
      rig.api.fallback = consentRequired;
      await rig.signIn();
      await rig.client.answerConsent(accepted: true);
      await rig.client.answerConsent(accepted: false);
      final outbox = await rig.store.loadOutbox();
      expect(outbox.single.payload['accepted'], isFalse);
    });

    test('refusing: tracking off, queue cleared, can be reopened', () async {
      await rig.signIn();
      await rig.client.record(
        bleObservation7(siteId: 1, badge: 'b', at: trackingNow),
      );
      await rig.client.answerConsent(accepted: false);
      expect(rig.state.phase, TrackingPhase.disabled);
      expect(rig.state.refused, isTrue);
      expect(await rig.queued(), 0);
      expect(await rig.tokens.read('7'), isNull);
      expect(rig.location.listening, isFalse);
    });

    test('the sync service sends the answer, then a token comes', () async {
      final h = await Harness.create();
      final rig = TrackingRig(api: FakeTrackingRepository());
      addTearDown(rig.dispose);
      // The rig and the sync service share one phone store.
      final client = TrackingClient(
        api: rig.api,
        tokens: rig.tokens,
        observations: h.store,
        store: h.store,
        transport: rig.transport,
        location: rig.location,
        scanner: rig.scanner,
        clock: () => SiteClock(now: () => trackingNow, timeZone: 'Africa/Lome'),
        now: () => trackingNow,
        uploadDelay: const Duration(hours: 1),
      );
      addTearDown(client.dispose);
      final sync = SyncService(store: h.store, repos: h.repos, clock: h.clock)
        ..sendConsent = client.sendConsent;
      addTearDown(sync.dispose);

      rig.api.fallback = consentRequired;
      await client.signedIn('7');
      expect(client.state.consentNeeded, isTrue);
      await client.answerConsent(accepted: true);

      // Offline: the answer stays in the outbox.
      rig.api.consentError = const NetworkException('offline');
      expect(await sync.drain(), DrainResult.waiting);
      expect(
        (await h.store.loadOutbox()).where((e) => e.kind == OutboxKind.consent),
        hasLength(1),
      );
      // Online again: sent once, with its client_uuid.
      rig.api.consentError = null;
      expect(await sync.drain(), DrainResult.done);
      expect(rig.api.consents.single.accepted, isTrue);
      expect(await h.store.loadOutbox(), isEmpty);

      rig.api.fallback = tokenFor();
      await client.refreshToken();
      expect(client.state.phase, TrackingPhase.active);
    });

    test('without a sender the consent entry waits (not lost)', () async {
      final h = await Harness.create();
      final sync = SyncService(store: h.store, repos: h.repos, clock: h.clock);
      addTearDown(sync.dispose);
      await h.store.insertOutbox(
        const OutboxEntry(
          kind: OutboxKind.consent,
          siteId: 0,
          date: '2026-10-31',
          clientUuid: 'c-1',
          payload: {
            'client_uuid': 'c-1',
            'accepted': true,
            'text_version': consentTextVersion,
          },
          createdAt: '2026-10-31T07:30:00Z',
        ),
      );
      expect(await sync.drain(), DrainResult.waiting);
      final e = (await h.store.loadOutbox()).single;
      expect(e.state, OutboxState.pending);
      expect(e.errorCode, ErrorCodes.notImplemented);
    });
  });

  group('GPS', () {
    test(
      'inside tracking hours: one fix per interval, with accuracy',
      () async {
        await rig.signIn();
        expect(rig.location.interval, const Duration(seconds: 60));
        expect(rig.location.requests, 1); // asked once for the permission
        expect(rig.state.indicator, TrackingIndicator.on);
        rig.location.add(fixAt(trackingNow, accuracy: 14.26));
        rig.location.add(fixAt(trackingNow.add(const Duration(seconds: 5))));
        rig.location.add(fixAt(trackingNow.add(const Duration(seconds: 61))));
        await settle();
        final items = await rig.store.nextObservations('7', 10);
        expect(items, hasLength(2));
        expect(items.first.json['type'], 'gps');
        expect(items.first.json['accuracy_m'], 14.3);
        expect(items.first.json['at'], '2026-10-31T07:30:00Z');
      },
    );

    test('mock locations are rejected (Android isMocked)', () async {
      await rig.signIn();
      rig.location.add(fixAt(trackingNow, mocked: true));
      await settle();
      expect(await rig.queued(), 0);
      expect(rig.state.mockedFixes, 1);
    });

    test('outside tracking hours: paused, no GPS', () async {
      rig.now = DateTime.utc(2026, 10, 31, 20, 0);
      await rig.signIn();
      expect(rig.state.insideHours, isFalse);
      expect(rig.state.indicator, TrackingIndicator.paused);
      expect(rig.location.listening, isFalse);
    });

    test('a fix taken after the end of the window is dropped', () async {
      await rig.signIn();
      rig.location.add(fixAt(DateTime.utc(2026, 10, 31, 19, 0, 1)));
      await settle();
      expect(await rig.queued(), 0);
    });

    test('location refused: shown as off, nothing recorded', () async {
      rig.location.answer = LocationAccess.denied;
      await rig.signIn();
      expect(rig.state.indicator, TrackingIndicator.off);
      expect(rig.location.listening, isFalse);
    });

    test('started only in the foreground, kept in the background', () async {
      rig.client.setForeground(false);
      await rig.signIn();
      expect(rig.location.listening, isFalse);
      rig.client.setForeground(true);
      await settle();
      expect(rig.location.listening, isTrue);
      rig.client.setForeground(false);
      await settle();
      expect(rig.location.listening, isTrue);
    });

    test('a kiosk phone records nothing', () async {
      rig.client.setKiosk(true);
      rig.client.setSite(1);
      await rig.signIn();
      expect(rig.location.listening, isFalse);
      expect(rig.scanner.listening, isFalse);
    });
  });

  group('BLE', () {
    test('only in the foreground, on a site of the token', () async {
      rig.client.setSite(2);
      await rig.signIn(); // token sites: [1]
      expect(rig.scanner.listening, isFalse);
      rig.client.setSite(1);
      expect(rig.scanner.listening, isTrue);
      expect(rig.state.scanningSiteId, 1);
      rig.client.setForeground(false);
      expect(rig.scanner.listening, isFalse);
    });

    test('each badge once per 30 s, with rssi and the site', () async {
      rig.client.setSite(1);
      await rig.signIn();
      final t0 = trackingNow;
      rig.scanner.see('ib:x:1:1', rssi: -60, at: t0);
      rig.scanner.see(
        'ib:x:1:1',
        rssi: -61,
        at: t0.add(const Duration(seconds: 2)),
      );
      rig.scanner.see(
        'ib:x:1:2',
        rssi: -80,
        at: t0.add(const Duration(seconds: 3)),
      );
      rig.scanner.see(
        'ib:x:1:1',
        rssi: -62,
        at: t0.add(const Duration(seconds: 31)),
      );
      await settle();
      final items = await rig.store.nextObservations('7', 10);
      expect(items.map((o) => (o.json['badge_id'], o.json['rssi'])), [
        ('ib:x:1:1', -60),
        ('ib:x:1:2', -80),
        ('ib:x:1:1', -62),
      ]);
      expect(items.every((o) => o.json['site_id'] == 1), isTrue);
      expect(items.every((o) => o.json['type'] == 'ble'), isTrue);
    });

    test('no scanning outside tracking hours', () async {
      rig.now = DateTime.utc(2026, 10, 31, 5, 0);
      rig.client.setSite(1);
      await rig.signIn();
      expect(rig.scanner.listening, isFalse);
    });
  });

  group('offline-first', () {
    test('airplane mode all morning, then everything catches up', () async {
      rig.client.setSite(1);
      rig.transport.offline = true;
      await rig.signIn();
      for (var i = 0; i < 120; i++) {
        rig.location.add(fixAt(trackingNow.add(Duration(minutes: i))));
        rig.scanner.see(
          'ib:x:1:${i % 5}',
          at: trackingNow.add(Duration(minutes: i, seconds: 10)),
        );
      }
      await settle();
      expect((await rig.client.uploadNow())?.outcome, UploadOutcome.retryLater);
      expect(await rig.queued(), 240);
      expect(rig.state.queued, 240);

      rig.transport.offline = false;
      rig.client.online();
      await settle();
      expect(await rig.queued(), 0);
      expect(rig.state.queued, 0);
      final uuids = {
        for (final b in rig.transport.batches)
          for (final o in b) o['client_uuid'],
      };
      expect(uuids, hasLength(240));
      expect(rig.transport.batches.every((b) => b.length <= 500), isTrue);
    });

    test('new observations do not cut the backoff short', () async {
      final rig = TrackingRig(uploadDelay: const Duration(milliseconds: 20));
      addTearDown(rig.dispose);
      rig.transport.offline = true;
      await rig.signIn();
      await rig.client.record(
        bleObservation7(siteId: 1, badge: 'a', at: trackingNow),
      );
      await Future<void>.delayed(const Duration(milliseconds: 80));
      expect(rig.transport.calls, 1); // failed: next try in 2 s
      await rig.client.record(
        bleObservation7(siteId: 1, badge: 'b', at: trackingNow),
      );
      await Future<void>.delayed(const Duration(milliseconds: 80));
      expect(rig.transport.calls, 1);
      // The network is back: right away.
      rig.transport.offline = false;
      rig.client.online();
      await settle();
      expect(rig.transport.calls, 2);
      expect(await rig.queued(), 0);
    });

    test('observations stay with their owner across sign-outs', () async {
      rig.transport.offline = true;
      await rig.signIn('7');
      rig.location.add(fixAt(trackingNow));
      await settle();
      await rig.client.signedOut();
      await rig.signIn('8'); // another foreman on the phone
      rig.transport.offline = false;
      await rig.client.uploadNow();
      expect(rig.transport.batches, isEmpty);
      expect(await rig.queued('7'), 1);
    });

    test('old observations are pruned before sending', () async {
      await rig.signIn();
      await rig.store.addObservations([
        bleObservation7(
          siteId: 1,
          badge: 'old',
          at: trackingNow.subtract(const Duration(days: 8)),
        ),
        bleObservation7(siteId: 1, badge: 'new', at: trackingNow),
      ]);
      await rig.client.uploadNow();
      final sent = rig.transport.batches.single;
      expect(sent.single['badge_id'], 'new');
    });
  });
}

QueuedObservation bleObservation7({
  required int siteId,
  required String badge,
  required DateTime at,
}) => bleObservation(
  owner: '7',
  siteId: siteId,
  badgeId: badge,
  rssi: -70,
  at: at,
);
