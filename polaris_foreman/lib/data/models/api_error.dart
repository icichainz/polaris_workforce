/// Every error response has `{code, message, details}`. Branch on [code].
class ApiError {
  const ApiError({
    required this.code,
    required this.message,
    this.details = const {},
  });

  factory ApiError.fromJson(Map<String, dynamic> json) => ApiError(
    code: json['code'] as String,
    message: json['message'] as String,
    details: (json['details'] as Map<String, dynamic>?) ?? const {},
  );

  final String code;
  final String message;
  final Map<String, dynamic> details;

  Map<String, dynamic> toJson() => {
    'code': code,
    'message': message,
    'details': details,
  };
}

/// Error codes from the contract's `ErrorCodeEnum`.
abstract final class ErrorCodes {
  static const notAuthenticated = 'not_authenticated';
  static const permissionDenied = 'permission_denied';
  static const notFound = 'not_found';
  static const validationError = 'validation_error';
  static const throttled = 'throttled';
  static const conflict = 'conflict';
  static const locked = 'locked';
  static const notImplemented = 'not_implemented';
  static const serverError = 'server_error';
}

/// The server answered with an error status.
class ApiException implements Exception {
  const ApiException(this.statusCode, this.error);

  final int statusCode;
  final ApiError error;

  String get code => error.code;

  int? get attemptsLeft => error.details['attempts_left'] as int?;

  /// ISO datetime, for `423 locked` after failed PINs.
  String? get lockedUntil => error.details['locked_until'] as String?;

  int? get retryAfterSeconds => error.details['retry_after'] as int?;

  @override
  String toString() => 'ApiException($statusCode ${error.code})';
}

/// No answer from the server (offline, timeout, connection reset).
class NetworkException implements Exception {
  const NetworkException([this.message = 'Network unreachable']);

  final String message;

  @override
  String toString() => 'NetworkException($message)';
}
