import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/models/models.dart';
import '../../l10n/gen/app_localizations.dart';
import '../../services/payout_service.dart';
import '../../state/providers.dart';
import '../../state/session.dart';
import '../../theme/tokens.dart';
import '../../tracking/consent_screen.dart';
import '../../tracking/tracking_indicator.dart';
import '../../tracking/tracking_providers.dart';
import '../widgets.dart';

/// Today (MobileHome artboard): dark header with date and site, sync banner,
/// the Roll call card, the Cash payout card and "This week".
class TodayScreen extends ConsumerWidget {
  const TodayScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final l = context.l10n;
    final me = ref.watch(meProvider);
    final site = ref.watch(currentSiteProvider);
    final today = ref.watch(todayProvider);
    final roll = ref.watch(rollCallViewProvider).valueOrNull;
    final payout = ref.watch(payoutViewProvider).valueOrNull;
    final week = ref.watch(weekProvider).valueOrNull ?? const [];

    return Scaffold(
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            color: PColors.ink,
            padding: EdgeInsets.fromLTRB(
              20,
              MediaQuery.paddingOf(context).top + 16,
              20,
              20,
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: InkWell(
                        key: const Key('todaySite'),
                        onTap: (me?.sites.length ?? 0) > 1
                            ? () => _chooseSite(context, ref, me!.sites)
                            : null,
                        // Mock builds only: try kiosk mode without the office.
                        onLongPress:
                            ref.watch(mockBackendProvider) == null ||
                                site == null
                            ? null
                            : () => _demoKiosk(context, ref, site),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              longDate(context, today),
                              style: const TextStyle(
                                color: PColors.onInkMuted,
                                fontSize: 13,
                              ),
                            ),
                            Row(
                              children: [
                                Flexible(
                                  child: Text(
                                    site?.name ?? '',
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(
                                      color: PColors.white,
                                      fontWeight: FontWeight.w800,
                                      fontSize: 22,
                                    ),
                                  ),
                                ),
                                if ((me?.sites.length ?? 0) > 1)
                                  const Icon(
                                    Icons.expand_more,
                                    color: PColors.amber,
                                  ),
                              ],
                            ),
                          ],
                        ),
                      ),
                    ),
                    PopupMenuButton<String>(
                      tooltip: me?.name,
                      onSelected: (v) async {
                        if (v == 'lang') {
                          ref.read(localeProvider.notifier).toggle();
                        } else if (v == 'tracking') {
                          context.push(trackingConsentRoute);
                        } else if (v == 'out') {
                          await _confirmSignOut(context, ref);
                        }
                      },
                      itemBuilder: (_) => [
                        PopupMenuItem(
                          value: 'lang',
                          child: Text(l.switchLanguage),
                        ),
                        if (consentOpenable(ref.read(trackingProvider)))
                          PopupMenuItem(
                            value: 'tracking',
                            child: Text(l.trackingMenu),
                          ),
                        PopupMenuItem(value: 'out', child: Text(l.signOut)),
                      ],
                      child: InitialsAvatar(
                        me?.name ?? '',
                        size: 40,
                        dark: true,
                        fontSize: 14,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 14),
                const SyncBanner(),
                const TrackingIndicatorPill(),
              ],
            ),
          ),
          Expanded(
            child: site == null
                ? Center(
                    child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Text(l.noSite, textAlign: TextAlign.center),
                    ),
                  )
                : RefreshIndicator(
                    onRefresh: () => ref.read(syncServiceProvider).runOnce(),
                    child: ListView(
                      padding: const EdgeInsets.all(20),
                      children: [
                        _RollCallCard(site: site, view: roll),
                        const SizedBox(height: 16),
                        _PayoutCard(roll: roll, payout: payout),
                        const SizedBox(height: 16),
                        SectionLabel(l.thisWeek),
                        const SizedBox(height: 8),
                        for (final d in week) _WeekRow(day: d),
                      ],
                    ),
                  ),
          ),
        ],
      ),
    );
  }

  /// Mock builds: what the office does on the Kiosk phones page — make this
  /// phone the kiosk of [site]. The router then opens the kiosk screen; a
  /// long press on the kiosk's site name ends it again.
  Future<void> _demoKiosk(
    BuildContext context,
    WidgetRef ref,
    AssignedSite site,
  ) async {
    final mock = ref.read(mockBackendProvider);
    if (mock == null) return;
    final messenger = ScaffoldMessenger.of(context);
    final text = context.l10n.mockKioskOn(site.name);
    mock.setKiosk(site.id);
    await ref.read(sessionProvider.notifier).refreshDeviceRole();
    messenger.showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _chooseSite(
    BuildContext context,
    WidgetRef ref,
    List<AssignedSite> sites,
  ) async {
    final picked = await showModalBottomSheet<AssignedSite>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              context.l10n.chooseSite,
              style: Theme.of(context).textTheme.titleLarge,
            ),
            for (final s in sites)
              ListTile(
                minTileHeight: 56,
                title: Text(s.name),
                subtitle: Text(s.address),
                onTap: () => Navigator.pop(context, s),
              ),
          ],
        ),
      ),
    );
    if (picked != null) {
      ref.read(currentSiteProvider.notifier).select(picked);
      ref.read(syncServiceProvider).kick();
    }
  }

  Future<void> _confirmSignOut(BuildContext context, WidgetRef ref) async {
    final l = context.l10n;
    final waiting = ref.read(syncServiceProvider).status.waiting;
    if (waiting > 0) {
      final ok = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          content: Text(l.signOutWaiting(waiting)),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: Text(l.cancel),
            ),
            TextButton(
              onPressed: () => Navigator.pop(context, true),
              child: Text(l.signOut),
            ),
          ],
        ),
      );
      if (ok != true) return;
    }
    await ref.read(sessionProvider.notifier).signOut();
  }
}

