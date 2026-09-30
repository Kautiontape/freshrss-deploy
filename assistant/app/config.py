"""Environment-driven configuration for the assistant service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # FreshRSS database (shared with the FreshRSS container)
    db_host: str = field(default_factory=lambda: _env("FRESHRSS_DB_HOST", _env("POSTGRES_HOST", "postgres")))
    db_port: int = field(default_factory=lambda: _env_int("FRESHRSS_DB_PORT", _env_int("POSTGRES_PORT", 5432)))
    db_name: str = field(default_factory=lambda: _env("POSTGRES_DB", "freshrss"))
    db_user: str = field(default_factory=lambda: _env("POSTGRES_USER", "freshrss"))
    db_password: str = field(default_factory=lambda: _env("POSTGRES_PASSWORD", ""))
    # FreshRSS username: tables are prefixed with it (shawn_entry, ...)
    freshrss_user: str = field(default_factory=lambda: _env("FRESHRSS_USER", _env("FRESHRSS_API_USER", "shawn")))
    freshrss_public_url: str = field(default_factory=lambda: _env("FRESHRSS_PUBLIC_URL", "http://freshrss.yuffie.ts.net:8080"))

    # Anthropic
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))

    # Service auth
    ui_password: str = field(default_factory=lambda: _env("ASSISTANT_PASSWORD"))
    internal_token: str = field(default_factory=lambda: _env("ASSISTANT_INTERNAL_TOKEN"))
    cookie_secret: str = field(default_factory=lambda: _env("ASSISTANT_COOKIE_SECRET", _env("ASSISTANT_PASSWORD")))

    # Helpers
    youtube_helper_url: str = field(default_factory=lambda: _env("YOUTUBE_HELPER_URL", "http://youtube-helper:8000"))

    # Email (reuses the digest settings)
    smtp_host: str = field(default_factory=lambda: _env("DIGEST_SMTP_HOST", "smtp.fastmail.com"))
    smtp_port: int = field(default_factory=lambda: _env_int("DIGEST_SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _env("DIGEST_SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("DIGEST_SMTP_PASSWORD"))
    email_to: str = field(default_factory=lambda: _env("DIGEST_TO_EMAIL"))

    # Worker tuning
    scoring_interval_s: int = field(default_factory=lambda: _env_int("ASSISTANT_SCORING_INTERVAL", 120))
    scoring_concurrency: int = field(default_factory=lambda: _env_int("ASSISTANT_SCORING_CONCURRENCY", 4))
    worker_enabled: bool = field(default_factory=lambda: _env("ASSISTANT_WORKER", "1") not in ("0", "false", "no"))
    timezone: str = field(default_factory=lambda: _env("TZ", "America/New_York"))

    @property
    def dsn(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password}"
        )


settings = Settings()
