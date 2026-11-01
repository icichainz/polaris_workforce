import 'dart:typed_data';
import 'dart:ui' show Locale;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/site_clock.dart';
import '../data/local/local_store.dart';
import '../data/local/token_store.dart';
import '../data/models/models.dart';
import '../data/repositories/mock/mock_backend.dart';
import '../data/repositories/repositories.dart';
import '../services/checkin_photo.dart';
import '../services/checkin_service.dart';
import '../services/location_service.dart';
import '../services/payout_service.dart';
import '../services/photo_cache.dart';
import '../services/proof_capture.dart';
import '../services/roll_call_service.dart';
import '../services/sync_service.dart';
import '../tracking/tracking_providers.dart';
import 'session.dart';

// ------------------------------------------------- overridden in main()/tests

final localStoreProvider = Provider<LocalStore>(
  (ref) => throw UnimplementedError('Override localStoreProvider'),
);

final tokenStoreProvider = Provider<TokenStore>(
  (ref) => throw UnimplementedError('Override tokenStoreProvider'),
);

final repositoriesProvider = Provider<Repositories>(
  (ref) => throw UnimplementedError('Override repositoriesProvider'),
);

/// The in-memory server when running with `USE_MOCK_API` (debug switches).
final mockBackendProvider = Provider<MockBackend?>((ref) => null);

final initialLocaleProvider = Provider<String?>((ref) => null);
final initialSiteIdProvider = Provider<int?>((ref) => null);

/// Fixed "now" for tests.
final nowProvider = Provider<DateTime Function()>((ref) => DateTime.now);

final proofCaptureProvider = Provider<ProofCapture>(
  (ref) => DeviceProofCapture(),
);

/// The check-in photo (front camera, compressed).
final checkInPhotoProvider = Provider<CheckInPhotoCapture>(
  (ref) => DeviceCheckInPhotoCapture(),
);

/// GPS for check-ins (geolocator on the phone; a fake in tests).
final locationPlatformProvider = Provider<LocationPlatform>(
  (ref) => const GeolocatorPlatformAdapter(),
);

/// Where downloaded photos are kept (files on the phone).
final photoStorageProvider = Provider<PhotoStorage>(
  (ref) => DiskPhotoStorage(),
);

/// The last `GET /checkin/device/` read before the first frame.
final initialCheckInDeviceProvider = Provider<CheckInDevice?>((ref) => null);

// ------------------------------------------------------------------- state

class LocaleController extends Notifier<Locale> {
  @override
  Locale build() => Locale(ref.watch(initialLocaleProvider) ?? 'en');

  void set(String code, {bool persist = true}) {
    state = Locale(code);
    // The mock server translates `worked` labels like Accept-Language would.
    ref.read(mockBackendProvider)?.language = code;
    if (persist) {
      ref.read(localStoreProvider).setValue(StoreKeys.language, code);
    }
  }

  void toggle() => set(state.languageCode == 'fr' ? 'en' : 'fr');
}

final localeProvider = NotifierProvider<LocaleController, Locale>(
  LocaleController.new,
);

Me? _me(SessionState s) => s is SignedIn ? s.me : null;

final meProvider = Provider<Me?>((ref) => _me(ref.watch(sessionProvider)));

final siteClockProvider = Provider<SiteClock>(
  (ref) => SiteClock(
    now: ref.watch(nowProvider),
    timeZone: ref.watch(meProvider)?.company.timeZone,
  ),
);

final todayProvider = Provider<String>(
  (ref) => ref.watch(siteClockProvider).today(),
);

final currencyProvider = Provider<Currency>(
  (ref) => ref.watch(meProvider)?.company.currency ?? Currency.xof,
);

/// Proof-of-payment rules (`GET /me/` → `company.payments`).
final paymentRulesProvider = Provider<PaymentRules>(
  (ref) => ref.watch(meProvider)?.company.payments ?? PaymentRules.standard,
);

class SiteController extends Notifier<AssignedSite?> {
  @override
  AssignedSite? build() {
    final sites = ref.watch(meProvider)?.sites ?? const [];
    if (sites.isEmpty) return null;
    final wanted = ref.read(initialSiteIdProvider);
    return sites.where((s) => s.id == wanted).firstOrNull ?? sites.first;
  }

