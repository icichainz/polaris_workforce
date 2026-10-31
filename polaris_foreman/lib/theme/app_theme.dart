import 'package:flutter/material.dart';

import 'tokens.dart';

// TODO(design): bundle Archivo (headings, 800) and Inter (body) as assets and
// set them here; the platform font is used until then (no network fonts).
const headingWeight = FontWeight.w800;

ThemeData buildTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: PColors.amber,
    primary: PColors.amber,
    onPrimary: PColors.ink,
    secondary: PColors.ink,
    onSecondary: PColors.white,
    surface: PColors.white,
    onSurface: PColors.ink,
    error: PColors.absentFg,
  );
  final base = ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: PColors.ground,
    dividerColor: PColors.line,
  );
  final text = base.textTheme.apply(
    bodyColor: PColors.ink,
    displayColor: PColors.ink,
  );
  final buttonShape = RoundedRectangleBorder(
    borderRadius: BorderRadius.circular(PSizes.radius),
  );
  return base.copyWith(
    textTheme: text.copyWith(
      headlineSmall: text.headlineSmall?.copyWith(
        fontWeight: headingWeight,
        fontSize: 26,
      ),
      titleLarge: text.titleLarge?.copyWith(
        fontWeight: headingWeight,
        fontSize: 20,
      ),
      bodyLarge: text.bodyLarge?.copyWith(fontSize: 16),
      bodyMedium: text.bodyMedium?.copyWith(fontSize: 15),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        minimumSize: const Size.fromHeight(PSizes.primaryButton),
        backgroundColor: PColors.amber,
        foregroundColor: PColors.ink,
        disabledBackgroundColor: PColors.neutralPill,
        disabledForegroundColor: PColors.steel,
        textStyle: const TextStyle(fontSize: 17, fontWeight: FontWeight.w700),
        shape: buttonShape,
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        minimumSize: const Size.fromHeight(52),
        foregroundColor: PColors.ink,
        side: const BorderSide(color: PColors.ink, width: 2),
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
        shape: buttonShape,
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: PColors.blue,
        textStyle: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: PColors.white,
      contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: PColors.border),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: PColors.border),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: PColors.ink, width: 2),
      ),
    ),
    appBarTheme: const AppBarTheme(
      backgroundColor: PColors.white,
      foregroundColor: PColors.ink,
      elevation: 0,
      scrolledUnderElevation: 0,
    ),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: PColors.ink,
      contentTextStyle: TextStyle(color: PColors.white, fontSize: 15),
      behavior: SnackBarBehavior.floating,
    ),
  );
}
