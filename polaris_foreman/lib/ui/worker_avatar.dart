import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../state/providers.dart';
import 'widgets.dart';

/// A worker's photo (protected `photo_url`, fetched with the device token
/// and cached on the phone), or their initials while there is none.
class WorkerAvatar extends ConsumerWidget {
  const WorkerAvatar({
    super.key,
    required this.name,
    required this.photoUrl,
    this.size = 44,
    this.fontSize,
  });

  final String name;
  final String? photoUrl;
  final double size;
  final double? fontSize;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final initials = InitialsAvatar(name, size: size, fontSize: fontSize);
    final url = photoUrl;
    if (url == null) return initials;
    final bytes = ref.watch(photoProvider(url)).valueOrNull;
    if (bytes == null) return initials;
    return Semantics(
      label: name,
      image: true,
      child: ClipOval(
        child: Image.memory(
          bytes,
          width: size,
          height: size,
          fit: BoxFit.cover,
          gaplessPlayback: true,
          cacheWidth: (size * MediaQuery.devicePixelRatioOf(context)).round(),
          errorBuilder: (_, _, _) => initials,
        ),
      ),
    );
  }
}
