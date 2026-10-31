// Session over the real HTTP client (against the mock server): device id,
// 401 anywhere → PIN with the outbox kept, sign in again → the queued writes
// go out; another foreman does not send them.
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/core/site_clock.dart';
import 'package:polaris_foreman/data/local/local_store.dart';
import 'package:polaris_foreman/data/local/memory_local_store.dart';
import 'package:polaris_foreman/data/local/token_store.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/http/http_repositories.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_backend.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_seed.dart';
import 'package:polaris_foreman/data/repositories/repositories.dart';
import 'package:polaris_foreman/services/roll_call_service.dart';
import 'package:polaris_foreman/services/sync_service.dart';
import 'package:polaris_foreman/state/providers.dart';
import 'package:polaris_foreman/state/session.dart';

import 'support/fakes.dart';
import 'support/mock_http_server.dart';

class App {
  App() {
    repos = httpRepositories(
      baseUrl: mockBaseUrl,
      token: tokens.read,
      language: () => 'en',
      client: mockHttpClient(backend, onExchange: exchanges.add),
      onUnauthorized: () => container.read(sessionProvider.notifier).expired(),
    );
    container = ProviderContainer(
      overrides: [
        localStoreProvider.overrideWithValue(store),
        tokenStoreProvider.overrideWithValue(tokens),
        repositoriesProvider.overrideWithValue(repos),
        nowProvider.overrideWithValue(() => fixedNow),
      ],
    );
    addTearDown(container.dispose);
  }

  final backend = MockBackend(
    latency: Duration.zero,
    approvalDelay: null,
    now: () => fixedNow,
  );
  final store = MemoryLocalStore();
  final tokens = MemoryTokenStore();
  final exchanges = <Exchange>[];
  late final ProviderContainer container;
  late final Repositories repos;

  SessionController get session => container.read(sessionProvider.notifier);
  SessionState get state => container.read(sessionProvider);

  Future<void> signIn() =>
      session.signIn(MockSeed.foremanPhone, MockSeed.foremanPin);

  SyncService sync() {
    final s = SyncService(
      store: store,
      repos: container.read(repositoriesProvider),
      clock: SiteClock(now: () => fixedNow, timeZone: 'Africa/Lome'),
      now: () => fixedNow,
      onSignedOut: session.expired,
    );
    addTearDown(s.dispose);
    return s;
  }

  RollCallService rollCall() => RollCallService(
    store,
    clock: SiteClock(now: () => fixedNow, timeZone: 'Africa/Lome'),
  );

  List<String> pinRequests() => [
    for (final x in exchanges)
      if (x.template == '/auth/pin/') x.request.body,
  ];
}

void main() {
  test('the device id is made once, kept in secure storage', () async {
    final app = App();
    await app.signIn();
    final id = await app.tokens.readDeviceId();
    expect(id, matches(RegExp(r'^[0-9a-f-]{36}$')));
    await app.session.signOut();
    expect(await app.tokens.read(), isNull);
    expect(await app.tokens.readDeviceId(), id, reason: 'kept on sign-out');
    await app.signIn();
    final bodies = app.pinRequests();
    expect(bodies, hasLength(2));
    for (final b in bodies) {
      expect(b, contains('"device_id":"$id"'));
      expect(b, contains('"device_name":"'));
    }
  });

  test('an id from an older build (local database) is kept', () async {
    final app = App();
    await app.store.setValue(StoreKeys.deviceId, 'legacy-device-id');
    await app.signIn();
    expect(await app.tokens.readDeviceId(), 'legacy-device-id');
    expect(
      app.pinRequests().single,
      contains('"device_id":"legacy-device-id"'),
    );
  });

  test(
    '401 on any call: PIN screen, outbox kept, sent after sign-in',
    () async {
      final app = App();
      await app.signIn();
      expect(app.state, isA<SignedIn>());
      expect(await app.store.getValue(StoreKeys.outboxOwner), '7');

      await app.rollCall().mark(
        siteId: MockSeed.siteId,
        date: today,
        workerId: 44,
        status: AttendanceStatus.present,
      );
      app.backend.revokeToken(); // An admin revoked the phone.

      // A foreground call (not the sync service) hits the 401.
      await expectLater(
        app.repos.crew.crew(MockSeed.siteId),
        throwsA(isA<ApiException>()),
      );
      await pumpEventQueue();
      final out = app.state;
      expect(out, isA<SignedOut>());
      expect((out as SignedOut).expired, isTrue);
      expect(await app.tokens.read(), isNull);
      expect(await app.store.loadOutbox(), hasLength(1), reason: 'kept');

      // The same foreman signs in again: the queued write is accepted.
      await app.signIn();
      expect(await app.sync().drain(), DrainResult.done);
      expect(await app.store.loadOutbox(), isEmpty);
      final day = await app.backend.day(
        await app.tokens.read(),
        MockSeed.siteId,
        today,
      );
      expect(day.records.single.workerId, 44);
    },
  );

  test('another foreman signed in does not send the outbox', () async {
    final app = App();
    await app.signIn();
    await app.rollCall().mark(
      siteId: MockSeed.siteId,
      date: today,
      workerId: 44,
      status: AttendanceStatus.present,
    );
    // The writes were made by user 99, who signed out without sending them.
    await app.store.setValue(StoreKeys.outboxOwner, '99');
    await app.session.signOut();
    await app.signIn(); // User 7.
    expect(await app.store.getValue(StoreKeys.outboxOwner), '99');
    final sync = app.sync();
    expect(await sync.outboxHeld(), isTrue);
    app.exchanges.clear();
    expect(await sync.drain(), DrainResult.held);
    expect(app.exchanges, isEmpty);
    expect(await app.store.loadOutbox(), hasLength(1));

    // Once the outbox is empty, the next sign-in owns it.
    final entry = (await app.store.loadOutbox()).single;
    await app.store.deleteOutbox(entry.id!);
    await app.session.signOut();
    await app.signIn();
    expect(await app.store.getValue(StoreKeys.outboxOwner), '7');
    expect(await sync.outboxHeld(), isFalse);
  });
}
