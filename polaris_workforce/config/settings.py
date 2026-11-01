"""Django settings for Polaris Workforce.

Values that differ between machines come from environment variables
(see .env.example). The defaults are production-safe: ``DEBUG`` is off unless
``DJANGO_DEBUG=true`` is set (do that in your local shell or ``.env``). Tests use
``tests/settings.py``.
"""

import os
from pathlib import Path

from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent


def env_str(name: str, default: str = "") -> str:
    """The variable, or ``default`` when it is unset or empty (``NAME=`` in a .env)."""
    return os.environ.get(name, "").strip() or str(default)


def env_bool(name: str, default: bool = False) -> bool:
    return env_str(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# Off unless asked for: a server started without the variable never shows debug pages.
DEBUG = env_bool("DJANGO_DEBUG", default=False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError(
            "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off. "
            "For local development, set DJANGO_DEBUG=true (see .env.example)."
        )
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
    # Wave 4
    "apps.payouts",
    "apps.messaging",
    "apps.portal",
    "apps.checkin",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.middleware.CurrentCompanyMiddleware",
    "apps.core.middleware.UserLanguageMiddleware",
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
                "apps.core.context_processors.company",
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
        "NAME": env_str("SQLITE_PATH", BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            "init_command": SQLITE_INIT_COMMAND,
            "transaction_mode": "IMMEDIATE",
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Authentication

AUTH_USER_MODEL = "accounts.User"

# Users of an inactive company cannot sign in (and their sessions stop working).
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.CompanyModelBackend"]

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

TIME_ZONE = env_str("DJANGO_TIME_ZONE", "UTC")
USE_TZ = True


# Static files

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded files: worker photos and payment proofs (photo, signature, thumb mark).
# Never served directly: files get random names (apps.core.storage) and are downloaded
# through views that check the viewer may see the file's site (apps.core.files).
# ``FieldFile.url`` points at the admins-only route under MEDIA_URL (Django admin uses
# it); screens and the API link to the per-object routes instead.

MEDIA_URL = "files/media/"
MEDIA_ROOT = Path(env_str("MEDIA_ROOT", BASE_DIR / "media"))

STORAGES = {
    "default": {"BACKEND": "apps.core.storage.RandomNameStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# How a protected file reaches the client once the view has checked access:
#   django — Django streams it (FileResponse). Fine for a small office.
#   nginx  — X-Accel-Redirect to PROTECTED_MEDIA_INTERNAL_URL + file name (an
#            `internal` nginx location aliased to MEDIA_ROOT).
#   apache — X-Sendfile with the file's absolute path (mod_xsendfile).
PROTECTED_MEDIA_MODE = env_str("PROTECTED_MEDIA_MODE", "django").strip().lower()
if PROTECTED_MEDIA_MODE not in {"django", "nginx", "apache"}:
    raise RuntimeError("PROTECTED_MEDIA_MODE must be django, nginx or apache.")
PROTECTED_MEDIA_INTERNAL_URL = env_str("PROTECTED_MEDIA_INTERNAL_URL", "/protected-media/")


# Cache: the PIN throttle and the office sign-in lockout live here, so it must be shared
# by every worker process and survive a restart. A file-based cache needs no extra
# service; keep DJANGO_CACHE_DIR on local disk, writable only by the app user.

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": env_str("DJANGO_CACHE_DIR", str(BASE_DIR / "var" / "cache")),
        "OPTIONS": {"MAX_ENTRIES": 20000},
    }
}


# Office sign-in limits (/accounts/login/ and /admin/login/): after LOGIN_MAX_ATTEMPTS
# wrong passwords for one (username, IP), that pair is locked out for
# LOGIN_LOCKOUT_MINUTES. The phone PIN has its own lockout (apps.accounts.services).

LOGIN_MAX_ATTEMPTS = int(env_str("DJANGO_LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_MINUTES = int(env_str("DJANGO_LOGIN_LOCKOUT_MINUTES", "15"))

# A phone's device token stops working after this many days without use (measured on
# ForemanDevice.last_seen_at); the foreman then signs in again with the PIN.
DEVICE_TOKEN_IDLE_DAYS = int(env_str("DEVICE_TOKEN_IDLE_DAYS", "30"))


# Security for production (only active when DEBUG is off)

if not DEBUG:
    # Cookies only travel over HTTPS. DJANGO_SECURE_COOKIES=false is for a local
    # plain-HTTP setup only (e.g. docker compose on http://localhost), never production.
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", default=True)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # Plain-HTTP requests are redirected to HTTPS. The proxy must set X-Forwarded-Proto
    # (see SECURE_PROXY_SSL_HEADER), or every request loops; turn it off only when the
    # proxy itself refuses plain HTTP.
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
    # HSTS: set once HTTPS works for the domain (and its subdomains, for the two flags).
    SECURE_HSTS_SECONDS = int(env_str("DJANGO_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS")
    SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD")


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
    # Throttles key on the client IP. Without NUM_PROXIES, DRF trusts the whole
    # X-Forwarded-For header, which a client can rotate to dodge the PIN throttle.
    # 0: use REMOTE_ADDR. Behind N reverse proxies that set the header, use N.
    "NUM_PROXIES": int(env_str("DJANGO_NUM_PROXIES", "0")),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "TIME_FORMAT": "%H:%M",
    "TIME_INPUT_FORMATS": ["%H:%M"],
    "COERCE_DECIMAL_TO_STRING": True,
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Polaris Workforce API",
    "VERSION": "1.2.0",
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
        {
            "name": "checkin",
            "description": "QR badges, kiosk phones, check-ins and roll-call suggestions (Wave 4).",
        },
        {
            "name": "files",
            "description": "Worker photos and payment proofs; send the Authorization header.",
        },
    ],
}
