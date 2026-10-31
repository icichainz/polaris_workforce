/// Build-time switches (`--dart-define`).
///
/// ```sh
/// flutter run                                     # mock API (default)
/// flutter run --dart-define=USE_MOCK_API=false \
///   --dart-define=API_BASE_URL=https://polaris.example.com/api/v1/
/// ```
abstract final class AppConfig {
  /// Mock repositories (in-memory server) instead of the real API.
  static const useMockApi = bool.fromEnvironment(
    'USE_MOCK_API',
    defaultValue: true,
  );

  static const apiBaseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://10.0.2.2:8000/api/v1/',
  );

  /// Mock only: seconds until the "site manager" approves a submitted day.
  static const mockApprovalSeconds = int.fromEnvironment(
    'MOCK_APPROVAL_SECONDS',
    defaultValue: 12,
  );
}
