import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/models/models.dart';
import '../../services/payout_service.dart';
import '../../state/providers.dart';
import '../../theme/tokens.dart';
import '../signature_pad.dart';
import '../widgets.dart';

/// Cash payout (MobilePayout artboard): In hand / Paid / Left, the current
/// worker with the amount to hand over, proof of receipt, the queue, and the
/// "Everyone is paid" end state.
class PayoutScreen extends ConsumerStatefulWidget {
  const PayoutScreen({super.key});

  @override
  ConsumerState<PayoutScreen> createState() => _PayoutScreenState();
}

class _PayoutScreenState extends ConsumerState<PayoutScreen> {
  int? _selected;
  ProofType _proof = ProofType.photo;
  String? _proofPath;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    // Fetch the sheet (or the not_open preview) if the network allows.
    Future.microtask(() async {
      final site = ref.read(currentSiteProvider);
      if (site == null) return;
      try {
        await ref
            .read(syncServiceProvider)
            .refreshPayout(site.id, ref.read(todayProvider));
      } catch (_) {
        // Offline: the cached sheet is shown.
      }
    });
  }

  void _select(int? lineId) => setState(() {
    _selected = lineId;
    _proofPath = null;
  });

  Future<void> _capture(ProofType type) async {
    setState(() {
      _proof = type;
      _proofPath = null;
    });
    final capture = ref.read(proofCaptureProvider);
    String? path;
    try {
      if (type == ProofType.photo) {
        path = await capture.takePhoto();
      } else {
        final png = await showSignatureSheet(
          context,
          thumb: type == ProofType.thumbMark,
        );
        if (png != null) path = await capture.savePng(png);
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(context.l10n.photoFailed)));
      }
    }
    if (mounted && path != null) setState(() => _proofPath = path);
  }

  Future<void> _confirm(PayoutView v, PayoutLine line) async {
    if (_proofPath == null || _busy) return;
    setState(() => _busy = true);
    try {
      await ref
          .read(payoutServiceProvider)
          .confirmPaid(
            siteId: v.sheet.siteId,
            date: v.sheet.date,
            line: line,
            proofType: _proof,
            proofPath: _proofPath,
          );
      _select(null);
    } on PayoutNotOpenException {
      // The sheet changed under us; the view refreshes from the store.
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final site = ref.watch(currentSiteProvider);
    final async = ref.watch(payoutViewProvider);
    final v = async.valueOrNull;
    final siteShort = site?.name.split(' ').first ?? '';

    if (v == null) {
      return Scaffold(
        body: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            BackHeader(
              title: l.payoutTitle,
              subtitle: siteShort,
              dark: true,
              onBack: () => context.pop(),
            ),
            Expanded(
              child: Center(
                child: async.isLoading
                    ? const CircularProgressIndicator()
                    : Padding(
                        padding: const EdgeInsets.all(24),
                        child: Text(l.noPayout, textAlign: TextAlign.center),
                      ),
              ),
            ),
          ],
        ),
      );
    }

    final sheet = v.sheet;
    final cur =
        v.lines
            .where((x) => x.payLineId == _selected && x.payment == null)
            .firstOrNull ??
        v.firstUnpaid;
    final date = shortDate(context, sheet.date);
    final manager = site?.manager?.name ?? l.theSiteManager;

    return Scaffold(
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          BackHeader(
            title: l.payoutTitle,
            subtitle: sheet.approvedAt == null
                ? l.payoutSubtitlePreview(siteShort, date)
                : l.payoutSubtitleApproved(
                    siteShort,
                    date,
                    timeOf(ref, sheet.approvedAt),
                  ),
            dark: true,
            onBack: () => context.pop(),
            bottom: Row(
              children: [
                _Figure(
                  label: l.inHand,
                  value: money(context, sheet.cashInHand, sheet.currency),
                  valueKey: const Key('inHand'),
                ),
                const SizedBox(width: 8),
                _Figure(
                  label: l.paid,
                  value: '${sheet.paidCount}/${sheet.lineCount}',
                  valueKey: const Key('paidCount'),
                ),
                const SizedBox(width: 8),
                _Figure(
                  label: l.left,
                  value: money(context, sheet.leftToPay, sheet.currency),
                  valueKey: const Key('left'),
                  accent: true,
                ),
              ],
            ),
          ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.fromLTRB(16, 14, 16, 24),
              children: [
                if (sheet.status == PayoutStatus.notOpen) ...[
                  InkNotice(
                    title: '',
                    body: l.previewBanner(manager),
                    icon: Icons.hourglass_top,
                  ),
                  const SizedBox(height: 12),
                ],
                if (sheet.status == PayoutStatus.closed) ...[
                  InkNotice(
                    title: '',
                    body: l.payoutClosedText,
                    icon: Icons.lock,
                  ),
                  const SizedBox(height: 12),
                ],
                if (cur != null)
                  _CurrentCard(
                    line: cur,
                    currency: sheet.currency,
                    open: v.open,
                    proof: _proof,
                    proofReady: _proofPath != null,
                    busy: _busy,
                    onProof: _capture,
                    onConfirm: () => _confirm(v, cur),
                  )
                else if (v.allPaid)
                  Container(
                    key: const Key('everyonePaid'),
                    padding: const EdgeInsets.all(18),
                    decoration: BoxDecoration(
                      color: PColors.presentBg,
                      borderRadius: BorderRadius.circular(PSizes.cardRadius),
                    ),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          l.everyonePaid,
                          style: const TextStyle(
                            color: PColors.presentFg,
                            fontSize: 22,
                            fontWeight: FontWeight.w800,
                          ),
                        ),
                        const SizedBox(height: 8),
                        Text(
                          l.returnCash(
                            money(context, sheet.cashInHand, sheet.currency),
                          ),
                          style: const TextStyle(
                            color: PColors.presentFg,
                            fontSize: 15,
                          ),
                        ),
                      ],
                    ),
                  ),
                const SizedBox(height: 16),
                SectionLabel(l.queue),
                const SizedBox(height: 8),
                for (final line in v.lines)
                  _QueueRow(
                    line: line,
                    currency: sheet.currency,
                    current: line.payLineId == cur?.payLineId,
                    unsent: v.unsent.contains(line.payLineId),
                    onTap: line.payment == null
                        ? () => _select(line.payLineId)
                        : null,
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _Figure extends StatelessWidget {
  const _Figure({
    required this.label,
    required this.value,
    required this.valueKey,
    this.accent = false,
  });
  final String label;
  final String value;
  final Key valueKey;
  final bool accent;

  @override
  Widget build(BuildContext context) => Expanded(
    child: Container(
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: PColors.inkRaised,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label.toUpperCase(),
            style: const TextStyle(
              color: PColors.onInkMuted,
              fontSize: 11,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.66,
            ),
          ),
          FittedBox(
            fit: BoxFit.scaleDown,
            child: Text(
              value,
              key: valueKey,
              style: TextStyle(
                color: accent ? PColors.amber : PColors.white,
                fontSize: 17,
                fontWeight: FontWeight.w700,
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
            ),
          ),
        ],
      ),
    ),
  );
}

