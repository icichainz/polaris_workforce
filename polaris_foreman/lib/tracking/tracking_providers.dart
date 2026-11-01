import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../data/local/local_store.dart';
import '../data/local/memory_local_store.dart';
import '../l10n/gen/app_localizations.dart';
import '../services/sync_service.dart';
import '../state/providers.dart';
import '../state/session.dart';
import 'sensors.dart';
import 'tracking_api.dart';
import 'tracking_client.dart';
import 'tracking_token_store.dart';

// ------------------------------------------------- overridden in main()/tests

/// Django's tracking endpoints. Off unless `main()` provides the real (or
/// mock) one, so tests and older setups show no tracking at all.
final trackingRepositoryProvider = Provider<TrackingRepository>(
  (ref) => const DisabledTrackingRepository(),
);

/// The tracker service (`POST /v1/observations`).
final trackerTransportProvider = Provider<TrackerTransport>(
  (ref) => HttpTrackerTransport(),
);

/// Where the tracker token is kept (secure storage in the app).
final trackingTokenStoreProvider = Provider<TrackingTokenStore>(
  (ref) => MemoryTrackingTokenStore(),
);

final trackingLocationProvider = Provider<TrackingLocationSource>(
  (ref) => const GeolocatorTrackingSource(),
);

final badgeScannerProvider = Provider<BadgeScanner>(
  (ref) => BluePlusBadgeScanner(),
);

/// The observation queue: the phone's database when it has one.
final observationStoreProvider = Provider<ObservationStore>((ref) {
  final store = ref.watch(localStoreProvider);
  return store is ObservationStore
      ? store as ObservationStore
      : MemoryLocalStore();
});

// ------------------------------------------------------------------- state

/// Tracking for the signed-in foreman ([TrackingClient]), following the
/// session (sign-in fetches the token, sign-out drops it), the current site
/// (badge scans), kiosk mode (nothing recorded) and the network (the queue
/// is sent when the sync service is online again).
class TrackingController extends Notifier<TrackingState> {
  late TrackingClient client;

  @override
  TrackingState build() {
    final c = TrackingClient(
      api: ref.read(trackingRepositoryProvider),
      tokens: ref.read(trackingTokenStoreProvider),
      observations: ref.read(observationStoreProvider),
      store: ref.read(localStoreProvider),
      transport: ref.read(trackerTransportProvider),
      location: ref.read(trackingLocationProvider),
      scanner: ref.read(badgeScannerProvider),
      clock: () => ref.read(siteClockProvider),
      now: ref.read(nowProvider),
      notice: _notice,
    );
    client = c;
    var disposed = false;
    ref.onDispose(() {
      disposed = true;
      c.dispose();
    });
    c.setSite(ref.read(currentSiteProvider)?.id);
    c.setKiosk(ref.read(kioskProvider) != null);
    c.onChange = (s) => state = s;
    c.kickSync = () => ref.read(syncServiceProvider).kick();

    ref.listen(currentSiteProvider, (_, site) => c.setSite(site?.id));
    ref.listen(kioskProvider, (_, k) => c.setKiosk(k != null));
    ref.listen(sessionProvider, (_, s) => _follow(c, s));
    ref.listen<AsyncValue<SyncStatus>>(syncStatusProvider, (before, now) {
      if (now.valueOrNull?.online == true &&
          before?.valueOrNull?.online != true) {
        c.online();
      }
    });
    Future.microtask(() {
      if (!disposed) _follow(c, ref.read(sessionProvider));
    });
    return const TrackingState();
  }

  void _follow(TrackingClient c, SessionState s) {
    if (s is SignedIn) {
      c.signedIn('${s.me.id}');
    } else {
      c.signedOut();
    }
  }

  BackgroundNotice _notice() {
    final l = lookupAppLocalizations(ref.read(localeProvider));
    return BackgroundNotice(
      title: l.trackingBackgroundTitle,
      text: l.trackingBackgroundText,
    );
  }

  Future<void> answerConsent({required bool accepted}) =>
      client.answerConsent(accepted: accepted);

  Future<void> sendConsent(OutboxEntry entry) => client.sendConsent(entry);

  void setForeground(bool foreground) => client.setForeground(foreground);
}

final trackingProvider = NotifierProvider<TrackingController, TrackingState>(
  TrackingController.new,
);

/// Tells tracking when the app goes to the background and comes back
/// (badges are scanned only while the app is open; GPS starts only in the
/// foreground). Wraps the app's navigator.
class TrackingLifecycle extends ConsumerStatefulWidget {
  const TrackingLifecycle({super.key, required this.child});
  final Widget child;

  @override
  ConsumerState<TrackingLifecycle> createState() => _TrackingLifecycleState();
}

class _TrackingLifecycleState extends ConsumerState<TrackingLifecycle> {
  late final AppLifecycleListener _listener;

  @override
  void initState() {
    super.initState();
    _listener = AppLifecycleListener(
      onStateChange: (s) => ref
          .read(trackingProvider.notifier)
          // `inactive`: a permission dialog or the app switcher; still open.
          .setForeground(
            s == AppLifecycleState.resumed || s == AppLifecycleState.inactive,
          ),
    );
  }

  @override
  void dispose() {
    _listener.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
