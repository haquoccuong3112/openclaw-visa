#!/usr/bin/env python3
"""Download original zip/file uploads from KH Telegram groups for test data.

Uses Pyrogram (MTProto user account) to access message history — Bot API doesn't
support getHistory. Run this once to build a local test-data corpus.

Setup:
  1. Get API_ID + API_HASH from https://my.telegram.org → App API
  2. Run: python3 tests/download_test_data.py
  3. First run: enter phone + 2FA code (session saved to tests/.tg_session)

Output structure:
  tests/test-data/
    nguyen-truong-an-2006/       ← safe folder name from applicant
      -5139285991_msg12345.zip
      -5139285991_msg12346.zip
    tran-dang-su-2006/
      ...

Each zip is named <chat_id>_msg<message_id>.<ext> to be idempotent.
Already-downloaded files are skipped.

Usage:
  python3 tests/download_test_data.py [--limit N]   # N groups max (default: all)
  python3 tests/download_test_data.py --dry-run      # list only, no download
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# ── paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
WS_DIR = SCRIPT_DIR.parent

# load scan-ocr.env for any env vars
env_path = WS_DIR.parent / "scan-ocr.env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

API_ID   = int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION  = str(SCRIPT_DIR / ".tg_session")
REGISTRY = WS_DIR / "group_registry.json"
OUT_DIR  = SCRIPT_DIR / "test-data"

# File extensions to download (zip + loose document files the pipeline handles)
DOWNLOAD_EXTS = {
    ".zip", ".pdf", ".jpg", ".jpeg", ".png",
    ".tiff", ".tif", ".bmp", ".webp", ".gif",
    ".doc", ".docx",
}
# Only include video/HEIC if explicitly wanted
# Max file size to download (bytes) — skip huge videos
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200 MB


def safe_folder_name(applicant: str) -> str:
    """'Nguyễn Trường An 2006' → 'nguyen-truong-an-2006'"""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", applicant.lower())
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_str = re.sub(r"[^a-z0-9]+", "-", ascii_str).strip("-")
    return ascii_str


def load_registry() -> list[dict]:
    """Return only KH groups (kind='kh') with folder_id set."""
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    out = []
    for chat_id, info in data.items():
        if info.get("kind") != "kh":
            continue
        if not info.get("folder_id"):
            continue
        out.append({
            "chat_id": int(chat_id),
            "applicant": info.get("applicant", "unknown"),
            "visa": info.get("visa", ""),
        })
    return out


async def download_group(client, group: dict, dry_run: bool) -> dict:
    """Scan 1 KH group message history → download all zip/file uploads.
    Returns {downloaded: N, skipped: N, errors: N}."""
    from pyrogram.errors import FloodWait, ChatForbidden, ChannelPrivate
    chat_id = group["chat_id"]
    applicant = group["applicant"]
    folder_name = safe_folder_name(applicant)
    out_dir = OUT_DIR / folder_name
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    stats = {"downloaded": 0, "skipped": 0, "errors": 0}
    print(f"\n[{chat_id}] {applicant}")

    try:
        n_scanned = 0
        async for msg in client.get_chat_history(chat_id):
            n_scanned += 1
            doc = msg.document
            if not doc:
                continue
            ext = Path(doc.file_name or "").suffix.lower() if doc.file_name else ""
            if ext not in DOWNLOAD_EXTS:
                continue
            if doc.file_size and doc.file_size > MAX_FILE_SIZE:
                print(f"  skip (too large {doc.file_size//1024//1024}MB): {doc.file_name}")
                stats["skipped"] += 1
                continue

            dest_name = f"{chat_id}_msg{msg.id}{ext}"
            dest_path = out_dir / dest_name
            if dest_path.exists():
                stats["skipped"] += 1
                continue

            if dry_run:
                print(f"  would download: {doc.file_name} → {dest_name} ({doc.file_size or 0} bytes)")
                stats["downloaded"] += 1
                continue

            try:
                await client.download_media(msg, file_name=str(dest_path))
                sz = dest_path.stat().st_size if dest_path.exists() else 0
                print(f"  ↓ {doc.file_name} → {dest_name} ({sz//1024}KB)")
                stats["downloaded"] += 1
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {doc.file_name}: {type(e).__name__}: {e}")
                stats["errors"] += 1

        print(f"  scanned {n_scanned} messages → {stats['downloaded']} dl / {stats['skipped']} skip / {stats['errors']} err")

    except (ChatForbidden, ChannelPrivate) as e:
        print(f"  ⚠ no access: {type(e).__name__}")
        stats["errors"] += 1
    except FloodWait as e:
        print(f"  ⚠ FloodWait {e.value}s — sleeping …")
        time.sleep(e.value + 2)
        stats["errors"] += 1
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {type(e).__name__}: {e}")
        stats["errors"] += 1

    return stats


async def main_async(args: argparse.Namespace) -> int:
    from pyrogram import Client

    if not API_ID or not API_HASH:
        print("ERROR: Set TELEGRAM_API_ID and TELEGRAM_API_HASH in scan-ocr.env")
        print("  Get them from https://my.telegram.org → App API")
        return 1

    groups = load_registry()
    print(f"Found {len(groups)} KH groups in registry")

    if args.limit:
        groups = groups[:args.limit]
        print(f"Limiting to first {len(groups)} groups")

    if not args.dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Output: {OUT_DIR}")

    async with Client(SESSION, api_id=API_ID, api_hash=API_HASH) as client:
        me = await client.get_me()
        print(f"Logged in as: {me.first_name} {me.last_name or ''} (@{me.username})")

        total = {"downloaded": 0, "skipped": 0, "errors": 0}
        for group in groups:
            stats = await download_group(client, group, dry_run=args.dry_run)
            for k in total:
                total[k] += stats[k]
            time.sleep(0.5)  # avoid flood

    print(f"\n=== DONE: {total['downloaded']} downloaded / {total['skipped']} skipped / {total['errors']} errors ===")
    if not args.dry_run:
        print(f"Test data saved to: {OUT_DIR}")
        # Write index
        index = {}
        for d in OUT_DIR.iterdir():
            if d.is_dir():
                files = sorted(f.name for f in d.iterdir())
                if files:
                    index[d.name] = files
        (OUT_DIR / "_index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Index written to: {OUT_DIR / '_index.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Download test data from Telegram KH groups")
    ap.add_argument("--limit", type=int, default=0, help="Max number of groups to process")
    ap.add_argument("--dry-run", action="store_true", help="List files without downloading")
    args = ap.parse_args()

    import asyncio
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
