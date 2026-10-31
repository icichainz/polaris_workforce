import 'auth.dart';
import 'enums.dart';
import 'money.dart';

class PayoutWorker {
  const PayoutWorker({
    required this.id,
    required this.name,
    required this.trade,
    required this.photoUrl,
  });

  factory PayoutWorker.fromJson(Map<String, dynamic> json) => PayoutWorker(
    id: json['id'] as int,
    name: json['name'] as String,
    trade: json['trade'] as String?,
    photoUrl: json['photo_url'] as String?,
  );

  final int id;
  final String name;
  final String? trade;
  final String? photoUrl;

  Map<String, dynamic> toJson() => {
    'id': id,
    'name': name,
    'trade': trade,
    'photo_url': photoUrl,
  };
}

class PaymentSummary {
  const PaymentSummary({
    required this.id,
    required this.clientUuid,
    required this.amount,
    required this.proofType,
    required this.paidAt,
  });

  factory PaymentSummary.fromJson(Map<String, dynamic> json) => PaymentSummary(
    id: json['id'] as int,
    clientUuid: json['client_uuid'] as String,
    amount: json['amount'] as int,
    proofType: ProofType.fromWire(json['proof_type'] as String),
    paidAt: json['paid_at'] as String,
  );

  final int id;
  final String clientUuid;
  final int amount;
  final ProofType proofType;
  final String paidAt;

  Map<String, dynamic> toJson() => {
    'id': id,
    'client_uuid': clientUuid,
    'amount': amount,
    'proof_type': proofType.wire,
    'paid_at': paidAt,
  };
}

class PayoutLine {
  const PayoutLine({
    required this.payLineId,
    required this.worker,
    required this.worked,
    required this.units,
    required this.gross,
    required this.advanceDeduction,
    required this.net,
    required this.payment,
  });

  factory PayoutLine.fromJson(Map<String, dynamic> json) => PayoutLine(
    payLineId: json['pay_line_id'] as int,
    worker: PayoutWorker.fromJson(json['worker'] as Map<String, dynamic>),
    worked: json['worked'] as String,
    units: json['units'] as String,
    gross: json['gross'] as int,
    advanceDeduction: json['advance_deduction'] as int,
    net: json['net'] as int,
    payment: json['payment'] == null
        ? null
        : PaymentSummary.fromJson(json['payment'] as Map<String, dynamic>),
  );

  final int payLineId;
  final PayoutWorker worker;

  /// Translated by the server, e.g. '1 day · late'.
  final String worked;

  /// Decimal string, e.g. "0.50". Never parsed into a double for money.
  final String units;
  final int gross;
  final int advanceDeduction;
  final int net;
  final PaymentSummary? payment;

  PayoutLine withPayment(PaymentSummary? p) => PayoutLine(
    payLineId: payLineId,
    worker: worker,
    worked: worked,
    units: units,
    gross: gross,
    advanceDeduction: advanceDeduction,
    net: net,
    payment: p,
  );

  Map<String, dynamic> toJson() => {
    'pay_line_id': payLineId,
    'worker': worker.toJson(),
    'worked': worked,
    'units': units,
    'gross': gross,
    'advance_deduction': advanceDeduction,
    'net': net,
    'payment': payment?.toJson(),
  };
}

/// `GET /sites/{id}/payouts/{date}/`.
class Payout {
  const Payout({
    required this.siteId,
    required this.date,
    required this.status,
    required this.dayStatus,
    required this.approvedAt,
    required this.currency,
    required this.cashIssued,
    required this.totalToPay,
    required this.paidTotal,
    required this.leftToPay,
    required this.cashInHand,
    required this.lineCount,
    required this.paidCount,
    required this.lines,
  });

  factory Payout.fromJson(Map<String, dynamic> json) => Payout(
    siteId: json['site_id'] as int,
    date: json['date'] as String,
    status: PayoutStatus.fromWire(json['status'] as String),
    dayStatus: DayStatus.fromWire(json['day_status'] as String),
    approvedAt: json['approved_at'] as String?,
    currency: Currency.fromJson(json['currency'] as Map<String, dynamic>),
    cashIssued: json['cash_issued'] as int,
    totalToPay: json['total_to_pay'] as int,
    paidTotal: json['paid_total'] as int,
    leftToPay: json['left_to_pay'] as int,
    cashInHand: json['cash_in_hand'] as int,
    lineCount: json['line_count'] as int,
    paidCount: json['paid_count'] as int,
    lines: (json['lines'] as List<dynamic>)
        .map((e) => PayoutLine.fromJson(e as Map<String, dynamic>))
        .toList(),
  );

