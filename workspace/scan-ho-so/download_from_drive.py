#!/usr/bin/env python3
"""Download processed documents from Google Drive per case → zip them for local testing.

For each case in group_registry.json, downloads all files from the case's
Drive folder (Personal Docs / Education / Asset / Employment) and zips them
into downloads/<case-id>.zip.

Usage:
    python3 download_from_drive.py                   # all cases
    python3 download_from_drive.py --chat -1234567   # one case
    python3 download_from_drive.py --dry-run         # list files, no download
    python3 download_from_drive.py --limit 10        # first N cases
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

_env_file = SCRIPT_DIR.parent / "scan-ocr.env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

sys.path.insert(0, str(SCRIPT_DIR))

REGISTRY  = SCRIPT_DIR / "group_registry.json"
DOWNLOADS = SCRIPT_DIR / "downloads"

SHARED_DRIVE_ID = os.environ.get("SHARED_DRIVE_ID", "0AIYOQpLqtMPvUk9PVA")
CASE_SUBFOLDERS = ["Personal Docs", "Education", "Asset", "Employment"]
SKIP_FOLDERS    = ["_Bot OCR & Metadata", "Old File"]


def get_drive():
    from lib.google_clients import drive
    return drive()


def list_folder(svc, folder_id: str) -> list[dict]:
    items, page_token = [], None
    while True:
        kwargs = dict(
            q=f"'{folder_id}' in parents and trashed=false",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="nextPageToken, files(id, name, mimeType, size)",
            pageSize=200,
        )
        if page_token:
            kwargs["pageToken"] = page_token
        resp = svc.files().list(**kwargs).execute()
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def download_file(svc, file_id: str, dest: Path):
    from googleapiclient.http import MediaIoBaseDownload
    import io
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    dest.write_bytes(buf.getvalue())


def process_case(svc, entry: dict, dry_run: bool) -> int:
    applicant  = entry.get("applicant", "?")
    folder_id  = entry.get("folder_id", "")
    drive_link = entry.get("drive_link", "")

    if not folder_id:
        print(f"  SKIP — no folder_id")
        return 0

    # Collect files from each top-level subfolder
    all_files: list[tuple[str, str, str]] = []  # (subfolder_name, file_id, file_name)

    top_items = list_folder(svc, folder_id)
    for item in top_items:
        if item["mimeType"] != "application/vnd.google-apps.folder":
            continue
        if item["name"] in SKIP_FOLDERS:
            continue
        if item["name"] not in CASE_SUBFOLDERS:
            continue
        sub_files = list_folder(svc, item["id"])
        for f in sub_files:
            if f["mimeType"] == "application/vnd.google-apps.folder":
                continue
            if f["name"].endswith((".json", ".md")):
                continue
            all_files.append((item["name"], f["id"], f["name"]))

    if not all_files:
        print(f"  no downloadable files found")
        return 0

    if dry_run:
        for subfolder, fid, fname in all_files:
            print(f"  [{subfolder}] {fname}")
        return len(all_files)

    # Download to temp dir then zip
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    import re
    slug = re.sub(r"[^\w\-]", "_", applicant.lower()).strip("_") or "case"
    zip_path = DOWNLOADS / f"{slug}.zip"

    with tempfile.TemporaryDirectory(prefix="drive_dl_") as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for subfolder, fid, fname in all_files:
                dest = tmp_path / fname
                print(f"  ↓ [{subfolder}] {fname} …", end="", flush=True)
                try:
                    download_file(svc, fid, dest)
                    size_kb = dest.stat().st_size // 1024
                    print(f" {size_kb} KB")
                    zf.write(dest, fname)
                except Exception as e:
                    print(f" ERROR: {e}")

    size_kb = zip_path.stat().st_size // 1024
    print(f"  → {zip_path.name}  ({size_kb} KB,  {len(all_files)} files)")
    return len(all_files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", metavar="CHAT_ID", help="Process only this KH chat_id")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Process only first N cases")
    parser.add_argument("--dry-run", action="store_true",
                        help="List files without downloading")
    args = parser.parse_args()

    if not REGISTRY.exists():
        sys.exit(f"group_registry.json not found at {REGISTRY}")

    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    entries  = [v for v in registry.values() if v.get("kind") == "kh" and v.get("folder_id")]

    if args.chat:
        target  = str(args.chat)
        entries = [e for e in entries if str(e.get("chat_id")) == target]
        if not entries:
            sys.exit(f"chat_id {target} not found in registry (kind=kh)")

    if args.limit:
        entries = entries[:args.limit]

    print(f"Cases to process: {len(entries)}")
    if args.dry_run:
        print("DRY RUN — no files will be downloaded\n")

    svc = get_drive()
    total_files = 0
    for entry in entries:
        applicant = entry.get("applicant", "?")
        visa      = entry.get("visa", "?")
        print(f"\n{'─'*60}")
        print(f"{applicant}  [{visa}]  →  {entry.get('drive_link','')}")
        try:
            n = process_case(svc, entry, dry_run=args.dry_run)
            total_files += n
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"Done. {total_files} files across {len(entries)} cases.")
    if not args.dry_run:
        print(f"Zips at: {DOWNLOADS}")


if __name__ == "__main__":
    main()
