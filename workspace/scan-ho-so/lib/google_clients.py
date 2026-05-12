"""Cached Google Drive + Sheets clients for the scan pipeline."""
from __future__ import annotations
import os
from functools import lru_cache
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload  # noqa: F401  (re-export)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Default credentials path: <workspace>/google-service-account.json — this file is
# <workspace>/scan-ho-so/lib/google_clients.py, so parents[2] is the workspace dir.
_DEFAULT_CREDS = Path(__file__).resolve().parents[2] / "google-service-account.json"


def _creds_path() -> str:
    """Resolved lazily: telegram_listener.py imports this module before it loads
    scan-ocr.env, so reading the env var at import time would miss it. The
    GOOGLE_APPLICATION_CREDENTIALS env var (set by scan-ocr.env) wins; else the default above."""
    return os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or str(_DEFAULT_CREDS)


@lru_cache(maxsize=1)
def _credentials():
    return service_account.Credentials.from_service_account_file(_creds_path(), scopes=SCOPES)


@lru_cache(maxsize=1)
def drive():
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


@lru_cache(maxsize=1)
def sheets():
    return build("sheets", "v4", credentials=_credentials(), cache_discovery=False)
