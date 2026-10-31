import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/models/models.dart';
import '../../services/roll_call_service.dart';
import '../../state/providers.dart';
import '../../state/session.dart';
import '../../theme/tokens.dart';
import '../widgets.dart';

/// Review & submit (MobileSubmit artboard).
class ReviewScreen extends ConsumerStatefulWidget {
  const ReviewScreen({super.key});

  @override
  ConsumerState<ReviewScreen> createState() => _ReviewScreenState();
}

class _ReviewScreenState extends ConsumerState<ReviewScreen> {
  TextEditingController? _note;
  bool _busy = false;

  @override
  void dispose() {
    _note?.dispose();
    super.dispose();
  }

  Future<void> _submit(RollCallView v) async {
    setState(() => _busy = true);
    final session = ref.read(sessionProvider);
    try {
      await ref
          .read(rollCallServiceProvider)
          .submit(
            siteId: v.site.id,
            date: v.date,
            crewWorkerIds: v.crew.map((w) => w.id),
            foremanNote: _note?.text.trim() ?? '',
            foreman: session is SignedIn
                ? PersonRef(id: session.me.id, name: session.me.name)
                : null,
          );
    } on UnmarkedWorkersException catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(context.l10n.notMarkedWarning(e.workerIds.length)),
          ),
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final v = ref.watch(rollCallViewProvider).valueOrNull;
    final sync = ref.watch(syncStatusProvider).valueOrNull;
    if (v == null) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    _note ??= TextEditingController(text: v.day?.foremanNote ?? '');
    final counts = v.counts;
    final submitted = !v.editable;
    final offline = sync?.online == false;
    final manager = v.site.manager?.name ?? l.theSiteManager;
    final unmarked = v.unmarked.length;
    final canSubmit = !submitted && unmarked == 0 && v.crew.isNotEmpty;

