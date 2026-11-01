import '../models/models.dart';

/// Kinds of queued writes, in the order the server needs them:
/// check-ins → records → submit → corrections → payments (records may be
/// built from suggestions, which need the check-ins on the server). A
/// tracking consent answer goes first: it depends on nothing.
enum OutboxKind {
  consent('consent', -1),
  checkin('checkin', 0),
  records('records', 1),
  submit('submit', 2),
  correction('correction', 3),
  payment('payment', 4);

  const OutboxKind(this.wire, this.rank);
  final String wire;
  final int rank;

  static OutboxKind fromWire(String v) => values.firstWhere((k) => k.wire == v);
}

enum OutboxState {
  /// Waiting to be sent (or to be retried).
  pending('pending'),

  /// Rejected by the server (400/403/404/409/423): not retried, shown to the
  /// foreman.
  failed('failed');

  const OutboxState(this.wire);
  final String wire;

  static OutboxState fromWire(String v) =>
      values.firstWhere((k) => k.wire == v);
}

/// One queued write.
///
/// * `records`: one entry per site and day. `payload.records` maps worker id
///   to a [RecordUpsert] JSON; edits merge into it and bump [version].
/// * `submit`: `{client_uuid, foreman_note}`.
/// * `correction`: `{client_uuid, worker_id, field, new_value, reason}`; the
///   record id is resolved when sending (records go first).
/// * `payment`: `{client_uuid, pay_line_id, amount, proof_type, paid_at,
///   proof_path}`.
/// * `checkin`: a `POST /checkins/` body plus `photo_path` (the compressed
///   photo, kept until the server has the check-in).
/// * `consent`: a `POST /tracking/consent/` body `{client_uuid, accepted,
///   text_version}` (site 0, the day it was answered).
///
/// Tracking observations (GPS fixes, badge sightings) are not entries: they
/// have their own table ([ObservationStore]).
class OutboxEntry {
  const OutboxEntry({
    this.id,
    required this.kind,
    required this.siteId,
    required this.date,
    required this.clientUuid,
    required this.payload,
    required this.createdAt,
    this.version = 1,
    this.attempts = 0,
    this.state = OutboxState.pending,
    this.errorCode,
    this.errorMessage,
    this.nextAttemptAt,
  });

  final int? id;
  final OutboxKind kind;
  final int siteId;
  final String date;

  /// Idempotency key of the write (empty for a records batch: each record
  /// carries its own).
  final String clientUuid;
  final Map<String, dynamic> payload;
  final String createdAt;
  final int version;
  final int attempts;
  final OutboxState state;
  final String? errorCode;
  final String? errorMessage;
  final DateTime? nextAttemptAt;

  OutboxEntry copyWith({
    int? id,
    Map<String, dynamic>? payload,
    int? version,
    int? attempts,
    OutboxState? state,
    String? Function()? errorCode,
    String? Function()? errorMessage,
    DateTime? Function()? nextAttemptAt,
  }) => OutboxEntry(
    id: id ?? this.id,
    kind: kind,
    siteId: siteId,
    date: date,
    clientUuid: clientUuid,
    payload: payload ?? this.payload,
    createdAt: createdAt,
    version: version ?? this.version,
    attempts: attempts ?? this.attempts,
    state: state ?? this.state,
    errorCode: errorCode != null ? errorCode() : this.errorCode,
    errorMessage: errorMessage != null ? errorMessage() : this.errorMessage,
    nextAttemptAt: nextAttemptAt != null ? nextAttemptAt() : this.nextAttemptAt,
  );
}

/// Sending order: by kind (check-ins → records → submit → corrections →
/// payments), then in the order the foreman acted.
List<OutboxEntry> sendingOrder(Iterable<OutboxEntry> entries) {
  final list = entries.toList();
  list.sort((a, b) {
    final r = a.kind.rank.compareTo(b.kind.rank);
    return r != 0 ? r : (a.id ?? 0).compareTo(b.id ?? 0);
  });
  return list;
}

/// What to do after a failed send.
sealed class RetryDecision {
  const RetryDecision();
}

/// Try the same entry again after [delay]; stop draining until then.
class RetryLater extends RetryDecision {
  const RetryLater(this.delay);
  final Duration delay;
}

/// The token is gone: keep the outbox, ask for the PIN.
class SignInAgain extends RetryDecision {
  const SignInAgain();
}

/// Permanent: mark the entry failed, show it, move on to the next one.
class GiveUp extends RetryDecision {
  const GiveUp();
}

/// Retry rules from `docs/api/README.md`: network errors, 5xx and 429 are
/// retried with backoff (429 waits `retry_after`); 401 needs the PIN; the
/// rest (400, 403, 404, 409, 423…) are not retried.
RetryDecision retryDecision(Object error, int attempts) {
  if (error is NetworkException) return RetryLater(backoff(attempts));
  if (error is ApiException) {
    final s = error.statusCode;
    if (s == 401) return const SignInAgain();
    if (s == 429) {
      final wait = error.retryAfterSeconds;
      return RetryLater(
        wait != null ? Duration(seconds: wait) : backoff(attempts),
      );
    }
    // 5xx, including 501 not_implemented while Wave 2 is not merged: keep
    // the write and retry later rather than lose it.
    if (s >= 500) return RetryLater(backoff(attempts));
    return const GiveUp();
  }
  return const GiveUp();
}

/// 2 s, 4 s, 8 s… capped at 5 minutes. [attempts] counts failures so far.
Duration backoff(int attempts) {
  final n = attempts < 1 ? 1 : (attempts > 9 ? 9 : attempts);
  final seconds = 1 << n; // 2^n
  return Duration(seconds: seconds > 300 ? 300 : seconds);
}
