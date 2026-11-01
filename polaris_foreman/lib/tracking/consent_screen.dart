import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../l10n/gen/app_localizations.dart';
import '../theme/tokens.dart';
import '../ui/widgets.dart';
import 'tracking_client.dart';
import 'tracking_models.dart';
import 'tracking_providers.dart';

/// Route of the consent notice.
const trackingConsentRoute = '/tracking/consent';

/// "Location tracking" notice (design §10 of `docs/presence-tracking.md`):
/// when, what, how long, who sees it, what for, and that refusing is fine.
/// Shown on first use (the server answers `consent_required`) and from the
/// Today menu after a refusal. Accept and Refuse weigh the same; the answer
/// goes through the outbox, so it works offline.
class TrackingConsentScreen extends ConsumerStatefulWidget {
  const TrackingConsentScreen({super.key});

  @override
  ConsumerState<TrackingConsentScreen> createState() =>
      _TrackingConsentScreenState();
}

/// "about every minute", "about every 30 seconds".
String trackingEvery(AppLocalizations l, int? seconds) {
  if (seconds == null) return l.trackingEveryGeneric;
  if (seconds >= 60 && seconds % 60 == 0) {
    return l.trackingEveryMinutes(seconds ~/ 60);
  }
  return l.trackingEverySeconds(seconds);
}

class _TrackingConsentScreenState extends ConsumerState<TrackingConsentScreen> {
  bool _busy = false;

  Future<void> _answer(bool accepted) async {
    setState(() => _busy = true);
    final messenger = ScaffoldMessenger.of(context);
    final saved = context.l10n.trackingConsentSaved;
    try {
      await ref
          .read(trackingProvider.notifier)
          .answerConsent(accepted: accepted);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    if (!mounted) return;
    messenger.showSnackBar(SnackBar(content: Text(saved)));
    if (context.canPop()) {
      context.pop();
    } else {
      context.go('/today');
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final s = ref.watch(trackingProvider);
    final ctx = s.consentContext;
    final hours = ctx.hours ?? s.token?.hours;
    final interval = ctx.gpsIntervalSeconds ?? s.token?.gpsIntervalSeconds;
    final days = ctx.retentionDays;
    final sections = [
      (
        Icons.schedule,
        l.trackingConsentWhenTitle,
        hours == null
            ? l.trackingConsentWhenGeneric
            : l.trackingConsentWhenHours(hours.start.text, hours.end.text),
      ),
      (
        Icons.place_outlined,
        l.trackingConsentWhatTitle,
        l.trackingConsentWhatText(trackingEvery(l, interval)),
      ),
      (
        Icons.delete_outline,
        l.trackingConsentKeepTitle,
        days == null
            ? l.trackingConsentKeepGeneric
            : l.trackingConsentKeepDays(days),
      ),
      (
        Icons.visibility_outlined,
        l.trackingConsentWhoTitle,
        l.trackingConsentWhoText,
      ),
      (
        Icons.fact_check_outlined,
        l.trackingConsentUseTitle,
        l.trackingConsentUseText,
      ),
    ];
    return Scaffold(
      backgroundColor: PColors.ground,
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            color: PColors.ink,
            padding: EdgeInsets.fromLTRB(
              20,
              MediaQuery.paddingOf(context).top + 20,
              20,
              20,
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Icon(Icons.my_location, color: PColors.amber, size: 28),
                const SizedBox(height: 10),
                Text(
                  l.trackingConsentTitle,
                  style: const TextStyle(
                    color: PColors.white,
                    fontWeight: FontWeight.w800,
                    fontSize: 22,
                  ),
                ),
                const SizedBox(height: 6),
                Text(
                  l.trackingConsentIntro,
                  style: const TextStyle(
                    color: PColors.onInkMuted,
                    fontSize: 15,
                    height: 1.4,
                  ),
                ),
              ],
            ),
          ),
          Expanded(
            child: ListView(
              key: const Key('consentText'),
              padding: const EdgeInsets.all(20),
              children: [
                for (final (icon, title, text) in sections) ...[
                  _Section(icon: icon, title: title, text: text),
                  const SizedBox(height: 12),
                ],
                Text(
                  l.trackingConsentChoice,
                  style: const TextStyle(
                    fontSize: 15,
                    height: 1.4,
                    fontWeight: FontWeight.w600,
                    color: PColors.ink,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  l.trackingConsentVersion(consentTextVersion),
                  style: const TextStyle(fontSize: 13, color: PColors.steel),
                ),
              ],
            ),
          ),
          BottomAction(
            child: Row(
              children: [
                Expanded(
                  child: OutlinedButton(
                    key: const Key('consentRefuse'),
                    onPressed: _busy ? null : () => _answer(false),
                    child: Text(l.trackingConsentRefuse),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: FilledButton(
                    key: const Key('consentAccept'),
                    onPressed: _busy ? null : () => _answer(true),
                    child: Text(l.trackingConsentAccept),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _Section extends StatelessWidget {
  const _Section({required this.icon, required this.title, required this.text});
  final IconData icon;
  final String title;
  final String text;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(14),
    decoration: BoxDecoration(
      color: PColors.white,
      border: Border.all(color: PColors.line),
      borderRadius: BorderRadius.circular(PSizes.radius),
    ),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, color: PColors.ink700, size: 22),
        const SizedBox(width: 12),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                style: const TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.w700,
                  color: PColors.ink,
                ),
              ),
              const SizedBox(height: 4),
              Text(
                text,
                style: const TextStyle(
                  fontSize: 15,
                  height: 1.4,
                  color: PColors.ink700,
                ),
              ),
            ],
          ),
        ),
      ],
    ),
  );
}

/// Whether the menu offers the notice: to answer it, to change a refusal,
/// or to withdraw an acceptance. Not when tracking is off by settings.
bool consentOpenable(TrackingState s) =>
    s.refused ||
    s.phase == TrackingPhase.active ||
    s.phase == TrackingPhase.consentRequired ||
    s.phase == TrackingPhase.consentPending;
