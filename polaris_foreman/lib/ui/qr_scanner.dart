import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../theme/tokens.dart';
import 'widgets.dart';

/// Builds the camera view that reads QR codes; [onScan] gets each text as
/// read. Tests replace it (no camera there).
typedef ScannerBuilder =
    Widget Function(
      BuildContext context, {
      required ValueChanged<String> onScan,
      required bool frontCamera,
    });

final scannerBuilderProvider = Provider<ScannerBuilder>(
  (ref) =>
      (context, {required onScan, required frontCamera}) => QrScannerView(
        key: ValueKey('camera-$frontCamera'),
        onScan: onScan,
        frontCamera: frontCamera,
      ),
);

/// Drops repeated reads of the same code: the camera sees a badge many
/// times a second while it is held up.
class ScanDebouncer {
  ScanDebouncer({
    this.window = const Duration(seconds: 4),
    DateTime Function()? now,
  }) : _now = now ?? DateTime.now;

  final Duration window;
  final DateTime Function() _now;
  String? _last;
  DateTime? _at;

  /// True when [text] should be recorded.
  bool accept(String text) {
    final now = _now();
    if (text == _last && _at != null && now.difference(_at!) < window) {
      return false;
    }
    _last = text;
    _at = now;
    return true;
  }
}

/// The phone's camera reading QR codes (`mobile_scanner`).
class QrScannerView extends StatefulWidget {
  const QrScannerView({
    super.key,
    required this.onScan,
    this.frontCamera = false,
  });

  final ValueChanged<String> onScan;
  final bool frontCamera;

  @override
  State<QrScannerView> createState() => _QrScannerViewState();
}

class _QrScannerViewState extends State<QrScannerView> {
  late final MobileScannerController _controller = MobileScannerController(
    formats: const [BarcodeFormat.qrCode],
    facing: widget.frontCamera ? CameraFacing.front : CameraFacing.back,
  );

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    return MobileScanner(
      controller: _controller,
      onDetect: (capture) {
        for (final b in capture.barcodes) {
          final text = b.rawValue;
          if (text != null && text.isNotEmpty) widget.onScan(text);
        }
      },
      errorBuilder: (context, error) => Container(
        color: PColors.ink,
        alignment: Alignment.center,
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.no_photography, color: PColors.amber, size: 40),
            const SizedBox(height: 12),
            Text(
              error.errorCode == MobileScannerErrorCode.permissionDenied
                  ? l.cameraDenied
                  : l.cameraError,
              textAlign: TextAlign.center,
              style: const TextStyle(color: PColors.white, fontSize: 16),
            ),
          ],
        ),
      ),
    );
  }
}
