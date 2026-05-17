#!/usr/bin/env python3
"""
scan_pipeline.py — robust, idempotent batch processor for Đồng Hành visa documents.

Part of the scan-ho-so app. Run it directly, or via the bot (`telegram_listener.py`
spawns it as a subprocess), or via the OpenClaw `scan-ho-so-pipeline` skill
(`../skills/scan-ho-so-pipeline/SKILL.md` — that file is just the procedure docs;
the code is here).

Pipeline (the SOP "unzip → OCR/summarize → rename → upload to Drive" task):
  1. Enumerate EVERY real file in the input .zip / directory (recursive),
     skipping macOS junk (__MACOSX, ._*). Unsupported extensions are NOT
     dropped — they are still uploaded (classified by filename only), so the
     count in == count out.
  2. DocAI OCR IN PARALLEL (thread pool, SCAN_OCR_WORKERS default 5).
  3. GPT vision classify IN PARALLEL (same thread pool, same workers) — first-page
     image + DocAI text → JSON with md_content + doc metadata.
     (Dedup + Drive upload stay sequential — the Drive client isn't thread-safe.)
  3. Upload the renamed file to its top folder under the case folder.
  4. Write a .json + .md sidecar into "_Bot OCR & Metadata".
  5. Retry each file up to --retries times with exponential backoff on ANY
     error. Uploads are skip-by-destination-name, so retries / re-runs are safe.
  6. Write a manifest.json covering EVERY input file with its final status
     (uploaded | duplicate | uploaded-no-ocr | failed | skipped-junk).
  7. Exit 0 only if nothing is still `failed`; otherwise exit 1 so the caller
     knows to re-run (which will pick up only the unfinished files).

Usage:
  scan_pipeline.py INPUT --case-folder-id ID --applicant "Hoang Thi Mo" \
      [--case-id MoTest91-WP10m] [--manifest PATH] [--retries 3] [--dry-run]
  scan_pipeline.py INPUT --from-registry <telegram_chat_id>   # resolve case from group_registry.json
  scan_pipeline.py --self-test

The Drive whitelist still applies: this only ever creates folders/files *under*
the case folder you pass (which itself lives under the bot's OpenClaw/Bot-folder
sandbox). It never lists or touches anything else.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import functools
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

# --- this file lives in the scan-ho-so app dir; lib/ and data/ are right here ---
# (SCAN_HO_SO_DIR env var can override, e.g. if you run a copy from elsewhere).
SCAN_HO_SO_DIR = Path(os.environ.get("SCAN_HO_SO_DIR", str(Path(__file__).resolve().parent)))
if str(SCAN_HO_SO_DIR) not in sys.path:
    sys.path.insert(0, str(SCAN_HO_SO_DIR))

# --- load env (OPENROUTER_API_KEY, GOOGLE_APPLICATION_CREDENTIALS, ...) ------
for env_path in (
    Path(os.environ.get("SCAN_OCR_ENV", "")) if os.environ.get("SCAN_OCR_ENV") else None,
    SCAN_HO_SO_DIR.parent / "scan-ocr.env",
    Path.home() / "scan-ocr.env",
):
    if env_path and env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        break

SHARED_DRIVE_ID = os.environ.get("SHARED_DRIVE_ID", "0AIYOQpLqtMPvUk9PVA")
# Model for per-doc vision classify (gpt-5-mini or any vision-capable model).
OCR_CLASSIFY_MODEL = os.environ.get("OCR_CLASSIFY_MODEL", "gpt-5-mini")
# Document AI processor id — REQUIRED.
DOCAI_PROCESSOR_ID = os.environ.get("GOOGLE_DOCUMENTAI_PROCESSOR_ID", "")
# Per-doc JSON schema (strict mode) for docai_classify_vision().
DOC_RESULT_SCHEMA = {
    "name": "doc_result",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["tag", "folder", "filename", "subject", "relation",
                     "confidence", "needs_vision", "person", "summary_vi", "md_content"],
        "properties": {
            "tag":          {"type": "string"},
            "folder":       {"type": "string",
                             "enum": ["Personal Docs", "Education", "Asset", "Employment"]},
            "filename":     {"type": "string"},
            "subject":      {"type": "string"},
            "relation":     {"type": "string",
                             "enum": ["applicant", "cha", "me", "vo", "chong",
                                      "con", "anh_chi_em", "khac", ""]},
            "confidence":   {"type": "string", "enum": ["high", "medium", "low"]},
            "needs_vision": {"type": "boolean"},
            "person": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["full_name", "date_of_birth", "relation"],
                    "properties": {
                        "full_name":     {"type": "string"},
                        "date_of_birth": {"type": "string"},
                        "relation":      {"type": "string"},
                    },
                },
            },
            "summary_vi": {"type": "string"},
            "md_content": {"type": "string"},
        },
    },
}
# Extensions DocAI can OCR (images + PDF; .doc/.docx converted via LibreOffice → PDF first).
DOCAI_OCR_EXTS = {
    ".pdf", ".jpg", ".jpeg", ".png",
    ".tiff", ".tif", ".bmp", ".webp", ".gif",
    ".doc", ".docx",
}
TOP_FOLDERS = ["Personal Docs", "Education", "Asset", "Employment"]
OCR_META_FOLDER = "_Bot OCR & Metadata"
DA_DUYET_FOLDER = "Đã duyệt"   # staff review folder — bot reads, never writes files here
# Số file được DocAI OCR + GPT vision classify ĐỒNG THỜI. Upload Drive + thẩm định vẫn tuần tự.
OCR_WORKERS = max(1, int(os.environ.get("SCAN_OCR_WORKERS", "5")))

OTHER_EXT_MIME = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".avi": "video/x-msvideo",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


# ============================================================================
# logging
# ============================================================================
def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ============================================================================
# file enumeration  (the part that used to "miss files")
# ============================================================================
def is_macos_junk(rel: Path) -> bool:
    return "__MACOSX" in rel.parts or rel.name.startswith("._") or rel.name == ".DS_Store"


def collect_from_dir(root: Path) -> list[tuple[Path, str]]:
    """Return [(abs_path, original_basename)] for every real file under root."""
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if is_macos_junk(rel):
            continue
        out.append((p, p.name))
    return out


def collect_from_zip(zip_path: Path, workdir: Path) -> list[tuple[Path, str]]:
    """Extract every real member of the zip into workdir; return [(path, basename)]."""
    out = []
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist()
                   if not m.is_dir() and not is_macos_junk(Path(m.filename))]
        for i, m in enumerate(members, 1):
            base = Path(m.filename).name
            if not base:
                continue
            dst = workdir / f"{i:03d}_{base}"
            with zf.open(m) as src, dst.open("wb") as fh:
                shutil.copyfileobj(src, fh)
            out.append((dst, base))
    return out


# ============================================================================
# DocAI OCR → gpt-5-mini structure extraction
# ============================================================================
def _call_classify_api(payload: dict, timeout: int = 120) -> str:
    """Call gpt-5-mini (OpenAI) → fallback OpenRouter. Returns raw response text."""
    import httpx

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = payload.get("model", OCR_CLASSIFY_MODEL)

    if openai_key:
        endpoint = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {openai_key}"}
    elif openrouter_key:
        endpoint = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {openrouter_key}"}
    else:
        raise RuntimeError("Thiếu OPENAI_API_KEY và OPENROUTER_API_KEY")

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(endpoint, headers=headers, json=payload)
        if resp.status_code >= 400:
            payload2 = {**payload, "response_format": {"type": "json_object"}}
            payload2.pop("response_format", None) if "json_schema" not in str(payload.get("response_format", {})) else None
            payload2["response_format"] = {"type": "json_object"}
            resp = client.post(endpoint, headers=headers, json=payload2)
        if resp.status_code >= 400:
            payload3 = {k: v for k, v in payload.items() if k != "response_format"}
            resp = client.post(endpoint, headers=headers, json=payload3)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def docai_classify_vision(
    pages_text: list[dict],
    filename: str,
    applicant: str = "",
    image_b64: str | None = None,
) -> dict:
    """DocAI OCR pages + optional first-page image → gpt-5-mini → DOC_RESULT_SCHEMA dict.

    Returns dict with keys: tag, folder, filename, subject, relation, confidence,
    needs_vision, person[], summary_vi, md_content. Returns fallback on error.
    """
    _FALLBACK = {
        "tag": "Khac", "folder": "Personal Docs", "filename": filename,
        "subject": "", "relation": "", "confidence": "low",
        "needs_vision": False, "person": [],
        "summary_vi": "(classify lỗi)", "md_content": "",
    }
    try:
        from lib.rule_loader import generate_doc_type_catalog
        _doc_catalog = generate_doc_type_catalog()
    except Exception:  # noqa: BLE001
        _doc_catalog = ""

    if not pages_text:
        text_block = "(không có text OCR)"
    elif len(pages_text) == 1:
        text_block = pages_text[0].get("text", "") or "(không đọc được)"
    else:
        parts = [f"[Trang {p['page']}]\n{p.get('text', '')}" for p in pages_text]
        text_block = "\n\n".join(parts)

    applicant_line = f'Đương đơn chính (applicant): "{applicant}"\n' if applicant else ""

    system_prompt = (
        "Bạn là chuyên gia phân loại và trích xuất hồ sơ visa Canada. "
        "Phân tích ảnh (nếu có) và text OCR → trả JSON theo schema được cung cấp.\n\n"
        f"{applicant_line}"
        f"DANH MỤC LOẠI GIẤY TỜ (tag PHẢI match TÊN trong danh sách):\n{_doc_catalog or '(catalog không load được)'}\n\n"
        "PHÂN LOẠI THEO BẢN CHẤT GIẤY TỜ (không theo thông tin được nhắc tới):\n"
        "• CCCD = tấm thẻ in 2 mặt có ảnh chân dung + chip/QR\n"
        "• Sao kê ngân hàng = CÓ kỳ sao kê (từ ngày–đến ngày) + danh sách giao dịch nhiều dòng + số dư đầu/cuối kỳ\n"
        '• Thông tin cá nhân (tự khai) → tag="CV" — khi khách hàng tự ghi/điền biểu mẫu\n'
        '• Ảnh thẻ (chân dung 1 người, phông đơn sắc) → tag="Anh the"\n'
        "• Ảnh trên giấy tờ (CCCD, hộ chiếu, bằng cấp) → phân loại theo giấy tờ đó, KHÔNG phải ảnh thẻ\n"
        "• bs = bản sao (viết tắt), KHÔNG phải tên người và KHÔNG phải bố\n"
        "• KHÔNG suy diễn relation nếu văn bản không ghi rõ chữ 'cha/bố/mẹ/vợ/chồng/con'\n\n"
        "FIELD md_content: Viết markdown tóm tắt TOÀN BỘ thông tin quan trọng của giấy tờ này "
        "(tất cả ngày tháng, số hiệu, tên, địa chỉ, số tiền, hạn sử dụng). "
        "Đây là nguồn data cho step thẩm định — càng đầy đủ càng tốt.\n"
        "Ví dụ cho CCCD:\n"
        "# CCCD - Nguyễn Văn A\n"
        "**Số CCCD:** 079123456789  **Ngày cấp:** 15/01/2024\n"
        "**Họ tên:** NGUYỄN VĂN A  **Ngày sinh:** 01/01/1990  **Giới tính:** Nam\n"
        "**Quê quán:** Xã Mỹ Lộc, Tam Bình, Vĩnh Long\n"
        "**Thường trú:** 45 Đường Nguyễn Trãi, P.2, TP Vĩnh Long"
    )

    text_prompt = f"Tên file: {filename}\n\nTEXT OCR:\n{text_block[:12000]}"

    if image_b64:
        user_content: list | str = [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            {"type": "text", "text": text_prompt},
        ]
    else:
        user_content = text_prompt

    payload = {
        "model": OCR_CLASSIFY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_schema", "json_schema": DOC_RESULT_SCHEMA},
    }
    try:
        text = _call_classify_api(payload)
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        d = json.loads(text)
        if isinstance(d, dict) and "tag" in d:
            d.setdefault("person", [])
            d.setdefault("md_content", "")
            return d
        return _FALLBACK
    except Exception as e:  # noqa: BLE001
        return {**_FALLBACK, "summary_vi": f"(classify lỗi: {type(e).__name__}: {e})"}


def _docai_ocr_one(path: "Path", src_name: str) -> "list[dict] | None":
    """OCR 1 file bằng DocAI, trả pages_text hoặc None."""
    try:
        from lib.docai_client import ocr_with_docai
        return ocr_with_docai(path)
    except Exception as e:  # noqa: BLE001
        log(f"  DocAI OCR lỗi cho {src_name}: {type(e).__name__}: {e}")
        return None


def _detect_pdf_segments(pages_text: list[dict], applicant: str) -> list[dict] | None:
    """One GPT call on all per-page OCR text → detect document boundaries.

    Returns list of segments [{"pages": [1,2,...], "tag": "CCCD"}, ...] when the
    PDF contains multiple documents. Returns None if single-doc or on error.
    Only meaningful for PDFs with >1 page (caller must check).
    """
    if len(pages_text) <= 1:
        return None

    page_blocks = [f'[Trang {p["page"]}]\n{p.get("text","") or "(trống)"}' for p in pages_text]
    combined = "\n\n".join(page_blocks)

    try:
        from lib.rule_loader import generate_doc_type_catalog
        tag_list = ", ".join(
            t.get("tag", "") for t in __import__("yaml").safe_load(
                open(SCAN_HO_SO_DIR / "data" / "doc_types.yaml", encoding="utf-8")
            ).get("doc_types", []) if t.get("tag")
        )
    except Exception:  # noqa: BLE001
        tag_list = "CCCD, Passport, GPLX, GKS, GKH, LLTP, XNCT, CT07, STK, Saoke, Sodat, DKKD, BHYT, BHXH, Anhthe, CV, Bangcap, Khac"

    system = (
        "Bạn phân tích văn bản OCR từ một file PDF có thể chứa nhiều loại giấy tờ Việt Nam ghép lại. "
        "Xác định ranh giới giữa các giấy tờ dựa vào nội dung OCR từng trang. "
        "Nếu toàn bộ file là một giấy tờ, trả đúng 1 segment. "
        "Trả về JSON: {\"segments\": [{\"pages\": [<số trang 1-based>,...], \"tag\": \"<loại>\"}]}"
    )
    user = (
        f"Khách hàng: {applicant}\n\n"
        f"Nội dung OCR từng trang:\n{combined}\n\n"
        f"tag phải là một trong: {tag_list}\n"
        "Trả về JSON với danh sách segment, mỗi segment là một giấy tờ riêng biệt. "
        "Đảm bảo mỗi trang xuất hiện đúng 1 lần trong đúng 1 segment.\n"
        "QUY TẮC GỘP TRANG: Các trang liên tiếp từ CÙNG đơn vị phát hành (cùng tên công ty/cơ quan) "
        "và liên quan đến cùng một giao dịch/hồ sơ thì GỘP vào 1 segment. "
        "Chỉ tách segment mới khi đơn vị phát hành KHÁC hoặc loại giấy tờ hoàn toàn khác nhau."
    )

    payload = {
        "model": OCR_CLASSIFY_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        raw = _call_classify_api(payload, timeout=60)
        data = json.loads(raw)
        segments = data.get("segments", [])
        if not segments or len(segments) <= 1:
            return None
        # Validate: every page covered exactly once
        all_pages = [pg for seg in segments for pg in seg.get("pages", [])]
        expected = list(range(1, len(pages_text) + 1))
        if sorted(all_pages) != expected:
            log(f"  _detect_pdf_segments: page coverage invalid {sorted(all_pages)} vs {expected}, skip split")
            return None
        return segments
    except Exception as e:  # noqa: BLE001
        log(f"  _detect_pdf_segments error (skip split): {type(e).__name__}: {e}")
        return None


def _split_pdf_segments(path: Path, segments: list[dict], workdir: Path) -> list[tuple[Path, dict]]:
    """Extract page ranges from a PDF into workdir (temp dir). Returns [(seg_path, seg_meta)]."""
    import io as _io
    import pypdf
    reader = pypdf.PdfReader(str(path))
    results = []
    for seg in segments:
        pages = seg.get("pages", [])
        if not pages:
            continue
        writer = pypdf.PdfWriter()
        for pg in pages:
            idx = pg - 1   # 0-based
            if 0 <= idx < len(reader.pages):
                writer.add_page(reader.pages[idx])
        buf = _io.BytesIO()
        writer.write(buf)
        seg_name = f"{path.stem}[p{pages[0]}-{pages[-1]}]{path.suffix}"
        seg_path = workdir / seg_name
        seg_path.write_bytes(buf.getvalue())
        results.append((seg_path, seg))
    return results


def docai_prefetch(files: list, *, dry_run: bool, workers: int) -> dict:
    """DocAI OCR ĐỒNG THỜI mọi file OCR-được → {src_name: pages_text | None}.
    File không OCR được (ext lạ) hoặc khi --dry-run: bỏ qua (không thêm vào dict)."""
    todo = [(p, n) for (p, n) in files if (not dry_run) and p.suffix.lower() in DOCAI_OCR_EXTS]
    if not todo:
        return {}
    n_workers = max(1, min(workers, len(todo)))
    log(f"DocAI OCR song song: {len(todo)} file, {n_workers} luồng …")
    out: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        fut_to_name = {ex.submit(_docai_ocr_one, p, n): n for (p, n) in todo}
        for fut in concurrent.futures.as_completed(fut_to_name):
            name = fut_to_name[fut]
            try:
                out[name] = fut.result()
            except Exception as e:  # noqa: BLE001
                log(f"  DocAI prefetch future lỗi cho {name}: {type(e).__name__}: {e}")
                out[name] = None
    ok = sum(1 for v in out.values() if isinstance(v, list))
    log(f"DocAI OCR xong: {ok}/{len(out)} ok" + ("" if ok == len(out) else f" ({len(out) - ok} sẽ thử lại tuần tự)"))
    return out



def vision_prefetch(files: list, ocr_cache: dict, applicant: str, *,
                    dry_run: bool, workers: int) -> dict:
    """GPT vision classify ĐỒNG THỜI mọi file OCR-được → {src_name: gem_dict}.
    File không OCR được hoặc khi --dry-run: bỏ qua."""
    todo = [(p, n) for (p, n) in files if (not dry_run) and p.suffix.lower() in DOCAI_OCR_EXTS]
    if not todo:
        return {}
    n_workers = max(1, min(workers, len(todo)))
    log(f"GPT vision classify song song: {len(todo)} file, {n_workers} luồng …")

    def _classify_one(path: Path, src_name: str) -> dict:
        pages_text = ocr_cache.get(src_name) or []
        image_bytes = _rasterize_first_page(path)
        image_b64 = base64.b64encode(image_bytes).decode() if image_bytes else None
        return docai_classify_vision(pages_text, src_name, applicant, image_b64)

    out: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        fut_to_name = {ex.submit(_classify_one, p, n): n for (p, n) in todo}
        for fut in concurrent.futures.as_completed(fut_to_name):
            name = fut_to_name[fut]
            try:
                out[name] = fut.result()
            except Exception as e:  # noqa: BLE001
                log(f"  vision prefetch lỗi cho {name}: {type(e).__name__}: {e}")
                out[name] = {}
    ok = sum(1 for v in out.values() if v.get("tag"))
    log(f"GPT vision xong: {ok}/{len(out)} ok" + ("" if ok == len(out) else f" ({len(out) - ok} thất bại)"))
    return out


def _strip_trailing_year(name: str) -> str:
    """P2.5 — 'Nguyen Thi Anh 1999' → 'Nguyen Thi Anh'. Áp dụng cho applicant fallback
    để KHÔNG ép năm sinh vào tên file. Group title (parse_group_title) giữ năm cho
    case_id, nhưng tên file phải sạch."""
    if not name:
        return name
    return re.sub(r"\s+(?:19|20)\d{2}\s*$", "", name).strip()


def subject_from_gemini(gem: dict, fallback: str) -> str:
    person = gem.get("person")
    name = ""
    if isinstance(person, list) and person:
        p0 = person[0]
        name = (p0.get("full_name") or p0.get("name") or "") if isinstance(p0, dict) else str(p0)
    elif isinstance(person, dict):
        name = person.get("full_name") or person.get("name") or ""
    elif isinstance(person, str):
        name = person
    return name or _strip_trailing_year(fallback)


# ----------------------------------------------------------------------------
# filename collision handling — never let two distinct source files in the same
# batch collapse to one Drive name (that silently loses a file). Per-run only:
# the Nth file of a given (tag, subject) gets " N" appended (SOP "file thứ N"),
# so re-running the same input reproduces the same names and stays idempotent.
# ----------------------------------------------------------------------------
def dedup_name(name_registry: dict, tag: str, subject_title: str, ext: str,
               is_english: bool, build_filename, relation: str | None = None) -> str:
    # relation cũng tham gia vào key dedup — tránh "CCCD" + "CCCD ba" cùng subject collide.
    key = (tag.lower().strip(), (relation or "").lower().strip(), subject_title.lower().strip())
    n = name_registry.get(key, 0) + 1
    name_registry[key] = n
    return build_filename(tag, subject_title, ext,
                          relation=relation, index=(n if n > 1 else None), is_english=is_english)


def _count_pdf_pages(path: Path) -> int:
    """Đếm số trang PDF. Trả 0 nếu lỗi (file corrupt / không phải PDF)."""
    try:
        import pypdf
        return len(pypdf.PdfReader(str(path)).pages)
    except Exception as e:  # noqa: BLE001
        log(f"  _count_pdf_pages({path.name}) lỗi: {type(e).__name__}: {e}")
        return 0


def _rasterize_first_page(path: Path) -> bytes | None:
    """PDF → rasterize page 0 at 150 DPI → JPEG bytes. Image → read bytes directly.
    Returns None on any error or unsupported format (e.g. .docx)."""
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tiff", ".tif"}:
        try:
            return path.read_bytes()
        except Exception:  # noqa: BLE001
            return None
    if ext == ".pdf":
        try:
            import io
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(str(path))
            if len(doc) == 0:
                return None
            page = doc[0]
            bitmap = page.render(scale=150 / 72)  # 150 DPI
            pil_image = bitmap.to_pil()
            buf = io.BytesIO()
            pil_image.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
        except Exception:  # noqa: BLE001
            return None
    return None


# ============================================================================
# Fix 7 — hash-based dedup: tránh upload trùng khi staff vô tình gửi lại cùng file.
# Cache trong-process: load 1 lần khi process_one đầu tiên cần, dùng lại trong cùng batch.
# ============================================================================
_HASH_CACHE: dict[str, dict[str, dict]] = {}   # {case_folder_id: {content_hash: sidecar_dict}}


def _find_sidecar_by_hash(case_folder_id: str, content_hash: str) -> dict | None:
    """Tra trong _Bot OCR & Metadata của case xem có sidecar nào content_hash khớp.
    Lazy load: lần đầu list folder + download mọi sidecar .json; subsequent hit cache."""
    if not case_folder_id or not content_hash:
        return None
    cache = _HASH_CACHE.get(case_folder_id)
    if cache is None:
        cache = {}
        try:
            from lib.drive_helpers import get_or_create_folder, list_folder, download_file_text
            meta_id = get_or_create_folder(OCR_META_FOLDER, case_folder_id, drive_id=SHARED_DRIVE_ID)
            files = list_folder(meta_id, drive_id=SHARED_DRIVE_ID)
            for name, fid in files.items():
                if not name.lower().endswith(".json"):
                    continue
                try:
                    d = json.loads(download_file_text(fid, drive_id=SHARED_DRIVE_ID))
                    h = d.get("content_hash") if isinstance(d, dict) else None
                    if h:
                        cache[h] = d
                except Exception:  # noqa: BLE001
                    continue
        except Exception as e:  # noqa: BLE001
            log(f"  _HASH_CACHE load failed for {case_folder_id}: {e}")
        _HASH_CACHE[case_folder_id] = cache
    return cache.get(content_hash)


# ============================================================================
# P1.4 — sweep stray .md / .json metadata sidecars khỏi 4 folder khách.
# Staff báo Vang.jpg trong Asset có Vang 2.md, Vang 4.json bên cạnh — đó là sidecar
# bị upload lạc folder (có thể remnant từ version cũ, hoặc cache Drive trả nhầm id).
# Sweeper scan Asset/Employment/Education/Personal Docs, move mọi .md/.json về
# _Bot OCR & Metadata. Idempotent: nếu cùng tên đã có trong meta folder → suffix _dup<ts>.
# ============================================================================
def sweep_stray_sidecars(case_folder_id: str) -> dict:
    """Trả {moved: int, skipped: int, errors: int, by_folder: {folder: count}}."""
    out = {"moved": 0, "skipped": 0, "errors": 0, "by_folder": {}}
    if not case_folder_id:
        return out
    try:
        from lib.drive_helpers import (get_or_create_folder, list_folder,
                                       rename_file, move_file, find_file_by_name)
    except Exception as e:  # noqa: BLE001
        log(f"  sweep_stray_sidecars: import drive_helpers lỗi: {e}")
        out["errors"] += 1
        return out
    try:
        meta_id = get_or_create_folder(OCR_META_FOLDER, case_folder_id, drive_id=SHARED_DRIVE_ID)
        existing_meta = list_folder(meta_id, drive_id=SHARED_DRIVE_ID)
    except Exception as e:  # noqa: BLE001
        log(f"  sweep_stray_sidecars: không truy cập được {OCR_META_FOLDER}: {e}")
        out["errors"] += 1
        return out
    for top in TOP_FOLDERS:
        try:
            top_id = get_or_create_folder(top, case_folder_id, drive_id=SHARED_DRIVE_ID)
            files = list_folder(top_id, drive_id=SHARED_DRIVE_ID)
        except Exception as e:  # noqa: BLE001
            log(f"  sweep_stray_sidecars: skip folder {top}: {e}")
            out["errors"] += 1
            continue
        for name, fid in files.items():
            low = name.lower()
            if not (low.endswith(".md") or low.endswith(".json")):
                continue
            # Đây là sidecar lạc folder — move về meta folder.
            target_name = name
            if name in existing_meta:
                ts = time.strftime("%Y%m%d-%H%M%S")
                stem, _, ext = name.rpartition(".")
                target_name = f"{stem}_dup{ts}.{ext}"
                try:
                    rename_file(fid, target_name, drive_id=SHARED_DRIVE_ID)
                except Exception as e:  # noqa: BLE001
                    log(f"  sweep: rename {name!r} → {target_name!r} lỗi: {e}")
                    out["errors"] += 1
                    continue
            try:
                move_file(fid, meta_id, drive_id=SHARED_DRIVE_ID)
                out["moved"] += 1
                out["by_folder"][top] = out["by_folder"].get(top, 0) + 1
                existing_meta[target_name] = fid   # update cache trong loop
                log(f"  sweep: moved {top}/{name!r} → {OCR_META_FOLDER}/{target_name!r}")
            except Exception as e:  # noqa: BLE001
                log(f"  sweep: move {name!r} lỗi: {e}")
                out["errors"] += 1
    if out["moved"] or out["errors"]:
        log(f"  sweep_stray_sidecars done: moved={out['moved']} errors={out['errors']} by_folder={out['by_folder']}")
    return out


# ============================================================================
# Đã duyệt — OCR file staff review → sidecar trong _Bot OCR & Metadata/
# ============================================================================
def scan_da_duyet_folder(case_folder_id: str, applicant: str, case_id: str) -> None:
    """OCR file mới trong 'Đã duyệt/' → sidecar (source=da-duyet) trong _Bot OCR & Metadata/.
    Idempotent: file đã có sidecar → skip. Bot không ghi/rename file trong Đã duyệt/."""
    if not case_folder_id:
        return
    try:
        from lib.drive_helpers import get_or_create_folder, list_folder, download_file_bytes, upload_file
        from lib.sop_naming import classify_doc_type
    except Exception as e:  # noqa: BLE001
        log(f"scan_da_duyet: import lỗi: {e}")
        return
    try:
        da_duyet_id = get_or_create_folder(DA_DUYET_FOLDER, case_folder_id, drive_id=SHARED_DRIVE_ID)
        da_duyet_files = list_folder(da_duyet_id, drive_id=SHARED_DRIVE_ID)
    except Exception as e:  # noqa: BLE001
        log(f"scan_da_duyet: không truy cập {DA_DUYET_FOLDER}/: {e}")
        return
    if not da_duyet_files:
        return
    try:
        meta_id = get_or_create_folder(OCR_META_FOLDER, case_folder_id, drive_id=SHARED_DRIVE_ID)
        existing_meta = list_folder(meta_id, drive_id=SHARED_DRIVE_ID)
    except Exception as e:  # noqa: BLE001
        log(f"scan_da_duyet: không đọc {OCR_META_FOLDER}: {e}")
        return

    n_new = 0
    for filename, fid in da_duyet_files.items():
        sidecar_name = f"da-duyet - {filename}.json"
        if sidecar_name in existing_meta:
            continue  # đã OCR lần trước
        ext = Path(filename).suffix.lower()
        if ext not in DOCAI_OCR_EXTS and ext not in OTHER_EXT_MIME:
            continue
        log(f"  da-duyet: OCR {filename}")
        try:
            data = download_file_bytes(fid, drive_id=SHARED_DRIVE_ID)
            content_hash = hashlib.sha1(data).hexdigest()
            gem: dict = {}
            if ext in DOCAI_OCR_EXTS:
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    tmp.write(data)
                    tmp_path = Path(tmp.name)
                try:
                    from lib.docai_client import ocr_with_docai
                    _pages = ocr_with_docai(tmp_path)
                    if _pages:
                        gem = docai_classify_vision(_pages, filename, applicant) or {}
                finally:
                    tmp_path.unlink(missing_ok=True)
            if not isinstance(gem, dict):
                gem = {}
            raw_dt = gem.get("tag", "")
            summary = str(gem.get("summary_vi", ""))[:400]
            cls = classify_doc_type(raw_dt, summary, filename)
            subject = subject_from_gemini(gem, applicant) or _strip_trailing_year(applicant)
            relation = gem.get("relation", "") or ""
            if relation == "applicant":
                relation = ""
            item = {
                "src_name": filename, "new_name": filename, "ext": ext,
                "tag": cls.tag, "folder": DA_DUYET_FOLDER,
                "subject": subject, "relation": relation,
                "confidence": cls.confidence, "needs_review": cls.needs_review,
                "is_english": False, "ocr": ext in DOCAI_OCR_EXTS,
                "summary": summary, "md_content": gem.get("md_content", ""),
                "case_id": case_id, "source": "da-duyet",
                "drive_link": f"https://drive.google.com/file/d/{fid}/view?usp=drivesdk",
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "content_hash": content_hash,
            }
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
                json.dump(item, fh, ensure_ascii=False, indent=2)
                jpath = fh.name
            upload_file(jpath, sidecar_name, meta_id, drive_id=SHARED_DRIVE_ID, mime="application/json")
            os.unlink(jpath)
            n_new += 1
            log(f"  da-duyet: {filename} → {cls.tag} ({cls.confidence}) — sidecar OK")
        except Exception as e:  # noqa: BLE001
            log(f"  da-duyet: {filename} lỗi: {type(e).__name__}: {e}")
    if n_new:
        log(f"scan_da_duyet: {n_new} file mới OCR trong {DA_DUYET_FOLDER}/")


def _items_to_dataset(items: list[dict]) -> list[dict]:
    """Convert process_one item list → dataset format for compute_coverage() + render_doc_md()."""
    out = []
    for it in items:
        tag = it.get("tag", "")
        if not tag or tag == "Khac":
            continue
        out.append({
            "loai": tag,
            "ten": it.get("new_name") or it.get("src_name", ""),
            "nguoi": it.get("subject", ""),
            "quan_he": it.get("relation", ""),
            "tom_tat": it.get("summary", ""),
        })
    return out


# ============================================================================
# process one file (with retries)
# ============================================================================
def process_one(path: Path, src_name: str, *, case_folder_id: str, applicant: str,
                case_id: str, retries: int, dry_run: bool, sop, name_registry: dict,
                pages_text: list | None = None, gem_cache: dict | None = None,
                force_rescan: bool = False) -> dict:
    classify_doc_type, build_filename, detect_english, title_case_ascii = sop
    ext = path.suffix.lower()
    can_ocr = ext in DOCAI_OCR_EXTS
    mime = OTHER_EXT_MIME.get(ext) or "application/octet-stream"

    # Fix 7 — hash dedup: tính SHA-1 1 lần; nếu sidecar có hash khớp → skip upload, ko OCR lại.
    # P1.3 — `force_rescan` bypass dedup (cho /oldfile): bot re-OCR + re-classify, upload bản tên đúng.
    # P3.x — nếu tên trong sidecar drift (code naming thay đổi) hoặc tag cũ là Khac → re-process.
    content_hash = hashlib.sha1(path.read_bytes()).hexdigest() if not dry_run else ""
    _old_drive_info = None  # set khi re-process do name drift / was-Khac → dùng để xoá file cũ sau
    if content_hash and case_folder_id and not dry_run and not force_rescan:
        existing = _find_sidecar_by_hash(case_folder_id, content_hash)
        if existing:
            _old_name = existing.get("new_name", "")
            _old_tag  = existing.get("tag", "Khac")
            # Rebuild tên theo code HIỆN TẠI để phát hiện naming drift
            try:
                _rebuilt = build_filename(
                    _old_tag,
                    existing.get("subject", ""),
                    existing.get("ext", path.suffix.lower()),
                    relation=existing.get("relation"),
                    is_english=bool(existing.get("is_english")),
                )
            except Exception:  # noqa: BLE001
                _rebuilt = _old_name
            _name_drifted = bool(_old_name and _rebuilt != _old_name)
            # da-duyet file được staff review — nếu tag Khac vẫn giữ nguyên (không re-OCR)
            _was_khac     = (_old_tag == "Khac") and existing.get("source") != "da-duyet"
            if not _name_drifted and not _was_khac:
                log(f"  {src_name} → duplicate-by-hash (đã có {_old_name!r}, skip upload)")
                return {
                    "src_name": src_name,
                    "new_name": _old_name,
                    "ext": ext,
                    "tag": _old_tag,
                    "folder": existing.get("folder", ""),
                    "subject": existing.get("subject", ""),
                    "relation": existing.get("relation"),
                    "confidence": existing.get("confidence", ""),
                    "needs_review": bool(existing.get("needs_review")),
                    "is_english": bool(existing.get("is_english")),
                    "ocr": True,
                    "summary": existing.get("summary", ""),
                    "extracted": existing.get("extracted", {}),
                    "case_id": case_id,
                    "status": "duplicate-by-hash",
                    "drive_link": existing.get("drive_link", ""),
                    "content_hash": content_hash,
                }
            # Tên drift hoặc tag cũ Khac → fall through để re-OCR + re-classify + upload tên đúng
            _reason = "name-drift" if _name_drifted else "was-Khac"
            log(f"  {src_name} → re-process ({_reason}): old={_old_name!r} rebuilt={_rebuilt!r}")
            _old_drive_info = {"old_name": _old_name, "old_folder": existing.get("folder") or "Personal Docs"}

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            gem: dict = {}
            if can_ocr and not dry_run:
                if gem_cache is not None:
                    # Pre-fetched by vision_prefetch() — use directly
                    gem = gem_cache
                else:
                    # Fallback: inline classify (retry path or non-prefetched file)
                    _pages = pages_text if isinstance(pages_text, list) else None
                    if _pages is None:
                        from lib.docai_client import ocr_with_docai
                        _pages = ocr_with_docai(path)
                    image_bytes = _rasterize_first_page(path)
                    image_b64 = base64.b64encode(image_bytes).decode() if image_bytes else None
                    gem = docai_classify_vision(_pages or [], src_name, applicant, image_b64)
            if not isinstance(gem, dict):
                gem = {}

            raw_dt = gem.get("tag", "")
            summary = str(gem.get("summary_vi", ""))[:400]
            md_content = str(gem.get("md_content", ""))
            cls = classify_doc_type(raw_dt, summary, src_name)
            needs_review = cls.needs_review or (not can_ocr)
            subject_raw = subject_from_gemini(gem, applicant) or _strip_trailing_year(applicant)
            subject_title = title_case_ascii(subject_raw) or "Unknown"
            is_eng = detect_english(summary, "")
            relation = gem.get("relation", "") or ""
            if relation == "applicant":
                relation = ""
            # Only the first retry attempt may consume a registry slot per file;
            # build it once on attempt 1 and reuse it on later attempts.
            if attempt == 1 or "new_name" not in locals():
                new_name = dedup_name(name_registry, cls.tag, subject_title, path.suffix, is_eng, build_filename,
                                      relation=relation)

            item = {
                "src_name": src_name, "new_name": new_name, "ext": ext,
                "tag": cls.tag, "folder": cls.folder, "subject": subject_title, "relation": relation,
                "confidence": cls.confidence if can_ocr else "low",
                "needs_review": needs_review, "is_english": is_eng,
                "ocr": can_ocr, "summary": summary, "md_content": md_content,
                "extracted": {},   # kept for backward compat (build_dataset reads as du_lieu)
                "content_hash": content_hash,
                "case_id": case_id,
            }

            if dry_run:
                item["status"] = "dry-run"
                item["drive_link"] = ""
                return item

            from lib.drive_helpers import get_or_create_folder, upload_file
            top_id = get_or_create_folder(cls.folder, case_folder_id, drive_id=SHARED_DRIVE_ID)
            up = upload_file(str(path), new_name, top_id, drive_id=SHARED_DRIVE_ID, mime=mime)
            item["drive_link"] = up["link"]
            item["status"] = "duplicate" if up.get("skipped") else ("uploaded" if can_ocr else "uploaded-no-ocr")

            # Nếu re-process do name drift: xoá file cũ sai tên trên Drive (best-effort)
            if _old_drive_info and item.get("status") == "uploaded":
                try:
                    from lib.drive_helpers import list_folder, delete_file
                    _old_top = get_or_create_folder(
                        _old_drive_info["old_folder"], case_folder_id, drive_id=SHARED_DRIVE_ID)
                    _old_fid = list_folder(_old_top, drive_id=SHARED_DRIVE_ID).get(_old_drive_info["old_name"])
                    if _old_fid:
                        delete_file(_old_fid, drive_id=SHARED_DRIVE_ID)
                        log(f"  đã xoá file cũ sai tên: {_old_drive_info['old_name']!r}")
                except Exception as _de:  # noqa: BLE001
                    log(f"  không xoá được file cũ {_old_drive_info.get('old_name')!r}: {_de}")

            # sidecars (best-effort: a sidecar failure must not lose the file).
            # Named after the full new_name (incl. extension) so two source files
            # that differ only by extension can't overwrite each other's metadata.
            try:
                meta_id = get_or_create_folder(OCR_META_FOLDER, case_folder_id, drive_id=SHARED_DRIVE_ID)
                stem = new_name
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
                    json.dump(item, fh, ensure_ascii=False, indent=2)
                    jpath = fh.name
                upload_file(jpath, f"{stem}.json", meta_id, drive_id=SHARED_DRIVE_ID, mime="application/json")
                os.unlink(jpath)
                review = " ⚠️ Cần kiểm tra" if needs_review else ""
                eng = " 🌐 ENG" if is_eng else ""
                md = (
                    f"# {new_name}\n\n"
                    f"**Loại:** {cls.tag} | **Folder:** {cls.folder}\n"
                    f"**Người:** {item['subject']} | **Confidence:** {item['confidence']}{review}{eng}\n"
                    f"**File gốc:** {src_name}\n\n"
                    + (md_content if md_content else f"## Tóm tắt\n{summary or '(no OCR)'}")
                )
                with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
                    fh.write(md)
                    mpath = fh.name
                upload_file(mpath, f"{stem}.md", meta_id, drive_id=SHARED_DRIVE_ID, mime="text/markdown")
                os.unlink(mpath)
            except Exception as side_err:  # noqa: BLE001
                item["sidecar_error"] = str(side_err)

            return item
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"  attempt {attempt}/{retries} failed for {src_name}: {e}")
            if attempt < retries:
                time.sleep(min(2 ** attempt, 30))

    return {
        "src_name": src_name, "ext": ext, "status": "failed",
        "error": f"{type(last_err).__name__}: {last_err}",
    }


# ============================================================================
# registry helper
# ============================================================================
def resolve_from_registry(chat_id: str) -> dict:
    reg_path = SCAN_HO_SO_DIR / "group_registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    info = reg.get(str(chat_id))
    if not info:
        raise SystemExit(f"chat_id {chat_id} not found in {reg_path}")
    folder_id = info.get("folder_id")
    applicant = info.get("applicant", "")
    visa = info.get("visa", "")
    if not folder_id:
        raise SystemExit(f"chat_id {chat_id} has no folder_id yet (case not set up)")
    case_id = re.sub(r"\s+", "-", applicant.upper()[:20]) + (f"-{visa}" if visa else "")
    return {"case_folder_id": folder_id, "applicant": applicant, "case_id": case_id,
            "drive_link": info.get("drive_link", "")}


# ============================================================================
# main
# ============================================================================
def run_self_test() -> int:
    from lib.sop_naming import classify_doc_type, build_filename
    samples = [
        ("Căn cước công dân", "thẻ căn cước 2 mặt, có ảnh chân dung, có chip/QR", "CCCD.pdf"),
        ("Thông tin cá nhân (tự khai)", "Tờ giấy khách hàng tự ghi họ tên, ngày sinh, số CCCD, địa chỉ", "info.jpg"),
        ("Căn cước công dân tự khai viết tay", "khách hàng tự điền số CCCD và thông tin cá nhân", "x.jpg"),
        ("Ảnh thẻ", "ảnh chân dung 1 người, phông trắng, kiểu ảnh dán hồ sơ", "Khac-Hoang Thi Mo.jpg"),
        ("Ảnh chân dung làm nông", "người làm nông trong nhà kính, thấy rõ mặt", "field.jpg"),
        ("", "", "BIA DAT.pdf"),
        ("Sao kê ngân hàng", "", "sao ke.pdf"),
        ("Giấy chứng nhận đăng ký HTX", "", "DKKD.pdf"),
        ("", "ảnh chăm sóc vườn hoa cúc nhà kính", "IMG_1234.jpg"),
    ]
    for dt, summ, fn in samples:
        c = classify_doc_type(dt, summ, fn)
        print(f"{c.confidence:6} {c.folder:14} {build_filename(c.tag, 'Hoang Thi Mo', '.pdf'):40} <- {dt or fn!r}")
    # regression: a self-filled / hand-written personal-info form must be CV, not CCCD
    assert classify_doc_type("Thông tin cá nhân (tự khai)", "khách tự ghi số CCCD và địa chỉ", "info.jpg").tag == "CV"
    assert classify_doc_type("Căn cước công dân tự khai viết tay", "khách hàng tự điền", "x.jpg").tag == "CV"
    # ...kể cả khi tên file là "CCCD-…" hoặc doc_type là "Căn cước" mà Gemini cờ extracted.la_to_khai=true
    assert classify_doc_type("Căn cước công dân", "tờ có ô số CCCD", "CCCD-Mo.jpg", extracted={"la_to_khai": True}).tag == "CV"
    assert classify_doc_type("Thông tin gia đình", "danh sách thành viên trong nhà, viết tay", "CCCD-Mo.jpg").tag == "CV"
    # ...but the real printed CCCD card must still be CCCD
    assert classify_doc_type("Căn cước công dân", "thẻ căn cước 2 mặt có ảnh chân dung và chip", "cccd.jpg").tag == "CCCD"
    # ảnh thẻ 5x7 (ảnh dán hồ sơ) → "Anh the"; ảnh người làm nông → "Anh-video lam nong"; nhóm/tiệc → "Anh gia dinh"
    assert classify_doc_type("Ảnh thẻ 5x7", "ảnh chân dung phông trắng", "Khac-Mo.jpg").tag == "Anh the"
    assert classify_doc_type("Ảnh", "", "x.jpg", extracted={"la_anh_the": True}).tag == "Anh the"
    assert classify_doc_type("Ảnh chân dung", "", "ID photo-Mo.jpg").tag == "Anh the"
    assert classify_doc_type("Ảnh chân dung người làm nông trong nhà kính", "chăm cây", "x.jpg").tag == "Anh-video lam nong"
    assert classify_doc_type("Ảnh chụp gia đình", "tiệc sinh nhật", "x.jpg").tag == "Anh gia dinh"
    # CCCD: ảnh chân dung in trên thẻ → vẫn CCCD, không bị cờ la_anh_the kéo thành "Anh the"
    assert classify_doc_type("Căn cước công dân", "thẻ căn cước có ảnh chân dung", "CCCD.pdf", extracted={"la_anh_the": True}).tag == "CCCD"
    # DocAI OCR + gpt-5-mini classify sanity
    import inspect as _inspect
    assert callable(docai_prefetch) and "pages_text" in _inspect.signature(process_one).parameters
    assert docai_prefetch([], dry_run=False, workers=3) == {}
    assert docai_prefetch([(Path("/nope/a.mov"), "a.mov")], dry_run=False, workers=2) == {}
    assert docai_prefetch([(Path("/nope/a.jpg"), "a.jpg")], dry_run=True, workers=2) == {}
    print(f"OCR_WORKERS={OCR_WORKERS} | docai_prefetch OK")
    # schema sanity
    assert OCR_CLASSIFY_MODEL, OCR_CLASSIFY_MODEL
    assert DOC_RESULT_SCHEMA["strict"] is True
    assert "tag" in DOC_RESULT_SCHEMA["schema"]["properties"]
    assert "md_content" in DOC_RESULT_SCHEMA["schema"]["properties"]
    assert callable(docai_classify_vision)
    # _find_sidecar_by_hash callable + miss case folder → None, không raise
    assert callable(_find_sidecar_by_hash)
    assert _find_sidecar_by_hash("", "abc") is None
    assert _find_sidecar_by_hash("nonexistent-folder", "") is None
    # _count_pdf_pages sanity
    try:
        import pypdf
        w = pypdf.PdfWriter()
        for _i in range(4):
            w.add_blank_page(width=72, height=72)
        _tmp = Path(tempfile.gettempdir()) / "_st_pdf_4p.pdf"
        with _tmp.open("wb") as _fh:
            w.write(_fh)
        assert _count_pdf_pages(_tmp) == 4
        _tmp.unlink(missing_ok=True)
        print("_count_pdf_pages OK (pypdf available)")
    except ImportError:
        print("_count_pdf_pages SKIP (pypdf chưa cài)")
    # checklist module sanity
    try:
        from lib import checklist as _ck
        assert _ck.CHECKLIST_DOC_TAGS and _ck.REQUIRED_DOCS and _ck.CHECKLIST_MODEL
        assert _ck.should_run_checklist({"items": [{"tag": "CCCD"}]}) is True
        assert _ck.should_run_checklist({"items": [{"tag": "Khac"}]}) is False
        _cov = _ck.compute_coverage([{"loai": "CCCD", "ten": "x", "nguoi": "y"}])
        assert _cov["required"] == 18 and _cov["have"] >= 1 and len(_cov["items"]) == 26
        _p = _ck._build_prompt("12/05/2026", "Test", _cov)
        assert "PHẦN 4" in _p and "{{" not in _p
        print(f"checklist module OK (model={_ck.CHECKLIST_MODEL}, provinces_loaded={bool(_ck.PROVINCES)})")
    except Exception as e:  # noqa: BLE001
        print(f"checklist module SELF-TEST FAILED: {type(e).__name__}: {e}")
        return 1
    print("self-test OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Robust unzip → OCR → rename → upload-to-Drive for visa docs.")
    ap.add_argument("input", nargs="?", help=".zip file or a directory of files")
    ap.add_argument("--case-folder-id", help="Drive folder id of the case (parent of the 4 top folders)")
    ap.add_argument("--applicant", default="", help="Applicant name (fallback subject for filenames)")
    ap.add_argument("--case-id", default="", help="Case id string for metadata (optional)")
    ap.add_argument("--from-registry", metavar="CHAT_ID", help="Resolve case folder/applicant from scan-ho-so/group_registry.json")
    ap.add_argument("--manifest", help="Where to write the manifest JSON (default: <input>.manifest.json)")
    ap.add_argument("--retries", type=int, default=3, help="Retries per file on error (default 3)")
    ap.add_argument("--dry-run", action="store_true", help="Enumerate + classify-by-filename only; no Gemini, no Drive writes")
    ap.add_argument("--self-test", action="store_true", help="Run the SOP naming self-test and exit")
    ap.add_argument("--no-checklist", action="store_true", help="Skip the AI-checklist cross-check step")
    ap.add_argument("--checklist-only", action="store_true",
                    help="Skip enumerate/OCR/upload; only (re)run the AI checklist for the case (input not required)")
    # P1.3 — force re-OCR ngay cả khi content_hash đã có sidecar. Dùng cho /oldfile khi staff
    # nghi file cũ bị nhầm tên — bot sẽ chạy lại classifier (đã update với P2.x) và upload
    # bản tên đúng. File tên sai cũ vẫn còn trên Drive (staff dọn thủ công).
    ap.add_argument("--force-rescan", action="store_true",
                    help="Bypass hash-dedup; OCR + classify lại mọi file (dùng cho /oldfile re-run sau khi update classifier).")
    ap.add_argument("--sweep-meta", action="store_true",
                    help="Trước khi xử lý batch, dọn mọi file .md/.json lạc trong 4 folder khách → _Bot OCR & Metadata.")
    args = ap.parse_args(argv)

    if args.self_test:
        return run_self_test()
    if not args.input and not args.checklist_only:
        ap.error("INPUT (.zip or directory) is required (unless --checklist-only)")

    # import SOP lib now (after env / sys.path set up)
    try:
        from lib.sop_naming import classify_doc_type, build_filename, detect_english, title_case_ascii
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"cannot import SOP lib from {SCAN_HO_SO_DIR}: {e}")
    sop = (classify_doc_type, build_filename, detect_english, title_case_ascii)

    case_folder_id = args.case_folder_id
    applicant = args.applicant
    case_id = args.case_id
    if args.from_registry:
        info = resolve_from_registry(args.from_registry)
        case_folder_id = case_folder_id or info["case_folder_id"]
        applicant = applicant or info["applicant"]
        case_id = case_id or info["case_id"]
    if not args.dry_run and not case_folder_id:
        ap.error("--case-folder-id (or --from-registry) is required unless --dry-run")
    if not case_id:
        case_id = (re.sub(r"\s+", "-", applicant.upper()[:20]) or "CASE")

    in_path = None
    if args.input:
        in_path = Path(args.input).expanduser().resolve()
        if not in_path.exists():
            raise SystemExit(f"input not found: {in_path}")

    today_vn = time.strftime("%d/%m/%Y")
    tmpdir = None
    try:
        if args.checklist_only:
            files = []
        elif in_path is None:
            raise SystemExit("no input and not --checklist-only")
        elif in_path.is_dir():
            files = collect_from_dir(in_path)
        elif in_path.suffix.lower() == ".zip":
            tmpdir = Path(tempfile.mkdtemp(prefix="scan_pipeline_"))
            files = collect_from_zip(in_path, tmpdir)
        else:
            # a single loose file
            files = [(in_path, in_path.name)]

        total = len(files)
        if args.checklist_only:
            log(f"checklist-only mode for case folder {case_folder_id} (applicant: {applicant or '?'})")
        else:
            log(f"input: {in_path.name} — {total} real file(s) to process (case folder {case_folder_id or '(dry)'})")
            if total == 0:
                log("nothing to do")

        # P1.4 — dọn sidecar lạc khỏi folder khách TRƯỚC khi xử lý (để báo cáo đếm đúng).
        # Triggered bởi --sweep-meta (telegram_listener.py truyền cho /check + /oldfile).
        if args.sweep_meta and case_folder_id and not args.dry_run:
            try:
                sweep_stray_sidecars(case_folder_id)
            except Exception as e:  # noqa: BLE001
                log(f"sweep_stray_sidecars failed (bỏ qua): {type(e).__name__}: {e}")

        # Bước 1: DocAI OCR tất cả file song song.
        ocr_cache = docai_prefetch(files, dry_run=args.dry_run, workers=OCR_WORKERS)
        # Bước 2: GPT vision classify tất cả file song song (dùng OCR output từ bước 1).
        vision_cache = vision_prefetch(files, ocr_cache, applicant, dry_run=args.dry_run, workers=OCR_WORKERS)

        name_registry: dict = {}
        items = []
        split_path_map: dict[str, str] = {}   # seg_src_name → local path (for vision_compare)
        for idx, (path, src_name) in enumerate(files, 1):
            log(f"[{idx}/{total}] {src_name}")

            # --- Multi-doc PDF splitting ---
            _pages = ocr_cache.get(src_name)
            if (path.suffix.lower() == ".pdf"
                    and isinstance(_pages, list) and len(_pages) > 1
                    and not args.dry_run):
                segments = _detect_pdf_segments(_pages, applicant)
                if segments:
                    log(f"  split: {src_name} → {len(segments)} segments")
                    tmpdir = Path(tempfile.mkdtemp(prefix="scan_split_"))
                    try:
                        split_parts = _split_pdf_segments(path, segments, tmpdir)

                        # Parallel: rasterize first page + GPT classify per segment.
                        # OCR text sliced from ocr_cache — no DocAI call needed.
                        _pages_set_cache = {p["page"]: p for p in _pages}

                        def _classify_seg(sp_sm):
                            sp, sm = sp_sm
                            page_nums = sm.get("pages", [])
                            sp_pages = [_pages_set_cache[pg] for pg in page_nums
                                        if pg in _pages_set_cache]
                            img = _rasterize_first_page(sp)
                            b64 = base64.b64encode(img).decode() if img else None
                            gem = docai_classify_vision(sp_pages, sp.name, applicant, b64)
                            return sp.name, sp_pages, gem

                        n_seg = max(1, min(OCR_WORKERS, len(split_parts)))
                        seg_classify: dict[str, tuple] = {}
                        with concurrent.futures.ThreadPoolExecutor(max_workers=n_seg) as ex:
                            futs = {ex.submit(_classify_seg, (sp, sm)): (sp, sm)
                                    for sp, sm in split_parts}
                            for fut in concurrent.futures.as_completed(futs):
                                sp, sm = futs[fut]
                                try:
                                    sname, sp_pages, gem = fut.result()
                                    seg_classify[sname] = (sp_pages, gem)
                                except Exception as e:  # noqa: BLE001
                                    log(f"  segment classify error {sp.name}: {e}")
                                    seg_classify[sp.name] = ([], {})

                        # Sequential: Drive upload (Drive client is not thread-safe)
                        for seg_path, seg_meta in split_parts:
                            seg_src = seg_path.name
                            seg_pages, seg_gem = seg_classify.get(seg_src, ([], {}))
                            it = process_one(seg_path, seg_src,
                                             case_folder_id=case_folder_id or "", applicant=applicant,
                                             case_id=case_id, retries=args.retries, dry_run=args.dry_run,
                                             sop=sop, name_registry=name_registry,
                                             pages_text=seg_pages, gem_cache=seg_gem,
                                             force_rescan=args.force_rescan)
                            it["split_from"] = src_name
                            it["split_pages"] = seg_meta.get("pages", [])
                            if it.get("status") == "uploaded":
                                it["status"] = "uploaded-split"
                            split_path_map[seg_src] = str(seg_path)
                            log(f"     -> {it.get('status','?')}  {it.get('new_name','')}")
                            items.append(it)
                    finally:
                        shutil.rmtree(tmpdir, ignore_errors=True)
                    continue   # skip normal process_one for this file

            # Single-doc path (all files)
            it = process_one(path, src_name, case_folder_id=case_folder_id or "", applicant=applicant,
                             case_id=case_id, retries=args.retries, dry_run=args.dry_run, sop=sop,
                             name_registry=name_registry, pages_text=ocr_cache.get(src_name),
                             gem_cache=vision_cache.get(src_name),
                             force_rescan=args.force_rescan)
            status = it.get("status", "?")
            log(f"     -> {status}  {it.get('new_name', '')}")
            items.append(it)

        n = {"uploaded": 0, "uploaded-no-ocr": 0, "uploaded-split": 0,
             "duplicate": 0, "duplicate-by-hash": 0, "failed": 0, "dry-run": 0}
        for it in items:
            n[it.get("status", "failed")] = n.get(it.get("status", "failed"), 0) + 1
        # P1.2 — reconciliation: file đầu vào nào không có item trong manifest = bị mất.
        # Cũ: assert hard-crash. Mới: soft warning + ghi vào manifest["dropped_files"]
        # + exit code 2 để telegram_listener cảnh báo Telegram Pro group.
        all_input_names = {src_name for (_, src_name) in files}
        unique_sources = {it.get("split_from") or it.get("src_name") for it in items if it.get("src_name") or it.get("split_from")}
        dropped_files = sorted(all_input_names - unique_sources)
        if dropped_files:
            log(f"⚠️ RECONCILIATION: {len(dropped_files)} file đầu vào không có trong manifest: {dropped_files}")
        manifest = {
            "input": str(in_path) if in_path else None,
            "case_folder_id": case_folder_id,
            "case_id": case_id,
            "applicant": applicant,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_input_files": total,
            "total_output_items": len(items),
            "unique_sources_covered": len(unique_sources),
            "counts": n,
            "dropped_files": dropped_files,    # P1.2: list tên file bị mất (rỗng nếu OK)
            "ok": n["failed"] == 0 and not dropped_files,
            "items": items,
        }

        # --- AI checklist cross-check: a bolt-on after OCR/upload; it must NEVER
        #     break the core result, so the whole thing is wrapped. Runs when there's
        #     a checklist-relevant doc in the batch (auto-debounce), or always in --checklist-only.
        # === Mức 3 — Vision compare (Anh thẻ × Passport/GPLX/CCCD) ===
        # Chỉ chạy khi batch có ít nhất 1 Anh thẻ + 1 doc có ảnh chân dung.
        vision_results: list[dict] = []
        if not args.dry_run and case_folder_id and not args.no_checklist:
            try:
                from lib import vision_check as _vc
                # Map src_name → local path (files đã ở workdir trước cleanup)
                src_to_path = {src_name: str(p) for (p, src_name) in files}
                src_to_path.update(split_path_map)   # include split segments
                items_with_paths = []
                for it in items:
                    src = it.get("src_name") or ""
                    lp = src_to_path.get(src)
                    if lp and Path(lp).exists():
                        items_with_paths.append({**it, "local_path": lp})
                pairs = _vc.find_compare_pairs(items_with_paths)
                if pairs:
                    log(f"vision_compare: chạy {len(pairs)} cặp Anh thẻ × giấy có ảnh")
                    vision_results = _vc.evaluate_pairs(pairs)
                    if vision_results:
                        log(f"vision_compare: xong {len(vision_results)}/{len(pairs)} cặp")
                        manifest["vision_compare"] = vision_results
            except Exception as e:  # noqa: BLE001
                log(f"vision_compare lỗi (bỏ qua): {type(e).__name__}: {e}")

        if not args.dry_run and case_folder_id and not args.no_checklist:
            try:
                scan_da_duyet_folder(case_folder_id, applicant, case_id)
            except Exception as e:  # noqa: BLE001
                log(f"scan_da_duyet_folder lỗi (bỏ qua): {type(e).__name__}: {e}")

        if not args.dry_run and case_folder_id and not args.no_checklist:
            try:
                from lib import checklist as _ck
                # Fix A: skip thẩm định nếu batch không có file mới (toàn dedup-by-hash).
                # /check (checklist_only) luôn chạy fresh; /oldfile + batch Telegram chỉ chạy
                # nếu có ≥1 file mới thật sự (uploaded + uploaded-split > 0).
                _counts = manifest.get("counts", {}) or {}
                _n_new = _counts.get("uploaded", 0) + _counts.get("uploaded-split", 0)
                _skip_unchanged = (not args.checklist_only) and _n_new == 0
                _do_run = args.checklist_only or (_ck.should_run_checklist(manifest) and not _skip_unchanged)

                if _skip_unchanged:
                    log("skip thẩm định: batch không có file mới (toàn file đã có/trùng nội dung) — báo cáo trước vẫn còn hiệu lực")
                    manifest["checklist"] = {"ran": False, "skipped": "no-new-files",
                                              "reason": "Batch không có file mới (toàn file đã có/trùng nội dung); báo cáo trước vẫn còn hiệu lực"}

                if _do_run:
                    log("running AI checklist ...")
                    if args.checklist_only:
                        # /check re-run: read Drive sidecars
                        ck = _ck.run_and_write(case_folder_id, applicant, SHARED_DRIVE_ID,
                                               today=today_vn,
                                               vision_compare=vision_results or None)
                    else:
                        # Fresh run: use in-memory md_contents from this batch
                        _md_contents = [it.get("md_content", "") for it in items if it.get("md_content")]
                        _dataset = _items_to_dataset(items)
                        ck = _ck.run_from_md_contents(
                            _md_contents, case_folder_id, applicant, today_vn,
                            dataset=_dataset, drive_id=SHARED_DRIVE_ID,
                            vision_compare=vision_results or None,
                        )
                    manifest["checklist"] = ck
                    cov = ck.get("coverage") or {}
                    log(f"checklist: ran={ck.get('ran')} model={ck.get('model')} "
                        f"extract={ck.get('extract_model')} have={cov.get('have')}/{cov.get('required')} "
                        f"doc={ck.get('report_link') or ck.get('md_link') or ''} err={ck.get('error')}")
            except Exception as e:  # noqa: BLE001
                log(f"checklist step failed (ignored): {type(e).__name__}: {e}")
                traceback.print_exc()
                manifest["checklist"] = {"ran": False, "error": f"{type(e).__name__}: {e}"}

        man_path = (Path(args.manifest) if args.manifest
                    else (in_path.with_suffix(in_path.suffix + ".manifest.json") if in_path
                          else Path(tempfile.gettempdir()) / "scan_checklist_manifest.json"))
        man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # human summary
        print("\n" + "=" * 60)
        if args.checklist_only:
            ck = manifest.get("checklist", {}) or {}
            cov = ck.get("coverage") or {}
            print("CHECKLIST-ONLY MODE")
            print(f"ran         : {ck.get('ran')}")
            print(f"model (eval) : {ck.get('model')}")
            print(f"model (extr) : {ck.get('extract_model')}")
            print(f"coverage    : {cov.get('have')}/{cov.get('required')} required docs")
            print(f"report (Doc): {ck.get('report_link') or ck.get('md_link') or ''}")
            if ck.get("error"):
                print(f"error       : {ck['error']}")
            print(f"manifest    : {man_path}")
            print("=" * 60)
            return 0 if ck.get("ran") else 1

        print(f"INPUT FILES : {total}")
        if not args.dry_run:
            print(f"UPLOADED    : {n['uploaded']}")
            print(f"UPLOADED*   : {n['uploaded-no-ocr']}   (no OCR — non pdf/jpg/png, needs review)")
            print(f"DUPLICATE   : {n['duplicate']}   (already in Drive, skipped)")
        else:
            print(f"DRY-RUN     : {n['dry-run']}")
        print(f"FAILED      : {n['failed']}")
        ck = manifest.get("checklist")
        if ck:
            cov = ck.get("coverage") or {}
            print(f"CHECKLIST   : ran={ck.get('ran')} model={ck.get('model')} extract={ck.get('extract_model')} have={cov.get('have')}/{cov.get('required')} doc={ck.get('report_link') or ck.get('md_link') or ''}")
        print(f"manifest    : {man_path}")
        if n["failed"]:
            print("\nFAILED FILES (re-run the same command — finished files are skipped):")
            for it in items:
                if it.get("status") == "failed":
                    print(f"  - {it['src_name']}: {it.get('error')}")
        # P1.2: in danh sách file bị mất (silent drop) — staff cần biết để gửi lại
        if dropped_files:
            print(f"\n⚠️ DROPPED FILES ({len(dropped_files)}) — không có trong manifest:")
            for nm in dropped_files:
                print(f"  - {nm}")
        review = [it for it in items if it.get("needs_review")]
        if review and not args.dry_run:
            print(f"\nNEEDS REVIEW ({len(review)}): " + ", ".join(it["src_name"] for it in review))
        print("=" * 60)
        # P1.2 exit code semantics:
        #   0 = success (no failed, no dropped)
        #   1 = some files failed (retry-able)
        #   2 = silent drop detected (manifest hụt file đầu vào) — Telegram listener cảnh báo
        if dropped_files:
            return 2
        return 0 if n["failed"] == 0 else 1
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2
    finally:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
