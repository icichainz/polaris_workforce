import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'tracking_models.dart';

/// The tracker token of the signed-in foreman, in secure storage next to
/// the device token, with the user it belongs to. Dropped on sign-out.
abstract interface class TrackingTokenStore {
  /// The saved token of [owner] (a token saved for another user is null).
  Future<TrackingToken?> read(String owner);
  Future<void> write(String owner, TrackingToken token);
  Future<void> clear();
}

class SecureTrackingTokenStore implements TrackingTokenStore {
  SecureTrackingTokenStore([FlutterSecureStorage? storage])
    : _storage = storage ?? const FlutterSecureStorage();

  static const _key = 'tracking_token';
  final FlutterSecureStorage _storage;

  @override
  Future<TrackingToken?> read(String owner) async {
    try {
      final raw = await _storage.read(key: _key);
      if (raw == null) return null;
      final json = jsonDecode(raw) as Map<String, dynamic>;
      if (json['owner'] != owner) return null;
      return TrackingToken.fromJson(json['token'] as Map<String, dynamic>);
    } catch (_) {
      return null; // Unreadable (older format, keystore reset): fetch again.
    }
  }

  @override
  Future<void> write(String owner, TrackingToken token) => _storage.write(
    key: _key,
    value: jsonEncode({'owner': owner, 'token': token.toJson()}),
  );

  @override
  Future<void> clear() => _storage.delete(key: _key);
}

class MemoryTrackingTokenStore implements TrackingTokenStore {
  String? _owner;
  TrackingToken? _token;

  @override
  Future<TrackingToken?> read(String owner) async =>
      owner == _owner ? _token : null;

  @override
  Future<void> write(String owner, TrackingToken token) async {
    _owner = owner;
    _token = token;
  }

  @override
  Future<void> clear() async {
    _owner = null;
    _token = null;
  }
}
