import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';

import '../core/dates.dart';
import '../data/local/outbox.dart';
import '../data/models/models.dart';
import '../l10n/gen/app_localizations.dart';
import '../services/sync_service.dart';
import '../state/providers.dart';
import '../theme/tokens.dart';

extension L10nX on BuildContext {
  AppLocalizations get l10n => AppLocalizations.of(this);
  String get lang => Localizations.localeOf(this).languageCode;
}

String statusWord(AppLocalizations l, AttendanceStatus s) => switch (s) {
  AttendanceStatus.present => l.statusPresent,
  AttendanceStatus.absent => l.statusAbsent,
  AttendanceStatus.halfDay => l.statusHalfDay,
  AttendanceStatus.late => l.statusLate,
};

String initials(String name) {
  final parts = name.trim().split(RegExp(r'\s+')).where((p) => p.isNotEmpty);
  return parts.take(2).map((p) => p[0].toUpperCase()).join();
}

/// "3 500 XOF"; French uses a decimal comma.
String money(BuildContext context, int minor, Currency currency) => formatMoney(
  minor,
  currency,
  decimalSeparator: context.lang == 'fr' ? ',' : '.',
);

String _cap(String s) => s.isEmpty ? s : s[0].toUpperCase() + s.substring(1);

/// "Friday 31 October".
String longDate(BuildContext context, String date) =>
    _cap(DateFormat('EEEE d MMMM', context.lang).format(parseIsoDate(date)));

/// "Fri 31 Oct".
String shortDate(BuildContext context, String date) =>
    DateFormat('EEE d MMM', context.lang).format(parseIsoDate(date));

/// "Thu 30".
String weekdayDay(BuildContext context, String date) =>
    _cap(DateFormat('EEE d', context.lang).format(parseIsoDate(date)));

/// `HH:MM` on the site's clock for an ISO timestamp.
String timeOf(WidgetRef ref, String? iso) {
  if (iso == null) return '';
  final t = DateTime.tryParse(iso);
  if (t == null) return '';
  return hm(ref.read(siteClockProvider).localOf(t));
}

/// "Polaris" + small caps "WORKFORCE".
class Lockup extends StatelessWidget {
  const Lockup({super.key, this.onDark = true});
  final bool onDark;

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 36,
          height: 36,
          decoration: BoxDecoration(
            color: PColors.amber,
            borderRadius: BorderRadius.circular(9),
          ),
          child: const Icon(Icons.foundation, color: PColors.ink, size: 20),
        ),
        const SizedBox(width: 10),
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              l.appName,
              style: TextStyle(
                fontWeight: FontWeight.w800,
                fontSize: 20,
                height: 1.1,
                color: onDark ? PColors.white : PColors.ink,
              ),
            ),
            Text(
              l.appSub.toUpperCase(),
              style: TextStyle(
                fontSize: 12,
                fontWeight: FontWeight.w600,
                letterSpacing: 0.96,
                height: 1.1,
                color: onDark ? PColors.onInkMuted : PColors.steel,
              ),
            ),
          ],
        ),
      ],
    );
  }
}

class Pill extends StatelessWidget {
  const Pill(this.text, {super.key, this.colors = StatusColors.neutral});
  final String text;
  final StatusColors colors;

  @override
  Widget build(BuildContext context) => Container(
    height: 26,
    padding: const EdgeInsets.symmetric(horizontal: 10),
    alignment: Alignment.center,
    decoration: BoxDecoration(
      color: colors.bg,
      borderRadius: BorderRadius.circular(999),
    ),
    child: Text(
      text,
      style: TextStyle(
        color: colors.fg,
        fontSize: 13,
        fontWeight: FontWeight.w600,
      ),
    ),
  );
}

class InitialsAvatar extends StatelessWidget {
  const InitialsAvatar(
    this.name, {
    super.key,
    this.size = 44,
    this.dark = false,
    this.fontSize,
  });
  final String name;
  final double size;
  final bool dark;
  final double? fontSize;

  @override
  Widget build(BuildContext context) => Container(
    width: size,
    height: size,
    alignment: Alignment.center,
    decoration: BoxDecoration(
      color: dark ? PColors.inkAvatar : PColors.line,
      shape: BoxShape.circle,
    ),
    child: Text(
      initials(name),
      style: TextStyle(
        fontWeight: FontWeight.w700,
        fontSize: fontSize ?? size * 0.34,
        color: dark ? PColors.white : PColors.ink,
      ),
    ),
  );
}

/// Small uppercase heading ("THIS WEEK").
class SectionLabel extends StatelessWidget {
  const SectionLabel(this.text, {super.key, this.color = PColors.steel});
  final String text;
  final Color color;