class _CurrentCard extends StatelessWidget {
  const _CurrentCard({
    required this.line,
    required this.currency,
    required this.open,
    required this.proof,
    required this.proofReady,
    required this.busy,
    required this.onProof,
    required this.onConfirm,
  });

  final PayoutLine line;
  final Currency currency;
  final bool open;
  final ProofType proof;
  final bool proofReady;
  final bool busy;
  final ValueChanged<ProofType> onProof;
  final VoidCallback onConfirm;

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final net = money(context, line.net, currency);
    return Container(
      key: Key('current-${line.payLineId}'),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: PColors.white,
        border: Border.all(color: PColors.ink, width: 2),
        borderRadius: BorderRadius.circular(PSizes.cardRadius),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              InitialsAvatar(line.worker.name, size: 52, fontSize: 17),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      line.worker.name,
                      style: const TextStyle(
                        fontSize: 19,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    Text(
                      [?line.worker.trade, line.worked].join(' · '),
                      style: const TextStyle(
                        fontSize: 14,
                        color: PColors.steel,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          _AmountRow(l.earnedToday, money(context, line.gross, currency)),
          const SizedBox(height: 4),
          _AmountRow(
            l.advanceRepayment,
            line.advanceDeduction == 0
                ? '—'
                : '− ${money(context, line.advanceDeduction, currency)}',
            color: PColors.halfFg,
          ),
          const SizedBox(height: 10),
          Container(
            padding: const EdgeInsets.only(top: 10),
            decoration: const BoxDecoration(
              border: Border(top: BorderSide(color: PColors.ink, width: 2)),
            ),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.baseline,
              textBaseline: TextBaseline.alphabetic,
              children: [
                Expanded(
                  child: Text(
                    l.handOver.toUpperCase(),
                    style: const TextStyle(
                      fontSize: 14,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.84,
                    ),
                  ),
                ),
                Flexible(
                  child: FittedBox(
                    fit: BoxFit.scaleDown,
                    child: Text(
                      net,
                      key: const Key('handOver'),
                      style: const TextStyle(
                        fontSize: 34,
                        fontWeight: FontWeight.w800,
                        fontFeatures: [FontFeature.tabularFigures()],
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
          if (open) ...[
            const SizedBox(height: 12),
            Text(
              l.proofOfReceipt,
              style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                for (final (type, label, icon) in [
                  (ProofType.photo, l.proofPhoto, Icons.photo_camera_outlined),
                  (ProofType.signature, l.proofSignature, Icons.draw_outlined),
                  (ProofType.thumbMark, l.proofThumb, Icons.fingerprint),
                ]) ...[
                  if (type != ProofType.photo) const SizedBox(width: 8),
                  Expanded(
                    child: _ProofButton(
                      key: Key('proof-${type.wire}'),
                      label: label,
                      icon: icon,
                      selected: proof == type,
                      ready: proof == type && proofReady,
                      onTap: () => onProof(type),
                    ),
                  ),
                ],
              ],
            ),
            if (proofReady)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Row(
                  children: [
                    const Icon(
                      Icons.check_circle,
                      color: PColors.presentFg,
                      size: 20,
                    ),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        l.proofReady,
                        key: const Key('proofReady'),
                        style: const TextStyle(
                          color: PColors.presentFg,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    TextButton(
                      onPressed: () => onProof(proof),
                      child: Text(l.redoProof),
                    ),
                  ],
                ),
              ),
            const SizedBox(height: 12),
            FilledButton(
              key: const Key('confirmPaid'),
              onPressed: proofReady && !busy ? onConfirm : null,
              child: FittedBox(
                fit: BoxFit.scaleDown,
                child: Text(proofReady ? l.confirmPaid(net) : l.takeProofFirst),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _AmountRow extends StatelessWidget {
  const _AmountRow(this.label, this.value, {this.color});
  final String label;
  final String value;
  final Color? color;

  @override
  Widget build(BuildContext context) => Row(
    children: [
      Expanded(
        child: Text(
          label,
          style: const TextStyle(fontSize: 15, color: PColors.ink700),
        ),
      ),
      Text(
        value,
        style: TextStyle(
          fontSize: 15,
          color: color,
          fontFeatures: const [FontFeature.tabularFigures()],
        ),
      ),
    ],
  );
}

class _ProofButton extends StatelessWidget {
  const _ProofButton({
    super.key,
    required this.label,
    required this.icon,
    required this.selected,
    required this.ready,
    required this.onTap,
  });

  final String label;
  final IconData icon;
  final bool selected;
  final bool ready;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Semantics(
    button: true,
    selected: selected,
    child: Material(
      color: selected ? PColors.amberTint : PColors.white,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(10),
        side: BorderSide(
          color: selected ? PColors.ink : PColors.border,
          width: selected ? 2 : 1,
        ),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: onTap,
        child: SizedBox(
          height: 64,
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(ready ? Icons.check : icon, color: PColors.ink, size: 22),
              const SizedBox(height: 4),
              FittedBox(
                fit: BoxFit.scaleDown,
                child: Text(
                  label,
                  style: const TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    ),
  );
}

class _QueueRow extends StatelessWidget {
  const _QueueRow({
    required this.line,
    required this.currency,
    required this.current,
    required this.unsent,
    required this.onTap,
  });

  final PayoutLine line;
  final Currency currency;
  final bool current;
  final bool unsent;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final paid = line.payment != null;
    final (text, colors) = paid
        ? (l.queuePaid, StatusColors.present)
        : current
        ? (l.queueNow, StatusColors.dark)
        : (l.queueToPay, StatusColors.half);
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Material(
        color: PColors.white,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(PSizes.radius),
          side: const BorderSide(color: PColors.line),
        ),
        child: InkWell(
          key: Key('queue-${line.payLineId}'),
          borderRadius: BorderRadius.circular(PSizes.radius),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            child: Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        line.worker.name,
                        style: const TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      Text(
                        [
                          ?line.worker.trade,
                          if (unsent) l.notSentYet,
                        ].join(' · '),
                        style: const TextStyle(
                          fontSize: 13,
                          color: PColors.steel,
                        ),
                      ),
                    ],
                  ),
                ),
                Text(
                  money(context, line.net, currency),
                  style: const TextStyle(
                    fontWeight: FontWeight.w700,
                    fontFeatures: [FontFeature.tabularFigures()],
                  ),
                ),
                const SizedBox(width: 12),
                Pill(text, colors: colors),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
