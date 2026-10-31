"""Django settings for Polaris Workforce.

Values that differ between machines come from environment variables
(see .env.example). Defaults are safe for local development only.
"""

import os
from pathlib import Path

from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", default=True)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off.")
    SECRET_KEY = "dev-only-insecure-key-change-me"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")


# Applications

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "drf_spectacular",
    # Polaris Workforce
    "apps.core",
    "apps.accounts",
    "apps.worksites",
    "apps.workers",
    "apps.attendance",
    "apps.payroll",
    "apps.dashboard",
    "apps.api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.nav_badges",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database — SQLite in WAL mode.
# WAL lets the office dashboard read while the API writes roll calls.
# IMMEDIATE transactions take the write lock up front, which avoids
# "database is locked" errors when two writers upgrade at the same time.

SQLITE_INIT_COMMAND = (
    "PRAGMA journal_mode=WAL;"
    "PRAGMA synchronous=NORMAL;"
    "PRAGMA busy_timeout=5000;"
    "PRAGMA temp_store=MEMORY;"
    "PRAGMA cache_size=-20000;"
)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            "init_command": SQLITE_INIT_COMMAND,
            "transaction_mode": "IMMEDIATE",
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Authentication

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "login"


# Internationalization — English and French; the company time zone is a setting.

LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", _("English")),
    ("fr", _("French")),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True

TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "UTC")
USE_TZ = True


# Static files

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded files: worker photos and payment proofs (photo, signature, thumb mark)

MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))


# Security for production (only active when DEBUG is off)

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))


# Django REST Framework — the foreman phone API (see docs/api/README.md)

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.api.authentication.DeviceTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        *(["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "EXCEPTION_HANDLER": "apps.api.exceptions.handler",
    "DEFAULT_PAGINATION_CLASS": "apps.api.pagination.StandardPagination",
    "DEFAULT_THROTTLE_RATES": {"pin": "10/minute"},
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "TIME_FORMAT": "%H:%M",
    "TIME_INPUT_FORMATS": ["%H:%M"],
    "COERCE_DECIMAL_TO_STRING": True,
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Polaris Workforce API",
    "VERSION": "1.0.0",
    "DESCRIPTION": (
        "Foreman phone API: PIN sign-in, crew lists, roll call, corrections, sync and "
        "cash payouts. Authenticate with `Authorization: Device <token>` from "
        "`POST /api/v1/auth/pin/`. Errors are `{code, message, details}`. Every phone "
        "write carries a `client_uuid` and is idempotent. Money is an integer in the "
        "currency's smallest unit, next to a `currency {code, decimals}` object. Dates "
        "are ISO 8601, times `HH:MM`. Conventions: docs/api/README.md."
    ),
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_ADD_EXPLICIT_BLANK_NULL_CHOICE": False,
    "ENUM_NAME_OVERRIDES": {
        "ErrorCodeEnum": "apps.api.enums.ERROR_CODES",
        "UserRoleEnum": "apps.api.enums.USER_ROLES",
        "LanguageEnum": "apps.api.enums.LANGUAGES",
        "PayTypeEnum": "apps.api.enums.PAY_TYPES",
        "AttendanceStatusEnum": "apps.api.enums.ATTENDANCE_STATUSES",
        "DayStatusEnum": "apps.api.enums.DAY_STATUSES",
        "CorrectableFieldEnum": "apps.api.enums.CORRECTABLE_FIELDS",
        "PayoutStatusEnum": "apps.api.enums.PAYOUT_STATUSES",
        "ProofTypeEnum": "apps.api.enums.PROOF_TYPES",
    },
    "TAGS": [
        {"name": "system", "description": "Reachability."},
        {"name": "accounts", "description": "PIN sign-in, sign-out, profile (Wave 2: A)."},
        {"name": "worksites", "description": "Crew lists (Wave 2: B)."},
        {"name": "attendance", "description": "Roll call, corrections, sync (Wave 2: C)."},
        {"name": "payroll", "description": "Cash payout and payments (Wave 2: D)."},
    ],
}
