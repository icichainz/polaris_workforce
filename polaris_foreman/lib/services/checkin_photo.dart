import 'dart:io';

import 'package:image_picker/image_picker.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:uuid/uuid.dart';

/// Takes the optional photo of a check-in. It is uploaded with the check-in
/// from the outbox (multipart), possibly hours later, then deleted.
abstract interface class CheckInPhotoCapture {
  /// Opens the camera; returns the saved JPEG path, or null if cancelled.
  Future<String?> take();
}

class DeviceCheckInPhotoCapture implements CheckInPhotoCapture {
  DeviceCheckInPhotoCapture({ImagePicker? picker})
    : _picker = picker ?? ImagePicker();

  final ImagePicker _picker;

  /// Longest side, in pixels (README: compress on the phone, ≤ 1280 px).
  static const maxSide = 1280.0;

  /// JPEG quality: a few hundred KB at most at 1280 px.
  static const quality = 60;

  @override
  Future<String?> take() async {
    // The worker faces the phone: the front camera.
    final shot = await _picker.pickImage(
      source: ImageSource.camera,
      maxWidth: maxSide,
      maxHeight: maxSide,
      imageQuality: quality,
      preferredCameraDevice: CameraDevice.front,
    );
    if (shot == null) return null;
    final base = await getApplicationDocumentsDirectory();
    final dir = Directory(p.join(base.path, 'checkins'));
    if (!dir.existsSync()) await dir.create(recursive: true);
    final target = p.join(dir.path, '${const Uuid().v4()}.jpg');
    await File(shot.path).copy(target);
    return target;
  }
}
