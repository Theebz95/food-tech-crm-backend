"""
Django settings for the Food Tech CRM backend.

Naming note: this module (config/settings.py) is unrelated to the `settings`
Django *app* at the project root (settings/). The app is named after the old
Supabase tables it will eventually replace (geofence_settings,
customer_portal_settings, business_hours, reservation_settings, ...). See
README.md for the full explanation.
"""

import os
from pathlib import Path

import dj_database_url
from celery.schedules import crontab
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    val = os.environ.get(key)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def env_list(key, default=""):
    val = os.environ.get(key, default)
    return [item.strip() for item in val.split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    # Local apps. `authentication` is listed before the others since it
    # provides AUTH_USER_MODEL, which everything else FKs into.
    "authentication",
    "core",
    "customers",
    "reservations",
    "loyalty",
    "employees",
    "finance",
    "inventory",
    "documents",
    "marketing",
    "settings.apps.SettingsConfig",
]

AUTH_USER_MODEL = "authentication.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
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
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Database ---------------------------------------------------------------
# Points at Supabase's Postgres connection string. Get the real value from
# Supabase Project Settings -> Database -> Connection string. On Railway,
# prefer the "Transaction" pooler URI (port 6543, pgbouncer) since Railway
# containers are short-lived and benefit from connection pooling.
DATABASES = {
    "default": dj_database_url.config(
        default=env(
            "DATABASE_URL",
            "postgres://postgres:password@localhost:5432/postgres",  # placeholder — replace via .env
        ),
        conn_max_age=600,
    )
}

# Supabase Auth owns password policy and the signup flow entirely; Django
# never sets or validates a password for these users (see authentication app).
AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- CORS --------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:8080"
)

# --- DRF / Supabase auth -------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "authentication.authentication.SupabaseAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/minute",
        "user": "1000/minute",
        # Scoped throttles for the public, unauthenticated Reservations
        # endpoints (reservations/public_views.py) — kept separate from the
        # global "anon" bucket above so a burst against one of these
        # doesn't also throttle every other anonymous endpoint in the API,
        # and so booking abuse specifically gets a much tighter limit than
        # read-only availability/hours checks. See README "Reservations
        # domain" -> "Guest booking permission model".
        "reservation_availability": "30/minute",
        "reservation_booking": "5/minute",
        # Guards a guessable 6-char confirmation code (~16.7M keyspace) —
        # tighter than the other guest scopes on purpose. "_global" is a
        # second, IP-independent cap (see reservations/throttles.py) so a
        # guess attempt distributed across many IPs can't bypass the
        # per-IP limit just by spreading out.
        "reservation_lookup": "5/minute",
        "reservation_lookup_global": "20/minute",
        "reservation_waitlist": "10/minute",
        "reservation_business_hours": "30/minute",
        # The marketing app's public tracking beacon will define its own,
        # stricter scoped throttle (keyed by IP + script_key) when that
        # domain is built — see README "Security hardening notes".
    },
}

SUPABASE_URL = env("SUPABASE_URL", "https://your-project.supabase.co")
SUPABASE_JWT_SECRET = env("SUPABASE_JWT_SECRET", "placeholder-jwt-secret")
SUPABASE_JWT_AUDIENCE = env("SUPABASE_JWT_AUDIENCE", "authenticated")
SUPABASE_SERVICE_ROLE_KEY = env("SUPABASE_SERVICE_ROLE_KEY", "")

# --- Supabase Storage (S3-compatible — see documents/storage.py) -------------
# Project Settings -> Storage -> S3 Connection in the Supabase dashboard.
# These are separate credentials from SUPABASE_SERVICE_ROLE_KEY above.
SUPABASE_STORAGE_ENDPOINT_URL = env(
    "SUPABASE_STORAGE_ENDPOINT_URL", "https://your-project.supabase.co/storage/v1/s3"
)
SUPABASE_STORAGE_BUCKET = env("SUPABASE_STORAGE_BUCKET", "documents")
SUPABASE_STORAGE_ACCESS_KEY_ID = env("SUPABASE_STORAGE_ACCESS_KEY_ID", "placeholder-access-key-id")
SUPABASE_STORAGE_SECRET_ACCESS_KEY = env("SUPABASE_STORAGE_SECRET_ACCESS_KEY", "placeholder-secret-access-key")
# Supabase Storage doesn't have real AWS regions, but boto3's S3 client
# requires a region_name — any value works; Supabase ignores it.
SUPABASE_STORAGE_REGION = env("SUPABASE_STORAGE_REGION", "us-east-1")

# --- Stripe --------------------------------------------------------------------
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", "sk_test_placeholder")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", "whsec_placeholder")
STRIPE_PRICE_BASE_PLAN = env("STRIPE_PRICE_BASE_PLAN", "")
STRIPE_PRICE_TIME_TRACKING_ADDON = env("STRIPE_PRICE_TIME_TRACKING_ADDON", "")
STRIPE_PRICE_LOYALTY_ADDON = env("STRIPE_PRICE_LOYALTY_ADDON", "")
STRIPE_PRICE_RESERVATIONS_ADDON = env("STRIPE_PRICE_RESERVATIONS_ADDON", "")

# --- Celery / Celery Beat -------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"

CELERY_BEAT_SCHEDULE = {
    # Replaces the old Postgres pg_cron job + check_expired_trials() RPC
    # (supabase/migrations/20260210071436..., 20260218042634...).
    "check-expired-trials": {
        "task": "core.tasks.check_expired_trials",
        "schedule": crontab(hour=0, minute=0),
    },
    # Stub — raises NotImplementedError until the Finance domain (and its
    # RecurringTransaction model) is built in a follow-up session.
    "generate-due-recurring-transactions": {
        "task": "finance.tasks.generate_due_recurring_transactions",
        "schedule": crontab(hour=1, minute=0),
    },
    # Replaces the old client-side, render-time recurring schedule
    # expansion — see employees/models.py module docstring "Security fix #2".
    "expand-recurring-schedules": {
        "task": "employees.tasks.expand_recurring_schedules",
        "schedule": crontab(hour=2, minute=0),
    },
}
