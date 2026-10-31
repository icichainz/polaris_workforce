import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

import '../data/models/models.dart';
import '../data/repositories/repositories.dart';

/// A cached file and the `ETag` it came with.
class CachedPhoto {
  const CachedPhoto(this.bytes, this.etag);
  final Uint8List bytes;
  final String? etag;
}

/// Where cached photos live (files on the phone; memory in tests).
abstract interface class PhotoStorage {
  Future<CachedPhoto?> read(String url);
  Future<void> write(String url, CachedPhoto photo);
  Future<void> delete(String url);
}

/// Worker photos (`photo_url`) and check-in photos are protected downloads:
/// fetched with the `Authorization` header (see [FilesRepository]), kept on
/// the phone and revalidated with `If-None-Match` (a `304` costs no
/// download; the network may be ~15 KB/s).
///
/// * A cached copy is shown at once; it is revalidated once per
///   [revalidateAfter] (and once per app run).
/// * `404` (no photo any more, or not your site): the copy is dropped and
///   the initials are shown.
/// * Offline, `401` or any other error: the cached copy (or nothing).
class PhotoCache {
  PhotoCache(
    this.files,
    this.storage, {
    this.revalidateAfter = const Duration(hours: 12),
    DateTime Function()? now,
  }) : _now = now ?? DateTime.now;

  final FilesRepository files;
  final PhotoStorage storage;
  final Duration revalidateAfter;
  final DateTime Function() _now;

  final Map<String, DateTime> _checked = {};
  final Map<String, Future<Uint8List?>> _loading = {};

  /// The photo's bytes, or null (show the initials).
  Future<Uint8List?> load(String url) =>
      _loading[url] ??= _load(url).whenComplete(() {
        // A block: `remove` returns this very future, which must not be
        // awaited by its own callback.
        _loading.remove(url);
      });

  Future<Uint8List?> _load(String url) async {
    final cached = await storage.read(url);
    final checked = _checked[url];
    if (cached != null &&
        checked != null &&
        _now().difference(checked) < revalidateAfter) {
      return cached.bytes;
    }
    try {
      final r = await files.download(url, etag: cached?.etag);
      _checked[url] = _now();
      if (r.notModified && cached != null) return cached.bytes;
      final bytes = r.bytes;
      if (bytes == null || bytes.isEmpty) return cached?.bytes;
      final fresh = CachedPhoto(Uint8List.fromList(bytes), r.etag);
      await storage.write(url, fresh);
      return fresh.bytes;
    } on ApiException catch (e) {
      if (e.statusCode == 404) {
        _checked[url] = _now();
        await storage.delete(url);
        return null;
      }
      return cached?.bytes;
    } on NetworkException {
      return cached?.bytes;
    }
  }
}

/// Photos as files in the app's support directory: `<sha256(url)>.img` and
/// `<sha256(url)>.etag`.
class DiskPhotoStorage implements PhotoStorage {
  DiskPhotoStorage({Future<Directory> Function()? directory})
    : _directory = directory ?? _defaultDirectory;

  final Future<Directory> Function() _directory;
  Directory? _dir;

  static Future<Directory> _defaultDirectory() async {
    final base = await getApplicationSupportDirectory();
    return Directory(p.join(base.path, 'photos'));
  }

  Future<String> _base(String url) async {
    final dir = _dir ??= await _directory();
    if (!dir.existsSync()) await dir.create(recursive: true);
    return p.join(dir.path, sha256.convert(utf8.encode(url)).toString());
  }

  @override
  Future<CachedPhoto?> read(String url) async {
    try {
      final base = await _base(url);
      final img = File('$base.img');
      if (!img.existsSync()) return null;
      final tag = File('$base.etag');
      return CachedPhoto(
        await img.readAsBytes(),
        tag.existsSync() ? await tag.readAsString() : null,
      );
    } on IOException {
      return null;
    }
  }

  @override
  Future<void> write(String url, CachedPhoto photo) async {
    try {
      final base = await _base(url);
      await File('$base.img').writeAsBytes(photo.bytes, flush: true);
      final tag = File('$base.etag');
      if (photo.etag == null) {
        if (tag.existsSync()) await tag.delete();
      } else {
        await tag.writeAsString(photo.etag!, flush: true);
      }
    } on IOException {
      // A full disk only costs a download next time.
    }
  }

  @override
  Future<void> delete(String url) async {
    try {
      final base = await _base(url);
      for (final f in [File('$base.img'), File('$base.etag')]) {
        if (f.existsSync()) await f.delete();
      }
    } on IOException {
      // Nothing to do.
    }
  }
}

class MemoryPhotoStorage implements PhotoStorage {
  final Map<String, CachedPhoto> photos = {};

  @override
  Future<CachedPhoto?> read(String url) async => photos[url];

  @override
  Future<void> write(String url, CachedPhoto photo) async =>
      photos[url] = photo;

  @override
  Future<void> delete(String url) async => photos.remove(url);
}
