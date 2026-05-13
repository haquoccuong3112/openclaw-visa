#!/usr/bin/env python3
"""vision_check — cross-photo comparison qua gemini-2.5-pro multi-image.

Mức 3 sprint: sau khi process_one loop xong, tìm các cặp giấy có ảnh chân dung
(Anh the × Passport / GPLX / HC) → gọi Gemini với 2 ảnh + prompt vision compare
→ trả {same_person, age_diff_months, phau_thuat_signs, anomalies}.

Result inject vào profile._vision_compare làm ground-truth cho LLM tầng 2
(pattern giống lib/checklist.py::build_dia_gioi).

Cover rule:
  - 1.2: phau_thuat_signs non-empty → 🔴 reject
  - 8.3: same_person AND age_diff_months > 6 → 🟡 warn

Constraints:
  - MAX_PAIRS_PER_CASE = 3 — tránh cost spike
  - Cache theo SHA-1 cặp (file_a_hash + file_b_hash)
  - Skip nếu thiếu Anh thẻ
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

import httpx

VISION_MODEL = os.environ.get("VISION_COMPARE_MODEL", "google/gemini-2.5-pro")
MAX_PAIRS_PER_CASE = int(os.environ.get("VISION_MAX_PAIRS", "3"))

# Loại giấy có ảnh chân dung — dùng để pair với Anh thẻ
DOCS_WITH_PORTRAIT = ("Passport", "GPLX", "CCCD")

# Schema strict cho response (OpenRouter json_schema)
_RESPONSE_SCHEMA = {
    "name": "vision_compare",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "same_person":     {"type": "boolean"},
            "confidence":      {"type": "string", "enum": ["high", "medium", "low"]},
            "age_diff_months": {"type": ["integer", "null"]},
            "phau_thuat_signs": {"type": "array", "items": {"type": "string"}},
            "anomalies":       {"type": "array", "items": {"type": "string"}},
            "ly_do":           {"type": "string"},
        },
        "required": ["same_person", "confidence", "phau_thuat_signs", "anomalies"],
        "additionalProperties": True,
    },
}


def _file_to_b64(path_or_bytes) -> str:
    """Read file bytes (path | bytes) → base64."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data = bytes(path_or_bytes)
    else:
        data = Path(path_or_bytes).read_bytes()
    return base64.b64encode(data).decode()


def _detect_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png"}.get(ext, "application/octet-stream")


def _pair_hash(a_bytes: bytes, b_bytes: bytes) -> str:
    """SHA-1 của cặp file để cache."""
    h = hashlib.sha1()
    h.update(a_bytes)
    h.update(b"::")
    h.update(b_bytes)
    return h.hexdigest()


