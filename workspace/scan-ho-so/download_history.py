#!/usr/bin/env python3
"""Download historical .zip files from KH Telegram groups.

Reads group_registry.json → for each KH group, iterates all messages,
downloads every .zip attachment, saves to downloads/<case-id>/<chat-date>_<filename>.

Session saved to download_history.session (gitignored, reused on next run).

Usage:
    python3 download_history.py                  # all KH groups
    python3 download_history.py --chat -1234567  # one group only
    python3 download_history.py --limit 500      # last N messages per group
    python3 download_history.py --dry-run        # list zips without downloading
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR   = Path(__file__).resolve().parent

# Load env from scan-ocr.env (same pattern as telegram_listener.py / scan_pipeline.py)
_env_file = SCRIPT_DIR.parent / "scan-ocr.env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

API_ID    = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH  = os.environ.get("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not API_ID or not API_HASH:
    sys.exit("Missing TELEGRAM_API_ID / TELEGRAM_API_HASH in scan-ocr.env")
if not BOT_TOKEN:
    sys.exit("Missing TELEGRAM_BOT_TOKEN in scan-ocr.env")
REGISTRY     = SCRIPT_DIR / "group_registry.json"
SESSION_FILE = SCRIPT_DIR / "download_history_bot"   # .session suffix added by Pyrogram
DOWNLOADS    = SCRIPT_DIR / "downloads"


def _slug(text: str) -> str:
    return re.sub(r"[^\w\-]", "_", text.strip().lower()).strip("_") or "unknown"


def _case_id(entry: dict) -> str:
    applicant = entry.get("applicant") or "unknown"
    visa      = entry.get("visa") or ""
    slug = _slug(applicant)
    if visa:
        slug = f"{slug}_{_slug(visa)}"
    return slug


async def download_group(client, entry: dict, limit: int | None, dry_run: bool):
    from pyrogram.errors import FloodWait
    chat_id   = int(entry["chat_id"])
    applicant = entry.get("applicant", "?")
    case_id   = _case_id(entry)
    out_dir   = DOWNLOADS / case_id
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'─'*60}")
    print(f"Group {chat_id}  →  {applicant}  [{case_id}]")

    count = downloaded = skipped = 0
    async for msg in client.get_chat_history(chat_id, limit=limit):
        doc = msg.document
        if not doc:
            continue
        fname = doc.file_name or ""
        if not fname.lower().endswith(".zip"):
            continue
        count += 1
        ts = msg.date.strftime("%Y%m%d_%H%M%S") if msg.date else "unknown"
        safe_name = re.sub(r"[^\w\-. ]", "_", fname)
        dest = out_dir / f"{ts}_{safe_name}"

        if dry_run:
            size_kb = (doc.file_size or 0) // 1024
            print(f"  [dry] {ts}  {fname}  ({size_kb} KB)")
            continue

        if dest.exists():
            print(f"  skip  {dest.name}  (exists)")
            skipped += 1
            continue

        print(f"  ↓  {dest.name} …", end="", flush=True)
        try:
            await client.download_media(msg, file_name=str(dest))
            size_kb = dest.stat().st_size // 1024
            print(f"  {size_kb} KB")
            downloaded += 1
        except FloodWait as e:
            print(f"\n  FloodWait {e.value}s — sleeping …")
            await asyncio.sleep(e.value + 2)
            await client.download_media(msg, file_name=str(dest))
            downloaded += 1
        except Exception as e:
            print(f"\n  ERROR: {type(e).__name__}: {e}")

    label = "found" if dry_run else f"downloaded={downloaded} skipped={skipped}"
    print(f"  total zips={count}  {label}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", metavar="CHAT_ID", help="Process only this chat_id")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Max messages to scan per group (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List zips without downloading")
    args = parser.parse_args()

    if not REGISTRY.exists():
        sys.exit(f"group_registry.json not found at {REGISTRY}")

    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    entries  = [v for v in registry.values() if v.get("kind") == "kh"]

    if args.chat:
        target = str(args.chat)
        entries = [e for e in entries if str(e.get("chat_id")) == target]
        if not entries:
            sys.exit(f"chat_id {target} not found in registry (kind=kh)")

    print(f"Groups to scan: {len(entries)}")
    if args.dry_run:
        print("DRY RUN — no files will be downloaded")

    from pyrogram import Client

    # Use bot token — bot is already in all KH groups, no phone login needed
    async with Client(str(SESSION_FILE), api_id=API_ID, api_hash=API_HASH,
                      bot_token=BOT_TOKEN) as client:
        for entry in entries:
            try:
                await download_group(client, entry, limit=args.limit, dry_run=args.dry_run)
            except Exception as e:
                print(f"  SKIP group {entry.get('chat_id')}: {type(e).__name__}: {e}")

    print(f"\nDone. Downloads at: {DOWNLOADS}")


if __name__ == "__main__":
    asyncio.run(main())
