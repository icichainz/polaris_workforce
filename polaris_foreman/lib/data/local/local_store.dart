import '../models/models.dart';
import 'outbox.dart';
import 'rows.dart';

export 'outbox.dart';
export 'rows.dart';

/// The phone's own database. The UI reads only from here; every write lands
/// here first (and in the outbox), the network only moves the outbox and
/// refreshes the cache.
///
/// Implementations: `SqfliteLocalStore` (the app) and `MemoryLocalStore`
/// (tests). Both are plain storage; the rules live in the services.
abstract interface class LocalStore {
  /// Fires after every write.
  Stream<void> get changes;

  // Key/value: device id, sync cursor, remembered user, language.
  Future<String?> getValue(String key);
  Future<void> setValue(String key, String? value);

  // Crew cache.
  Future<void> saveCrew(Crew crew);
  Future<Crew?> loadCrew(int siteId);

  // Days (server summaries; the server wins).
  Future<void> upsertDays(Iterable<AttendanceDaySummary> days);
  Future<AttendanceDaySummary?> loadDay(int siteId, String date);
  Future<AttendanceDaySummary?> dayById(int dayId);

  /// Newest first.
  Future<List<AttendanceDaySummary>> loadDays(int siteId);

  // Records.
  Future<void> upsertRecords(Iterable<LocalRecord> records);
  Future<List<LocalRecord>> loadRecords(int siteId, String date);
  Future<LocalRecord?> loadRecord(int siteId, String date, int workerId);
  Future<LocalRecord?> recordByServerId(int serverId);

  // Payout sheets and local payments.
  Future<void> savePayout(Payout payout);
  Future<Payout?> loadPayout(int siteId, String date);
  Future<void> upsertPayment(LocalPayment payment);
  Future<List<LocalPayment>> loadPayments(int siteId, String date);

  // Outbox.
  Future<int> insertOutbox(OutboxEntry entry);
  Future<void> updateOutbox(OutboxEntry entry);

  /// Deletes the entry; with [ifVersion], only if it was not changed since.
  /// Returns whether it was deleted.
  Future<bool> deleteOutbox(int id, {int? ifVersion});

  /// All entries, oldest first.
  Future<List<OutboxEntry>> loadOutbox();

  Future<void> close();
}

/// Keys used with [LocalStore.getValue].
abstract final class StoreKeys {
  static const deviceId = 'device_id';
  static const syncSince = 'sync_since';
  static const me = 'me';
  static const rememberedPhone = 'remembered_phone';
  static const rememberedName = 'remembered_name';
  static const rememberedSite = 'remembered_site';
  static const language = 'language';
  static const siteId = 'site_id';
}