    return Scaffold(
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          BackHeader(
            title: l.reviewTitle,
            subtitle:
                '${v.site.name.split(' ').first} · ${shortDate(context, v.date)}',
            onBack: () => context.pop(),
          ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Row(
                  children: [
                    _CountTile(AttendanceStatus.present, counts.present),
                    const SizedBox(width: 8),
                    _CountTile(AttendanceStatus.halfDay, counts.halfDay),
                    const SizedBox(width: 8),
                    _CountTile(AttendanceStatus.late, counts.late),
                    const SizedBox(width: 8),
                    _CountTile(AttendanceStatus.absent, counts.absent),
                  ],
                ),
                const SizedBox(height: 14),
                _Exceptions(view: v),
                const SizedBox(height: 14),
                Text.rich(
                  TextSpan(
                    children: [
                      TextSpan(text: '${l.noteLabel} '),
                      TextSpan(
                        text: l.optional,
                        style: const TextStyle(
                          fontWeight: FontWeight.w400,
                          color: PColors.steel,
                        ),
                      ),
                    ],
                  ),
                  style: const TextStyle(
                    fontSize: 14,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 6),
                TextField(
                  key: const Key('note'),
                  controller: _note,
                  enabled: !submitted,
                  minLines: 2,
                  maxLines: 4,
                  maxLength: 1000,
                  style: const TextStyle(fontSize: 16),
                ),
                if (unmarked > 0 && !submitted) ...[
                  const SizedBox(height: 8),
                  Text(
                    l.notMarkedWarning(unmarked),
                    key: const Key('unmarkedWarning'),
                    style: const TextStyle(
                      color: PColors.absentFg,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ],
                if (offline && !submitted) ...[
                  const SizedBox(height: 14),
                  InkNotice(
                    title: l.offlineSubmitTitle,
                    body: l.offlineSubmitBody,
                  ),
                ],
                if (submitted) ...[
                  const SizedBox(height: 14),
                  Container(
                    key: const Key('submittedPanel'),
                    padding: const EdgeInsets.all(14),
                    decoration: BoxDecoration(
                      color: PColors.presentBg,
                      borderRadius: BorderRadius.circular(PSizes.radius),
                    ),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Icon(Icons.check, color: PColors.presentFg),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Text.rich(
                            TextSpan(
                              children: [
                                TextSpan(
                                  text:
                                      '${l.submittedAt(timeOf(ref, v.day?.submittedAt))} ',
                                  style: const TextStyle(
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                                TextSpan(
                                  text: v.submitPending
                                      ? l.submittedWaiting(manager)
                                      : l.submittedSent(manager),
                                ),
                              ],
                            ),
                            style: const TextStyle(
                              color: PColors.presentFg,
                              fontSize: 15,
                              height: 1.45,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ],
            ),
          ),
          BottomAction(
            child: submitted
                ? OutlinedButton(
                    key: const Key('backToToday'),
                    onPressed: () => context.go('/today'),
                    style: OutlinedButton.styleFrom(
                      minimumSize: const Size.fromHeight(PSizes.primaryButton),
                    ),
                    child: Text(l.backToToday),
                  )
                : FilledButton(
                    key: const Key('submit'),
                    onPressed: canSubmit && !_busy ? () => _submit(v) : null,
                    child: Text(l.submitCount(v.crew.length)),
                  ),
          ),
        ],
      ),
    );
  }
}

class _CountTile extends StatelessWidget {
  const _CountTile(this.status, this.count);
  final AttendanceStatus status;
  final int count;

  @override
  Widget build(BuildContext context) {
    final c = StatusColors.of(status);
    return Expanded(
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 12),
        decoration: BoxDecoration(
          color: c.bg,
          borderRadius: BorderRadius.circular(PSizes.radius),
        ),
        child: Column(
          children: [
            Text(
              '$count',
              style: TextStyle(
                color: c.fg,
                fontSize: 26,
                fontWeight: FontWeight.w800,
              ),
            ),
            FittedBox(
              fit: BoxFit.scaleDown,
              child: Text(
                '${status.letter} ${statusWord(context.l10n, status)}',
                style: TextStyle(
                  color: c.fg,
                  fontSize: 12,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Exceptions extends StatelessWidget {
  const _Exceptions({required this.view});
  final RollCallView view;

  static const _maxRows = 5;

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final v = view;
    final half = <CrewWorker>[], late = <CrewWorker>[], absent = <CrewWorker>[];
    for (final w in v.crew) {
      switch (v.records[w.id]?.status) {
        case AttendanceStatus.halfDay:
          half.add(w);
        case AttendanceStatus.late:
          late.add(w);
        case AttendanceStatus.absent:
          absent.add(w);
        case AttendanceStatus.present:
        case null:
          break;
      }
    }
    // Long absences first: they are the ones the site manager asks about.
    absent.sort((a, b) => b.absentStreak.compareTo(a.absentStreak));

    final rows = <Widget>[];
    void row(AttendanceStatus s, String name, String note) =>
        rows.add(_ExceptionRow(status: s, name: name, note: note));
    for (final w in half) {
      final r = v.records[w.id]!;
      row(
        AttendanceStatus.halfDay,
        w.name,
        r.arrival != null && r.departure != null
            ? l.halfDayTimes(r.arrival!, r.departure!)
            : l.statusHalfDay,
      );
    }
    for (final w in late) {
      final r = v.records[w.id]!;
      row(
        AttendanceStatus.late,
        w.name,
        r.arrival == null ? l.statusLate : l.lateArrived(r.arrival!),
      );
    }
    final room = (_maxRows - rows.length).clamp(1, absent.length + 1);
    final single = absent.length <= room ? absent : absent.take(room - 1);
    for (final w in single) {
      row(
        AttendanceStatus.absent,
        w.name,
        w.absentStreak > 0 ? l.absentStreak(w.absentStreak + 1) : l.absentToday,
      );
    }
    final rest = absent.skip(single.length).toList();
    if (rest.isNotEmpty) {
      row(
        AttendanceStatus.absent,
        l.moreAbsent(rest.length),
        rest.map((w) => w.name).join(', '),
      );
    }

    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: PColors.white,
        border: Border.all(color: PColors.line),
        borderRadius: BorderRadius.circular(PSizes.radius),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SectionLabel(l.exceptionsTitle),
          const SizedBox(height: 4),
          if (rows.isEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(l.noExceptions),
            ),
          ...rows,
        ],
      ),
    );
  }
}

class _ExceptionRow extends StatelessWidget {
  const _ExceptionRow({
    required this.status,
    required this.name,
    required this.note,
  });
  final AttendanceStatus status;
  final String name;
  final String note;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(vertical: 8),
    decoration: const BoxDecoration(
      border: Border(top: BorderSide(color: PColors.neutralPill)),
    ),
    child: Row(
      children: [
        StatusBadge(status),
        const SizedBox(width: 10),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                name,
                style: const TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                ),
              ),
              Text(
                note,
                style: const TextStyle(fontSize: 13, color: PColors.steel),
              ),
            ],
          ),
        ),
        TextButton(
          onPressed: () => context.pop(),
          child: Text(context.l10n.change),
        ),
      ],
    ),
  );
}