  void select(AssignedSite site) {
    state = site;
    ref.read(localStoreProvider).setValue(StoreKeys.siteId, '${site.id}');
  }
}

final currentSiteProvider = NotifierProvider<SiteController, AssignedSite?>(
  SiteController.new,
);

/// This phone's check-in role (`GET /checkin/device/`): attendance mode, and
/// the kiosk site when the office switched it to kiosk mode.
class CheckInDeviceController extends Notifier<CheckInDevice?> {
  @override
  CheckInDevice? build() => ref.watch(initialCheckInDeviceProvider);

  void set(CheckInDevice? device) {
    final old = state;
    if (old != null &&
        device != null &&
        old.attendanceMode == device.attendanceMode &&
        old.kiosk?.siteId == device.kiosk?.siteId) {
      return;
    }
    state = device;
  }
}

final checkInDeviceProvider =
    NotifierProvider<CheckInDeviceController, CheckInDevice?>(
      CheckInDeviceController.new,
    );

/// Set on a kiosk phone: only the kiosk screen of that site is shown.
final kioskProvider = Provider<Kiosk?>(
  (ref) => ref.watch(checkInDeviceProvider)?.kiosk,
);

// ---------------------------------------------------------------- services

final Provider<SyncService> syncServiceProvider = Provider<SyncService>((ref) {
  final service = SyncService(
    store: ref.watch(localStoreProvider),
    repos: ref.watch(repositoriesProvider),
    clock: ref.read(siteClockProvider),
    now: ref.watch(nowProvider),
  );
  service.onSignedOut = () => ref.read(sessionProvider.notifier).expired();
  service.onDeviceRole = (d) => ref.read(checkInDeviceProvider.notifier).set(d);
  service.onSuggestions = (siteId, date) async {
    await ref.read(checkInServiceProvider).applySuggestions(siteId, date);
  };
  service.sendConsent = (e) =>
      ref.read(trackingProvider.notifier).sendConsent(e);
  ref.listen(siteClockProvider, (_, c) => service.clock = c);
  // Sync while signed in; stop (outbox kept) when signed out.
  ref.listen(sessionProvider, (_, s) {
    if (s is SignedIn) {
      service.start();
    } else {
      service.pause();
    }
  });
  ref.onDispose(service.dispose);
  return service;
});

final Provider<RollCallService> rollCallServiceProvider =
    Provider<RollCallService>(
      (ref) => RollCallService(
        ref.watch(localStoreProvider),
        clock: ref.watch(siteClockProvider),
        onWrite: () => ref.read(syncServiceProvider).kick(),
      ),
    );

final Provider<CheckInService> checkInServiceProvider =
    Provider<CheckInService>(
      (ref) => CheckInService(
        ref.watch(localStoreProvider),
        clock: ref.watch(siteClockProvider),
        rollCall: ref.watch(rollCallServiceProvider),
        onWrite: () => ref.read(syncServiceProvider).kick(),
      ),
    );

final photoCacheProvider = Provider<PhotoCache>(
  (ref) => PhotoCache(
    ref.watch(repositoriesProvider).files,
    ref.watch(photoStorageProvider),
    now: ref.watch(nowProvider),
  ),
);

/// A protected photo (`photo_url`): cached on the phone, revalidated with
/// `If-None-Match`. Null: show the initials.
final photoProvider = FutureProvider.family<Uint8List?, String>(
  (ref, url) => ref.watch(photoCacheProvider).load(url),
);

final payoutServiceProvider = Provider<PayoutService>(
  (ref) => PayoutService(
    ref.watch(localStoreProvider),
    clock: ref.watch(siteClockProvider),
    onWrite: () => ref.read(syncServiceProvider).kick(),
  ),
);

/// Ticks after every local write, so views re-read the store.
final storeTickProvider = StreamProvider<int>((ref) async* {
  var n = 0;
  yield n;
  await for (final _ in ref.watch(localStoreProvider).changes) {
    yield ++n;
  }
});

final syncStatusProvider = StreamProvider<SyncStatus>((ref) async* {
  final service = ref.watch(syncServiceProvider);
  yield service.status;
  yield* service.statusStream;
});

// ------------------------------------------------------------------- views

