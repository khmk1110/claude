"""社内管理システム 設定。

秘密情報（SECRET_KEY・DB接続情報）は環境変数から読み込む。ソースコードに書かない。
追加パスワード（給与・経費）は DB にハッシュで保存し、`manage.py set_stepup_password` で設定する。
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CSRF_TRUSTED_ORIGINS=(list, []),
)
environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env("DEBUG")
SECRET_KEY = env("SECRET_KEY", default="dev-only-insecure-key" if DEBUG else environ.Env.NOTSET)
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")
# Render は公開アドレスを RENDER_EXTERNAL_HOSTNAME で渡すので、手入力しなくてよいように自動で追加する
_render_host = env("RENDER_EXTERNAL_HOSTNAME", default="")
if _render_host:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, _render_host]
    CSRF_TRUSTED_ORIGINS = [*CSRF_TRUSTED_ORIGINS, f"https://{_render_host}"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "apps.core",
    "apps.accounts",
    "apps.members",
    "apps.shifts",
    "apps.attendance",
    "apps.payroll",
    "apps.expenses",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.CurrentRequestMiddleware",
    "apps.core.nocache.NoStoreHtmlMiddleware",
    # 給与・経費の URL は、ビュー側のデコレーターに加えてここでも追加認証を検証する（二重の防御）
    "apps.accounts.middleware.StepUpGateMiddleware",
]

ROOT_URLCONF = "config.urls"
CSRF_FAILURE_VIEW = "apps.core.views.csrf_failure"

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
                "apps.core.context_processors.company",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": env.db("DATABASE_URL", default="postgres://app:app@localhost:5432/kanri")}
DATABASES["default"]["ATOMIC_REQUESTS"] = True

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ja"
TIME_ZONE = "Asia/Tokyo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# アップロードした資料は公開ディレクトリで配信しない。認証付きビュー経由でのみ返す。
MEDIA_ROOT = Path(env("MEDIA_ROOT", default=str(BASE_DIR / "media")))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STORAGES = {
    # 資料・写真は DB に保存（DB のバックアップだけで丸ごと復元できる）
    "default": {"BACKEND": "apps.core.storage.DatabaseStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage" if not DEBUG
                    else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024

# --- セッション ---
SESSION_COOKIE_AGE = 60 * 60 * 10  # ログインは最長10時間
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_CONTENT_TYPE_NOSNIFF = True

# --- 給与・経費の追加認証 ---
STEPUP_IDLE_SECONDS = env.int("STEPUP_IDLE_SECONDS", default=30 * 60)  # 30分操作がなければ再認証
STEPUP_MAX_AGE_SECONDS = env.int("STEPUP_MAX_AGE_SECONDS", default=8 * 60 * 60)  # 認証から最長8時間
STEPUP_MAX_FAILURES = env.int("STEPUP_MAX_FAILURES", default=5)  # 連続失敗の上限
STEPUP_LOCK_SECONDS = env.int("STEPUP_LOCK_SECONDS", default=15 * 60)  # 上限到達後のロック時間
# パス先頭一致で追加認証を要求する。新しいビューを追加してデコレーターを付け忘れても守られる。
STEPUP_PROTECTED_PREFIXES = {
    "/payroll/": "payroll",
    "/api/payroll/": "payroll",
    "/admin/payroll/": "payroll",
    "/expenses/": "expenses",
    "/api/expenses/": "expenses",
    "/admin/expenses/": "expenses",
}

COMPANY_NAME = env("COMPANY_NAME", default="（会社名を COMPANY_NAME に設定）")

# 明細書の発行者欄（実際の値は環境変数で設定。リポジトリには書かない）
ISSUER_REPRESENTATIVE = env("ISSUER_REPRESENTATIVE", default="")
ISSUER_ADDRESS = env("ISSUER_ADDRESS", default="")
ISSUER_TEL = env("ISSUER_TEL", default="")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