def compare_portraits(anh_the_path: Path, doc_path: Path, doc_type: str,
                       model: str | None = None) -> dict | None:
    """So sánh ảnh chân dung trên 2 file (Anh thẻ vs doc_type).

    Trả dict structured. None nếu lỗi không recover được."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return None
    try:
        anh_the_b64 = _file_to_b64(anh_the_path)
        anh_the_mime = _detect_mime(anh_the_path)
        doc_b64 = _file_to_b64(doc_path)
        doc_mime = _detect_mime(doc_path)
    except Exception as e:  # noqa: BLE001
        print(f"vision_check: read file lỗi: {type(e).__name__}: {e}", flush=True)
        return None

    prompt = (
        "Bạn nhận 2 file kèm bên dưới — đều có ảnh chân dung của người làm hồ sơ visa.\n"
        f"FILE 1: Ảnh thẻ chân dung độc lập (5x7 phông trắng hoặc tương tự).\n"
        f"FILE 2: {doc_type} (giấy chính thức có in ảnh chân dung của KH ở 1 trang).\n\n"
        "Nhiệm vụ — trả JSON 1 dòng (không markdown, không giải thích ngoài JSON):\n"
        "{\n"
        '  "same_person": true|false  (có phải CÙNG MỘT người trên 2 ảnh không),\n'
        '  "confidence":  "high"|"medium"|"low"  (độ chắc chắn),\n'
        '  "age_diff_months": <số nguyên — ƯỚC LƯỢNG cách nhau bao nhiêu tháng dựa trên visual age (tóc/da/khuôn mặt); null nếu không ước lượng được>,\n'
        '  "phau_thuat_signs": ["mũi","mí mắt","cằm",...]  (DẤU HIỆU phẫu thuật thẩm mỹ rõ — chỉ ghi nếu THẤY KHÁC BIỆT cấu trúc khuôn mặt giữa 2 ảnh không giải thích được bằng tuổi/cân nặng; rỗng nếu không thấy),\n'
        '  "anomalies": [...]  (bất thường khác — vd ảnh fake, photoshop nặng, ghép mặt; rỗng nếu không),\n'
        '  "ly_do": "<1 câu giải thích kết luận>"\n'
        "}\n\n"
        "QUAN TRỌNG:\n"
        "- Không tự ý gắn `phau_thuat_signs` cho thay đổi tự nhiên (gầy/béo, đeo kính, makeup khác).\n"
        "  CHỈ gắn khi cấu trúc xương/đường nét khuôn mặt RÕ RÀNG khác.\n"
        "- `same_person`=false nếu nghi 2 người khác — đây là red flag nghiêm trọng cho hồ sơ.\n"
        "- Nếu file 2 là PDF nhiều trang, tìm trang có ảnh chân dung (trang bio-data của HC, mặt trước GPLX/CCCD).\n"
        "- `confidence=low` nếu ảnh mờ/quá nhỏ/không nhìn rõ mặt."
    )

    payload = {
        "model": model or VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "file", "file": {"filename": Path(anh_the_path).name,
                                       "file_data": f"data:{anh_the_mime};base64,{anh_the_b64}"}},
            {"type": "file", "file": {"filename": Path(doc_path).name,
                                       "file_data": f"data:{doc_mime};base64,{doc_b64}"}},
        ]}],
        "temperature": 0.1,
        "response_format": {"type": "json_schema", "json_schema": _RESPONSE_SCHEMA},
    }

    try:
        with httpx.Client(timeout=120) as client:
            resp = client.post("https://openrouter.ai/api/v1/chat/completions",
                               headers={"Authorization": f"Bearer {api_key}"}, json=payload)
            if resp.status_code >= 400:
                payload["response_format"] = {"type": "json_object"}
                resp = client.post("https://openrouter.ai/api/v1/chat/completions",
                                   headers={"Authorization": f"Bearer {api_key}"}, json=payload)
            if resp.status_code >= 400:
                payload.pop("response_format", None)
                resp = client.post("https://openrouter.ai/api/v1/chat/completions",
                                   headers={"Authorization": f"Bearer {api_key}"}, json=payload)
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        result = json.loads(text)
        if not isinstance(result, dict):
            return None
        # Normalize fields
        result.setdefault("same_person", False)
        result.setdefault("confidence", "low")
        result.setdefault("age_diff_months", None)
        result.setdefault("phau_thuat_signs", [])
        result.setdefault("anomalies", [])
        return result
    except Exception as e:  # noqa: BLE001
        print(f"vision_check: compare lỗi ({Path(anh_the_path).name} vs {Path(doc_path).name}): "
              f"{type(e).__name__}: {e}", flush=True)
        return None


def find_compare_pairs(dataset: list[dict], download_fn=None) -> list[tuple[dict, dict]]:
    """Tìm các cặp (Anh thẻ doc, doc có ảnh) trong dataset. Cap MAX_PAIRS_PER_CASE.

    `dataset` là list[item] từ scan_pipeline (mỗi item có ten/tag/drive_link/local_path).
    Returns list[(anh_the_item, doc_item)] — mỗi item dict đã include `local_path` để compare_portraits đọc bytes.
    """
    anh_the_items = [d for d in dataset if (d.get("tag") or d.get("loai")) == "Anh the"]
    portrait_items = [d for d in dataset if (d.get("tag") or d.get("loai")) in DOCS_WITH_PORTRAIT]
    if not anh_the_items or not portrait_items:
        return []
    # Anh thẻ chính = file đầu tiên (caller có thể sort theo confidence/freshness sau)
    main_anh_the = anh_the_items[0]
    # Ưu tiên Passport > GPLX > CCCD
    priority = {"Passport": 0, "GPLX": 1, "CCCD": 2}
    portrait_items.sort(key=lambda d: priority.get(d.get("tag") or d.get("loai"), 99))
    pairs: list[tuple[dict, dict]] = []
    for doc in portrait_items[:MAX_PAIRS_PER_CASE]:
        pairs.append((main_anh_the, doc))
    return pairs


def evaluate_pairs(pairs: list[tuple[dict, dict]],
                    cache: dict | None = None) -> list[dict]:
    """Chạy compare_portraits cho từng cặp. Trả list[{file_a, file_b, result}].

    `cache`: dict {pair_hash: result} để skip nếu đã compare lần trước.
    Cap cost: stop sau MAX_PAIRS_PER_CASE pairs.
    """
    cache = cache if cache is not None else {}
    out: list[dict] = []
    for a, b in pairs[:MAX_PAIRS_PER_CASE]:
        a_path = a.get("local_path")
        b_path = b.get("local_path")
        if not a_path or not b_path:
            continue
        try:
            a_bytes = Path(a_path).read_bytes()
            b_bytes = Path(b_path).read_bytes()
        except Exception as e:  # noqa: BLE001
            print(f"vision_check: read pair lỗi: {type(e).__name__}: {e}", flush=True)
            continue
        ph = _pair_hash(a_bytes, b_bytes)
        if ph in cache:
            result = cache[ph]
            cached = True
        else:
            result = compare_portraits(Path(a_path), Path(b_path),
                                        doc_type=b.get("tag") or b.get("loai") or "?")
            cache[ph] = result
            cached = False
        if result is None:
            continue
        out.append({
            "file_a": a.get("ten") or a.get("new_name") or "?",
            "file_a_tag": a.get("tag") or a.get("loai"),
            "file_b": b.get("ten") or b.get("new_name") or "?",
            "file_b_tag": b.get("tag") or b.get("loai"),
            "result": result,
            "cached": cached,
        })
    return out


_FILE_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{20,})")


def _file_id_from_link(drive_link: str) -> str:
    if not drive_link:
        return ""
    m = _FILE_ID_RE.search(drive_link)
    return m.group(1) if m else ""


def compare_pairs_for_case(case_folder_id: str, dataset: list[dict],
                            drive_id: str | None = None) -> list[dict]:
    """Case-level vision compare — download files từ Drive cho mọi cặp (Anh thẻ × portrait doc).
    Cache result trong sidecar `_vision_compare.json` ở `_Bot OCR & Metadata` để /check re-run
    không tốn token. Trả list[{file_a, file_b, result}].

    Khi nào dùng:
      - scan_pipeline.py sau process_one (batch mode): item.local_path đã sẵn → có thể truyền
        trực tiếp evaluate_pairs(), HOẶC gọi function này để bot dùng cache nếu đã có.
      - checklist.py run_and_write (/check mode): không có local_path → bắt buộc dùng function này.
    """
    try:
        try:
            from .drive_helpers import (
                get_or_create_folder, find_file_by_name, download_file_bytes, upload_file,
            )
        except ImportError:
            from drive_helpers import (  # type: ignore  # noqa
                get_or_create_folder, find_file_by_name, download_file_bytes, upload_file,
            )
    except Exception as e:  # noqa: BLE001
        print(f"vision_check: drive_helpers import lỗi: {e}", flush=True)
        return []

    # 1) Skip nếu không có Anh thẻ
    has_anh_the = any((d.get("loai") or d.get("tag")) == "Anh the" for d in dataset)
    has_portrait = any((d.get("loai") or d.get("tag")) in DOCS_WITH_PORTRAIT for d in dataset)
    if not (has_anh_the and has_portrait):
        return []

    # 2) Đọc sidecar cache cũ nếu có
    META_FOLDER = "_Bot OCR & Metadata"
    SIDECAR_NAME = "_vision_compare.json"
    cached: list[dict] = []
    cache_index: dict[str, dict] = {}   # pair_hash → result
    try:
        meta_id = get_or_create_folder(META_FOLDER, case_folder_id, drive_id=drive_id)
        existing_id = find_file_by_name(SIDECAR_NAME, meta_id, drive_id=drive_id,
                                         mime_type="application/json")
        if existing_id:
            cached_text = download_file_bytes(existing_id, drive_id=drive_id).decode("utf-8")
            cached = json.loads(cached_text)
            if isinstance(cached, list):
                for item in cached:
                    if isinstance(item, dict) and item.get("pair_hash"):
                        cache_index[item["pair_hash"]] = item
    except Exception as e:  # noqa: BLE001
        print(f"vision_check: cache read lỗi (ignored): {type(e).__name__}: {e}", flush=True)

    # 3) Xây danh sách pair (max MAX_PAIRS_PER_CASE)
    anh_the_items = [d for d in dataset if (d.get("loai") or d.get("tag")) == "Anh the"]
    portrait_items = [d for d in dataset if (d.get("loai") or d.get("tag")) in DOCS_WITH_PORTRAIT]
    if not anh_the_items or not portrait_items:
        return []
    main_anh_the = anh_the_items[0]
    priority = {"Passport": 0, "GPLX": 1, "CCCD": 2}
    portrait_items.sort(key=lambda d: priority.get(d.get("loai") or d.get("tag"), 99))

    out: list[dict] = []
    n_calls = 0
    for doc in portrait_items[:MAX_PAIRS_PER_CASE]:
        a_link = main_anh_the.get("drive_link", "")
        b_link = doc.get("drive_link", "")
        a_fid = _file_id_from_link(a_link)
        b_fid = _file_id_from_link(b_link)
        if not a_fid or not b_fid:
            continue
        # Pair hash dựa trên FILE_ID (rẻ — không cần download nếu cache hit)
        ph = hashlib.sha1(f"{a_fid}::{b_fid}".encode()).hexdigest()
        if ph in cache_index:
            out.append({**cache_index[ph], "cached": True})
            continue
        # Cache miss → download + compare
        try:
            a_bytes = download_file_bytes(a_fid, drive_id=drive_id)
            b_bytes = download_file_bytes(b_fid, drive_id=drive_id)
        except Exception as e:  # noqa: BLE001
            print(f"vision_check: download pair lỗi: {type(e).__name__}: {e}", flush=True)
            continue
        # Write to temp files for compare_portraits
        try:
            import tempfile
            a_ten = main_anh_the.get("ten") or "anh_the.jpg"
            b_ten = doc.get("ten") or "doc.pdf"
            with tempfile.NamedTemporaryFile(suffix="-" + Path(a_ten).suffix, delete=False) as af:
                af.write(a_bytes); a_path = af.name
            with tempfile.NamedTemporaryFile(suffix="-" + Path(b_ten).suffix, delete=False) as bf:
                bf.write(b_bytes); b_path = bf.name
            result = compare_portraits(Path(a_path), Path(b_path),
                                        doc_type=doc.get("loai") or doc.get("tag") or "?")
            try:
                os.unlink(a_path); os.unlink(b_path)
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            print(f"vision_check: compare lỗi: {type(e).__name__}: {e}", flush=True)
            continue
        if result is None:
            continue
        n_calls += 1
        out.append({
            "file_a": main_anh_the.get("ten") or "?",
            "file_a_tag": "Anh the",
            "file_b": doc.get("ten") or "?",
            "file_b_tag": doc.get("loai") or doc.get("tag"),
            "pair_hash": ph,
            "result": result,
            "cached": False,
        })

    # 4) Persist updated cache (giữ entry cũ + thêm mới)
    if out and n_calls > 0:
        try:
            merged = list(cache_index.values())
            new_hashes = {x["pair_hash"] for x in out if x.get("pair_hash")}
            merged = [m for m in merged if m.get("pair_hash") not in new_hashes]
            merged.extend([x for x in out if x.get("pair_hash") and not x.get("cached")])
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                              encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=2)
                cache_path = fh.name
            upload_file(cache_path, SIDECAR_NAME, meta_id, drive_id=drive_id,
                        mime="application/json")
            try:
                os.unlink(cache_path)
            except Exception:  # noqa: BLE001
                pass
            print(f"vision_check: cache saved ({len(merged)} pairs total, {n_calls} new)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"vision_check: cache save lỗi (ignored): {type(e).__name__}: {e}", flush=True)

    return out


# ============================================================================
# self-test (không gọi API — chỉ verify shape + helpers)
# ============================================================================
if __name__ == "__main__":
    # _pair_hash
    h1 = _pair_hash(b"abc", b"def")
    h2 = _pair_hash(b"abc", b"def")
    h3 = _pair_hash(b"def", b"abc")
    assert h1 == h2 and h1 != h3, "pair_hash phải deterministic + order-sensitive"

    # find_compare_pairs
    dataset = [
        {"tag": "Anh the", "ten": "Anh the-A.jpg", "local_path": "/tmp/a.jpg"},
        {"tag": "Passport", "ten": "Passport-A.pdf", "local_path": "/tmp/p.pdf"},
        {"tag": "GPLX", "ten": "GPLX-A.jpg", "local_path": "/tmp/g.jpg"},
        {"tag": "CCCD", "ten": "CCCD-A.pdf", "local_path": "/tmp/c.pdf"},
        {"tag": "Sao ke", "ten": "Sao ke-A.pdf", "local_path": "/tmp/s.pdf"},   # KHÔNG cặp
    ]
    pairs = find_compare_pairs(dataset)
    assert len(pairs) == 3, f"phải có 3 cặp (cap), got {len(pairs)}: {pairs}"
    # Ưu tiên Passport > GPLX > CCCD
    assert pairs[0][1]["tag"] == "Passport", pairs[0]
    assert pairs[1][1]["tag"] == "GPLX", pairs[1]
    assert pairs[2][1]["tag"] == "CCCD", pairs[2]

    # Không có Anh thẻ → 0 cặp
    no_anh = [{"tag": "Passport", "local_path": "/tmp/p"}]
    assert find_compare_pairs(no_anh) == []

    # Không có doc có ảnh → 0 cặp
    no_portrait = [{"tag": "Anh the", "local_path": "/tmp/a"}, {"tag": "Sao ke", "local_path": "/tmp/s"}]
    assert find_compare_pairs(no_portrait) == []

    # Module sanity
    assert callable(compare_portraits)
    assert callable(evaluate_pairs)
    assert VISION_MODEL.startswith("google/")
    assert MAX_PAIRS_PER_CASE >= 1
    print(f"vision_check OK | model={VISION_MODEL} | max_pairs={MAX_PAIRS_PER_CASE}")
