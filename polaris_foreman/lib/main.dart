import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'app.dart';
import 'config.dart';
import 'core/site_clock.dart';
import 'data/local/local_store.dart';
import 'data/local/sqflite_local_store.dart';
import 'data/local/token_store.dart';
import 'data/repositories/http/http_repositories.dart';
import 'data/repositories/mock/mock_backend.dart';
import 'data/repositories/mock/mock_repositories.dart';
import 'data/repositories/mock/mock_seed.dart';
import 'state/providers.dart';
import 'state/session.dart';

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
  final repos = mock != null
      ? mockRepositories(mock, tokens.read)
      : httpRepositories(
          baseUrl: AppConfig.apiBaseUrl,
          token: tokens.read,
          language: () => language ?? 'en',
        );
  final session = await loadSession(store, await tokens.read());

  final container = ProviderContainer(
    overrides: [
      localStoreProvider.overrideWithValue(store),
      tokenStoreProvider.overrideWithValue(tokens),
      repositoriesProvider.overrideWithValue(repos),
      mockBackendProvider.overrideWithValue(mock),
      initialSessionProvider.overrideWithValue(session),
      initialLocaleProvider.overrideWithValue(language),
      initialSiteIdProvider.overrideWithValue(siteId),
    ],
  );
  if (session is SignedIn) container.read(syncServiceProvider).start();

  runApp(
    UncontrolledProviderScope(container: container, child: const PolarisApp()),
  );
}
