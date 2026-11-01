import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/data/local/memory_local_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/tracking/observations.dart';
import 'package:polaris_foreman/tracking/tracking_models.dart';
import 'package:polaris_foreman/tracking/uploader.dart';

import 'tracking_fakes.dart';

void main() {
  late MemoryLocalStore store;
  late FakeTrackerTransport tracker;
  late TrackingToken? current;
  late List<TrackingToken?> refreshes;
  late int refreshCalls;
  late ObservationUploader uploader;

  Future<void> queue(int n, {String owner = '7', int from = 0}) =>
      store.addObservations([
        for (var i = from; i < from + n; i++)
          gpsObservation(
            owner: owner,
            fix: fixAt(trackingNow.add(Duration(minutes: i))),
            clientUuid: 'o-$i',
          ),
      ]);

  setUp(() {
    store = MemoryLocalStore();
    tracker = FakeTrackerTransport();
    current = tokenFor();
    refreshes = [];
    refreshCalls = 0;
    uploader = ObservationUploader(
      store: store,
      transport: tracker,
      currentToken: () async => current,
      refreshToken: () async {
        refreshCalls++;
        final next = refreshes.isEmpty ? null : refreshes.removeAt(0);
        if (next != null) current = next;
        return next;
      },
      now: () => trackingNow,
    );
  });

  test('batches of at most 500, oldest first, all removed once sent', () async {
    await queue(1203);
    final r = await uploader.drain('7');
    expect(r.outcome, UploadOutcome.done);
    expect(r.sent, 1203);
    expect(tracker.batches.map((b) => b.length), [500, 500, 203]);
    expect(tracker.batches.first.first['client_uuid'], 'o-0');
    expect(tracker.batches.last.last['client_uuid'], 'o-1202');
    expect(tracker.tokens.toSet(), {'tracker-jwt-1'});
    expect(await store.countObservations(), 0);
  });

  test('only the signed-in foreman\'s observations are sent', () async {
    await queue(3, owner: '7');
    await queue(2, owner: '8', from: 10);
    await uploader.drain('7');
    expect(tracker.batches.single.length, 3);
    expect(await store.countObservations('8'), 2);
  });

  test('duplicates and rejected items are acknowledged too', () async {
    await queue(3);
    tracker.handler = (batch, _) async => const IngestResult(
      accepted: 1,
      duplicates: 1,
      late: 0,
      rejected: [
        IngestRejection(ref: 'o-2', code: 'unknown_badge', message: ''),
      ],
    );
    final r = await uploader.drain('7');
    expect(r.outcome, UploadOutcome.done);
    expect(r.sent, 3);
    expect(await store.countObservations(), 0);
  });

  test('a repeat after a lost answer is harmless (duplicates)', () async {
    await queue(2);
    var calls = 0;
    tracker.handler = (batch, _) async {
      calls++;
      for (final o in batch) {
        tracker.received.add(o['client_uuid'] as String);
      }
      // The tracker stored them, but the answer never reached the phone.
      if (calls == 1) throw const NetworkException('reset');
      return IngestResult(accepted: 0, duplicates: batch.length, late: 0);
    };
    expect((await uploader.drain('7')).outcome, UploadOutcome.retryLater);
    expect(await store.countObservations(), 2);
    final r = await uploader.drain('7');
    expect(r.outcome, UploadOutcome.done);
    expect(tracker.batches[1].map((o) => o['client_uuid']), ['o-0', 'o-1']);
    expect(await store.countObservations(), 0);
  });

  test('401: refresh the token, then the same batch again', () async {
    await queue(2);
    tracker.handler = (batch, token) async {
      if (token == 'tracker-jwt-1') throw apiError(401, 'invalid_token');
      return IngestResult(accepted: batch.length, duplicates: 0, late: 0);
    };
    refreshes.add(tokenFor(token: 'tracker-jwt-2'));
    final r = await uploader.drain('7');
    expect(r.outcome, UploadOutcome.done);
    expect(refreshCalls, 1);
    expect(tracker.tokens, ['tracker-jwt-1', 'tracker-jwt-2']);
    expect(tracker.batches[0], tracker.batches[1]);
    expect(await store.countObservations(), 0);
  });

  test('401 and no new token: kept for later', () async {
    await queue(2);
    tracker.handler = (_, _) async => throw apiError(401, 'invalid_token');
    final r = await uploader.drain('7');
    expect(r.outcome, UploadOutcome.noToken);
    expect(refreshCalls, 1);
    expect(await store.countObservations(), 2);
  });

  test('an expired token is refreshed before sending', () async {
    current = tokenFor(expiresAt: trackingNow);
    refreshes.add(tokenFor(token: 'fresh'));
    await queue(1);
    await uploader.drain('7');
    expect(tracker.tokens, ['fresh']);
  });

  test('no token at all: nothing is sent, nothing lost', () async {
    current = null;
    await queue(1);
    final r = await uploader.drain('7');
    expect(r.outcome, UploadOutcome.noToken);
    expect(tracker.batches, isEmpty);
    expect(await store.countObservations(), 1);
  });

  test('offline, 5xx, 429: kept, retried with backoff', () async {
    await queue(4);
    tracker.offline = true;
    final a = await uploader.drain('7');
    expect(a.outcome, UploadOutcome.retryLater);
    expect(a.retryIn, const Duration(seconds: 2));
    final b = await uploader.drain('7');
    expect(b.retryIn, const Duration(seconds: 4));
    tracker.offline = false;
    tracker.handler = (_, _) async => throw apiError(503, 'server_error');
    expect((await uploader.drain('7')).retryIn, const Duration(seconds: 8));
    tracker.handler = (_, _) async => throw const ApiException(
      429,
      ApiError(code: 'throttled', message: '', details: {'retry_after': 42}),
    );
    expect((await uploader.drain('7')).retryIn, const Duration(seconds: 42));
    expect(await store.countObservations(), 4);
  });

  test('413: smaller batches', () async {
    await queue(10);
    tracker.handler = (batch, _) async {
      if (batch.length > 4) throw apiError(413, 'batch_too_large');
      return IngestResult(accepted: batch.length, duplicates: 0, late: 0);
    };
    final r = await uploader.drain('7');
    expect(r.outcome, UploadOutcome.done);
    expect(r.sent, 10);
    expect(await store.countObservations(), 0);
  });

  test('400: the malformed item is found and dropped, the rest go', () async {
    await queue(9);
    tracker.handler = (batch, _) async {
      if (batch.any((o) => o['client_uuid'] == 'o-5')) {
        throw apiError(400, 'invalid_body');
      }
      return IngestResult(accepted: batch.length, duplicates: 0, late: 0);
    };
    final r = await uploader.drain('7');
    expect(r.outcome, UploadOutcome.done);
    expect(r.sent, 8);
    expect(r.dropped, 1);
    expect(await store.countObservations(), 0);
  });

  test('airplane mode: everything waits, then catches up in order', () async {
    tracker.offline = true;
    for (var hour = 0; hour < 3; hour++) {
      await queue(250, from: hour * 250);
      expect((await uploader.drain('7')).outcome, UploadOutcome.retryLater);
    }
    expect(await store.countObservations(), 750);
    tracker.offline = false;
    final r = await uploader.drain('7');
    expect(r.outcome, UploadOutcome.done);
    expect(r.sent, 750);
    expect(tracker.batches.map((b) => b.length), [500, 250]);
    final sent = [
      for (final b in tracker.batches)
        for (final o in b) o['client_uuid'],
    ];
    expect(sent, [for (var i = 0; i < 750; i++) 'o-$i']);
  });

  test('a second drain joins the running one', () async {
    await queue(3);
    final a = uploader.drain('7');
    final b = uploader.drain('7');
    expect(identical(a, b), isTrue);
    await a;
    expect(tracker.batches, hasLength(1));
  });
}
