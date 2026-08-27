"""Application configuration for the quota alert service.

The M1 core intentionally keeps configuration dependency-free. Secrets are read from
environment variables by the notification layer later and must never be persisted here.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str = "development"
    app_timezone: str = "Asia/Hong_Kong"
    database_path: Path = Path("data/quota_alert.sqlite3")
    missing_confirmations_required: int = 2
    quota_source_service_id: int = 579
    quota_source_timeout_seconds: float = 20.0

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)


def load_settings() -> Settings:
    """Load non-secret runtime settings from environment variables."""

    missing_confirmations = int(os.getenv("MISSING_CONFIRMATIONS_REQUIRED", "2"))
    if missing_confirmations < 1:
        raise ValueError("MISSING_CONFIRMATIONS_REQUIRED must be >= 1")

    service_id = int(os.getenv("QUOTA_SOURCE_SERVICE_ID", "579"))
    if service_id <= 0:
        raise ValueError("QUOTA_SOURCE_SERVICE_ID must be positive")

    timeout_seconds = float(os.getenv("QUOTA_SOURCE_TIMEOUT_SECONDS", "20"))
    if timeout_seconds <= 0:
        raise ValueError("QUOTA_SOURCE_TIMEOUT_SECONDS must be positive")

    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        app_timezone=os.getenv("APP_TIMEZONE", "Asia/Hong_Kong"),
        database_path=Path(os.getenv("DATABASE_PATH", "data/quota_alert.sqlite3")),
        missing_confirmations_required=missing_confirmations,
        quota_source_service_id=service_id,
        quota_source_timeout_seconds=timeout_seconds,
    )
