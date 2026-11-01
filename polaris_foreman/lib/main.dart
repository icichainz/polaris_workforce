import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'app.dart';
import 'config.dart';
import 'core/site_clock.dart';
import 'data/models/models.dart';
import 'data/local/local_store.dart';
import 'data/local/sqflite_local_store.dart';
import 'data/local/token_store.dart';
import 'data/repositories/http/http_repositories.dart';
import 'data/repositories/mock/mock_backend.dart';
import 'data/repositories/mock/mock_repositories.dart';
import 'data/repositories/mock/mock_seed.dart';
import 'state/providers.dart';
import 'state/session.dart';
import 'tracking/mock_tracking.dart';
import 'tracking/tracking_api.dart';
import 'tracking/tracking_providers.dart';
import 'tracking/tracking_token_store.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  initTimeZones();

  final store = await SqfliteLocalStore.open();
  final tokens = SecureTokenStore();
  final language = await store.getValue(StoreKeys.language);
  final siteId = int.tryParse(await store.getValue(StoreKeys.siteId) ?? '');

  MockBackend? mock;
  if (AppConfig.useMockApi) {
    mock = MockBackend(
      approvalDelay: Duration(seconds: AppConfig.mockApprovalSeconds),
    )..language = language ?? 'fr';
    // First launch on mocks: show the artboard's foreman card.
    if (await store.getValue(StoreKeys.rememberedPhone) == null) {
      await store.setValue(StoreKeys.rememberedPhone, MockSeed.foremanPhone);
      await store.setValue(StoreKeys.rememberedName, MockSeed.foreman.name);
      await store.setValue(StoreKeys.rememberedSite, MockSeed.site.name);
    }
  }
  // The HTTP client reads the current language and reports 401s through the
  // container, which needs the repositories: hence `late`.
  late final ProviderContainer container;
  final repos = mock != null
      ? mockRepositories(mock, tokens.read)
      : httpRepositories(
          baseUrl: AppConfig.apiBaseUrl,
          token: tokens.read,
          language: () => container.read(localeProvider).languageCode,
          // A 401 on any call: back to the PIN screen, outbox kept.
          onUnauthorized: () =>
              container.read(sessionProvider.notifier).expired(),
        );
  // Presence tracking (`lib/tracking/`): Django's token and consent
  // endpoints with the device token, and the tracker service.
  final TrackingRepository tracking;
  final TrackerTransport tracker;
  if (mock != null) {
    tracking = MockTrackingRepository(mock, tokens.read);
    tracker = MockTrackerTransport(mock);
  } else {
    tracking = HttpTrackingApi(
      ApiClient(
        baseUrl: AppConfig.apiBaseUrl,
        token: tokens.read,
        language: () => container.read(localeProvider).languageCode,
        onUnauthorized: () =>
            container.read(sessionProvider.notifier).expired(),
      ),
    );
    tracker = HttpTrackerTransport();
  }
  final session = await loadSession(store, await tokens.read());
  // A kiosk phone opens on its kiosk screen, even offline.
  final rawDevice = await store.getValue(StoreKeys.checkinDevice);
  final device = rawDevice == null
      ? null
      : CheckInDevice.fromJson(jsonDecode(rawDevice) as Map<String, dynamic>);

  container = ProviderContainer(
    overrides: [
      localStoreProvider.overrideWithValue(store),
      tokenStoreProvider.overrideWithValue(tokens),
      repositoriesProvider.overrideWithValue(repos),
      mockBackendProvider.overrideWithValue(mock),
      initialSessionProvider.overrideWithValue(session),
      initialLocaleProvider.overrideWithValue(language),
      initialSiteIdProvider.overrideWithValue(siteId),
      initialCheckInDeviceProvider.overrideWithValue(device),
      trackingRepositoryProvider.overrideWithValue(tracking),
      trackerTransportProvider.overrideWithValue(tracker),
      trackingTokenStoreProvider.overrideWithValue(SecureTrackingTokenStore()),
      observationStoreProvider.overrideWithValue(store),
    ],
  );
  if (session is SignedIn) container.read(syncServiceProvider).start();
  // Follows the session from now on (token after sign-in, dropped on
  // sign-out).
  container.read(trackingProvider);

  runApp(
    UncontrolledProviderScope(container: container, child: const PolarisApp()),
  );
}
