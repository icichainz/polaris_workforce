import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Where the device token lives. It is shown only once by the server.
///
/// The install's device id (`device_id` of `POST /auth/pin/`) lives next to
/// it: made once per install, kept across sign-outs ([clear] leaves it).
abstract interface class TokenStore {
  Future<String?> read();
  Future<void> write(String token);
  Future<void> clear();

  Future<String?> readDeviceId();
  Future<void> writeDeviceId(String id);

  /// A salted hash of the signed-in foreman's PIN, so a kiosk phone can be
  /// unlocked offline ([clear] leaves it; a new sign-in replaces it).
  Future<String?> readPinCheck();
  Future<void> writePinCheck(String? value);
}

class SecureTokenStore implements TokenStore {
  SecureTokenStore([FlutterSecureStorage? storage])
    : _storage = storage ?? const FlutterSecureStorage();

  static const _key = 'device_token';
  static const _deviceIdKey = 'device_id';
  static const _pinCheckKey = 'pin_check';
  final FlutterSecureStorage _storage;
  String? _cached;
  bool _loaded = false;

  @override
  Future<String?> read() async {
    if (!_loaded) {
      _cached = await _storage.read(key: _key);
      _loaded = true;
    }
    return _cached;
  }

  @override
  Future<void> write(String token) async {
    _cached = token;
    _loaded = true;
    await _storage.write(key: _key, value: token);
  }

  @override
  Future<void> clear() async {
    _cached = null;
    _loaded = true;
    await _storage.delete(key: _key);
  }

  @override
  Future<String?> readDeviceId() => _storage.read(key: _deviceIdKey);

  @override
  Future<void> writeDeviceId(String id) =>
      _storage.write(key: _deviceIdKey, value: id);

  @override
  Future<String?> readPinCheck() => _storage.read(key: _pinCheckKey);

  @override
  Future<void> writePinCheck(String? value) => value == null
      ? _storage.delete(key: _pinCheckKey)
      : _storage.write(key: _pinCheckKey, value: value);
}

class MemoryTokenStore implements TokenStore {
  MemoryTokenStore([this._token]);
  String? _token;
  String? _deviceId;
  String? _pinCheck;

  @override
  Future<String?> readPinCheck() async => _pinCheck;

  @override
  Future<void> writePinCheck(String? value) async => _pinCheck = value;

  @override
  Future<String?> readDeviceId() async => _deviceId;

  @override
  Future<void> writeDeviceId(String id) async => _deviceId = id;

  @override
  Future<String?> read() async => _token;

  @override
  Future<void> write(String token) async => _token = token;

  @override
  Future<void> clear() async => _token = null;
}
