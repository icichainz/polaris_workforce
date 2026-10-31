import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import '../theme/tokens.dart';
import 'widgets.dart';

/// Strokes drawn by a finger (signature) or a thumb (thumb mark).
class SignatureController extends ChangeNotifier {
  final List<List<Offset>> strokes = [];

  bool get isEmpty => strokes.every((s) => s.isEmpty);

  void start(Offset p) {
    strokes.add([p]);
    notifyListeners();
  }

  void extend(Offset p) {
    if (strokes.isEmpty) strokes.add([]);
    strokes.last.add(p);
    notifyListeners();
  }

  void clear() {
    strokes.clear();
    notifyListeners();
  }
}

class SignaturePainter extends CustomPainter {
  SignaturePainter(this.controller, {this.strokeWidth = 3, this.background})
    : super(repaint: controller);

  final SignatureController controller;
  final double strokeWidth;
  final Color? background;

  @override
  void paint(Canvas canvas, Size size) {
    if (background != null) {
      canvas.drawRect(Offset.zero & size, Paint()..color = background!);
    }
    final paint = Paint()
      ..color = PColors.ink
      ..strokeWidth = strokeWidth
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round
      ..style = PaintingStyle.stroke;
    for (final s in controller.strokes) {
      if (s.length == 1) {
        canvas.drawCircle(
          s.first,
          strokeWidth / 2,
          paint..style = PaintingStyle.fill,
        );
        paint.style = PaintingStyle.stroke;
        continue;
      }
      final path = Path()..moveTo(s.first.dx, s.first.dy);
      for (final p in s.skip(1)) {
        path.lineTo(p.dx, p.dy);
      }
      canvas.drawPath(path, paint);
    }
  }

  @override
  bool shouldRepaint(SignaturePainter old) =>
      old.strokeWidth != strokeWidth || old.controller != controller;
}

/// Renders the strokes to a PNG with a white background.
Future<Uint8List> exportSignaturePng(
  SignatureController controller,
  Size size, {
  double strokeWidth = 3,
  double pixelRatio = 2,
}) async {
  final recorder = ui.PictureRecorder();
  final canvas = Canvas(recorder)..scale(pixelRatio);
  SignaturePainter(
    controller,
    strokeWidth: strokeWidth,
    background: PColors.white,
  ).paint(canvas, size);
  final picture = recorder.endRecording();
  final image = await picture.toImage(
    (size.width * pixelRatio).round(),
    (size.height * pixelRatio).round(),
  );
  final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
  image.dispose();
  picture.dispose();
  return bytes!.buffer.asUint8List();
}

/// A bottom sheet to collect a signature or a thumb mark. Returns PNG bytes.
Future<Uint8List?> showSignatureSheet(
  BuildContext context, {
  required bool thumb,
}) {
  return showModalBottomSheet<Uint8List>(
    context: context,
    isScrollControlled: true,
    enableDrag: false,
    showDragHandle: true,
    builder: (context) => _SignatureSheet(thumb: thumb),
  );
}

class _SignatureSheet extends StatefulWidget {
  const _SignatureSheet({required this.thumb});
  final bool thumb;

  @override
  State<_SignatureSheet> createState() => _SignatureSheetState();
}

class _SignatureSheetState extends State<_SignatureSheet> {
  final _controller = SignatureController();
  Size _size = const Size(340, 260);

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  double get _width => widget.thumb ? 26 : 3.5;

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              widget.thumb ? l.thumbHere : l.signHere,
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 12),
            LayoutBuilder(
              builder: (context, c) {
                _size = Size(c.maxWidth, 260);
                return Container(
                  height: 260,
                  decoration: BoxDecoration(
                    color: PColors.white,
                    border: Border.all(color: PColors.border, width: 2),
                    borderRadius: BorderRadius.circular(PSizes.radius),
                  ),
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(PSizes.radius),
                    child: GestureDetector(
                      key: const Key('signaturePad'),
                      onPanStart: (d) => _controller.start(d.localPosition),
                      onPanUpdate: (d) => _controller.extend(d.localPosition),
                      onTapDown: (d) => _controller.start(d.localPosition),
                      child: CustomPaint(
                        painter: SignaturePainter(
                          _controller,
                          strokeWidth: _width,
                        ),
                        size: Size.infinite,
                      ),
                    ),
                  ),
                );
              },
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton(
                    onPressed: _controller.clear,
                    child: Text(l.clear),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: ListenableBuilder(
                    listenable: _controller,
                    builder: (context, _) => FilledButton(
                      key: const Key('signatureDone'),
                      onPressed: _controller.isEmpty
                          ? null
                          : () async {
                              final png = await exportSignaturePng(
                                _controller,
                                _size,
                                strokeWidth: _width,
                              );
                              if (context.mounted) Navigator.pop(context, png);
                            },
                      child: Text(l.done),
                    ),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