  @override
  Widget build(BuildContext context) => Text(
    text.toUpperCase(),
    style: TextStyle(
      fontSize: 13,
      fontWeight: FontWeight.w700,
      letterSpacing: 0.78,
      color: color,
    ),
  );
}

/// One of the four status buttons: always the letter and the word.
class StatusButton extends StatelessWidget {
  const StatusButton({
    super.key,
    required this.status,
    required this.selected,
    required this.onTap,
  });

  final AttendanceStatus status;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final c = StatusColors.of(status);
    final word = statusWord(context.l10n, status);
    return Semantics(
      button: true,
      selected: selected,
      enabled: onTap != null,
      label: '${status.letter} $word',
      child: Material(
        color: selected ? c.bg : PColors.white,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(10),
          side: BorderSide(
            color: selected ? c.fg : PColors.border,
            width: selected ? 2 : 1,
          ),
        ),
        child: InkWell(
          borderRadius: BorderRadius.circular(10),
          onTap: onTap,
          child: ExcludeSemantics(
            child: SizedBox(
              height: PSizes.statusButton,
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 2),
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Text(
                      status.letter,
                      style: TextStyle(
                        fontSize: 16,
                        height: 1.1,
                        fontWeight: FontWeight.w700,
                        color: selected ? c.fg : PColors.ink700,
                      ),
                    ),
                    FittedBox(
                      fit: BoxFit.scaleDown,
                      child: Text(
                        word,
                        maxLines: 1,
                        style: TextStyle(
                          fontSize: 11,
                          fontWeight: FontWeight.w600,
                          color: selected ? c.fg : PColors.ink700,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Square letter badge of the review list.
class StatusBadge extends StatelessWidget {
  const StatusBadge(this.status, {super.key});
  final AttendanceStatus status;

  @override
  Widget build(BuildContext context) {
    final c = StatusColors.of(status);
    return Container(
      width: 34,
      height: 34,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        color: c.bg,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(
        status.letter,
        style: TextStyle(
          color: c.fg,
          fontWeight: FontWeight.w800,
          fontSize: 16,
        ),
      ),
    );
  }
}

/// White header with a back button, title and subtitle.
class BackHeader extends StatelessWidget {
  const BackHeader({
    super.key,
    required this.title,
    required this.subtitle,
    required this.onBack,
    this.trailing,
    this.dark = false,
    this.bottom,
  });

  final String title;
  final String subtitle;
  final VoidCallback onBack;
  final Widget? trailing;
  final bool dark;
  final Widget? bottom;

  @override
  Widget build(BuildContext context) {
    final fg = dark ? PColors.white : PColors.ink;
    return Container(
      decoration: BoxDecoration(
        color: dark ? PColors.ink : PColors.white,
        border: dark
            ? null
            : const Border(bottom: BorderSide(color: PColors.line)),
      ),
      padding: EdgeInsets.fromLTRB(
        8,
        MediaQuery.paddingOf(context).top + 12,
        16,
        12,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              SizedBox(
                width: 44,
                height: 44,
                child: IconButton(
                  tooltip: MaterialLocalizations.of(context).backButtonTooltip,
                  onPressed: onBack,
                  icon: Icon(Icons.chevron_left, color: fg, size: 28),
                ),
              ),
              const SizedBox(width: 4),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: TextStyle(
                        fontWeight: FontWeight.w800,
                        fontSize: 20,
                        color: fg,
                      ),
                    ),
                    Text(
                      subtitle,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: 13,
                        color: dark ? PColors.onInkMuted : PColors.steel,
                      ),
                    ),
                  ],
                ),
              ),
              ?trailing,
            ],
          ),
          if (bottom != null)
            Padding(
              padding: const EdgeInsets.only(left: 8, top: 12),
              child: bottom,
            ),
        ],
      ),
    );
  }
}

