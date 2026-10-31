// Round-trips the example payloads of docs/api/openapi.yaml through the
// models: fromJson(example).toJson() must give the example back unchanged.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:polaris_foreman/data/models/models.dart';
import 'package:yaml/yaml.dart';

final _spec = File('../docs/api/openapi.yaml');

Map<String, dynamic> _plain(Object yaml) =>
    jsonDecode(jsonEncode(yaml)) as Map<String, dynamic>;

late final Map<String, dynamic> _paths;

Map<String, dynamic> _op(String path, String method) =>
    (_paths['/api/v1$path'] as Map<String, dynamic>)[method]
        as Map<String, dynamic>;

Map<String, dynamic> response(
  String path,
  String method,
  String status,
  String example,
) {
  final content =
      ((_op(path, method)['responses'] as Map)[status] as Map)['content']
          as Map;
  final examples = (content['application/json'] as Map)['examples'] as Map;
  return (examples[example] as Map)['value'] as Map<String, dynamic>;
}

Map<String, dynamic> request(String path, String method, String example) {
  final content = (_op(path, method)['requestBody'] as Map)['content'] as Map;
  final examples = (content['application/json'] as Map)['examples'] as Map;
  return (examples[example] as Map)['value'] as Map<String, dynamic>;
}

void roundTrip<T>(
  Map<String, dynamic> json,
  T Function(Map<String, dynamic>) from,
  Map<String, dynamic> Function(T) to,
) {
  expect(to(from(json)), json);
}

void main() {
  final skip = _spec.existsSync() ? null : 'docs/api/openapi.yaml not found';

  setUpAll(() {
    if (skip != null) return;
    final doc = _plain(loadYaml(_spec.readAsStringSync()) as Object);
    _paths = doc['paths'] as Map<String, dynamic>;
  });

  group('responses', skip: skip, () {
    test('PinLoginResponse', () {
      roundTrip(
        response('/auth/pin/', 'post', '200', 'SignedIn'),
        PinLoginResponse.fromJson,
        (v) => v.toJson(),
      );
    });

    test('Me', () {
      final json = response('/me/', 'get', '200', 'Foreman');
      roundTrip(json, Me.fromJson, (v) => v.toJson());
      final me = Me.fromJson(json);
      expect(me.company.currency, Currency.xof);
      expect(me.sites.single.rollCallDue, '08:00');
    });

    test('Crew', () {
      roundTrip(
        response('/sites/{site_id}/crew/', 'get', '200', 'Crew'),
        Crew.fromJson,
        (v) => v.toJson(),
      );
    });

    test('AttendanceDayList', () {
      roundTrip(
        response('/sites/{site_id}/days/', 'get', '200', 'ThisWeek'),
        AttendanceDayList.fromJson,
        (v) => v.toJson(),
      );
    });

    test('AttendanceDay: draft and not_started', () {
      const path = '/sites/{site_id}/days/{date}/';
      roundTrip(
        response(path, 'get', '200', 'Draft'),
        AttendanceDay.fromJson,
        (v) => v.toJson(),
      );
      final notStarted = response(path, 'get', '200', 'NotStarted');
      roundTrip(notStarted, AttendanceDay.fromJson, (v) => v.toJson());
      final day = AttendanceDay.fromJson(notStarted);
      expect(day.summary.id, isNull);
      expect(day.summary.status, DayStatus.notStarted);
    });

    test('records PUT and submit responses', () {
      roundTrip(
        response(
          '/sites/{site_id}/days/{date}/records/',
          'put',
          '200',
          'Saved',
        ),
        AttendanceDay.fromJson,
        (v) => v.toJson(),
      );
      final submitted = response(
        '/sites/{site_id}/days/{date}/submit/',
        'post',
        '200',
        'Submitted',
      );
      roundTrip(submitted, AttendanceDay.fromJson, (v) => v.toJson());
      expect(
        AttendanceDay.fromJson(submitted).records.map((r) => r.status),
        contains(AttendanceStatus.halfDay),
      );
    });

    test('Correction', () {
      roundTrip(
        response('/records/{record_id}/corrections/', 'post', '201', 'Created'),
        Correction.fromJson,
        (v) => v.toJson(),
      );
    });

    test('Payout keeps its invariants', () {
      final json = response(
        '/sites/{site_id}/payouts/{date}/',
        'get',
        '200',
        'OpenSheet',
      );
      roundTrip(json, Payout.fromJson, (v) => v.toJson());
      final p = Payout.fromJson(json);
      expect(p.totalToPay, p.lines.fold<int>(0, (s, l) => s + l.net));
      expect(p.leftToPay, p.totalToPay - p.paidTotal);
      expect(p.cashInHand, p.cashIssued - p.paidTotal);
      final rebuilt = Payout.fromLines(
        siteId: p.siteId,
        date: p.date,
        status: p.status,
        dayStatus: p.dayStatus,
        approvedAt: p.approvedAt,
        currency: p.currency,
        cashIssued: p.cashIssued,
        lines: p.lines,
      );
      expect(rebuilt.toJson(), json);
    });

    test('Payment (201 and replayed 200)', () {
      for (final status in ['201', '200']) {
        roundTrip(
          response('/payments/', 'post', status, 'Paid'),
          Payment.fromJson,
          (v) => v.toJson(),
        );
      }
    });

    test('Sync', () {
      roundTrip(
        response('/sync/', 'get', '200', 'Changes'),
        SyncResponse.fromJson,
        (v) => v.toJson(),
      );
    });

    test('Error bodies', () {
      final e = response('/auth/pin/', 'post', '429', 'Throttled');
      roundTrip(e, ApiError.fromJson, (v) => v.toJson());
      expect(ApiException(429, ApiError.fromJson(e)).retryAfterSeconds, 42);
      final c = response('/payments/', 'post', '409', 'Conflict');
      roundTrip(c, ApiError.fromJson, (v) => v.toJson());
    });
  });

  group('requests', skip: skip, () {
    test('PinLoginRequest', () {
      roundTrip(
        request('/auth/pin/', 'post', 'SignIn'),
        PinLoginRequest.fromJson,
        (v) => v.toJson(),
      );
    });

    test('RecordBatchRequest items', () {
      final json = request(
        '/sites/{site_id}/days/{date}/records/',
        'put',
        'Marks',
      );
      final items = (json['records'] as List).cast<Map<String, dynamic>>();
      for (final item in items) {
        roundTrip(item, RecordUpsert.fromJson, (v) => v.toJson());
      }
    });

    test('SubmitDayRequest', () {
      roundTrip(
        request('/sites/{site_id}/days/{date}/submit/', 'post', 'Submit'),
        SubmitDayRequest.fromJson,
        (v) => v.toJson(),
      );
    });

    test('CorrectionCreateRequest', () {
      roundTrip(
        request('/records/{record_id}/corrections/', 'post', 'CorrectArrival'),
        CorrectionCreateRequest.fromJson,
        (v) => v.toJson(),
      );
    });

    test('PaymentCreateRequest', () {
      roundTrip(
        request('/payments/', 'post', 'PayWithPhotoProof'),
        PaymentCreateRequest.fromJson,
        (v) => v.toJson(),
      );
    });
  });
}
