import 'dart:async';

import '../models/models.dart';
import 'local_store.dart';

/// In-memory [LocalStore] for tests and previews.
class MemoryLocalStore implements LocalStore {
  final _changes = StreamController<void>.broadcast();
  final Map<String, String> _kv = {};
  final Map<int, Crew> _crews = {};
  final Map<String, AttendanceDaySummary> _days = {};
  final Map<String, LocalRecord> _records = {};
  final Map<String, Payout> _payouts = {};
  final Map<String, LocalPayment> _payments = {};
  final Map<int, OutboxEntry> _outbox = {};
  int _outboxSeq = 0;

  String _dk(int siteId, String date) => '$siteId|$date';
  String _rk(int siteId, String date, int workerId) =>
      '$siteId|$date|$workerId';

  void _changed() {
    if (!_changes.isClosed) _changes.add(null);
  }

  @override
  Stream<void> get changes => _changes.stream;

  @override
  Future<String?> getValue(String key) async => _kv[key];

  @override
  Future<void> setValue(String key, String? value) async {
    if (value == null) {
      _kv.remove(key);
    } else {
      _kv[key] = value;
    }
    _changed();
  }

  @override
  Future<void> saveCrew(Crew crew) async {
    _crews[crew.siteId] = crew;
    _changed();
  }

  @override
  Future<Crew?> loadCrew(int siteId) async => _crews[siteId];

  @override
  Future<void> upsertDays(Iterable<AttendanceDaySummary> days) async {
    for (final d in days) {
      _days[_dk(d.siteId, d.date)] = d;
    }
    _changed();
  }

  @override
  Future<AttendanceDaySummary?> loadDay(int siteId, String date) async =>
      _days[_dk(siteId, date)];

  @override
  Future<AttendanceDaySummary?> dayById(int dayId) async =>
      _days.values.where((d) => d.id == dayId).firstOrNull;

  @override
  Future<List<AttendanceDaySummary>> loadDays(int siteId) async =>
      _days.values.where((d) => d.siteId == siteId).toList()
        ..sort((a, b) => b.date.compareTo(a.date));

  @override
  Future<void> upsertRecords(Iterable<LocalRecord> records) async {
    for (final r in records) {
      _records[_rk(r.siteId, r.date, r.workerId)] = r;
    }
    _changed();
  }

  @override
  Future<List<LocalRecord>> loadRecords(int siteId, String date) async =>
      _records.values
          .where((r) => r.siteId == siteId && r.date == date)
          .toList();

  @override
  Future<LocalRecord?> loadRecord(
    int siteId,
    String date,
    int workerId,
  ) async => _records[_rk(siteId, date, workerId)];

  @override
  Future<LocalRecord?> recordByServerId(int serverId) async =>
      _records.values.where((r) => r.serverId == serverId).firstOrNull;

  @override
  Future<void> savePayout(Payout payout) async {
    _payouts[_dk(payout.siteId, payout.date)] = payout;
    _changed();
  }

  @override
  Future<Payout?> loadPayout(int siteId, String date) async =>
      _payouts[_dk(siteId, date)];

  @override
  Future<void> upsertPayment(LocalPayment payment) async {
    _payments[payment.clientUuid] = payment;
    _changed();
  }

  @override
  Future<List<LocalPayment>> loadPayments(int siteId, String date) async =>
      _payments.values
          .where((p) => p.siteId == siteId && p.date == date)
          .toList();

  @override
  Future<int> insertOutbox(OutboxEntry entry) async {
    final id = ++_outboxSeq;
    _outbox[id] = entry.copyWith(id: id);
    _changed();
    return id;
  }

  @override
  Future<void> updateOutbox(OutboxEntry entry) async {
    if (entry.id != null && _outbox.containsKey(entry.id)) {
      _outbox[entry.id!] = entry;
      _changed();
    }
  }

  @override
  Future<bool> deleteOutbox(int id, {int? ifVersion}) async {
    final e = _outbox[id];
    if (e == null) return false;
    if (ifVersion != null && e.version != ifVersion) return false;
    _outbox.remove(id);
    _changed();
    return true;
  }

  @override
  Future<List<OutboxEntry>> loadOutbox() async =>
      (_outbox.values.toList()..sort((a, b) => a.id!.compareTo(b.id!)));

  @override
  Future<void> close() async => _changes.close();
}
