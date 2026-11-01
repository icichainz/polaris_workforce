/// One GPS fix or BLE sighting waiting for the tracker
/// (`POST /v1/observations`), as its JSON body item.
class QueuedObservation {
  const QueuedObservation({
    required this.clientUuid,
    required this.owner,
    required this.at,
    required this.json,
  });

  /// The observation's idempotency key (also in [json]).
  final String clientUuid;

  /// User id of the foreman whose token may send it (GPS fixes are theirs).
  final String owner;

  /// Device time of the observation (also in [json]), RFC 3339.
  final String at;
  final Map<String, dynamic> json;
}

/// The tracking part of the phone's outbox: observations for the tracker.
///
/// They live next to the other queued writes (the same database, schema 3)
/// but in their own table, because they are many (one GPS fix a minute,
/// plus badge sightings), go to another server with another token, in
/// batches, and must not show in the sync banner's "N waiting" or wake the
/// screens: writing here does not fire `LocalStore.changes`.
abstract interface class ObservationStore {
  /// Adds observations; one whose `client_uuid` is already queued is
  /// ignored.
  Future<void> addObservations(Iterable<QueuedObservation> items);

  /// The oldest [limit] observations of [owner], in the order they were
  /// taken.
  Future<List<QueuedObservation>> nextObservations(String owner, int limit);

  /// Removes sent (acknowledged) observations.
  Future<void> removeObservations(Iterable<String> clientUuids);

  /// Observations waiting, for [owner] or for everyone.
  Future<int> countObservations([String? owner]);

  /// Drops observations taken before [before], then the oldest ones beyond
  /// [keepAtMost]. Returns how many went.
  Future<int> pruneObservations({required String before, int? keepAtMost});

  /// Drops every observation of [owner] (tracking refused or turned off).
  Future<void> clearObservations(String owner);
}
