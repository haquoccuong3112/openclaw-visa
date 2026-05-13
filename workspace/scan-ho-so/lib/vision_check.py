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
