import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/checkin_rules.dart';
import '../../data/local/local_store.dart';
import '../../data/models/models.dart';
import '../../services/checkin_service.dart';
import '../../services/location_service.dart';
import '../../state/providers.dart';
import '../../theme/tokens.dart';
import '../checkin_widgets.dart';
import '../qr_scanner.dart';
import '../widgets.dart';

/// The crew of a site from the phone's cache, by worker id.
final crewByIdProvider = FutureProvider.family<Map<int, CrewWorker>, int>((
  ref,
  siteId,
) async {
  await ref.watch(storeTickProvider.future);
  final crew = await ref.watch(localStoreProvider).loadCrew(siteId);
  return {for (final w in crew?.workers ?? const <CrewWorker>[]) w.id: w};
});

/// What a scan screen shares with the kiosk: camera reads go through the
/// debouncer, an optional photo, the latest GPS fix, then the outbox.
mixin ScanController<T extends ConsumerStatefulWidget> on ConsumerState<T> {
  late final GpsTracker gps;
  final debouncer = ScanDebouncer();
  late CheckInDirection direction;
  bool withPhoto = false;
  bool busy = false;
  ScanResult? last;
  Timer? _gpsTimer;

  int get scanSiteId;
  bool get kiosk;

  void initScanning() {
    final clock = ref.read(siteClockProvider);
    direction = defaultDirection(clock.local());
    gps = GpsTracker(
      ref.read(locationPlatformProvider),
      now: ref.read(nowProvider),
    );
    unawaited(gps.start().then((_) => mounted ? setState(() {}) : null));
    _gpsTimer = Timer.periodic(const Duration(seconds: 60), (_) async {
      await gps.refresh();
      if (mounted) setState(() {});
    });
  }

  void disposeScanning() => _gpsTimer?.cancel();

  /// Called with every code the camera reads.
  Future<void> onCode(String text) async {
    if (busy || !debouncer.accept(text)) return;
    setState(() => busy = true);
    try {
      String? photo;
      if (withPhoto) {
        try {
          photo = await ref.read(checkInPhotoProvider).take();
        } catch (_) {
          photo = null; // No camera for the photo: the scan still counts.
        }
      }
      final fix = gps.fresh();
      if (fix == null) unawaited(gps.refresh());
      final result = await ref
          .read(checkInServiceProvider)
          .scan(
            siteId: scanSiteId,
            text: text,
            direction: direction,
            fix: fix,
            photoPath: photo,
            kiosk: kiosk,
          );
      unawaited(
        result.recognised && result.flags.isEmpty
            ? HapticFeedback.mediumImpact()
            : HapticFeedback.heavyImpact(),
      );
      if (mounted) setState(() => last = result);
      onScanned(result);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  /// After a scan was saved (kiosk: start the timer that clears it).
  void onScanned(ScanResult result) {}

  /// Mock builds only: a scan of a crew badge (sometimes an unknown one),
  /// since emulators have no badge to show the camera.
  Future<void> simulateScan() async {
    final mock = ref.read(mockBackendProvider);
    if (mock == null) return;
    final crew = await ref.read(localStoreProvider).loadCrew(scanSiteId);
    final workers = crew?.workers ?? const <CrewWorker>[];
    final rnd = math.Random();
    final text = workers.isEmpty || rnd.nextInt(8) == 0
        ? 'PW1:AAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBB'
        : mock.badgeOf(workers[rnd.nextInt(workers.length)].id);
    debouncer.accept(''); // A simulated scan is never a repeat.
    await onCode(text);
  }
}

/// "Scan badges" from the roll call: each badge scan is a check-in (In,
/// or Out after midday or with the toggle), saved on the phone and queued in
/// the outbox; the scans then pre-fill the roll call.
class ScanScreen extends ConsumerStatefulWidget {
  const ScanScreen({super.key});

  @override
  ConsumerState<ScanScreen> createState() => _ScanScreenState();
}

class _ScanScreenState extends ConsumerState<ScanScreen>
    with ScanController<ScanScreen> {
  late final int _siteId;

  @override
  int get scanSiteId => _siteId;

  @override
  bool get kiosk => false;

  @override
  void initState() {
    super.initState();
    _siteId = ref.read(currentSiteProvider)!.id;
    initScanning();
  }

  @override
  void dispose() {
    disposeScanning();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final site = ref.watch(currentSiteProvider);
    final crew = ref.watch(crewByIdProvider(_siteId)).valueOrNull ?? const {};
    final scans =
        ref.watch(checkInListProvider(_siteId)).valueOrNull ?? const [];
    final view = ref.watch(rollCallViewProvider).valueOrNull;
    final scanner = ref.watch(scannerBuilderProvider);
    final mock = ref.watch(mockBackendProvider);
    final latest = last == null
        ? null
        : scans
              .where((c) => c.clientUuid == last!.checkIn.clientUuid)
              .firstOrNull;

    return Scaffold(
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          BackHeader(
            title: l.scanTitle,
            subtitle: site?.name ?? '',
            onBack: () => context.pop(),
            trailing: Text(
              l.scanCount(scans.length),
              key: const Key('scanCount'),
              style: const TextStyle(fontWeight: FontWeight.w700),
            ),
          ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.all(16),
              children: [
                ClipRRect(
                  borderRadius: BorderRadius.circular(PSizes.cardRadius),
                  child: AspectRatio(
                    aspectRatio: 4 / 3,
                    child: Stack(
                      fit: StackFit.expand,
                      children: [
                        scanner(context, onScan: onCode, frontCamera: false),
                        const _Viewfinder(),
                        if (busy)
                          const Center(child: CircularProgressIndicator()),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                DirectionToggle(
                  value: direction,
                  onChanged: (d) => setState(() => direction = d),
                ),
                const SizedBox(height: 4),
                SwitchListTile(
                  key: const Key('photoToggle'),
                  contentPadding: EdgeInsets.zero,
                  value: withPhoto,
                  onChanged: (v) => setState(() => withPhoto = v),
                  title: Text(l.photoWithScan),
                ),
                Row(
                  children: [
                    Icon(
                      gps.state == GpsState.ready
                          ? Icons.gps_fixed
                          : Icons.gps_off,
                      size: 18,
                      color: PColors.steel,
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        gpsLine(l, gps.state, gps.latest),
                        key: const Key('gpsLine'),
                        style: const TextStyle(color: PColors.steel),
                      ),
                    ),
                  ],
                ),
                if (view?.mode == AttendanceMode.selfCheckIn) ...[
                  const SizedBox(height: 8),
                  Text(
                    l.selfCheckInNote,
                    style: const TextStyle(color: PColors.ink700),
                  ),
                ],
                if (mock != null) ...[
                  const SizedBox(height: 8),
                  OutlinedButton.icon(
                    key: const Key('simulateScan'),
                    onPressed: busy ? null : simulateScan,
                    icon: const Icon(Icons.qr_code_2),
                    label: Text(l.simulateScan),
                  ),
                ],
                if (latest != null) ...[
                  const SizedBox(height: 12),
                  ScanFeedbackCard(
                    feedback: ScanFeedback.of(
                      latest,
                      crew,
                      localFlags: last!.flags,
                    ),
                  ),
                ],
                const SizedBox(height: 16),
                SectionLabel(l.todayScans),
                const SizedBox(height: 8),
                for (final c in scans)
                  _ScanRow(
                    key: ValueKey('scan-${c.clientUuid}'),
                    checkIn: c,
                    crew: crew,
                  ),
              ],
            ),
          ),
          BottomAction(
            child: FilledButton(
              key: const Key('backToRollCall'),
              onPressed: () => context.pop(),
              child: Text(l.backToRollCall),
            ),
          ),
        ],
      ),
    );
  }
}

class _ScanRow extends ConsumerWidget {
  const _ScanRow({super.key, required this.checkIn, required this.crew});
  final LocalCheckIn checkIn;
  final Map<int, CrewWorker> crew;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final l = context.l10n;
    final f = ScanFeedback.of(checkIn, crew);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: PColors.white,
        border: Border.all(color: PColors.line),
        borderRadius: BorderRadius.circular(PSizes.radius),
      ),
      child: Row(
        children: [
          Container(
            width: 10,
            height: 10,
            decoration: BoxDecoration(
              color: f.colors.fg,
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  f.title(l),
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
                if (f.flags.isNotEmpty)
                  Text(
                    f.flags
                        .map((x) => flagMessage(l, x, f.direction))
                        .join(' · '),
                    style: TextStyle(fontSize: 13, color: f.colors.fg),
                  ),
              ],
            ),
          ),
          Text(
            '${directionWord(l, f.direction)} ${timeOf(ref, checkIn.at)}',
            style: const TextStyle(
              fontWeight: FontWeight.w600,
              fontFeatures: [FontFeature.tabularFigures()],
            ),
          ),
          const SizedBox(width: 8),
          Icon(
            checkIn.sent ? Icons.cloud_done : Icons.cloud_upload_outlined,
            size: 18,
            color: PColors.steel,
            semanticLabel: checkIn.sent ? l.scanSent : l.scanQueued,
          ),
        ],
      ),
    );
  }
}

/// Four corners over the camera: where to hold the badge.
class _Viewfinder extends StatelessWidget {
  const _Viewfinder();

  @override
  Widget build(BuildContext context) => IgnorePointer(
    child: Center(
      child: FractionallySizedBox(
        widthFactor: 0.6,
        heightFactor: 0.75,
        child: DecoratedBox(
          decoration: BoxDecoration(
            border: Border.all(color: PColors.amber, width: 3),
            borderRadius: BorderRadius.circular(16),
          ),
        ),
      ),
    ),
  );
}
