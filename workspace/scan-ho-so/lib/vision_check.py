#!/usr/bin/env python3
"""vision_check — cross-photo comparison via AWS Rekognition CompareFaces.

Mức 3 sprint: sau khi process_one loop xong, tìm các cặp giấy có ảnh chân dung
(Anh the × Passport / GPLX / CCCD) → gọi AWS Rekognition CompareFaces
→ trả {same_person, confidence, age_diff_months, phau_thuat_signs, anomalies, rekognition_similarity}.

Result inject vào profile._vision_compare làm ground-truth cho LLM tầng 2.

Cover rule:
  - 8.3: same_person AND age_diff_months > 6 → 🟡 warn
  - Note: phau_thuat_signs luôn [] — Rekognition không phát hiện phẫu thuật thẩm mỹ.
    Rule 1.2 (phau_thuat → 🔴 reject) cần kiểm tra thủ công.

Constraints:
  - MAX_PAIRS_PER_CASE = 3 — tránh cost spike
  - Cache theo SHA-1 cặp (file_id_a + file_id_b)
  - Skip nếu thiếu Anh thẻ hoặc thiếu AWS_ACCESS_KEY_ID

Env vars:
  AWS_ACCESS_KEY_ID       — required
  AWS_SECRET_ACCESS_KEY   — required
  AWS_REGION              — region (default: us-east-1)
  VISION_MAX_PAIRS        — max pairs per case (default: 3)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

MAX_PAIRS_PER_CASE = int(os.environ.get("VISION_MAX_PAIRS", "3"))

# Loại giấy có ảnh chân dung — dùng để pair với Anh thẻ
DOCS_WITH_PORTRAIT = ("Passport", "GPLX", "CCCD")


def _pair_hash(a_bytes: bytes, b_bytes: bytes) -> str:
    """SHA-1 của cặp file để cache."""
    h = hashlib.sha1()
    h.update(a_bytes)
    h.update(b"::")
    h.update(b_bytes)
    return h.hexdigest()


def _pdf_to_jpeg(pdf_bytes: bytes) -> bytes | None:
    """Rasterize PDF page 0 → JPEG bytes via pypdfium2. Returns None on error."""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf_bytes)
        if len(doc) == 0:
            return None
        page = doc[0]
        bitmap = page.render(scale=150 / 72)  # 150 DPI
        pil_image = bitmap.to_pil()
        buf = io.BytesIO()
        pil_image.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        print(f"vision_check: _pdf_to_jpeg lỗi: {type(e).__name__}: {e}", flush=True)
        return None


def _to_image_bytes(file_bytes: bytes) -> bytes | None:
    """Convert file bytes to JPEG if PDF; pass through if already image. No face detection."""
    if file_bytes[:4] == b"%PDF":
        return _pdf_to_jpeg(file_bytes)
    return file_bytes


def _pdf_find_face_page(pdf_bytes: bytes, client, max_pages: int = 5) -> bytes | None:
    """Rasterize PDF pages 0..max_pages-1, return JPEG of first page where Rekognition
    detects a face. Falls back to page 0 JPEG if no face found on any scanned page."""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf_bytes)
        n = min(len(doc), max_pages)
        if n == 0:
            return None
        first_page_jpeg: bytes | None = None
        for i in range(n):
            page = doc[i]
            bitmap = page.render(scale=150 / 72)  # 150 DPI
            pil_img = bitmap.to_pil()
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=85)
            jpeg = buf.getvalue()
            if first_page_jpeg is None:
                first_page_jpeg = jpeg
            try:
                resp = client.detect_faces(Image={"Bytes": jpeg}, Attributes=["DEFAULT"])
                if resp.get("FaceDetails"):
                    if i > 0:
                        print(f"vision_check: khuôn mặt ở trang {i} (bỏ qua trang 0)", flush=True)
                    return jpeg
            except Exception:  # noqa: BLE001
                pass
        print("vision_check: không tìm thấy khuôn mặt trong PDF, dùng trang 0", flush=True)
        return first_page_jpeg
    except Exception as e:  # noqa: BLE001
        print(f"vision_check: _pdf_find_face_page lỗi: {type(e).__name__}: {e}", flush=True)
        return None


def _rekognition_compare(source_bytes: bytes, target_bytes: bytes, client) -> dict:
    """AWS Rekognition CompareFaces + DetectFaces → normalized result dict.

    Similarity thresholds:
      ≥ 95 → same_person=True,  confidence=high
      ≥ 80 → same_person=True,  confidence=medium
      ≥ 60 → same_person=True,  confidence=low
      ≥ 40 → same_person=False, confidence=medium
      <  40 → same_person=False, confidence=high
    """
    from botocore.exceptions import BotoCoreError, ClientError

    # Step 1: Compare faces
    try:
        resp = client.compare_faces(
            SourceImage={"Bytes": source_bytes},
            TargetImage={"Bytes": target_bytes},
            SimilarityThreshold=50.0,
            QualityFilter="AUTO",
        )
    except (BotoCoreError, ClientError) as e:
        raise RuntimeError(f"Rekognition compare_faces lỗi: {e}") from e

    matches = resp.get("FaceMatches", [])
    similarity = float(matches[0]["Similarity"]) if matches else 0.0

    if similarity >= 95:
        same_person, confidence = True, "high"
    elif similarity >= 80:
        same_person, confidence = True, "medium"
    elif similarity >= 60:
        same_person, confidence = True, "low"
    elif similarity >= 40:
        same_person, confidence = False, "medium"
    else:
        same_person, confidence = False, "high"

    # Step 2: Age estimation from both images (best-effort)
    age_diff_months: int | None = None
    try:
        src_faces = client.detect_faces(Image={"Bytes": source_bytes}, Attributes=["ALL"])
        tgt_faces = client.detect_faces(Image={"Bytes": target_bytes}, Attributes=["ALL"])
        if src_faces["FaceDetails"] and tgt_faces["FaceDetails"]:
            src_ar = src_faces["FaceDetails"][0]["AgeRange"]
            tgt_ar = tgt_faces["FaceDetails"][0]["AgeRange"]
            src_age = (src_ar["Low"] + src_ar["High"]) / 2
            tgt_age = (tgt_ar["Low"] + tgt_ar["High"]) / 2
            age_diff_months = int(abs(src_age - tgt_age) * 12)
    except Exception:  # noqa: BLE001
        pass

    anomalies: list[str] = []
    if not matches:
        anomalies.append("Không tìm thấy khuôn mặt khớp")
    if resp.get("SourceImageFace") is None:
        anomalies.append("Không phát hiện khuôn mặt trong ảnh thẻ")

    age_note = f", age_diff≈{age_diff_months // 12}yr" if age_diff_months else ""
    ly_do = f"Rekognition similarity={similarity:.1f}%, same_person={same_person}{age_note}"

    return {
        "same_person": same_person,
        "confidence": confidence,
        "age_diff_months": age_diff_months,
        "phau_thuat_signs": [],   # Rekognition không phát hiện phẫu thuật thẩm mỹ
        "anomalies": anomalies,
        "ly_do": ly_do,
        "rekognition_similarity": similarity,
    }


def compare_portraits(anh_the_path: Path, doc_path: Path, doc_type: str,
                       model: str | None = None) -> dict | None:
    """So sánh ảnh chân dung trên 2 file (Anh thẻ vs doc_type) via AWS Rekognition.

    `model` parameter nhận nhưng bỏ qua — Rekognition không chọn model.
    Trả dict structured. None nếu lỗi không recover được."""
    if not os.environ.get("AWS_ACCESS_KEY_ID", ""):
        print("vision_check: thiếu AWS_ACCESS_KEY_ID — bỏ qua vision compare", flush=True)
        return None

    import boto3
    region = (os.environ.get("AWS_REGION")
              or os.environ.get("AWS_DEFAULT_REGION")
              or "us-east-1")
    client = boto3.client("rekognition", region_name=region)

    try:
        raw_a = Path(anh_the_path).read_bytes()
        raw_b = Path(doc_path).read_bytes()
    except Exception as e:  # noqa: BLE001
        print(f"vision_check: đọc file lỗi: {type(e).__name__}: {e}", flush=True)
        return None

    # For PDFs: scan pages to find the one with a face rather than blindly using page 0
    img_a = _pdf_find_face_page(raw_a, client) if raw_a[:4] == b"%PDF" else raw_a
    img_b = _pdf_find_face_page(raw_b, client) if raw_b[:4] == b"%PDF" else raw_b
    if img_a is None or img_b is None:
        print(f"vision_check: convert ảnh thất bại ({Path(anh_the_path).name}, "
              f"{Path(doc_path).name})", flush=True)
        return None

    try:
        result = _rekognition_compare(img_a, img_b, client)
    except Exception as e:  # noqa: BLE001
        print(f"vision_check: Rekognition lỗi ({Path(anh_the_path).name} vs "
              f"{Path(doc_path).name}): {type(e).__name__}: {e}", flush=True)
        return None

    result.setdefault("same_person", False)
    result.setdefault("confidence", "low")
    result.setdefault("age_diff_months", None)
    result.setdefault("phau_thuat_signs", [])
    result.setdefault("anomalies", [])
    return result


def find_compare_pairs(dataset: list[dict], download_fn=None) -> list[tuple[dict, dict]]:
    """Tìm các cặp (Anh thẻ doc, doc có ảnh) trong dataset. Cap MAX_PAIRS_PER_CASE.

    `dataset` là list[item] từ scan_pipeline (mỗi item có ten/tag/drive_link/local_path).
    Returns list[(anh_the_item, doc_item)] — mỗi item dict đã include `local_path` để compare_portraits đọc bytes.
    """
    anh_the_items = [d for d in dataset if (d.get("tag") or d.get("loai")) == "Anh the"]
    portrait_items = [d for d in dataset if (d.get("tag") or d.get("loai")) in DOCS_WITH_PORTRAIT]
    if not anh_the_items or not portrait_items:
        return []
    main_anh_the = anh_the_items[0]
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
    không tốn tiền. Trả list[{file_a, file_b, result}].
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

    # 1) Skip nếu không có Anh thẻ hoặc doc có ảnh
    has_anh_the = any((d.get("loai") or d.get("tag")) == "Anh the" for d in dataset)
    has_portrait = any((d.get("loai") or d.get("tag")) in DOCS_WITH_PORTRAIT for d in dataset)
    if not (has_anh_the and has_portrait):
        return []

    # 2) Đọc sidecar cache cũ nếu có
    META_FOLDER = "_Bot OCR & Metadata"
    SIDECAR_NAME = "_vision_compare.json"
    cached: list[dict] = []
    cache_index: dict[str, dict] = {}
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

    # 3) Xây danh sách pair
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
        try:
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

    # 4) Persist updated cache
    if out and n_calls > 0:
        try:
            merged = list(cache_index.values())
            new_hashes = {x["pair_hash"] for x in out if x.get("pair_hash")}
            merged = [m for m in merged if m.get("pair_hash") not in new_hashes]
            merged.extend([x for x in out if x.get("pair_hash") and not x.get("cached")])
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

    # _to_image_bytes: non-PDF passthrough
    fake_jpg = b"\xff\xd8\xff" + b"\x00" * 10
    assert _to_image_bytes(fake_jpg) is fake_jpg

    # find_compare_pairs
    dataset = [
        {"tag": "Anh the", "ten": "Anh the-A.jpg", "local_path": "/tmp/a.jpg"},
        {"tag": "Passport", "ten": "Passport-A.pdf", "local_path": "/tmp/p.pdf"},
        {"tag": "GPLX", "ten": "GPLX-A.jpg", "local_path": "/tmp/g.jpg"},
        {"tag": "CCCD", "ten": "CCCD-A.pdf", "local_path": "/tmp/c.pdf"},
        {"tag": "Sao ke", "ten": "Sao ke-A.pdf", "local_path": "/tmp/s.pdf"},
    ]
    pairs = find_compare_pairs(dataset)
    assert len(pairs) == 3, f"phải có 3 cặp (cap), got {len(pairs)}"
    assert pairs[0][1]["tag"] == "Passport"
    assert pairs[1][1]["tag"] == "GPLX"
    assert pairs[2][1]["tag"] == "CCCD"

    assert find_compare_pairs([{"tag": "Passport", "local_path": "/tmp/p"}]) == []
    assert find_compare_pairs([{"tag": "Anh the", "local_path": "/tmp/a"},
                                {"tag": "Sao ke", "local_path": "/tmp/s"}]) == []

    assert callable(compare_portraits)
    assert callable(evaluate_pairs)
    assert MAX_PAIRS_PER_CASE >= 1
    print(f"vision_check OK (AWS Rekognition) | max_pairs={MAX_PAIRS_PER_CASE}")
