"""Cached Google Drive + Sheets clients for the scan pipeline."""
from __future__ import annotations
import os
from functools import lru_cache
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload  # noqa: F401  (re-export)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

CREDS_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/home/cuong/google-service-account.json",
)


@lru_cache(maxsize=1)
def _credentials():
    return service_account.Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)


@lru_cache(maxsize=1)
def drive():
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


@lru_cache(maxsize=1)
def sheets():
    return build("sheets", "v4", credentials=_credentials(), cache_discovery=False)