  /// Builds a sheet whose totals follow the contract's invariants from [lines].
  factory Payout.fromLines({
    required int siteId,
    required String date,
    required PayoutStatus status,
    required DayStatus dayStatus,
    required String? approvedAt,
    required Currency currency,
    required int cashIssued,
    required List<PayoutLine> lines,
  }) {
    var total = 0, paid = 0, count = 0;
    for (final l in lines) {
      total += l.net;
      if (l.payment != null) {
        paid += l.payment!.amount;
        count++;
      }
    }
    return Payout(
      siteId: siteId,
      date: date,
      status: status,
      dayStatus: dayStatus,
      approvedAt: approvedAt,
      currency: currency,
      cashIssued: cashIssued,
      totalToPay: total,
      paidTotal: paid,
      leftToPay: total - paid,
      cashInHand: cashIssued - paid,
      lineCount: lines.length,
      paidCount: count,
      lines: lines,
    );
  }

  final int siteId;
  final String date;
  final PayoutStatus status;
  final DayStatus dayStatus;
  final String? approvedAt;
  final Currency currency;
  final int cashIssued;
  final int totalToPay;
  final int paidTotal;
  final int leftToPay;
  final int cashInHand;
  final int lineCount;
  final int paidCount;
  final List<PayoutLine> lines;

  Map<String, dynamic> toJson({bool withLines = true}) => {
    'site_id': siteId,
    'date': date,
    'status': status.wire,
    'day_status': dayStatus.wire,
    'approved_at': approvedAt,
    'currency': currency.toJson(),
    'cash_issued': cashIssued,
    'total_to_pay': totalToPay,
    'paid_total': paidTotal,
    'left_to_pay': leftToPay,
    'cash_in_hand': cashInHand,
    'line_count': lineCount,
    'paid_count': paidCount,
    if (withLines) 'lines': lines.map((l) => l.toJson()).toList(),
  };
}

/// `POST /payments/` response.
class Payment {
  const Payment({
    required this.id,
    required this.clientUuid,
    required this.payLineId,
    required this.amount,
    required this.currency,
    required this.proofType,
    required this.proofUrl,
    required this.paidAt,
    required this.paidBy,
    this.replayed = false,
  });

  factory Payment.fromJson(
    Map<String, dynamic> json, {
    bool replayed = false,
  }) => Payment(
    id: json['id'] as int,
    clientUuid: json['client_uuid'] as String,
    payLineId: json['pay_line_id'] as int,
    amount: json['amount'] as int,
    currency: Currency.fromJson(json['currency'] as Map<String, dynamic>),
    proofType: ProofType.fromWire(json['proof_type'] as String),
    proofUrl: json['proof_url'] as String?,
    paidAt: json['paid_at'] as String,
    paidBy: PersonRef.fromJson(json['paid_by'] as Map<String, dynamic>),
    replayed: replayed,
  );

  final int id;
  final String clientUuid;
  final int payLineId;
  final int amount;
  final Currency currency;
  final ProofType proofType;
  final String? proofUrl;
  final String paidAt;
  final PersonRef paidBy;

  /// `200` + `Idempotent-Replayed: true`: the server already had this
  /// `client_uuid` and returned the original payment. Not part of the JSON.
  final bool replayed;

  Payment asReplayed() => Payment.fromJson(toJson(), replayed: true);

  PaymentSummary get summary => PaymentSummary(
    id: id,
    clientUuid: clientUuid,
    amount: amount,
    proofType: proofType,
    paidAt: paidAt,
  );

  Map<String, dynamic> toJson() => {
    'id': id,
    'client_uuid': clientUuid,
    'pay_line_id': payLineId,
    'amount': amount,
    'currency': currency.toJson(),
    'proof_type': proofType.wire,
    'proof_url': proofUrl,
    'paid_at': paidAt,
    'paid_by': paidBy.toJson(),
  };
}

/// `POST /payments/` body. The proof file travels next to it as multipart.
class PaymentCreateRequest {
  const PaymentCreateRequest({
    required this.clientUuid,
    required this.payLineId,
    required this.amount,
    required this.proofType,
    this.paidAt,
  });

  factory PaymentCreateRequest.fromJson(Map<String, dynamic> json) =>
      PaymentCreateRequest(
        clientUuid: json['client_uuid'] as String,
        payLineId: json['pay_line_id'] as int,
        amount: json['amount'] as int,
        proofType: ProofType.fromWire(json['proof_type'] as String),
        paidAt: json['paid_at'] as String?,
      );

  final String clientUuid;
  final int payLineId;
  final int amount;
  final ProofType proofType;
  final String? paidAt;

  Map<String, dynamic> toJson() => {
    'client_uuid': clientUuid,
    'pay_line_id': payLineId,
    'amount': amount,
    'proof_type': proofType.wire,
    if (paidAt != null) 'paid_at': paidAt,
  };
}
