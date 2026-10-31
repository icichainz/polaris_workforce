import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../data/local/local_store.dart';
import '../data/models/models.dart';
import '../l10n/gen/app_localizations.dart';
import '../services/location_service.dart';
import '../theme/tokens.dart';
import 'widgets.dart';
import 'worker_avatar.dart';

String directionWord(AppLocalizations l, CheckInDirection d) =>
    d == CheckInDirection.checkIn ? l.directionIn : l.directionOut;

/// What the phone shows for a flag (README "What the phone shows").
String flagMessage(AppLocalizations l, CheckInFlag f, CheckInDirection d) =>
    switch (f) {
      CheckInFlag.unknownBadge => l.badgeNotRecognised,
      CheckInFlag.revokedBadge => l.badgeRevoked,
      CheckInFlag.notOnCrew => l.notOnCrew,
      CheckInFlag.outsideGeofence => l.outsideSite,
      CheckInFlag.duplicate =>
        d == CheckInDirection.checkIn
            ? l.alreadyCheckedIn
            : l.alreadyCheckedOut,
    };

enum ScanTone { ok, warn, error }

/// One check-in as the screens show it: the server's answer once there is
/// one, else what the phone worked out offline.
class ScanFeedback {
  const ScanFeedback({
    required this.checkIn,
    required this.worker,
    required this.flags,
  });

  /// [localFlags]: what the phone saw at the scan (used until the server
  /// answers).
  factory ScanFeedback.of(
    LocalCheckIn c,
    Map<int, CrewWorker> crew, {
    List<CheckInFlag>? localFlags,
  }) {
    final id = c.knownWorkerId;
    final flags =
        c.server?.flags ??
        localFlags ??
        (id == null ? const [CheckInFlag.unknownBadge] : const []);
    return ScanFeedback(
      checkIn: c,
      worker: id == null ? null : crew[id],
      flags: flags,
    );
  }

  final LocalCheckIn checkIn;
  final CrewWorker? worker;
  final List<CheckInFlag> flags;

  CheckInDirection get direction => checkIn.direction;

  ScanTone get tone {
    if (flags.contains(CheckInFlag.unknownBadge) ||
        flags.contains(CheckInFlag.revokedBadge)) {
      return ScanTone.error;
    }
    return flags.isEmpty ? ScanTone.ok : ScanTone.warn;
  }

  StatusColors get colors => switch (tone) {
    ScanTone.ok => StatusColors.present,
    ScanTone.warn => StatusColors.half,
    ScanTone.error => StatusColors.absent,
  };

  String title(AppLocalizations l) =>
      worker?.name ??
      (checkIn.knownWorkerId == null
          ? l.badgeNotRecognisedTitle
          : l.workerNumber(checkIn.knownWorkerId!));
}

/// The big answer to a scan: name, In/Out, what is wrong if anything.
class ScanFeedbackCard extends ConsumerWidget {
  const ScanFeedbackCard({super.key, required this.feedback, this.big = false});

  final ScanFeedback feedback;

  /// Kiosk size: readable from an arm's length.
  final bool big;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final l = context.l10n;
    final f = feedback;
    final c = f.colors;
    final icon = switch (f.tone) {
      ScanTone.ok => Icons.check_circle,
      ScanTone.warn => Icons.warning_amber_rounded,
      ScanTone.error => Icons.cancel,
    };
    final time = timeOf(ref, f.checkIn.at);
    return Semantics(
      liveRegion: true,
      container: true,
      child: Container(
        key: const Key('scanFeedback'),
        padding: EdgeInsets.all(big ? 20 : 14),
        decoration: BoxDecoration(
          color: c.bg,
          border: Border.all(color: c.fg, width: 2),
          borderRadius: BorderRadius.circular(PSizes.cardRadius),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (f.worker != null)
              WorkerAvatar(
                name: f.worker!.name,
                photoUrl: f.worker!.photoUrl,
                size: big ? 72 : 48,
              )
            else
              Icon(icon, color: c.fg, size: big ? 72 : 48),
            SizedBox(width: big ? 16 : 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    f.title(l),
                    style: TextStyle(
                      fontSize: big ? 30 : 19,
                      fontWeight: FontWeight.w800,
                      color: PColors.ink,
                      height: 1.15,
                    ),
                  ),
                  SizedBox(height: big ? 8 : 4),
                  Row(
                    children: [
                      Container(
                        padding: EdgeInsets.symmetric(
                          horizontal: big ? 14 : 10,
                          vertical: big ? 6 : 3,
                        ),
                        decoration: BoxDecoration(
                          color: c.fg,
                          borderRadius: BorderRadius.circular(999),
                        ),
                        child: Text(
                          directionWord(l, f.direction),
                          style: TextStyle(
                            color: PColors.white,
                            fontWeight: FontWeight.w800,
                            fontSize: big ? 22 : 14,
                          ),
                        ),
                      ),
                      const SizedBox(width: 10),
                      Text(
                        time,
                        style: TextStyle(
                          fontSize: big ? 22 : 15,
                          fontWeight: FontWeight.w600,
                          color: PColors.ink700,
                          fontFeatures: const [FontFeature.tabularFigures()],
                        ),
                      ),
                    ],
                  ),
                  for (final flag in f.flags)
                    Padding(
                      padding: EdgeInsets.only(top: big ? 8 : 4),
                      child: Text(
                        flagMessage(l, flag, f.direction),
                        style: TextStyle(
                          fontSize: big ? 20 : 14,
                          fontWeight: FontWeight.w700,
                          color: c.fg,
                        ),
                      ),
                    ),
                  if (!f.checkIn.sent)
                    Padding(
                      padding: EdgeInsets.only(top: big ? 8 : 4),
                      child: Text(
                        l.scanQueued,
                        style: TextStyle(
                          fontSize: big ? 16 : 13,
                          color: PColors.ink700,
                        ),
                      ),
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

/// In / Out switch: large, two segments.
class DirectionToggle extends StatelessWidget {
  const DirectionToggle({
    super.key,
    required this.value,
    required this.onChanged,
    this.big = false,
  });

  final CheckInDirection value;
  final ValueChanged<CheckInDirection> onChanged;
  final bool big;

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    return SegmentedButton<CheckInDirection>(
      key: const Key('direction'),
      showSelectedIcon: false,
      style: SegmentedButton.styleFrom(
        minimumSize: Size(0, big ? 64 : 48),
        textStyle: TextStyle(
          fontSize: big ? 22 : 16,
          fontWeight: FontWeight.w800,
        ),
        selectedBackgroundColor: PColors.amber,
        selectedForegroundColor: PColors.ink,
      ),
      segments: [
        ButtonSegment(
          value: CheckInDirection.checkIn,
          icon: const Icon(Icons.login),
          label: Text(l.directionIn, key: const Key('directionIn')),
        ),
        ButtonSegment(
          value: CheckInDirection.checkOut,
          icon: const Icon(Icons.logout),
          label: Text(l.directionOut, key: const Key('directionOut')),
        ),
      ],
      selected: {value},
      onSelectionChanged: (s) => onChanged(s.single),
    );
  }
}

/// GPS state as a short line under the scanner.
String gpsLine(AppLocalizations l, GpsState state, GpsFix? fix) =>
    switch (state) {
      GpsState.serviceOff => l.gpsOff,
      GpsState.denied || GpsState.deniedForever => l.gpsDenied,
      GpsState.unknown => l.gpsLooking,
      GpsState.ready =>
        fix == null
            ? l.gpsLooking
            : fix.accuracyM == null
            ? l.gpsReadyNoAccuracy
            : l.gpsReady(fix.accuracyM!),
    };