class RollCallView {
  const RollCallView({
    required this.site,
    required this.date,
    required this.crew,
    required this.records,
    required this.day,
    required this.status,
    required this.submitPending,
    this.mode,
    this.prefilled = const {},
    this.checkIns = const [],
  });

  final AssignedSite site;
  final String date;
  final List<CrewWorker> crew;
  final Map<int, LocalRecord> records;
  final AttendanceDaySummary? day;

  /// Counts a submit still in the outbox as submitted.
  final DayStatus status;
  final bool submitPending;

  /// The attendance mode as last seen (null before the first sync with a
  /// 1.2 server).
  final AttendanceMode? mode;

  /// Marks pre-filled from check-ins; a record still equal to one was not
  /// changed by hand.
  final Map<int, SuggestedMark> prefilled;

  /// Today's check-ins made on this phone.
  final List<LocalCheckIn> checkIns;

  /// The worker's mark came from check-ins and was not changed by hand.
  bool fromCheckIn(int workerId) {
    final r = records[workerId], p = prefilled[workerId];
    return r != null && p != null && p.matches(r);
  }

  DayCounts get counts => DayCounts.of(
    crew.length,
    crew.map((w) => records[w.id]?.status).whereType<AttendanceStatus>(),
  );

  int get marked => crew.where((w) => records.containsKey(w.id)).length;

  List<int> get unmarked => [
    for (final w in crew)
      if (!records.containsKey(w.id)) w.id,
  ];

  bool get editable => status.editable;
  bool get correctable =>
      status == DayStatus.submitted || status == DayStatus.approved;
}

Future<void> _tick(Ref ref) => ref.watch(storeTickProvider.future);

final rollCallViewProvider = FutureProvider<RollCallView?>((ref) async {
  await _tick(ref);
  final site = ref.watch(currentSiteProvider);
  if (site == null) return null;
  final date = ref.watch(todayProvider);
  final store = ref.watch(localStoreProvider);
  final crew = await store.loadCrew(site.id);
  final records = await store.loadRecords(site.id, date);
  final day = await store.loadDay(site.id, date);
  final outbox = await store.loadOutbox();
  final checkins = ref.watch(checkInServiceProvider);
  return RollCallView(
    mode: await checkins.mode(site.id),
    prefilled: await checkins.prefilled(site.id, date),
    checkIns: await store.loadCheckIns(site.id, date),
    site: site,
    date: date,
    crew: crew?.workers ?? const [],
    records: {for (final r in records) r.workerId: r},
    day: day,
    status: effectiveDayStatus(day, outbox),
    submitPending: outbox.any(
      (e) =>
          e.kind == OutboxKind.submit &&
          e.state == OutboxState.pending &&
          e.siteId == site.id &&
          e.date == date,
    ),
  );
});

/// Days before today, newest first ("This week").
final weekProvider = FutureProvider<List<AttendanceDaySummary>>((ref) async {
  await _tick(ref);
  final site = ref.watch(currentSiteProvider);
  if (site == null) return const [];
  final today = ref.watch(todayProvider);
  final days = await ref.watch(localStoreProvider).loadDays(site.id);
  return days.where((d) => d.date.compareTo(today) < 0).take(6).toList();
});

final payoutViewProvider = FutureProvider<PayoutView?>((ref) async {
  await _tick(ref);
  final site = ref.watch(currentSiteProvider);
  if (site == null) return null;
  final date = ref.watch(todayProvider);
  final store = ref.watch(localStoreProvider);
  final sheet = await store.loadPayout(site.id, date);
  if (sheet == null) return null;
  return buildPayoutView(sheet, await store.loadPayments(site.id, date));
});

final failedOutboxProvider = FutureProvider<List<OutboxEntry>>((ref) async {
  await _tick(ref);
  final outbox = await ref.watch(localStoreProvider).loadOutbox();
  return outbox.where((e) => e.state == OutboxState.failed).toList();
});

/// Today's check-ins made on this phone for a site, newest first.
final checkInListProvider = FutureProvider.family<List<LocalCheckIn>, int>((
  ref,
  siteId,
) async {
  await _tick(ref);
  final date = ref.watch(todayProvider);
  final list = await ref.watch(localStoreProvider).loadCheckIns(siteId, date);
  return list.reversed.toList();
});
