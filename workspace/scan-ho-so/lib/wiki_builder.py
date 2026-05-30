"""wiki_builder.py — build/update case wiki local bằng GLM-5.1 (Ollama Cloud).

Sau mỗi lần pipeline OCR xong, gọi hàm này để tổng hợp toàn bộ tài liệu thành
một wiki.md có cấu trúc (nhân thân + cross-ref + nội dung OCR đầy đủ). Wiki lưu
local tại wikis/{case_folder_id}.md — chat.py đọc thẳng từ đây, không cần gọi Drive.

Dùng bởi:
  scan_pipeline.py → build_or_update_wiki()  (sau process_one loop)
  lib/chat.py      → load_wiki()             (đọc context cho Q&A)
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SCAN_HO_SO_DIR = Path(os.environ.get("SCAN_HO_SO_DIR", str(_HERE.parent)))
WIKI_DIR = SCAN_HO_SO_DIR / "wikis"

WIKI_MODEL = os.environ.get("WIKI_MODEL", "glm-5.1")
WIKI_BASE_URL = os.environ.get("WIKI_BASE_URL", "https://ollama.com/v1")
WIKI_API_KEY = os.environ.get("WIKI_API_KEY", "")
WIKI_TIMEOUT = int(os.environ.get("WIKI_TIMEOUT", "120"))
DATASET_CACHE_DIR = WIKI_DIR  # lưu dataset cache cùng thư mục với wiki


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def load_wiki(case_folder_id: str) -> str | None:
    """Đọc wiki local. Trả None nếu chưa có hoặc lỗi."""
    path = WIKI_DIR / f"{_safe_filename(case_folder_id)}.md"
    try:
        return path.read_text(encoding="utf-8") if path.exists() else None
    except Exception:
        return None


def load_ctx_cache(case_folder_id: str) -> dict | None:
    """Đọc ctx cache local (name_to_link + coverage items).
    Trả None nếu chưa có — caller dùng build_dataset từ Drive."""
    import json as _json
    path = WIKI_DIR / f"{_safe_filename(case_folder_id)}.ctx.json"
    try:
        return _json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def build_or_update_wiki(
    items: list[dict],
    case_folder_id: str,
    applicant: str,
    visa: str = "",
) -> Path:
    """Gọi GLM-5.1 (Ollama Cloud) sinh wiki.md từ items, lưu local.

    items: list sidecar dicts từ process_one() — cần tag, subject, relation,
           md_content/summary, drive_link, confidence, folder, status.
    Trả về Path wiki.md đã ghi (dù LLM lỗi — fallback vẫn ghi file).
    """
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    wiki_path = WIKI_DIR / f"{_safe_filename(case_folder_id)}.md"

    existing_wiki = ""
    if wiki_path.exists():
        try:
            existing_wiki = wiki_path.read_text(encoding="utf-8")
        except Exception:
            pass
    existing_log = _extract_log_section(wiki_path)
    timestamp = time.strftime("%Y-%m-%d %H:%M")
    new_files = [
        it.get("new_name") or it.get("src_name", "")
        for it in items
        if it.get("status") in ("uploaded", "uploaded-split", "uploaded-no-ocr")
    ]
    log_entry = (
        f"- {timestamp}: {len(new_files)} file mới"
        + (f" ({', '.join(new_files[:5])}{'...' if len(new_files) > 5 else ''})"
           if new_files else "")
    )

    docs_block = _format_items_for_prompt(items)
    header = (
        f"# Hồ sơ: {applicant}{f' — {visa}' if visa else ''}\n"
        f"Cập nhật: {timestamp} | Case: {case_folder_id}\n"
    )
    log_block = f"## Cập nhật\n{log_entry}\n{existing_log}"

    if existing_wiki:
        # Incremental update: merge tài liệu mới vào wiki cũ
        prompt = (
            "Dưới đây là WIKI HỒ SƠ hiện tại và CÁC TÀI LIỆU MỚI vừa OCR.\n"
            "Hãy cập nhật wiki: thêm tài liệu mới vào đúng vị trí (nhân thân + chi tiết), "
            "giữ nguyên toàn bộ nội dung cũ, cập nhật header (Cập nhật/Tài liệu), "
            "và thay section '## Cập nhật' bằng log mới.\n\n"
            "=== WIKI HIỆN TẠI ===\n"
            + existing_wiki + "\n\n"
            "=== TÀI LIỆU MỚI ===\n\n"
            + docs_block + "\n\n"
            "=== LOG MỚI (thay section ## Cập nhật) ===\n"
            + log_block
        )
    else:
        # First build
        prompt = (
            "Tạo wiki hồ sơ khách hàng theo cấu trúc dưới đây. Giữ NGUYÊN VĂN nội dung OCR.\n\n"
            + header + "\n"
            "## Nhân thân\n"
            "[Mỗi người 1 dòng: **Tên** (quan hệ) — liệt kê loại giấy tờ]\n\n"
            "## Chi tiết từng tài liệu\n"
            "[Mỗi tài liệu: ### tên file, rồi Loại/Người/Quan hệ/Folder/Confidence, "
            "Drive link, rồi toàn bộ nội dung OCR]\n\n"
            + log_block + "\n\n"
            "---\nDỮ LIỆU ĐẦU VÀO:\n\n"
            + docs_block
        )

    try:
        wiki_text = _call_ollama(prompt)
        if not wiki_text.strip():
            raise ValueError("GLM trả về rỗng")
    except Exception as e:
        print(f"wiki_builder: GLM lỗi ({type(e).__name__}: {e}) → dùng fallback", flush=True)
        wiki_text = _build_fallback(
            items, applicant, visa, case_folder_id,
            timestamp, log_entry, existing_log,
        )

    wiki_path.write_text(wiki_text, encoding="utf-8")

    # Lưu ctx cache để chat.py dùng thay build_dataset (tránh tải Drive)
    _save_ctx_cache(items, case_folder_id)

    return wiki_path


# ---------------------------------------------------------------------------
# internal
# ---------------------------------------------------------------------------

def _safe_filename(case_folder_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", case_folder_id)[:80]


def _call_ollama(prompt: str) -> str:
    import httpx
    payload = {
        "model": WIKI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "stream": False,
    }
    with httpx.Client(timeout=WIKI_TIMEOUT) as client:
        resp = client.post(
            f"{WIKI_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {WIKI_API_KEY}"},
            json=payload,
        )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or ""


def _extract_log_section(wiki_path: Path) -> str:
    if not wiki_path.exists():
        return ""
    try:
        txt = wiki_path.read_text(encoding="utf-8")
        if "## Cập nhật" in txt:
            return txt.split("## Cập nhật", 1)[1].strip()
    except Exception:
        pass
    return ""


def _format_items_for_prompt(items: list[dict]) -> str:
    parts = []
    for it in items:
        name = it.get("new_name") or it.get("src_name", "?")
        parts.append(
            f"### {name}\n"
            f"Loại: {it.get('tag', '')} | Người: {it.get('subject', '')} | "
            f"Quan hệ: {it.get('relation', '') or 'đương đơn'} | "
            f"Folder: {it.get('folder', '')} | Confidence: {it.get('confidence', '')}\n"
            f"Drive: {it.get('drive_link', '')}\n\n"
            + (it.get("md_content") or it.get("summary") or "(no OCR)")
        )
    return "\n\n---\n\n".join(parts)


def _save_ctx_cache(items: list[dict], case_folder_id: str) -> None:
    """Lưu ctx cache compact cho chat.py dùng thay build_dataset."""
    import json as _json
    # Dataset format tương thích với build_dataset() output
    dataset = []
    for it in items:
        name = it.get("new_name") or it.get("src_name", "")
        dataset.append({
            "ten": name,
            "loai": it.get("tag", ""),
            "folder": it.get("folder", ""),
            "nguoi": it.get("subject", ""),
            "quan_he": it.get("relation", ""),
            "tom_tat": (it.get("md_content") or it.get("summary") or "")[:2000],
            "du_lieu": it.get("extracted") or {},
            "confidence": it.get("confidence", ""),
            "needs_review": bool(it.get("needs_review")),
            "drive_link": it.get("drive_link", ""),
            "content_hash": it.get("content_hash", ""),
            "source": it.get("source", "bot"),
        })
    cache_path = WIKI_DIR / f"{_safe_filename(case_folder_id)}.ctx.json"
    try:
        existing_raw = cache_path.read_text(encoding="utf-8") if cache_path.exists() else "[]"
        existing = _json.loads(existing_raw)
    except Exception:
        existing = []
    # Merge: giữ existing entries không trùng ten với items mới
    new_names = {d["ten"] for d in dataset}
    merged = [d for d in existing if d.get("ten") not in new_names] + dataset
    cache_path.write_text(_json.dumps(merged, ensure_ascii=False), encoding="utf-8")


def _build_fallback(
    items: list[dict],
    applicant: str,
    visa: str,
    case_folder_id: str,
    timestamp: str,
    log_entry: str,
    existing_log: str,
) -> str:
    """Sinh wiki không cần LLM — fallback khi GLM lỗi."""
    by_person: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        subj = it.get("subject") or applicant or "Không rõ"
        by_person[subj].append(it)

    lines = [
        f"# Hồ sơ: {applicant}{f' — {visa}' if visa else ''}",
        f"Cập nhật: {timestamp} | Tài liệu: {len(items)} | Case: {case_folder_id}",
        "",
        "## Nhân thân",
    ]
    for person, docs in by_person.items():
        rel = docs[0].get("relation", "") or "đương đơn"
        lines.append(f"- **{person}** ({rel}): {', '.join(d.get('tag', '?') for d in docs)}")

    lines += ["", "## Chi tiết từng tài liệu", ""]
    for it in items:
        name = it.get("new_name") or it.get("src_name", "?")
        lines += [
            f"### {name}",
            (
                f"Loại: {it.get('tag', '')} | Người: {it.get('subject', '')} | "
                f"Quan hệ: {it.get('relation', '') or 'đương đơn'} | "
                f"Folder: {it.get('folder', '')} | Confidence: {it.get('confidence', '')}"
            ),
            f"Drive: {it.get('drive_link', '')}",
            "",
            it.get("md_content") or it.get("summary") or "(no OCR)",
            "",
        ]

    lines += ["## Cập nhật", log_entry]
    if existing_log:
        lines.append(existing_log)
    return "\n".join(lines)
