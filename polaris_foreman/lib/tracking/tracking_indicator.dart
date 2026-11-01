import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../theme/tokens.dart';
import '../ui/widgets.dart';
import 'consent_screen.dart';
import 'tracking_client.dart';
import 'tracking_providers.dart';

/// Small "Location on / paused / off" pill for the Today header; nothing at
/// all when tracking is off for this foreman. Tap: what is recorded now.
class TrackingIndicatorPill extends ConsumerWidget {
  const TrackingIndicatorPill({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final l = context.l10n;
    final s = ref.watch(trackingProvider);
    final (String, StatusColors, IconData)? look = s.consentNeeded
        ? (l.trackingConsentNeeded, StatusColors.half, Icons.my_location)
        : switch (s.indicator) {
            TrackingIndicator.hidden => null,
            TrackingIndicator.on => (
              l.trackingOn,
              StatusColors.present,
              Icons.my_location,
            ),
            TrackingIndicator.paused => (
              l.trackingPaused,
              StatusColors.neutral,
              Icons.location_disabled,
            ),
            TrackingIndicator.off => (
              l.trackingOff,
              StatusColors.absent,
              Icons.location_off,
            ),
          };
    if (look == null) return const SizedBox.shrink();
    final (text, colors, icon) = look;
    return Padding(
      padding: const EdgeInsets.only(top: 10),
      child: Align(
        alignment: Alignment.centerLeft,
        child: Semantics(
          button: true,
          child: InkWell(
            key: const Key('trackingIndicator'),
            borderRadius: BorderRadius.circular(999),
            onTap: s.consentNeeded
                ? () => context.push(trackingConsentRoute)
                : () => showTrackingSheet(context),
            child: Container(
              height: 28,
              padding: const EdgeInsets.symmetric(horizontal: 10),
              decoration: BoxDecoration(
                color: colors.bg,
                borderRadius: BorderRadius.circular(999),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(icon, size: 15, color: colors.fg),
                  const SizedBox(width: 6),
                  Text(
                    text,
                    style: TextStyle(
                      color: colors.fg,
                      fontSize: 13,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// What tracking does right now, and the notice.
Future<void> showTrackingSheet(BuildContext context) =>
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (context) => Consumer(
        builder: (context, ref, _) {
          final l = context.l10n;
          final s = ref.watch(trackingProvider);
          final hours = s.token?.hours;
          final lines = <String>[
            if (hours != null)
              switch (s.indicator) {
                TrackingIndicator.paused => l.trackingPausedDetail(
                  hours.start.text,
                  hours.end.text,
                ),
                TrackingIndicator.off => l.trackingOffDetail,
                _ => l.trackingOnDetail(hours.start.text, hours.end.text),
              },
            if (s.scanningSiteId != null) l.trackingBadgesDetail,
            l.trackingQueued(s.queued),
            if (s.mockedFixes > 0) l.trackingMocked(s.mockedFixes),
          ];
          return SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 16),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    l.trackingConsentTitle,
                    style: Theme.of(context).textTheme.titleLarge,
                  ),
                  const SizedBox(height: 8),
                  for (final line in lines)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 6),
                      child: Text(
                        line,
                        style: const TextStyle(
                          fontSize: 15,
                          height: 1.4,
                          color: PColors.ink700,
                        ),
                      ),
                    ),
                  const SizedBox(height: 8),
                  OutlinedButton(
                    key: const Key('trackingReadNotice'),
                    onPressed: () {
                      final router = GoRouter.of(context);
                      Navigator.pop(context);
                      router.push(trackingConsentRoute);
                    },
                    child: Text(l.trackingReadNotice),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
