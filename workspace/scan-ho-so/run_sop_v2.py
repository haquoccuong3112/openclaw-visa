#!/usr/bin/env python3
"""SOP-v2 reprocessing of the Hoàng Thị Mơ test set.

What it does:
  1. Walk extracted/ for real files (skip __MACOSX + ._*).
  2. Reuse Gemini results from the previous run (no API calls).
  3. Classify each → SOP tag + 1 of 4 top folders.
  4. Build SOP-compliant filename.
  5. Create new case folder with only 4 top folders + _Bot OCR & Metadata.
  6. Upload renamed files to the right top folder.
  7. Write OCR JSON + text into _Bot OCR & Metadata.
  8. Append a single batch to the master Sheet (Documents tab) with light fields only.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.sop_naming import classify_doc_type, build_filename, detect_english, title_case_ascii  # noqa
from lib.google_clients import drive  # noqa
from lib.drive_helpers import get_or_create_folder, upload_file  # noqa

# --- Config ----------------------------------------------------------
RUN_DIR = Path("/home/cuong/.openclaw/workspace/scan-ho-so/runs/hoang-thi-mo-20260510-2048")
EXTRACTED = RUN_DIR / "extracted"
GEMINI_DIR = RUN_DIR / "gemini"

OPENCLAW_FOLDER_ID = "1VUpoBV3fAudONv5mMFXYguRThKfOLyz7"
SHARED_DRIVE_ID = "0AIYOQpLqtMPvUk9PVA"

CASE_ID = "MoTest91-WP10m-V2"
CASE_FOLDER_NAME = "2026-05_Hoang-Thi-Mo-OCRTEST_V2"
APPLICANT_NAME = "Hoang Thi Mo"

TOP_FOLDERS = ["Personal Docs", "Education", "Asset", "Employment"]
OCR_META_FOLDER = "_Bot OCR & Metadata"



EXT_MIME = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
}


# --- Step 1: collect real files -------------------------------------
def is_macos_junk(p: Path) -> bool:
    parts = set(p.parts)
    if "__MACOSX" in parts:
        return True
    if p.name.startswith("._"):
        return True
    return False


def collect_real_files() -> list[Path]:
    out = []
    for p in EXTRACTED.rglob("*"):
        if not p.is_file():
            continue
        if is_macos_junk(p):
            continue
        if p.suffix.lower() not in EXT_MIME:
            continue
        out.append(p)
    return sorted(out)


# --- Step 2: load existing Gemini result for a file -----------------
_NESTED_DT_RE = re.compile(r'"doc_type"\s*:\s*"([^"]+)"')
_NESTED_NAME_RE = re.compile(r'"full_name"\s*:\s*"([^"]+)"')


def load_gemini_for(file_path: Path) -> dict:
    """Match by basename in gemini/ subdir."""
    base = file_path.name
    # files in gemini/ are named like "MoTest91-WP10m-{NN}-{base}.gemini.json"
    for gp in GEMINI_DIR.glob("*.gemini.json"):
        if gp.name.endswith(f"-{base}.gemini.json") or gp.name.endswith(f"_{base}.gemini.json"):
            try:
                d = json.loads(gp.read_text())
            except Exception as e:
                return {"_load_err": str(e), "_path": str(gp)}

            # Repair: when summary_vi accidentally contains a (possibly truncated)
            # JSON object that has the real doc_type/person, lift those fields up.
            if d.get("doc_type") in ("Chưa phân loại", "Không xác định", None, ""):
                sv = d.get("summary_vi")
                if isinstance(sv, str) and sv.strip().startswith("{"):
                    nested = None
                    try:
                        nested = json.loads(sv)
                    except Exception:
                        nested = None
                    if isinstance(nested, dict):
                        if nested.get("doc_type"):
                            d["doc_type"] = nested["doc_type"]
                        if nested.get("person") and not d.get("person"):
                            d["person"] = nested["person"]
                        if nested.get("summary_vi"):
                            d["summary_vi"] = nested["summary_vi"]
                    else:
                        # Truncated JSON — regex-extract critical fields
                        m = _NESTED_DT_RE.search(sv)
                        if m:
                            d["doc_type"] = m.group(1)
                        if not d.get("person"):
                            n = _NESTED_NAME_RE.search(sv)
                            if n:
                                d["person"] = [{"full_name": n.group(1)}]
            return d
    return {}


# --- Step 3: extract subject name -----------------------------------
def extract_subject(gem: dict) -> str:
    person = gem.get("person")
    if isinstance(person, list) and person:
        first = person[0]
        if isinstance(first, dict):
            return first.get("full_name") or first.get("name") or ""
        return str(first)
    if isinstance(person, str):
        return person
    if isinstance(person, dict):
        return person.get("full_name") or person.get("name") or ""
    return ""


def get_summary(gem: dict) -> str:
    s = gem.get("summary_vi") or gem.get("summary") or ""
    if isinstance(s, list):
        s = " / ".join(str(x) for x in s)
    return str(s)[:400]


# --- Step 4: process each file --------------------------------------
def process_file(p: Path, idx_per_tag: dict[str, int]) -> dict:
    gem = load_gemini_for(p)
    raw_dt = gem.get("doc_type", "")
    summary = get_summary(gem)
    cls = classify_doc_type(raw_dt, summary, p.name)
    subject_raw = extract_subject(gem) or APPLICANT_NAME
    is_eng = detect_english(summary, "")
    # index per (tag, subject) bucket
    bucket = (cls.tag, title_case_ascii(subject_raw))
    idx_per_tag[bucket] = idx_per_tag.get(bucket, 0) + 1
    use_idx = idx_per_tag[bucket] if idx_per_tag[bucket] > 1 else None
    new_name = build_filename(
        tag=cls.tag,
        subject_name=subject_raw,
        extension=p.suffix,
        relation=None,  # phase 1: no relation detection (need applicant context)
        index=use_idx,
        is_english=is_eng,
    )
    return {
        "src": str(p),
        "src_name": p.name,
        "raw_doc_type": raw_dt or "",
        "tag": cls.tag,
        "folder": cls.folder,
        "confidence": cls.confidence,
        "needs_review": cls.needs_review,
        "subject": title_case_ascii(subject_raw) if subject_raw else "Unknown",
        "summary": summary,
        "new_name": new_name,
        "is_english": is_eng,
        "gemini": gem,
    }


# --- Step 5: orchestrate --------------------------------------------
def run():
    files = collect_real_files()
    print(f"Found {len(files)} real files (after stripping macOS junk).")

    # First pass: classify all so we can see proposed renames before uploading
    idx_per_tag: dict[tuple, int] = {}
    items = []
    # Sort so dedup-by-content is stable: by basename
    for p in files:
        items.append(process_file(p, idx_per_tag))

    # Dedup: same (subject, tag, src basename) → keep first
    seen = set()
    deduped = []
    for it in items:
        key = (it["tag"], it["subject"], it["src_name"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    print(f"After dedup: {len(deduped)} files.")

    # Print plan
    print("\n=== RENAME PLAN ===")
    for it in deduped:
        flag = "⚠️" if it["needs_review"] else "  "
        print(f"  {flag} [{it['confidence']:6}] {it['folder']:14} | {it['src_name'][:40]:40} → {it['new_name']}")

    # Confirm before touching Drive
    if "--dry" in sys.argv:
        print("\n[DRY RUN] stopping before any Drive/Sheets writes.")
        return

    # Build case folder + 4 top folders + OCR meta
    print("\n=== Creating Drive folders ===")
    case_id = get_or_create_folder(CASE_FOLDER_NAME, OPENCLAW_FOLDER_ID, drive_id=SHARED_DRIVE_ID)
    print(f"  Case folder: {case_id}")
    top_ids = {}
    for f in TOP_FOLDERS:
        fid = get_or_create_folder(f, case_id, drive_id=SHARED_DRIVE_ID)
        top_ids[f] = fid
        print(f"  {f}: {fid}")
    ocr_meta_id = get_or_create_folder(OCR_META_FOLDER, case_id, drive_id=SHARED_DRIVE_ID)
    print(f"  {OCR_META_FOLDER}: {ocr_meta_id}")

    # Upload files + OCR sidecars
    print("\n=== Uploading ===")
    results = []
    for it in deduped:
        target_folder_id = top_ids[it["folder"]]
        mime = EXT_MIME.get(Path(it["src"]).suffix.lower())
        up = upload_file(it["src"], it["new_name"], target_folder_id, drive_id=SHARED_DRIVE_ID, mime=mime)
        print(f"  {'SKIP' if up['skipped'] else 'OK  '} {it['folder']:14} | {it['new_name']}")

        base = Path(it["new_name"]).stem
        gem = it["gemini"]

        # --- JSON metadata (machine-readable) ---
        meta_obj = {
            "case_id": CASE_ID,
            "src_name": it["src_name"],
            "std_name": it["new_name"],
            "tag": it["tag"],
            "folder": it["folder"],
            "subject": it["subject"],
            "confidence": it["confidence"],
            "needs_review": it["needs_review"],
            "is_english": it["is_english"],
            "drive_link": up["link"],
            "gemini": gem,
        }
        meta_path = f"/tmp/{base}.json"
        with open(meta_path, "w") as fh:
            json.dump(meta_obj, fh, ensure_ascii=False, indent=2)
        upload_file(meta_path, f"{base}.json", ocr_meta_id, drive_id=SHARED_DRIVE_ID, mime="application/json")

        # --- Markdown tóm tắt (human-readable) ---
        review_flag = " ⚠️ Cần kiểm tra" if it["needs_review"] else ""
        eng_flag = " 🌐 Bản tiếng Anh" if it["is_english"] else ""
        md_parts = []
        md_parts.append(f"# {it['new_name']}\n\n")
        md_parts.append(f"**Loại giấy tờ:** {it['tag']}  \n")
        md_parts.append(f"**Folder:** {it['folder']}  \n")
        md_parts.append(f"**Người:** {it['subject']}  \n")
        md_parts.append(f"**Confidence:** {it['confidence']}{review_flag}{eng_flag}  \n")
        md_parts.append(f"**File gốc:** {it['src_name']}  \n")
        md_parts.append(f"**Drive:** {up['link']}  \n\n")
        if it["summary"]:
            md_parts.append(f"## Tóm tắt\n{it['summary']}\n\n")
        if gem.get("key_fields"):
            md_parts.append("## Thông tin chính\n")
            for k, v in gem["key_fields"].items():
                md_parts.append(f"- **{k}:** {v}\n")
            md_parts.append("\n")
        if gem.get("person"):
            persons = gem["person"] if isinstance(gem["person"], list) else [gem["person"]]
            md_parts.append("## Người liên quan\n")
            for p in persons:
                if isinstance(p, dict):
                    for k, v in p.items():
                        md_parts.append(f"- **{k}:** {v}\n")
                else:
                    md_parts.append(f"- {p}\n")
            md_parts.append("\n")
        md_path = f"/tmp/{base}.md"
        Path(md_path).write_text("".join(md_parts), encoding="utf-8")
        upload_file(md_path, f"{base}.md", ocr_meta_id, drive_id=SHARED_DRIVE_ID, mime="text/markdown")

        results.append(it)

        # cleanup tmp
        try:
            os.remove(meta_path)
            os.remove(md_path)
        except Exception:
            pass

    needs_review = [r for r in results if r["needs_review"]]
    review_note = f" | ⚠️ {len(needs_review)} cần kiểm tra" if needs_review else ""
    print(f"\n✅ Done — {len(results)} files{review_note}")
    print(f"📁 Case folder: https://drive.google.com/drive/folders/{case_id}")


if __name__ == "__main__":
    run()
