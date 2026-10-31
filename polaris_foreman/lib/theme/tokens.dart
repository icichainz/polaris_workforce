import 'package:flutter/material.dart';

import '../data/models/enums.dart';

/// Design tokens from `design/project/Main.dc.html` / `Components.dc.html`.
abstract final class PColors {
  static const ink = Color(0xFF16191D);
  static const ink700 = Color(0xFF3F4650);
  static const steel = Color(0xFF5B626E);
  static const line = Color(0xFFE3E5E9);
  static const ground = Color(0xFFF4F5F7);
  static const white = Color(0xFFFFFFFF);
  static const amber = Color(0xFFF5B000);
  static const amberText = Color(0xFF8A5A00);
  static const amberTint = Color(0xFFFFF4DB);
  static const blue = Color(0xFF1A56C4);

  // Dark surfaces of the PIN screen and dark headers.
  static const inkRaised = Color(0xFF2A3038);
  static const inkAvatar = Color(0xFF2E343C);
  static const onInkMuted = Color(0xFFC9CDD3);
  static const onInkDisabled = Color(0xFFA3A9B2);

  // Neutral fills.
  static const neutralPill = Color(0xFFEEF0F3);
  static const tabTrack = Color(0xFFE9EBEF);
  static const border = Color(0xFFC9CED6);

  // Status pairs (background / foreground).
  static const presentBg = Color(0xFFDCF2E4);
  static const presentFg = Color(0xFF135C33);
  static const absentBg = Color(0xFFFBE1DE);
  static const absentFg = Color(0xFF9B1C13);
  static const halfBg = Color(0xFFFDEBD2);
  static const halfFg = Color(0xFF8A4A06);
  static const lateBg = Color(0xFFDDE8F8);
  static const lateFg = Color(0xFF1A4C99);
}

/// Background/foreground pair of a status.
class StatusColors {
  const StatusColors(this.bg, this.fg);
  final Color bg;
  final Color fg;

  static const present = StatusColors(PColors.presentBg, PColors.presentFg);
  static const absent = StatusColors(PColors.absentBg, PColors.absentFg);
  static const half = StatusColors(PColors.halfBg, PColors.halfFg);
  static const late = StatusColors(PColors.lateBg, PColors.lateFg);
  static const neutral = StatusColors(PColors.neutralPill, PColors.ink700);
  static const dark = StatusColors(PColors.ink, PColors.white);

  static StatusColors of(AttendanceStatus s) => switch (s) {
    AttendanceStatus.present => present,
    AttendanceStatus.absent => absent,
    AttendanceStatus.halfDay => half,
    AttendanceStatus.late => late,
  };
}

abstract final class PSizes {
  /// Primary buttons: one thumb, gloves on.
  static const primaryButton = 56.0;
  static const statusButton = 52.0;
  static const radius = 12.0;
  static const cardRadius = 14.0;
}
