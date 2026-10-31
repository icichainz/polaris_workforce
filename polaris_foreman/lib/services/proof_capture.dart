import 'dart:io';
import 'dart:typed_data';

import 'package:image_picker/image_picker.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:uuid/uuid.dart';

/// Takes and stores proof-of-payment files on the phone. They are uploaded
/// with the payment from the outbox, possibly hours later.
abstract interface class ProofCapture {
  /// Opens the camera; returns the saved JPEG path, or null if cancelled.
  Future<String?> takePhoto();

  /// Saves a drawn signature or thumb mark (PNG bytes).
  Future<String> savePng(Uint8List bytes);
}

class DeviceProofCapture implements ProofCapture {
  DeviceProofCapture({ImagePicker? picker}) : _picker = picker ?? ImagePicker();

  final ImagePicker _picker;

  Future<Directory> _dir() async {
    final base = await getApplicationDocumentsDirectory();
    final dir = Directory(p.join(base.path, 'proofs'));
    if (!dir.existsSync()) await dir.create(recursive: true);
    return dir;
  }

  @override
  Future<String?> takePhoto() async {
    // Compressed on the phone: JPEG, longest side ≤ 1280 px (README).
    final shot = await _picker.pickImage(
      source: ImageSource.camera,
      maxWidth: 1280,
      maxHeight: 1280,
      imageQuality: 70,
      preferredCameraDevice: CameraDevice.rear,
    );
    if (shot == null) return null;
    final target = p.join((await _dir()).path, '${const Uuid().v4()}.jpg');
    await File(shot.path).copy(target);
    return target;
  }

  @override
  Future<String> savePng(Uint8List bytes) async {
    final target = p.join((await _dir()).path, '${const Uuid().v4()}.png');
    await File(target).writeAsBytes(bytes, flush: true);
    return target;
  }
}
