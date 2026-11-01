import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/models/models.dart';
import '../../services/checkin_service.dart';
import '../../services/sync_service.dart';
import '../../state/providers.dart';
import '../../state/session.dart';
import '../../theme/tokens.dart';
import '../checkin_widgets.dart';
import '../qr_scanner.dart';
import '../widgets.dart';
import 'scan_screen.dart';

/// Kiosk phone (the office switched it to kiosk mode for one site): only
/// this screen. Workers scan their badge, choose In or Out (and a photo when
/// it is on), and see their name at once; check-ins go through the outbox
/// and work offline. Leaving needs the foreman's PIN.
class KioskScreen extends ConsumerStatefulWidget {
  const KioskScreen({super.key});

  /// How long a scan's answer stays on screen.
  static const feedbackFor = Duration(seconds: 6);

  @override
  ConsumerState<KioskScreen> createState() => _KioskScreenState();
}

class _KioskScreenState extends ConsumerState<KioskScreen>
    with ScanController<KioskScreen> {
  late final Kiosk _kiosk;
  bool _front = true;
  Timer? _clear;

  @override
  int get scanSiteId => _kiosk.siteId;

  @override
  bool get kiosk => true;

  @override
  void initState() {
    super.initState();
    _kiosk = ref.read(kioskProvider)!;
    initScanning();
    // Full screen: no status or navigation bar to wander off with.
    unawaited(
      SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky),
    );
  }

  @override
  void dispose() {
    _clear?.cancel();
    disposeScanning();
    unawaited(SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge));
    super.dispose();
  }

  @override
  void onScanned(ScanResult result) {
    _clear?.cancel();
    _clear = Timer(KioskScreen.feedbackFor, () {
      if (mounted) setState(() => last = null);
    });
  }

  /// Mock builds: what the office's "End kiosk mode" does. The router goes
  /// back to Today once the device role is refreshed.
  Future<void> _endDemoKiosk() async {
    final mock = ref.read(mockBackendProvider);
    if (mock == null) return;
    final messenger = ScaffoldMessenger.of(context);
    final text = context.l10n.mockKioskOff;
    mock.setKiosk(null);
    await ref.read(sessionProvider.notifier).refreshDeviceRole();
    messenger.showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _exit() async {
    final l = context.l10n;
    final ok = await askForemanPin(context, ref);
    if (!ok || !mounted) return;
    final out = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(l.kioskExit),
        content: Text(l.kioskExitText),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: Text(l.cancel),
          ),
          TextButton(
            key: const Key('kioskSignOut'),
            onPressed: () => Navigator.pop(context, true),
            child: Text(l.signOut),
          ),
        ],
      ),
    );
    if (out == true) await ref.read(sessionProvider.notifier).signOut();
  }

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final crew =
        ref.watch(crewByIdProvider(_kiosk.siteId)).valueOrNull ?? const {};
    final scans =
        ref.watch(checkInListProvider(_kiosk.siteId)).valueOrNull ?? const [];
    final sync =
        ref.watch(syncStatusProvider).valueOrNull ?? const SyncStatus();
    final scanner = ref.watch(scannerBuilderProvider);
    final mock = ref.watch(mockBackendProvider);
    final latest = last == null
        ? null
        : scans
              .where((c) => c.clientUuid == last!.checkIn.clientUuid)
              .firstOrNull;
    final top = MediaQuery.paddingOf(context).top;

    return PopScope(
      canPop: false,
      child: Scaffold(
        backgroundColor: PColors.ink,
        body: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: EdgeInsets.fromLTRB(20, top + 12, 8, 8),
              child: Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          l.kioskTitle.toUpperCase(),
                          style: const TextStyle(
                            color: PColors.amber,
                            fontWeight: FontWeight.w700,
                            letterSpacing: 0.9,
                            fontSize: 13,
                          ),
                        ),
                        GestureDetector(
                          // Mock builds only: end the demo kiosk mode.
                          onLongPress: ref.watch(mockBackendProvider) == null
                              ? null
                              : _endDemoKiosk,
                          child: Text(
                            _kiosk.siteName,
                            key: const Key('kioskSite'),
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: PColors.white,
                              fontWeight: FontWeight.w800,
                              fontSize: 22,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                  Icon(
                    sync.online == false ? Icons.wifi_off : Icons.wifi,
                    color: PColors.onInkMuted,
                    size: 20,
                  ),
                  const SizedBox(width: 6),
                  Text(
                    l.bannerWaiting(sync.waiting),
                    style: const TextStyle(color: PColors.onInkMuted),
                  ),
                  IconButton(
                    key: const Key('kioskCamera'),
                    tooltip: l.switchCamera,
                    onPressed: () => setState(() => _front = !_front),
                    icon: const Icon(
                      Icons.cameraswitch,
                      color: PColors.onInkMuted,
                    ),
                  ),
                  IconButton(
                    key: const Key('kioskExit'),
                    tooltip: l.kioskExit,
                    onPressed: _exit,
                    icon: const Icon(Icons.lock, color: PColors.onInkMuted),
                  ),
                ],
              ),
            ),
            Expanded(
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(PSizes.cardRadius),
                  child: Stack(
                    fit: StackFit.expand,
                    children: [
                      scanner(context, onScan: onCode, frontCamera: _front),
                      if (latest == null)
                        Align(
                          alignment: Alignment.bottomCenter,
                          child: Container(
                            margin: const EdgeInsets.all(16),
                            padding: const EdgeInsets.symmetric(
                              horizontal: 20,
                              vertical: 12,
                            ),
                            decoration: BoxDecoration(
                              color: PColors.ink.withValues(alpha: 0.8),
                              borderRadius: BorderRadius.circular(999),
                            ),
                            child: Text(
                              l.kioskHint,
                              style: const TextStyle(
                                color: PColors.white,
                                fontSize: 22,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ),
                        )
                      else
                        Align(
                          alignment: Alignment.bottomCenter,
                          child: Padding(
                            padding: const EdgeInsets.all(12),
                            child: ScanFeedbackCard(
                              big: true,
                              feedback: ScanFeedback.of(
                                latest,
                                crew,
                                localFlags: last!.flags,
                              ),
                            ),
                          ),
                        ),
                      if (busy)
                        const Center(child: CircularProgressIndicator()),
                    ],
                  ),
                ),
              ),
            ),
            Container(
              color: PColors.white,
              padding: EdgeInsets.fromLTRB(
                16,
                12,
                16,
                12 + MediaQuery.paddingOf(context).bottom,
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  DirectionToggle(
                    big: true,
                    value: direction,
                    onChanged: (d) => setState(() => direction = d),
                  ),
                  SwitchListTile(
                    key: const Key('photoToggle'),
                    contentPadding: EdgeInsets.zero,
                    value: withPhoto,
                    onChanged: (v) => setState(() => withPhoto = v),
                    title: Text(
                      l.photoWithScan,
                      style: const TextStyle(fontSize: 17),
                    ),
                  ),
                  Text(
                    gpsLine(l, gps.state, gps.latest),
                    key: const Key('gpsLine'),
                    style: const TextStyle(color: PColors.steel),
                  ),
                  if (mock != null)
                    TextButton.icon(
                      key: const Key('simulateScan'),
                      onPressed: busy ? null : simulateScan,
                      icon: const Icon(Icons.qr_code_2),
                      label: Text(l.simulateScan),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Asks for the signed-in foreman's PIN (checked on the phone). True when it
/// is right.
Future<bool> askForemanPin(BuildContext context, WidgetRef ref) async {
  final ok = await showDialog<bool>(
    context: context,
    builder: (_) => const _PinDialog(),
  );
  return ok ?? false;
}

class _PinDialog extends ConsumerStatefulWidget {
  const _PinDialog();

  @override
  ConsumerState<_PinDialog> createState() => _PinDialogState();
}

class _PinDialogState extends ConsumerState<_PinDialog> {
  final _pin = TextEditingController();
  bool _wrong = false;
  bool _busy = false;

  @override
  void dispose() {
    _pin.dispose();
    super.dispose();
  }

  Future<void> _check() async {
    if (_pin.text.length != 4 || _busy) return;
    setState(() => _busy = true);
    final ok = await ref.read(sessionProvider.notifier).verifyPin(_pin.text);
    if (!mounted) return;
    if (ok) {
      Navigator.pop(context, true);
    } else {
      setState(() {
        _busy = false;
        _wrong = true;
        _pin.clear();
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    return AlertDialog(
      title: Text(l.kioskPinTitle),
      content: TextField(
        key: const Key('kioskPin'),
        controller: _pin,
        autofocus: true,
        obscureText: true,
        keyboardType: TextInputType.number,
        maxLength: 4,
        inputFormatters: [FilteringTextInputFormatter.digitsOnly],
        onSubmitted: (_) => _check(),
        decoration: InputDecoration(errorText: _wrong ? l.kioskPinWrong : null),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context, false),
          child: Text(l.cancel),
        ),
        TextButton(
          key: const Key('kioskPinOk'),
          onPressed: _busy ? null : _check,
          child: Text(l.continueButton),
        ),
      ],
    );
  }
}
