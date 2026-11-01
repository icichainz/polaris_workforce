import '../data/local/observations.dart';
import '../data/local/outbox.dart' show backoff;
import '../data/models/models.dart';
import 'tracking_api.dart';
import 'tracking_models.dart';

/// How a drain ended.
enum UploadOutcome {
  /// Nothing left for this foreman.
  done,

  /// No usable tracker token (none yet, or it could not be refreshed).
  noToken,

  /// The tracker could not be reached or failed: try again after
  /// [UploadReport.retryIn].
  retryLater,
}

class UploadReport {
  const UploadReport(
    this.outcome, {
    this.sent = 0,
    this.dropped = 0,
    this.retryIn,
  });

  final UploadOutcome outcome;

  /// Acknowledged by the tracker (accepted, duplicates and rejected items
  /// alike: none of them is sent again).
  final int sent;

  /// Refused one by one as malformed (`400`): dropped, never resent.
  final int dropped;
  final Duration? retryIn;

  @override
  String toString() =>
      'UploadReport(${outcome.name}, sent: $sent, dropped: $dropped, '
      'retryIn: $retryIn)';
}

/// Moves queued observations to the tracker: oldest first, in batches of at
/// most [maxBatch] (the contract's limit, 500).
///
/// * `200`: every item of the batch is done, whether accepted, a
///   duplicate (sent before, the answer was lost) or rejected (the tracker
///   says not to resend those).
/// * `401`: the token expired or was revoked: [refreshToken], then the same
///   batch again, once.
/// * `413`: smaller batches.
/// * `400`: the batch is halved until the malformed item is alone; that one
///   is dropped, the others go through.
/// * Anything else (offline, timeout, 5xx, 429, 403, 404…): the batch stays
///   queued; retry after a backoff (429: `retry_after`).
class ObservationUploader {
  ObservationUploader({
    required this.store,
    required this.transport,
    required this.currentToken,
    required this.refreshToken,
    this.maxBatch = 500,
    DateTime Function()? now,
  }) : assert(maxBatch >= 1 && maxBatch <= 500),
       _now = now ?? DateTime.now;

  final ObservationStore store;
  final TrackerTransport transport;

  /// The token on the phone (may be expired), or null.
  final Future<TrackingToken?> Function() currentToken;

  /// Asks Django for a new token; null when none came.
  final Future<TrackingToken?> Function() refreshToken;
  final int maxBatch;
  final DateTime Function() _now;

  int _failures = 0;
  late int _batch = maxBatch;
  Future<UploadReport>? _running;

  /// Sends everything queued for [owner]; one drain at a time (a second
  /// call joins the running one).
  Future<UploadReport> drain(String owner) =>
      _running ??= _drain(owner).whenComplete(() => _running = null);

  Future<UploadReport> _drain(String owner) async {
    var sent = 0, dropped = 0;
    var token = await currentToken();
    if (token != null && token.expiredAt(_now())) token = null;
    token ??= await refreshToken();
    if (token == null) {
      return const UploadReport(UploadOutcome.noToken);
    }
    var refreshed = false;
    while (true) {
      final items = await store.nextObservations(owner, _batch);
      if (items.isEmpty) {
        _failures = 0;
        return UploadReport(UploadOutcome.done, sent: sent, dropped: dropped);
      }
      try {
        final (ok, bad) = await _sendSplitting(token!, items);
        sent += ok;
        dropped += bad;
        _failures = 0;
        refreshed = false;
      } on ApiException catch (e) {
        if (e.statusCode == 401 && !refreshed) {
          refreshed = true;
          token = await refreshToken();
          if (token == null) {
            return UploadReport(
              UploadOutcome.noToken,
              sent: sent,
              dropped: dropped,
            );
          }
          continue;
        }
        if (e.statusCode == 413 && _batch > 1) {
          _batch = _batch ~/ 2;
          continue;
        }
        return _later(e, sent, dropped);
      } on NetworkException catch (e) {
        return _later(e, sent, dropped);
      }
    }
  }

  UploadReport _later(Object error, int sent, int dropped) {
    _failures++;
    Duration wait = backoff(_failures);
    if (error is ApiException && error.statusCode == 429) {
      final s = error.retryAfterSeconds;
      if (s != null) wait = Duration(seconds: s);
    }
    return UploadReport(
      UploadOutcome.retryLater,
      sent: sent,
      dropped: dropped,
      retryIn: wait,
    );
  }

  /// Posts [items]; on `400` posts each half instead, down to single items
  /// (a malformed one is dropped). Other errors propagate, leaving unsent
  /// items queued. Returns (acknowledged, dropped).
  Future<(int, int)> _sendSplitting(
    TrackingToken token,
    List<QueuedObservation> items,
  ) async {
    try {
      await transport.postObservations(
        trackerUrl: token.trackerUrl,
        token: token.token,
        observations: [for (final o in items) o.json],
      );
      await store.removeObservations(items.map((o) => o.clientUuid));
      return (items.length, 0);
    } on ApiException catch (e) {
      if (e.statusCode != 400) rethrow;
      if (items.length == 1) {
        await store.removeObservations([items.single.clientUuid]);
        return (0, 1);
      }
      final mid = items.length ~/ 2;
      final (a1, b1) = await _sendSplitting(token, items.sublist(0, mid));
      final (a2, b2) = await _sendSplitting(token, items.sublist(mid));
      return (a1 + a2, b1 + b2);
    }
  }
}
