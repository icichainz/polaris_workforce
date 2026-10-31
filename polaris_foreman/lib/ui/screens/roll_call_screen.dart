import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/local/local_store.dart';
import '../../data/models/models.dart';
import '../../state/providers.dart';
import '../../theme/tokens.dart';
import '../widgets.dart';

/// Asks why a submitted record changes. Returns the reason, or null.
Future<String?> askReason(BuildContext context) {
  final l = context.l10n;
  final controller = TextEditingController();
  return showDialog<String>(
    context: context,
    builder: (context) => AlertDialog(
      title: Text(l.reasonTitle),
      content: TextField(
        key: const Key('reason'),
        controller: controller,
        autofocus: true,
        maxLength: 500,
        decoration: InputDecoration(hintText: l.reasonHint),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(l.cancel),
        ),
        TextButton(
          onPressed: () {
            final v = controller.text.trim();
            if (v.isNotEmpty) Navigator.pop(context, v);
          },
          child: Text(l.save),
        ),
      ],
    ),
  );
}

/// Roll call (MobileRollCall artboard).
class RollCallScreen extends ConsumerStatefulWidget {
  const RollCallScreen({super.key});

  @override
  ConsumerState<RollCallScreen> createState() => _RollCallScreenState();
}

class _RollCallScreenState extends ConsumerState<RollCallScreen> {
  bool _allTab = false;

  /// Workers listed under "To mark": unmarked when the tab was opened, so a
  /// row does not jump away the moment it is marked.
  Set<int>? _toMark;

  void _openToMark(RollCallView v) {
    setState(() {
      _allTab = false;
      _toMark = v.unmarked.toSet();
    });
  }

  Future<void> _pick(
    RollCallView v,
    CrewWorker w,
    AttendanceStatus status,
  ) async {
    final service = ref.read(rollCallServiceProvider);
    if (v.editable) {
      await service.mark(
        siteId: v.site.id,
        date: v.date,
        workerId: w.id,
        status: status,
        workStart: v.site.workStart,
      );
    } else if (v.correctable) {
      final current = v.records[w.id];
      if (current == null || current.status == status) return;
      final reason = await askReason(context);
      if (reason == null) return;
      await service.correct(
        siteId: v.site.id,
        date: v.date,
        workerId: w.id,
        field: CorrectableField.status,
        newValue: status.wire,
        reason: reason,
      );
    }
  }