/// Online / offline / sending / N waiting, as in the Today artboard.
class SyncBanner extends ConsumerWidget {
  const SyncBanner({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final l = context.l10n;
    final s = ref.watch(syncStatusProvider).valueOrNull ?? const SyncStatus();
    final mock = ref.watch(mockBackendProvider);
    final IconData icon;
    final String head;
    final String tail;
    if (s.online == false) {
      icon = Icons.wifi_off;
      head = l.bannerOffline;
      tail = l.bannerWorksNormally;
    } else if (s.syncing && s.waiting > 0) {
      icon = Icons.sync;
      head = l.bannerSending;
      tail = '';
    } else if (s.online == null) {
      icon = Icons.wifi_find;
      head = l.bannerChecking;
      tail = '';
    } else {
      icon = Icons.wifi;
      head = l.bannerOnline;
      tail = s.waiting == 0 ? l.bannerAllSent : '';
    }
    final banner = Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: PColors.inkRaised,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        children: [
          Row(
            children: [
              Icon(icon, color: PColors.amber, size: 18),
              const SizedBox(width: 10),
              Expanded(
                child: Text.rich(
                  TextSpan(
                    children: [
                      TextSpan(
                        text: head,
                        style: const TextStyle(fontWeight: FontWeight.w700),
                      ),
                      if (tail.isNotEmpty) TextSpan(text: ' · $tail'),
                    ],
                  ),
                  style: const TextStyle(color: PColors.white, fontSize: 14),
                ),
              ),
              Text(
                l.bannerWaiting(s.waiting),
                style: const TextStyle(color: PColors.onInkMuted, fontSize: 14),
              ),
            ],
          ),
          if (s.failed > 0)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Row(
                children: [
                  const Icon(
                    Icons.error_outline,
                    color: PColors.absentBg,
                    size: 18,
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      l.bannerFailed(s.failed),
                      style: const TextStyle(
                        color: PColors.absentBg,
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
    return Semantics(
      liveRegion: true,
      child: GestureDetector(
        onTap: s.failed > 0 ? () => showFailedSheet(context, ref) : null,
        // Mock builds: long-press toggles a simulated network outage.
        onLongPress: mock == null
            ? null
            : () {
                mock.offline = !mock.offline;
                ScaffoldMessenger.of(context).showSnackBar(
                  SnackBar(
                    content: Text(
                      mock.offline ? l.mockOfflineOn : l.mockOfflineOff,
                    ),
                  ),
                );
                ref.read(syncServiceProvider).kick();
              },
        child: banner,
      ),
    );
  }
}

/// Lists outbox entries the server refused; the foreman may discard them.
Future<void> showFailedSheet(BuildContext context, WidgetRef ref) {
  return showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    builder: (context) => Consumer(
      builder: (context, ref, _) {
        final l = context.l10n;
        final items = ref.watch(failedOutboxProvider).valueOrNull ?? const [];
        String label(OutboxEntry e) {
          final d = shortDate(context, e.date);
          return switch (e.kind) {
            OutboxKind.records => l.kindRecords(d),
            OutboxKind.submit => l.kindSubmit(d),
            OutboxKind.correction => l.kindCorrection(d),
            OutboxKind.payment => l.kindPayment(d),
          };
        }

        return SafeArea(
          child: ListView(
            shrinkWrap: true,
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
            children: [
              Text(
                l.failedTitle,
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 4),
              Text(l.failedHint, style: const TextStyle(color: PColors.steel)),
              const SizedBox(height: 8),
              for (final e in items)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text(label(e)),
                  subtitle: Text(
                    '${e.errorCode ?? ''} · ${e.errorMessage ?? ''}',
                  ),
                  trailing: TextButton(
                    onPressed: () =>
                        ref.read(localStoreProvider).deleteOutbox(e.id!),
                    child: Text(l.discard),
                  ),
                ),
            ],
          ),
        );
      },
    ),
  );
}

/// Dark rounded notice (offline explanation).
class InkNotice extends StatelessWidget {
  const InkNotice({
    super.key,
    required this.title,
    required this.body,
    this.icon = Icons.wifi_off,
  });
  final String title;
  final String body;
  final IconData icon;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
    decoration: BoxDecoration(
      color: PColors.ink,
      borderRadius: BorderRadius.circular(PSizes.radius),
    ),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, color: PColors.amber, size: 20),
        const SizedBox(width: 10),
        Expanded(
          child: Text.rich(
            TextSpan(
              children: [
                if (title.isNotEmpty)
                  TextSpan(
                    text: '$title ',
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                TextSpan(text: body),
              ],
            ),
            style: const TextStyle(
              color: PColors.white,
              fontSize: 14,
              height: 1.45,
            ),
          ),
        ),
      ],
    ),
  );
}

/// Bottom bar holding the screen's main action.
class BottomAction extends StatelessWidget {
  const BottomAction({super.key, required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Container(
    decoration: const BoxDecoration(
      color: PColors.white,
      border: Border(top: BorderSide(color: PColors.line)),
      boxShadow: [
        BoxShadow(
          color: Color(0x1416191D),
          blurRadius: 16,
          offset: Offset(0, -4),
        ),
      ],
    ),
    padding: EdgeInsets.fromLTRB(
      16,
      12,
      16,
      12 + MediaQuery.paddingOf(context).bottom,
    ),
    child: child,
  );
}
