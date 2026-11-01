import 'dart:async';
import 'dart:convert';

import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart';

import '../models/models.dart';
import 'local_store.dart';

/// [LocalStore] on the phone's SQLite database (sqflite), with the
/// tracking observations ([ObservationStore]) in the same file.
class SqfliteLocalStore implements LocalStore, ObservationStore {
  SqfliteLocalStore._(this._db);

  /// 2: check-ins (contract 1.2). 3: tracking observations.
  static const _schemaVersion = 3;

  final Database _db;
  final _changes = StreamController<void>.broadcast();

  static Future<SqfliteLocalStore> open({String name = 'polaris.db'}) async {
    final dir = await getDatabasesPath();
    final db = await openDatabase(
      p.join(dir, name),
      version: _schemaVersion,
      onCreate: (db, version) async {
        final batch = db.batch();
        for (final sql in _schema) {
          batch.execute(sql);
        }
        await batch.commit(noResult: true);
      },
      onUpgrade: (db, from, to) async {
        if (from < 2) {
          for (final sql in _checkinSchema) {
            await db.execute(sql);
          }
        }
        if (from < 3) {
          for (final sql in _observationSchema) {
            await db.execute(sql);
          }
        }
      },
    );
    return SqfliteLocalStore._(db);
  }

  static const _schema = [
    'CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    'CREATE TABLE crew_workers (site_id INTEGER NOT NULL, worker_id INTEGER '
        'NOT NULL, sort INTEGER NOT NULL, json TEXT NOT NULL, '
        'PRIMARY KEY (site_id, worker_id))',
    'CREATE TABLE crews (site_id INTEGER PRIMARY KEY, date TEXT NOT NULL, '
        'count INTEGER NOT NULL)',
    'CREATE TABLE days (site_id INTEGER NOT NULL, date TEXT NOT NULL, '
        'id INTEGER, json TEXT NOT NULL, PRIMARY KEY (site_id, date))',
    'CREATE INDEX days_id ON days (id)',
    'CREATE TABLE records (site_id INTEGER NOT NULL, date TEXT NOT NULL, '
        'worker_id INTEGER NOT NULL, client_uuid TEXT NOT NULL, '
        'server_id INTEGER, day_id INTEGER, status TEXT NOT NULL, '
        'arrival TEXT, departure TEXT, note TEXT NOT NULL, '
        'corrected INTEGER NOT NULL, updated_at TEXT, '
        'PRIMARY KEY (site_id, date, worker_id))',
    'CREATE INDEX records_server_id ON records (server_id)',
    'CREATE TABLE payout_sheets (site_id INTEGER NOT NULL, date TEXT NOT '
        'NULL, json TEXT NOT NULL, PRIMARY KEY (site_id, date))',
    'CREATE TABLE payout_lines (site_id INTEGER NOT NULL, date TEXT NOT NULL, '
        'pay_line_id INTEGER NOT NULL, sort INTEGER NOT NULL, '
        'json TEXT NOT NULL, PRIMARY KEY (site_id, date, pay_line_id))',
    'CREATE TABLE payments (client_uuid TEXT PRIMARY KEY, site_id INTEGER '
        'NOT NULL, date TEXT NOT NULL, pay_line_id INTEGER NOT NULL, '
        'worker_id INTEGER NOT NULL, amount INTEGER NOT NULL, '
        'proof_type TEXT NOT NULL, proof_path TEXT, paid_at TEXT NOT NULL, '
        'server_id INTEGER)',
    'CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'kind TEXT NOT NULL, site_id INTEGER NOT NULL, date TEXT NOT NULL, '
        'client_uuid TEXT NOT NULL, payload TEXT NOT NULL, '
        'created_at TEXT NOT NULL, version INTEGER NOT NULL, '
        'attempts INTEGER NOT NULL, state TEXT NOT NULL, error_code TEXT, '
        'error_message TEXT, next_attempt_at TEXT)',
    ..._checkinSchema,
    ..._observationSchema,
  ];

  static const _checkinSchema = [
    'CREATE TABLE checkins (client_uuid TEXT PRIMARY KEY, site_id INTEGER '
        'NOT NULL, date TEXT NOT NULL, at TEXT NOT NULL, json TEXT NOT NULL)',
    'CREATE INDEX checkins_day ON checkins (site_id, date)',
  ];

  /// Tracking observations waiting for the tracker (`id` keeps the order
  /// they were taken in).
  static const _observationSchema = [
    'CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'client_uuid TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, '
        'at TEXT NOT NULL, json TEXT NOT NULL)',
    'CREATE INDEX observations_owner ON observations (owner, id)',
    'CREATE INDEX observations_at ON observations (at)',
  ];

  void _changed() {
    if (!_changes.isClosed) _changes.add(null);
  }

  @override
  Stream<void> get changes => _changes.stream;

  // ------------------------------------------------------------------- kv

  @override
  Future<String?> getValue(String key) async {
    final rows = await _db.query('kv', where: 'key = ?', whereArgs: [key]);
    return rows.isEmpty ? null : rows.first['value'] as String;
  }

  @override
  Future<void> setValue(String key, String? value) async {
    if (value == null) {
      await _db.delete('kv', where: 'key = ?', whereArgs: [key]);
    } else {
      await _db.insert('kv', {
        'key': key,
        'value': value,
      }, conflictAlgorithm: ConflictAlgorithm.replace);
    }
    _changed();
  }

  // ----------------------------------------------------------------- crew

  @override
  Future<void> saveCrew(Crew crew) async {
    await _db.transaction((txn) async {
      await txn.delete(
        'crew_workers',
        where: 'site_id = ?',
        whereArgs: [crew.siteId],
      );
      final batch = txn.batch();
      for (var i = 0; i < crew.workers.length; i++) {
        final w = crew.workers[i];
        batch.insert('crew_workers', {
          'site_id': crew.siteId,
          'worker_id': w.id,
          'sort': i,
          'json': jsonEncode(w.toJson()),
        });
      }
      batch.insert('crews', {
        'site_id': crew.siteId,
        'date': crew.date,
        'count': crew.count,
      }, conflictAlgorithm: ConflictAlgorithm.replace);
      await batch.commit(noResult: true);
    });
    _changed();
  }

  @override
  Future<Crew?> loadCrew(int siteId) async {
    final head = await _db.query(
      'crews',
      where: 'site_id = ?',
      whereArgs: [siteId],
    );
    if (head.isEmpty) return null;
    final rows = await _db.query(
      'crew_workers',
      where: 'site_id = ?',
      whereArgs: [siteId],
      orderBy: 'sort',
    );
    return Crew(
      siteId: siteId,
      date: head.first['date'] as String,
      count: head.first['count'] as int,
      workers: [
        for (final r in rows)
          CrewWorker.fromJson(
            jsonDecode(r['json'] as String) as Map<String, dynamic>,
          ),
      ],
    );
  }

  // ----------------------------------------------------------------- days

  @override
  Future<void> upsertDays(Iterable<AttendanceDaySummary> days) async {
    final batch = _db.batch();
    for (final d in days) {
      batch.insert('days', {
        'site_id': d.siteId,
        'date': d.date,
        'id': d.id,
        'json': jsonEncode(d.toJson()),
      }, conflictAlgorithm: ConflictAlgorithm.replace);
    }
    await batch.commit(noResult: true);
    _changed();
  }

  AttendanceDaySummary _day(Map<String, Object?> row) =>
      AttendanceDaySummary.fromJson(
        jsonDecode(row['json'] as String) as Map<String, dynamic>,
      );

  @override
  Future<AttendanceDaySummary?> loadDay(int siteId, String date) async {
    final rows = await _db.query(
      'days',
      where: 'site_id = ? AND date = ?',
      whereArgs: [siteId, date],
    );
    return rows.isEmpty ? null : _day(rows.first);
  }

  @override
  Future<AttendanceDaySummary?> dayById(int dayId) async {
    final rows = await _db.query('days', where: 'id = ?', whereArgs: [dayId]);
    return rows.isEmpty ? null : _day(rows.first);
  }

  @override
  Future<List<AttendanceDaySummary>> loadDays(int siteId) async {
    final rows = await _db.query(
      'days',
      where: 'site_id = ?',
      whereArgs: [siteId],
      orderBy: 'date DESC',
    );
    return rows.map(_day).toList();
  }

  // -------------------------------------------------------------- records

  Map<String, Object?> _recordRow(LocalRecord r) => {
    'site_id': r.siteId,
    'date': r.date,
    'worker_id': r.workerId,
    'client_uuid': r.clientUuid,
    'server_id': r.serverId,
    'day_id': r.dayId,
    'status': r.status.wire,
    'arrival': r.arrival,
    'departure': r.departure,
    'note': r.note,
    'corrected': r.corrected ? 1 : 0,
    'updated_at': r.updatedAt,
  };

  LocalRecord _record(Map<String, Object?> row) => LocalRecord(
    siteId: row['site_id'] as int,
    date: row['date'] as String,
    workerId: row['worker_id'] as int,
    clientUuid: row['client_uuid'] as String,
    serverId: row['server_id'] as int?,
    dayId: row['day_id'] as int?,
    status: AttendanceStatus.fromWire(row['status'] as String),
    arrival: row['arrival'] as String?,
    departure: row['departure'] as String?,
    note: row['note'] as String,
    corrected: (row['corrected'] as int) == 1,
    updatedAt: row['updated_at'] as String?,
  );

  @override
  Future<void> upsertRecords(Iterable<LocalRecord> records) async {
    final batch = _db.batch();
    for (final r in records) {
      batch.insert(
        'records',
        _recordRow(r),
        conflictAlgorithm: ConflictAlgorithm.replace,
      );
    }
    await batch.commit(noResult: true);
    _changed();
  }

  @override
  Future<List<LocalRecord>> loadRecords(int siteId, String date) async {
    final rows = await _db.query(
      'records',
      where: 'site_id = ? AND date = ?',
      whereArgs: [siteId, date],
    );
    return rows.map(_record).toList();
  }

  @override
  Future<LocalRecord?> loadRecord(int siteId, String date, int workerId) async {
    final rows = await _db.query(
      'records',
      where: 'site_id = ? AND date = ? AND worker_id = ?',
      whereArgs: [siteId, date, workerId],
    );
    return rows.isEmpty ? null : _record(rows.first);
  }

  @override
  Future<LocalRecord?> recordByServerId(int serverId) async {
    final rows = await _db.query(
      'records',
      where: 'server_id = ?',
      whereArgs: [serverId],
    );
    return rows.isEmpty ? null : _record(rows.first);
  }

  // --------------------------------------------------------------- payout

  @override
  Future<void> savePayout(Payout payout) async {
    await _db.transaction((txn) async {
      await txn.delete(
        'payout_lines',
        where: 'site_id = ? AND date = ?',
        whereArgs: [payout.siteId, payout.date],
      );
      final batch = txn.batch();
      batch.insert('payout_sheets', {
        'site_id': payout.siteId,
        'date': payout.date,
        'json': jsonEncode(payout.toJson(withLines: false)),
      }, conflictAlgorithm: ConflictAlgorithm.replace);
      for (var i = 0; i < payout.lines.length; i++) {
        final l = payout.lines[i];
        batch.insert('payout_lines', {
          'site_id': payout.siteId,
          'date': payout.date,
          'pay_line_id': l.payLineId,
          'sort': i,
          'json': jsonEncode(l.toJson()),
        });
      }
      await batch.commit(noResult: true);
    });
    _changed();
  }

  @override
  Future<Payout?> loadPayout(int siteId, String date) async {
    final head = await _db.query(
      'payout_sheets',
      where: 'site_id = ? AND date = ?',
      whereArgs: [siteId, date],
    );
    if (head.isEmpty) return null;
    final lines = await _db.query(
      'payout_lines',
      where: 'site_id = ? AND date = ?',
      whereArgs: [siteId, date],
      orderBy: 'sort',
    );
    final json =
        jsonDecode(head.first['json'] as String) as Map<String, dynamic>
          ..['lines'] = [
            for (final l in lines) jsonDecode(l['json'] as String),
          ];
    return Payout.fromJson(json);
  }

  @override
  Future<void> upsertPayment(LocalPayment payment) async {
    await _db.insert('payments', {
      'client_uuid': payment.clientUuid,
      'site_id': payment.siteId,
      'date': payment.date,
      'pay_line_id': payment.payLineId,
      'worker_id': payment.workerId,
      'amount': payment.amount,
      'proof_type': payment.proofType.wire,
      'proof_path': payment.proofPath,
      'paid_at': payment.paidAt,
      'server_id': payment.serverId,
    }, conflictAlgorithm: ConflictAlgorithm.replace);
    _changed();
  }

  @override
  Future<List<LocalPayment>> loadPayments(int siteId, String date) async {
    final rows = await _db.query(
      'payments',
      where: 'site_id = ? AND date = ?',
      whereArgs: [siteId, date],
    );
    return [
      for (final r in rows)
        LocalPayment(
          clientUuid: r['client_uuid'] as String,
          siteId: r['site_id'] as int,
          date: r['date'] as String,
          payLineId: r['pay_line_id'] as int,
          workerId: r['worker_id'] as int,
          amount: r['amount'] as int,
          proofType: ProofType.fromWire(r['proof_type'] as String),
          proofPath: r['proof_path'] as String?,
          paidAt: r['paid_at'] as String,
          serverId: r['server_id'] as int?,
        ),
    ];
  }

  // ------------------------------------------------------------- checkins

  @override
  Future<void> upsertCheckIn(LocalCheckIn checkIn) async {
    await _db.insert('checkins', {
      'client_uuid': checkIn.clientUuid,
      'site_id': checkIn.siteId,
      'date': checkIn.date,
      'at': checkIn.at,
      'json': jsonEncode(checkIn.toJson()),
    }, conflictAlgorithm: ConflictAlgorithm.replace);
    _changed();
  }

  @override
  Future<List<LocalCheckIn>> loadCheckIns(int siteId, String date) async {
    final rows = await _db.query(
      'checkins',
      where: 'site_id = ? AND date = ?',
      whereArgs: [siteId, date],
    );
    return [
      for (final r in rows)
        LocalCheckIn.fromJson(
          jsonDecode(r['json'] as String) as Map<String, dynamic>,
        ),
    ]..sort((a, b) => a.instant.compareTo(b.instant));
  }

  // --------------------------------------------------------------- outbox

  Map<String, Object?> _outboxRow(OutboxEntry e) => {
    'kind': e.kind.wire,
    'site_id': e.siteId,
    'date': e.date,
    'client_uuid': e.clientUuid,
    'payload': jsonEncode(e.payload),
    'created_at': e.createdAt,
    'version': e.version,
    'attempts': e.attempts,
    'state': e.state.wire,
    'error_code': e.errorCode,
    'error_message': e.errorMessage,
    'next_attempt_at': e.nextAttemptAt?.toIso8601String(),
  };

  @override
  Future<int> insertOutbox(OutboxEntry entry) async {
    final id = await _db.insert('outbox', _outboxRow(entry));
    _changed();
    return id;
  }

  @override
  Future<void> updateOutbox(OutboxEntry entry) async {
    await _db.update(
      'outbox',
      _outboxRow(entry),
      where: 'id = ?',
      whereArgs: [entry.id],
    );
    _changed();
  }

  @override
  Future<bool> deleteOutbox(int id, {int? ifVersion}) async {
    final n = ifVersion == null
        ? await _db.delete('outbox', where: 'id = ?', whereArgs: [id])
        : await _db.delete(
            'outbox',
            where: 'id = ? AND version = ?',
            whereArgs: [id, ifVersion],
          );
    if (n > 0) _changed();
    return n > 0;
  }

  @override
  Future<List<OutboxEntry>> loadOutbox() async {
    final rows = await _db.query('outbox', orderBy: 'id');
    return [
      for (final r in rows)
        OutboxEntry(
          id: r['id'] as int,
          kind: OutboxKind.fromWire(r['kind'] as String),
          siteId: r['site_id'] as int,
          date: r['date'] as String,
          clientUuid: r['client_uuid'] as String,
          payload: jsonDecode(r['payload'] as String) as Map<String, dynamic>,
          createdAt: r['created_at'] as String,
          version: r['version'] as int,
          attempts: r['attempts'] as int,
          state: OutboxState.fromWire(r['state'] as String),
          errorCode: r['error_code'] as String?,
          errorMessage: r['error_message'] as String?,
          nextAttemptAt: r['next_attempt_at'] == null
              ? null
              : DateTime.parse(r['next_attempt_at'] as String),
        ),
    ];
  }

  // --------------------------------------------------------- observations
  // No `_changed()`: observations do not concern the screens.

  @override
  Future<void> addObservations(Iterable<QueuedObservation> items) async {
    final batch = _db.batch();
    for (final o in items) {
      batch.insert('observations', {
        'client_uuid': o.clientUuid,
        'owner': o.owner,
        'at': o.at,
        'json': jsonEncode(o.json),
      }, conflictAlgorithm: ConflictAlgorithm.ignore);
    }
    await batch.commit(noResult: true);
  }

  @override
  Future<List<QueuedObservation>> nextObservations(
    String owner,
    int limit,
  ) async {
    final rows = await _db.query(
      'observations',
      where: 'owner = ?',
      whereArgs: [owner],
      orderBy: 'id',
      limit: limit,
    );
    return [
      for (final r in rows)
        QueuedObservation(
          clientUuid: r['client_uuid'] as String,
          owner: r['owner'] as String,
          at: r['at'] as String,
          json: jsonDecode(r['json'] as String) as Map<String, dynamic>,
        ),
    ];
  }

  @override
  Future<void> removeObservations(Iterable<String> clientUuids) async {
    final ids = clientUuids.toList();
    // SQLite allows 999 parameters per statement.
    for (var i = 0; i < ids.length; i += 500) {
      final chunk = ids.sublist(i, i + 500 > ids.length ? ids.length : i + 500);
      await _db.delete(
        'observations',
        where: 'client_uuid IN (${List.filled(chunk.length, '?').join(',')})',
        whereArgs: chunk,
      );
    }
  }

  @override
  Future<int> countObservations([String? owner]) async {
    final rows = owner == null
        ? await _db.rawQuery('SELECT COUNT(*) AS n FROM observations')
        : await _db.rawQuery(
            'SELECT COUNT(*) AS n FROM observations WHERE owner = ?',
            [owner],
          );
    return rows.first['n'] as int;
  }

  @override
  Future<int> pruneObservations({
    required String before,
    int? keepAtMost,
  }) async {
    var n = await _db.delete(
      'observations',
      where: 'at < ?',
      whereArgs: [before],
    );
    if (keepAtMost != null) {
      n += await _db.rawDelete(
        'DELETE FROM observations WHERE id NOT IN '
        '(SELECT id FROM observations ORDER BY id DESC LIMIT ?)',
        [keepAtMost],
      );
    }
    return n;
  }

  @override
  Future<void> clearObservations(String owner) async {
    await _db.delete('observations', where: 'owner = ?', whereArgs: [owner]);
  }

  @override
  Future<void> close() async {
    await _changes.close();
    await _db.close();
  }
}
