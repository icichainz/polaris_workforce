import 'dart:convert';
import 'dart:io' show Platform;

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:uuid/uuid.dart';

import '../data/local/local_store.dart';
import '../data/models/models.dart';
import 'providers.dart';

/// The foreman last signed in on this phone (the PIN screen's card).
class RememberedUser {
  const RememberedUser({required this.phone, required this.name, this.site});
  final String phone;
  final String name;
  final String? site;
}

sealed class SessionState {
  const SessionState();
}

class SignedOut extends SessionState {
  const SignedOut({this.remembered, this.expired = false});
  final RememberedUser? remembered;

  /// True after a 401: the token was revoked or expired.
  final bool expired;
}

class SignedIn extends SessionState {
  const SignedIn(this.me);
  final Me me;
}

/// Reads the saved session before the first frame.
Future<SessionState> loadSession(LocalStore store, String? token) async {
  final rawMe = await store.getValue(StoreKeys.me);
  if (token != null && rawMe != null) {
    return SignedIn(Me.fromJson(jsonDecode(rawMe) as Map<String, dynamic>));
  }
  return SignedOut(remembered: await _remembered(store));
}

Future<RememberedUser?> _remembered(LocalStore store) async {
  final phone = await store.getValue(StoreKeys.rememberedPhone);
  final name = await store.getValue(StoreKeys.rememberedName);
  if (phone == null || name == null) return null;
  return RememberedUser(
    phone: phone,
    name: name,
    site: await store.getValue(StoreKeys.rememberedSite),
  );
}

final initialSessionProvider = Provider<SessionState>(
  (ref) => const SignedOut(),
);

class SessionController extends Notifier<SessionState> {
  @override
  SessionState build() => ref.watch(initialSessionProvider);

  LocalStore get _store => ref.read(localStoreProvider);

  /// The install's device id: a UUID made once and kept in secure storage
  /// for as long as the app is installed (one token per device on the
  /// server). An id from an older build (kept in the local database) is
  /// moved over, so the device keeps its identity.
  Future<String> deviceId() async {
    final tokens = ref.read(tokenStoreProvider);
    final saved = await tokens.readDeviceId();
    if (saved != null) return saved;
    final id = await _store.getValue(StoreKeys.deviceId) ?? const Uuid().v4();
    await tokens.writeDeviceId(id);
    return id;
  }

  /// Shown on the office Users screen.
  // TODO(I2): the model name (e.g. 'Tecno Spark 10') needs device_info_plus,
  // a new dependency; until then the platform only.
  static String deviceName() {
    try {
      if (Platform.isAndroid) return 'Android phone';
      if (Platform.isIOS) return 'iPhone';
      return 'Polaris Foreman (${Platform.operatingSystem})';
    } catch (_) {
      return 'Polaris Foreman';
    }
  }

  /// `POST /auth/pin/` then `GET /me/`. Throws [ApiException] /
  /// [NetworkException] for the PIN screen to show.
  Future<void> signIn(String phone, String pin) async {
    final repos = ref.read(repositoriesProvider);
    final tokens = ref.read(tokenStoreProvider);
    final resp = await repos.auth.signIn(
      PinLoginRequest(
        phone: phone,
        pin: pin,
        deviceId: await deviceId(),
        deviceName: deviceName(),
      ),
    );
    await tokens.write(resp.token);
    final me = await repos.auth.me();
    // Writes queued before a logout or a 401 are sent once the same foreman
    // is back (the server accepts them with the new token). Another foreman
    // signing in does not take over a non-empty outbox.
    final owner = await _store.getValue(StoreKeys.outboxOwner);
    if (owner == null || (await _store.loadOutbox()).isEmpty) {
      await _store.setValue(StoreKeys.outboxOwner, '${me.id}');
    }
    await _store.setValue(StoreKeys.me, jsonEncode(me.toJson()));
    await _store.setValue(StoreKeys.rememberedPhone, me.phone);
    await _store.setValue(StoreKeys.rememberedName, me.name);
    await _store.setValue(
      StoreKeys.rememberedSite,
      me.sites.isEmpty ? null : me.sites.first.name,
    );
    // The user's language applies unless the foreman picked one on the phone.
    if (await _store.getValue(StoreKeys.language) == null) {
      ref.read(localeProvider.notifier).set(me.language.wire, persist: false);
    }
    state = SignedIn(me); // syncServiceProvider starts on this change.
  }

  /// Refreshes `GET /me/` when online (sites may change).
  Future<void> refreshMe() async {
    if (state is! SignedIn) return;
    try {
      final me = await ref.read(repositoriesProvider).auth.me();
      await _store.setValue(StoreKeys.me, jsonEncode(me.toJson()));
      state = SignedIn(me);
    } catch (_) {
      // Offline or 401: the sync service handles it.
    }
  }

  /// Signs out on purpose. The outbox stays on the phone.
  Future<void> signOut() async {
    try {
      await ref.read(repositoriesProvider).auth.logout();
    } catch (_) {
      // Offline: the token is dropped locally anyway.
    }
    await ref.read(tokenStoreProvider).clear();
    await _store.setValue(StoreKeys.me, null);
    state = SignedOut(remembered: await _remembered(_store));
  }

  /// 401 from the server (any call): back to the PIN, outbox kept.
  Future<void> expired() async {
    if (state is! SignedIn) return;
    await ref.read(tokenStoreProvider).clear();
    state = SignedOut(remembered: await _remembered(_store), expired: true);
  }

  /// "Not you?": forget the remembered foreman.
  Future<void> forget() async {
    await _store.setValue(StoreKeys.rememberedPhone, null);
    await _store.setValue(StoreKeys.rememberedName, null);
    await _store.setValue(StoreKeys.rememberedSite, null);
    state = const SignedOut();
  }
}

final sessionProvider = NotifierProvider<SessionController, SessionState>(
  SessionController.new,
);
