"""Production WSGI application factory used by Gunicorn."""

from __future__ import annotations

import os

from .config import load_settings
from .web import WebApplication


def create_application() -> WebApplication:
    settings = load_settings()
    return WebApplication(
        settings.database_path,
        signing_secret=os.getenv("TOKEN_SIGNING_SECRET", ""),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8080"),
        app_env=settings.app_env,
    )
