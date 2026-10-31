import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:polaris_foreman/app.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:polaris_foreman/data/repositories/mock/mock_seed.dart';
import 'package:polaris_foreman/l10n/gen/app_localizations.dart';
import 'package:polaris_foreman/state/providers.dart';
import 'package:polaris_foreman/state/session.dart';
import 'package:polaris_foreman/theme/app_theme.dart';

import 'fakes.dart';

/// Pumps the app at [location] on a 390×844 phone, against the mock server
/// of [h] and its in-memory phone store, in English.
Future<ProviderContainer> pumpAt(
  WidgetTester tester,
  Harness h, {
  required String location,
  SessionState? session,
  int? siteId,
  FakeProofCapture? proofs,
}) async {
  tester.view.devicePixelRatio = 3;
  tester.view.physicalSize = const Size(390 * 3, 844 * 3);
  addTearDown(tester.view.reset);

  final container = ProviderContainer(
    overrides: [
      localStoreProvider.overrideWithValue(h.store),
      tokenStoreProvider.overrideWithValue(h.tokens),
      repositoriesProvider.overrideWithValue(h.repos),
      initialSessionProvider.overrideWithValue(
        session ?? SignedIn(MockSeed.me(language: Language.en)),
      ),
      initialLocaleProvider.overrideWithValue('en'),
      initialSiteIdProvider.overrideWithValue(siteId),
      nowProvider.overrideWithValue(() => fixedNow),
      proofCaptureProvider.overrideWithValue(proofs ?? FakeProofCapture()),
    ],
  );
  addTearDown(container.dispose);
  final router = GoRouter(initialLocation: location, routes: appRoutes);
  addTearDown(router.dispose);
  await tester.pumpWidget(
    UncontrolledProviderScope(
      container: container,
      child: Consumer(
        builder: (context, ref, _) => MaterialApp.router(
          theme: buildTheme(),
          locale: ref.watch(localeProvider),
          supportedLocales: AppLocalizations.supportedLocales,
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          routerConfig: router,
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return container;
}

/// Puts the crew of [siteId] in the phone's cache, as a sync would.
Future<Crew> seedCrew(Harness h, int siteId) async {
  final crew = await h.backend.crew(await h.token(), siteId, today);
  await h.store.saveCrew(crew);
  return crew;
}