(String, StatusColors) dayPill(
  AppLocalizations l,
  DayStatus status, {
  int marked = 0,
  int total = 0,
}) => switch (status) {
  DayStatus.notStarted => (l.dayNotStarted, StatusColors.neutral),
  DayStatus.draft => (l.dayDraft(marked, total), StatusColors.half),
  DayStatus.submitted => (l.daySubmitted, StatusColors.late),
  DayStatus.approved => (l.dayApproved, StatusColors.present),
  DayStatus.rejected => (l.dayRejected, StatusColors.absent),
  DayStatus.locked => (l.dayLocked, StatusColors.dark),
};

class _RollCallCard extends StatelessWidget {
  const _RollCallCard({required this.site, required this.view});
  final AssignedSite site;
  final RollCallView? view;

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final v = view;
    final status = v?.status ?? DayStatus.notStarted;
    final crew = v?.crew.length ?? 0;
    final marked = v?.marked ?? 0;
    // Marks saved on the phone but not sent yet still mean "in progress".
    final shown = status == DayStatus.notStarted && marked > 0
        ? DayStatus.draft
        : status;
    final (pill, colors) = dayPill(l, shown, marked: marked, total: crew);
    final label = switch (shown) {
      DayStatus.notStarted => l.startRollCall,
      DayStatus.draft => l.continueRollCall,
      DayStatus.rejected => l.fixRollCall,
      _ => l.viewRollCall,
    };
    final primary = shown.editable;
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: PColors.white,
        border: Border.all(
          color: primary ? PColors.ink : PColors.line,
          width: primary ? 2 : 1,
        ),
        borderRadius: BorderRadius.circular(PSizes.cardRadius),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: SectionLabel(
                  site.rollCallDue == null
                      ? l.morning
                      : l.morningDue(site.rollCallDue!),
                  color: PColors.amberText,
                ),
              ),
              Pill(pill, colors: colors),
            ],
          ),
          const SizedBox(height: 12),
          Text(l.rollCall, style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 4),
          Text(
            l.crewCount(crew),
            style: const TextStyle(fontSize: 16, color: PColors.ink700),
          ),
          if (shown == DayStatus.rejected &&
              (v?.day?.rejectionNote.isNotEmpty ?? false))
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                l.sentBack(v!.day!.rejectionNote),
                style: const TextStyle(color: PColors.absentFg, fontSize: 15),
              ),
            ),
          const SizedBox(height: 12),
          primary
              ? FilledButton(
                  key: const Key('openRollCall'),
                  onPressed: () => context.push('/roll-call'),
                  child: Text(label),
                )
              : OutlinedButton(
                  key: const Key('openRollCall'),
                  onPressed: () => context.push('/roll-call'),
                  child: Text(label),
                ),
        ],
      ),
    );
  }
}

