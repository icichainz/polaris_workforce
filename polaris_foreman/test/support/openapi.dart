// Reads docs/api/openapi.yaml: examples per operation, and a small JSON
// schema checker (the subset the contract uses: $ref, allOf, nullable, type,
// required, properties, items, enum, pattern, format, minimum/maximum).
import 'dart:convert';
import 'dart:io';

import 'package:yaml/yaml.dart';

final specFile = File('../docs/api/openapi.yaml');

/// Null when the spec is there; a skip reason otherwise.
final String? specSkip = specFile.existsSync()
    ? null
    : 'docs/api/openapi.yaml not found';

Map<String, dynamic> _plain(Object yaml) =>
    jsonDecode(jsonEncode(yaml)) as Map<String, dynamic>;

class OpenApi {
  OpenApi._(this.doc);

  factory OpenApi.load() =>
      OpenApi._(_plain(loadYaml(specFile.readAsStringSync()) as Object));

  final Map<String, dynamic> doc;

  String get version => (doc['info'] as Map)['version'] as String;

  Map<String, dynamic> get paths => doc['paths'] as Map<String, dynamic>;

  Map<String, dynamic> get schemas =>
      (doc['components'] as Map)['schemas'] as Map<String, dynamic>;

  /// [path] without the `/api/v1` prefix, e.g. `/sites/{site_id}/crew/`.
  Map<String, dynamic> op(String path, String method) =>
      (paths['/api/v1$path'] as Map<String, dynamic>)[method]
          as Map<String, dynamic>;

  Map<String, dynamic>? _response(String path, String method, String status) =>
      (op(path, method)['responses'] as Map)[status] as Map<String, dynamic>?;

  Map<String, dynamic> _examples(Map<String, dynamic> content) =>
      ((content['application/json'] as Map)['examples'] as Map?)
          ?.cast<String, dynamic>() ??
      const {};

  /// The `value` of a response example.
  Map<String, dynamic> response(
    String path,
    String method,
    String status,
    String example,
  ) {
    final content = _response(path, method, status)!['content'] as Map;
    return (_examples(content.cast())[example] as Map)['value']
        as Map<String, dynamic>;
  }

  /// Every example of a response: name → value.
  Map<String, Map<String, dynamic>> responses(
    String path,
    String method,
    String status,
  ) {
    final content = _response(path, method, status)!['content'] as Map;
    return {
      for (final e in _examples(content.cast()).entries)
        e.key: (e.value as Map)['value'] as Map<String, dynamic>,
    };
  }

  /// The `value` of a JSON request example.
  Map<String, dynamic> request(String path, String method, String example) {
    final content = (op(path, method)['requestBody'] as Map)['content'] as Map;
    return (_examples(content.cast())[example] as Map)['value']
        as Map<String, dynamic>;
  }

  /// The JSON schema of a response (null for a response without a body).
  Map<String, dynamic>? responseSchema(
    String path,
    String method,
    String status,
  ) {
    final r = _response(path, method, status);
    final content = r?['content'] as Map?;
    if (content == null) return null;
    return (content['application/json'] as Map)['schema']
        as Map<String, dynamic>;
  }

  /// Statuses the contract lists for an operation.
  Set<String> statuses(String path, String method) =>
      (op(path, method)['responses'] as Map).keys.cast<String>().toSet();

  /// Problems with [value] against [schema] (empty: valid).
  List<String> check(Object? value, Map<String, dynamic> schema) {
    final errors = <String>[];
    _check(value, schema, r'$', errors);
    return errors;
  }

  /// Problems with [value] against `#/components/schemas/[name]`.
  List<String> checkSchema(Object? value, String name) =>
      check(value, {r'$ref': '#/components/schemas/$name'});

  Map<String, dynamic> _resolve(Map<String, dynamic> schema) {
    var s = schema;
    while (s[r'$ref'] is String) {
      final ref = s[r'$ref'] as String;
      s = schemas[ref.split('/').last] as Map<String, dynamic>;
    }
    return s;
  }

  static final _date = RegExp(r'^\d{4}-\d{2}-\d{2}$');
  static final _uuid = RegExp(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
  );

  void _check(Object? v, Map<String, dynamic> raw, String at, List<String> e) {
    final nullable = raw['nullable'] == true;
    final s = _resolve(raw);
    if (v == null) {
      if (!nullable && s['nullable'] != true) e.add('$at: null');
      return;
    }
    final all = s['allOf'] as List?;
    if (all != null) {
      for (final sub in all) {
        _check(v, (sub as Map).cast(), at, e);
      }
    }
    final en = s['enum'] as List?;
    if (en != null && !en.contains(v)) e.add('$at: $v not in $en');
    switch (s['type']) {
      case 'object':
        if (v is! Map) return e.add('$at: not an object');
        for (final r in (s['required'] as List?) ?? const []) {
          if (!v.containsKey(r)) e.add('$at: missing $r');
        }
        final props = (s['properties'] as Map?)?.cast<String, dynamic>();
        if (props == null) return;
        for (final entry in v.entries) {
          final p = props[entry.key];
          if (p == null) {
            if (s['additionalProperties'] == null) {
              e.add('$at: unexpected ${entry.key}');
            }
            continue;
          }
          _check(entry.value, (p as Map).cast(), '$at.${entry.key}', e);
        }
      case 'array':
        if (v is! List) return e.add('$at: not an array');
        final items = (s['items'] as Map?)?.cast<String, dynamic>();
        if (items == null) return;
        for (var i = 0; i < v.length; i++) {
          _check(v[i], items, '$at[$i]', e);
        }
      case 'integer':
        if (v is! int) return e.add('$at: not an integer ($v)');
        final min = s['minimum'] as num?, max = s['maximum'] as num?;
        if (min != null && v < min) e.add('$at: $v < $min');
        if (max != null && v > max) e.add('$at: $v > $max');
      case 'number':
        if (v is! num) e.add('$at: not a number');
      case 'boolean':
        if (v is! bool) e.add('$at: not a boolean');
      case 'string':
        if (v is! String) return e.add('$at: not a string ($v)');
        final pattern = s['pattern'] as String?;
        if (pattern != null && !RegExp(pattern).hasMatch(v)) {
          e.add('$at: "$v" does not match $pattern');
        }
        switch (s['format']) {
          case 'date':
            if (!_date.hasMatch(v)) e.add('$at: "$v" is not a date');
          case 'date-time':
            if (DateTime.tryParse(v) == null || !v.contains('T')) {
              e.add('$at: "$v" is not a date-time');
            }
          case 'uuid':
            if (!_uuid.hasMatch(v)) e.add('$at: "$v" is not a uuid');
          case 'uri':
            if (Uri.tryParse(v)?.hasScheme != true) {
              e.add('$at: "$v" is not a uri');
            }
        }
    }
  }
}