  Future<void> _editTime(RollCallView v, LocalRecord r, bool arrival) async {
    final current = arrival ? r.arrival : r.departure;
    final parts = (current ?? v.site.workStart ?? '07:00').split(':');
    final picked = await showTimePicker(
      context: context,
      initialTime: TimeOfDay(
        hour: int.parse(parts[0]),
        minute: int.parse(parts[1]),
      ),
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(context).copyWith(alwaysUse24HourFormat: true),
        child: child!,
      ),
    );
    if (picked == null || !mounted) return;
    final value =
        '${picked.hour.toString().padLeft(2, '0')}:'
        '${picked.minute.toString().padLeft(2, '0')}';
    final service = ref.read(rollCallServiceProvider);
    if (v.editable) {
      await service.setTimes(
        siteId: v.site.id,
        date: v.date,
        workerId: r.workerId,
        arrival: arrival ? () => value : null,
        departure: arrival ? null : () => value,
      );
    } else if (v.correctable) {
      final reason = await askReason(context);
      if (reason == null) return;
      await service.correct(
        siteId: v.site.id,
        date: v.date,
        workerId: r.workerId,
        field: arrival ? CorrectableField.arrival : CorrectableField.departure,
        newValue: value,
        reason: reason,
      );
    }
  }

  Future<void> _allPresent(RollCallView v) async {
    final ids = v.unmarked;
    if (ids.isEmpty) return;
    await ref
        .read(rollCallServiceProvider)
        .markMany(
          siteId: v.site.id,
          date: v.date,
          marks: {for (final id in ids) id: AttendanceStatus.present},
          workStart: v.site.workStart,
        );
  }

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final v = ref.watch(rollCallViewProvider).valueOrNull;
    if (v == null) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    // Snapshot once the crew is known (it may arrive after the screen opens).
    if (_toMark == null && v.crew.isNotEmpty) _toMark = v.unmarked.toSet();
    final toMark = _toMark ?? const <int>{};
    final total = v.crew.length;
    final marked = v.marked;
    final left = total - marked;
    final shown = _allTab
        ? v.crew
        : v.crew.where((w) => toMark.contains(w.id)).toList();
    final allDone = left == 0 && total > 0;

    return Scaffold(
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          BackHeader(
            title: l.rollCall,
            subtitle: l.rollCallSubtitle(
              v.site.name.split(' ').first,
              shortDate(context, v.date),
            ),
            onBack: () => context.pop(),
            trailing: Text(
              '$marked/$total',
              key: const Key('progressCount'),
              style: const TextStyle(
                fontWeight: FontWeight.w800,
                fontSize: 18,
                fontFeatures: [FontFeature.tabularFigures()],
              ),
            ),
            bottom: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                ClipRRect(
                  borderRadius: BorderRadius.circular(999),
                  child: LinearProgressIndicator(
                    value: total == 0 ? 0 : marked / total,
                    minHeight: 6,
                    backgroundColor: PColors.neutralPill,
                    color: PColors.presentFg,
                  ),
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: _Tabs(
                        allTab: _allTab,
                        toMarkLabel: l.tabToMark(left),
                        allLabel: l.tabAll(total),
                        onToMark: () => _openToMark(v),
                        onAll: () => setState(() => _allTab = true),
                      ),
                    ),
                    if (v.editable) ...[
                      const SizedBox(width: 8),
                      Tooltip(
                        message: l.allPresentHint,
                        child: OutlinedButton(
                          key: const Key('allPresent'),
                          onPressed: left == 0 ? null : () => _allPresent(v),
                          style: OutlinedButton.styleFrom(
                            minimumSize: const Size(64, 44),
                            foregroundColor: PColors.presentFg,
                            side: const BorderSide(
                              color: PColors.presentFg,
                              width: 2,
                            ),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(10),
                            ),
                            textStyle: const TextStyle(
                              fontSize: 14,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          child: Text(l.allPresent),
                        ),
                      ),
                    ],
                  ],
                ),
              ],
            ),
          ),
          if (!v.editable)
            Container(
              color: v.status == DayStatus.locked
                  ? PColors.ink
                  : PColors.lateBg,
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
              child: Text(
                v.status == DayStatus.locked
                    ? l.lockedReadOnly
                    : l.submittedReadOnly,
                style: TextStyle(
                  fontWeight: FontWeight.w600,
                  color: v.status == DayStatus.locked
                      ? PColors.white
                      : PColors.lateFg,
                ),
              ),
            ),
          Expanded(
            child: v.crew.isEmpty
                ? Center(
                    child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Text(l.noCrew, textAlign: TextAlign.center),
                    ),
                  )
                : shown.isEmpty
                ? Center(child: Text(l.everyoneMarked))
                : ListView.separated(
                    padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
                    itemCount: shown.length,
                    separatorBuilder: (_, _) => const SizedBox(height: 10),
                    itemBuilder: (context, i) {
                      final w = shown[i];
                      final r = v.records[w.id];
                      return WorkerCard(
                        key: ValueKey('worker-${w.id}'),
                        worker: w,
                        record: r,
                        enabled: v.editable || v.correctable,
                        onPick: (s) => _pick(v, w, s),
                        onTimeTap: r == null || !(v.editable || v.correctable)
                            ? null
                            : (arrival) => _editTime(v, r, arrival),
                      );
                    },
                  ),
          ),
          BottomAction(
            child: FilledButton(
              key: const Key('toReview'),
              onPressed: () => context.push('/review'),
              style: allDone || !v.editable
                  ? null
                  : FilledButton.styleFrom(
                      backgroundColor: PColors.ink,
                      foregroundColor: PColors.white,
                    ),
              child: Text(
                allDone || !v.editable
                    ? l.reviewSubmit
                    : l.reviewNotMarked(left),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _Tabs extends StatelessWidget {
  const _Tabs({
    required this.allTab,
    required this.toMarkLabel,
    required this.allLabel,
    required this.onToMark,
    required this.onAll,
  });

  final bool allTab;
  final String toMarkLabel;
  final String allLabel;
  final VoidCallback onToMark;
  final VoidCallback onAll;

  Widget _tab(String label, bool selected, VoidCallback onTap, Key key) =>
      Expanded(
        child: Semantics(
          selected: selected,
          button: true,
          child: Material(
            color: selected ? PColors.white : Colors.transparent,
            borderRadius: BorderRadius.circular(7),
            child: InkWell(
              key: key,
              borderRadius: BorderRadius.circular(7),
              onTap: onTap,
              child: SizedBox(
                height: 36,
                child: Center(
                  child: FittedBox(
                    fit: BoxFit.scaleDown,
                    child: Text(
                      label,
                      style: TextStyle(
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                        color: selected ? PColors.ink : PColors.ink700,
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      );

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(4),
    decoration: BoxDecoration(
      color: PColors.tabTrack,
      borderRadius: BorderRadius.circular(10),
    ),
    child: Row(
      children: [
        _tab(toMarkLabel, !allTab, onToMark, const Key('tabToMark')),
        const SizedBox(width: 4),
        _tab(allLabel, allTab, onAll, const Key('tabAll')),
      ],
    ),
  );
}

/// One worker of the roll call: name, trade, time, four status buttons.
class WorkerCard extends StatelessWidget {
  const WorkerCard({
    super.key,
    required this.worker,
    required this.record,
    required this.enabled,
    required this.onPick,
    this.onTimeTap,
  });

  final CrewWorker worker;
  final LocalRecord? record;
  final bool enabled;
  final ValueChanged<AttendanceStatus> onPick;

  /// Called with `true` for the arrival, `false` for the departure.
  final ValueChanged<bool>? onTimeTap;

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final r = record;
    final time = r?.arrival == null
        ? null
        : r!.departure == null
        ? r.arrival!
        : '${r.arrival}–${r.departure}';
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: PColors.white,
        border: Border.all(color: PColors.line),
        borderRadius: BorderRadius.circular(PSizes.radius),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              InitialsAvatar(worker.name),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      worker.name,
                      style: const TextStyle(
                        fontSize: 17,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    if (worker.trade != null)
                      Text(
                        worker.trade!,
                        style: const TextStyle(
                          fontSize: 13,
                          color: PColors.steel,
                        ),
                      ),
                  ],
                ),
              ),
              if (time != null)
                InkWell(
                  onTap: onTimeTap == null
                      ? null
                      : () => onTimeTap!(
                          r!.departure == null ||
                              r.status != AttendanceStatus.halfDay,
                        ),
                  onLongPress: onTimeTap == null || r!.departure == null
                      ? null
                      : () => onTimeTap!(false),
                  child: Padding(
                    padding: const EdgeInsets.all(8),
                    child: Text(
                      time,
                      semanticsLabel:
                          '${l.arrival} ${r!.arrival}'
                          '${r.departure == null ? '' : ', ${l.departure} ${r.departure}'}',
                      style: const TextStyle(
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                        color: PColors.ink700,
                        fontFeatures: [FontFeature.tabularFigures()],
                      ),
                    ),
                  ),
                ),
            ],
          ),
          const SizedBox(height: 10),
          Semantics(
            container: true,
            label: l.statusFor(worker.name),
            child: Row(
              children: [
                for (final s in AttendanceStatus.values) ...[
                  if (s != AttendanceStatus.values.first)
                    const SizedBox(width: 6),
                  Expanded(
                    child: StatusButton(
                      key: Key('status-${worker.id}-${s.wire}'),
                      status: s,
                      selected: r?.status == s,
                      onTap: enabled ? () => onPick(s) : null,
                    ),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}