class _PayoutCard extends StatelessWidget {
  const _PayoutCard({required this.roll, required this.payout});
  final RollCallView? roll;
  final PayoutView? payout;

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final p = payout;
    final status =
        p?.sheet.status ?? roll?.day?.payoutStatus ?? PayoutStatus.notOpen;
    final (pill, colors) = switch (status) {
      PayoutStatus.notOpen => (l.payoutAfterApproval, StatusColors.neutral),
      PayoutStatus.open => (l.payoutOpen, StatusColors.half),
      PayoutStatus.paid => (l.payoutPaidPill, StatusColors.present),
      PayoutStatus.closed => (l.payoutClosed, StatusColors.dark),
    };
    final open = status == PayoutStatus.open;
    final text = open && p != null
        ? l.payoutOpenText(
            p.sheet.lineCount - p.sheet.paidCount,
            money(context, p.sheet.leftToPay, p.currency),
          )
        : status == PayoutStatus.notOpen
        ? l.payoutOpensText
        : '';
    final label = switch (status) {
      PayoutStatus.notOpen => l.previewPayout,
      PayoutStatus.open => l.startPayout,
      _ => l.viewPayout,
    };
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: PColors.white,
        border: Border.all(
          color: open ? PColors.ink : PColors.line,
          width: open ? 2 : 1,
        ),
        borderRadius: BorderRadius.circular(PSizes.cardRadius),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(child: SectionLabel(l.evening)),
              Pill(pill, colors: colors),
            ],
          ),
          const SizedBox(height: 10),
          Text(l.cashPayout, style: Theme.of(context).textTheme.titleLarge),
          if (text.isNotEmpty) ...[
            const SizedBox(height: 4),
            Text(
              text,
              style: const TextStyle(fontSize: 15, color: PColors.ink700),
            ),
          ],
          const SizedBox(height: 10),
          open
              ? FilledButton(
                  key: const Key('openPayout'),
                  onPressed: () => context.push('/payout'),
                  child: Text(label),
                )
              : OutlinedButton(
                  key: const Key('openPayout'),
                  onPressed: () => context.push('/payout'),
                  child: Text(label),
                ),
        ],
      ),
    );
  }
}

class _WeekRow extends StatelessWidget {
  const _WeekRow({required this.day});
  final AttendanceDaySummary day;

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final c = day.counts;
    final (text, colors) = day.payoutStatus == PayoutStatus.paid
        ? (l.dayPaid, StatusColors.present)
        : dayPill(
            l,
            day.status,
            marked: c.expected - c.unmarked,
            total: c.expected,
          );
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: PColors.white,
        border: Border.all(color: PColors.line),
        borderRadius: BorderRadius.circular(PSizes.radius),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              weekdayDay(context, day.date),
              style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
            ),
          ),
          Text(
            '${c.onSite} / ${c.expected}',
            style: const TextStyle(
              fontSize: 15,
              color: PColors.ink700,
              fontFeatures: [FontFeature.tabularFigures()],
            ),
          ),
          const SizedBox(width: 12),
          Pill(text, colors: colors),
        ],
      ),
    );
  }
}
