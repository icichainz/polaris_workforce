import 'package:uuid/uuid.dart';

import '../core/dates.dart';
import '../core/site_clock.dart';
import '../data/local/local_store.dart';
import '../data/models/models.dart';

/// The cash sheet as the foreman sees it: the server's sheet plus payments
/// made on this phone that have not reached the server yet. Totals keep the
/// contract's invariants (`left = total − paid`, `in hand = issued − paid`).
class PayoutView {
  const PayoutView({required this.sheet, required this.unsent});

  final Payout sheet;

  /// Pay line ids paid on the phone but not sent yet.
  final Set<int> unsent;

  List<PayoutLine> get lines => sheet.lines;
  Currency get currency => sheet.currency;
  bool get open => sheet.status == PayoutStatus.open;
  bool get allPaid => lines.isNotEmpty && lines.every((l) => l.payment != null);

  PayoutLine? get firstUnpaid =>
      lines.where((l) => l.payment == null).firstOrNull;
}

PayoutView buildPayoutView(Payout server, List<LocalPayment> local) {
  final byLine = {for (final p in local) p.payLineId: p};
  final unsent = <int>{};
  final lines = [
    for (final l in server.lines)
      if (l.payment == null && byLine.containsKey(l.payLineId))
        () {
          final p = byLine[l.payLineId]!;
          if (!p.sent) unsent.add(l.payLineId);
          return l.withPayment(
            PaymentSummary(
              id: p.serverId ?? 0,
              clientUuid: p.clientUuid,
              amount: p.amount,
              proofType: p.proofType,
              paidAt: p.paidAt,
            ),
          );
        }()
      else
        l,
  ];
  final everyonePaid =
      lines.isNotEmpty && lines.every((l) => l.payment != null);
  final sheet = Payout.fromLines(
    siteId: server.siteId,
    date: server.date,
    status: server.status == PayoutStatus.open && everyonePaid
        ? PayoutStatus.paid
        : server.status,
    dayStatus: server.dayStatus,
    approvedAt: server.approvedAt,
    currency: server.currency,
    cashIssued: server.cashIssued,
    lines: lines,
  );
  return PayoutView(sheet: sheet, unsent: unsent);
}

/// Thrown when paying a line that cannot be paid (sheet not open, paid).
class PayoutNotOpenException implements Exception {
  const PayoutNotOpenException();
}

/// The proof does not follow `company.payments`: a type the company does
/// not accept, or no file for a type in `file_required_for`.
class ProofNotAcceptedException implements Exception {
  const ProofNotAcceptedException(this.proofType);
  final ProofType proofType;
}

class PayoutService {
  PayoutService(
    this.store, {
    required this.clock,
    String Function()? newUuid,
    this.onWrite,
  }) : _newUuid = newUuid ?? (() => const Uuid().v4());

  final LocalStore store;
  final SiteClock clock;
  final String Function() _newUuid;
  final void Function()? onWrite;

  /// Records that [line] was paid in cash, with proof. The `client_uuid` is
  /// made now, at the tap, and reused for every retry; `paid_at` is now too,
  /// because the payment may reach the server hours later.
  Future<LocalPayment> confirmPaid({
    required int siteId,
    required String date,
    required PayoutLine line,
    required ProofType proofType,
    String? proofPath,
    PaymentRules rules = PaymentRules.standard,
  }) async {
    if (!rules.accepts(proofType) ||
        (rules.needsFile(proofType) && proofPath == null)) {
      throw ProofNotAcceptedException(proofType);
    }
    final cached = await store.loadPayout(siteId, date);
    if (cached == null || cached.status != PayoutStatus.open) {
      throw const PayoutNotOpenException();
    }
    final already = (await store.loadPayments(
      siteId,
      date,
    )).any((p) => p.payLineId == line.payLineId);
    if (already || line.payment != null) {
      throw const PayoutNotOpenException();
    }
    final uuid = _newUuid();
    final paidAt = isoTimestamp(clock.instant());
    final payment = LocalPayment(
      clientUuid: uuid,
      siteId: siteId,
      date: date,
      payLineId: line.payLineId,
      workerId: line.worker.id,
      amount: line.net,
      proofType: proofType,
      proofPath: proofPath,
      paidAt: paidAt,
    );
    await store.upsertPayment(payment);
    await store.insertOutbox(
      OutboxEntry(
        kind: OutboxKind.payment,
        siteId: siteId,
        date: date,
        clientUuid: uuid,
        payload: {
          ...PaymentCreateRequest(
            clientUuid: uuid,
            payLineId: line.payLineId,
            amount: line.net,
            proofType: proofType,
            paidAt: paidAt,
          ).toJson(),
          'proof_path': proofPath,
        },
        createdAt: paidAt,
      ),
    );
    onWrite?.call();
    return payment;
  }
}
