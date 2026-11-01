import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import 'l10n/gen/app_localizations.dart';
import 'state/providers.dart';
import 'state/session.dart';
import 'theme/app_theme.dart';
import 'tracking/consent_screen.dart';
import 'tracking/tracking_providers.dart';
import 'ui/screens/kiosk_screen.dart';
import 'ui/screens/payout_screen.dart';
import 'ui/screens/pin_screen.dart';
import 'ui/screens/review_screen.dart';
import 'ui/screens/roll_call_screen.dart';
import 'ui/screens/scan_screen.dart';
import 'ui/screens/today_screen.dart';

final appRoutes = <RouteBase>[
  GoRoute(path: '/pin', builder: (_, _) => const PinScreen()),
  GoRoute(path: '/today', builder: (_, _) => const TodayScreen()),
  GoRoute(path: '/roll-call', builder: (_, _) => const RollCallScreen()),
  GoRoute(path: '/review', builder: (_, _) => const ReviewScreen()),
  GoRoute(path: '/payout', builder: (_, _) => const PayoutScreen()),
  GoRoute(path: '/scan', builder: (_, _) => const ScanScreen()),
  GoRoute(path: '/kiosk', builder: (_, _) => const KioskScreen()),
  GoRoute(
    path: trackingConsentRoute,
    builder: (_, _) => const TrackingConsentScreen(),
  ),
];

/// Where the app may be: signed out → the PIN screen; a kiosk phone → only
/// the kiosk screen; a foreman phone → anything but the kiosk screen. When
/// the tracking notice must be answered ([consent]), Today opens it (other
/// screens are left alone: never in the middle of a roll call or payout).
String? appRedirect({
  required bool signedIn,
  required bool kiosk,
  required String location,
  bool consent = false,
}) {
  if (!signedIn) return location == '/pin' ? null : '/pin';
  if (kiosk) return location == '/kiosk' ? null : '/kiosk';
  if (consent && (location == '/today' || location == '/pin')) {
    return trackingConsentRoute;
  }
  if (location == '/pin' || location == '/kiosk') return '/today';
  return null;
}

/// Bridges the session to go_router's `refreshListenable`.
class _SessionListenable extends ChangeNotifier {
  void ping() => notifyListeners();
}

final routerProvider = Provider<GoRouter>((ref) {
  final refresh = _SessionListenable();
  ref.listen(sessionProvider, (_, _) => refresh.ping());
  ref.listen(kioskProvider, (a, b) {
    if (a?.siteId != b?.siteId) refresh.ping();
  });
  ref.listen(
    trackingProvider.select((s) => s.consentNeeded),
    (_, _) => refresh.ping(),
  );
  final router = GoRouter(
    initialLocation: '/today',
    refreshListenable: refresh,
    redirect: (context, state) => appRedirect(
      signedIn: ref.read(sessionProvider) is SignedIn,
      kiosk: ref.read(kioskProvider) != null,
      location: state.matchedLocation,
      consent: ref.read(trackingProvider).consentNeeded,
    ),
    routes: appRoutes,
  );
  ref.onDispose(() {
    router.dispose();
    refresh.dispose();
  });
  return router;
});

class PolarisApp extends ConsumerWidget {
  const PolarisApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return MaterialApp.router(
      onGenerateTitle: (context) => 'Polaris Workforce',
      debugShowCheckedModeBanner: false,
      theme: buildTheme(),
      locale: ref.watch(localeProvider),
      supportedLocales: AppLocalizations.supportedLocales,
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      routerConfig: ref.watch(routerProvider),
      builder: (context, child) => TrackingLifecycle(child: child!),
    );
  }
}
