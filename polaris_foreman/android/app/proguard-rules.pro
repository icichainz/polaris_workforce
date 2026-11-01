# Release builds shrink with R8 (the Flutter Gradle plugin reads this file).
# flutter_blue_plus reads its protobuf messages by reflection: without this
# rule, scans fail in release builds ("Field androidScanMode_ ... not found").
-keep class com.lib.flutter_blue_plus.* { *; }
