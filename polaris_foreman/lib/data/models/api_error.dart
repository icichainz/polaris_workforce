/// Every error response has `{code, message, details}`. Branch on [code].
class ApiError {
  const ApiError({
    required this.code,
    required this.message,
    this.details = const {},
  });

  factory ApiError.fromJson(Map<String, dynamic> json) => ApiError(
    code: json['code'] as String,
    message: (json['message'] as String?) ?? '',
    details: (json['details'] as Map<String, dynamic>?) ?? const {},
  );

  final String code;
  final String message;
  final Map<String, dynamic> details;

  /// Errors on the items of a list field (contract 1.1: always a JSON list,
  /// one entry per item sent, up to the last item with an error; `{}` for an
  /// item without errors). Null when [field] has no item errors, e.g. for
  /// `{"records": {"non_field_errors": [...]}}` (an error on the field
  /// itself, such as too many items).
  List<Map<String, dynamic>>? itemErrors(String field) {
    final raw = details[field];
    if (raw is! List) return null;
    return [
      for (final item in raw)
        item is Map<String, dynamic> ? item : const <String, dynamic>{},
    ];
  }

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
  static const methodNotAllowed = 'method_not_allowed';
  static const unsupportedMediaType = 'unsupported_media_type';
  static const notAcceptable = 'not_acceptable';
  static const serverError = 'server_error';

  /// Not from the server: an error status whose body is not the API's JSON
  /// (a proxy's HTML page, for example) and has no better code.
  static const httpError = 'http_error';

  /// The code that goes with [status] when the body does not say.
  static String forStatus(int status) => switch (status) {
    400 => validationError,
    401 => notAuthenticated,
    403 => permissionDenied,
    404 => notFound,
    405 => methodNotAllowed,
    406 => notAcceptable,
    409 => conflict,
    415 => unsupportedMediaType,
    423 => locked,
    429 => throttled,
    501 => notImplemented,
    >= 500 => serverError,
    _ => httpError,
  };
}

int? _int(Object? v) => switch (v) {
  int() => v,
  num() => v.toInt(),
  String() => int.tryParse(v),
  _ => null,
};

/// The server answered with an error status.
class ApiException implements Exception {
  const ApiException(this.statusCode, this.error, {this.retryAfterHeader});

  /// HTTP status.
  final int statusCode;
  final ApiError error;

  /// `Retry-After` header (seconds), when the server sent one.
  final int? retryAfterHeader;

  int get status => statusCode;
  String get code => error.code;
  String get message => error.message;
  Map<String, dynamic> get details => error.details;

  /// Wrong PIN: tries left before the lock.
  int? get attemptsLeft => _int(details['attempts_left']);

  /// ISO datetime: `423 locked` after failed PINs, and the 5th wrong PIN
  /// (`401` with `attempts_left: 0`).
  String? get lockedUntil => details['locked_until'] as String?;

  /// `429 throttled`: `details.retry_after`, else the `Retry-After` header.
  int? get retryAfterSeconds =>
      _int(details['retry_after']) ?? retryAfterHeader;

  /// `409 conflict` on submit: workers left unmarked.
  List<int> get unmarkedWorkerIds => [
    for (final v in (details['unmarked_worker_ids'] as List?) ?? const [])
      ?_int(v),
  ];

  @override
  String toString() =>
      'ApiException($statusCode ${error.code}: ${error.message})';
}

/// No answer from the server (offline, timeout, connection reset, TLS
/// handshake failure, or a reply that is not the API's at all).
class NetworkException implements Exception {
  const NetworkException([this.message = 'Network unreachable']);

  final String message;

  @override
  String toString() => 'NetworkException($message)';
}
