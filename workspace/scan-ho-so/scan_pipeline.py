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
  2. For each file: Gemini OCR/understanding → classify (SOP tag + 1 of 4 top
     folders) → build the SOP-compliant filename.
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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "google/gemini-2.5-flash-lite")
TOP_FOLDERS = ["Personal Docs", "Education", "Asset", "Employment"]
OCR_META_FOLDER = "_Bot OCR & Metadata"

# Extensions Gemini can read (OCR/understanding). Everything else is still
# uploaded — just classified from the filename and flagged needs_review.
OCR_EXT_MIME = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
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
# Gemini OCR / document understanding  (sync; one call per file)
# ============================================================================
def gemini_classify_file(path: Path, filename: str) -> dict:
    import httpx  # local import so --self-test works without it installed

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"doc_type": "", "person": [], "summary_vi": "(no OPENROUTER_API_KEY)", "key_fields": {}, "extracted": {}}
    mime = OCR_EXT_MIME.get(path.suffix.lower(), "application/pdf")
    content_b64 = base64.b64encode(path.read_bytes()).decode()
    prompt = f"""Đọc trực tiếp file hồ sơ visa Canada đính kèm và trả về JSON MỘT DÒNG, THUẦN (không markdown, không giải thích ngoài JSON).

Các trường:
- doc_type: loại giấy tờ tiếng Việt — PHÂN LOẠI THEO BẢN CHẤT GIẤY TỜ, KHÔNG theo các trường/thông tin mà nó nhắc tới.
  • "Căn cước công dân"/"Hộ chiếu"/"Sổ tiết kiệm"/"Lý lịch tư pháp"/"Sao kê ngân hàng"/… CHỈ khi file ĐÚNG LÀ giấy tờ đó
    (vd: CCCD = tấm thẻ in 2 mặt có ảnh chân dung + chip/QR; hộ chiếu = cuốn hộ chiếu; sổ tiết kiệm = cuốn sổ ngân hàng).
  • Một tấm ẢNH CHÂN DUNG / ảnh thẻ của MỘT người (phông trắng/xanh, kiểu ảnh dán hồ sơ — KHÔNG phải ảnh sinh hoạt /
    ảnh chụp nhóm) → doc_type = "Ảnh thẻ". Ảnh chụp gia đình / nhóm người / tiệc → "Ảnh gia đình".
  • Một tờ giấy / biểu mẫu do KHÁCH HÀNG TỰ KHAI / VIẾT TAY / TỰ ĐIỀN thông tin cá nhân (họ tên, ngày sinh, số CCCD,
    địa chỉ, người thân…) → doc_type = "Thông tin cá nhân (tự khai)" (≈ sơ yếu lý lịch), KHÔNG phải "Căn cước công dân"
    chỉ vì có ô "Số CCCD". Tương tự với các loại giấy khác — đừng vì file nhắc đến số/tên gì mà gán nhầm loại.
- person: [{{"full_name":"...","date_of_birth":"..."}}]
- summary_vi: tóm tắt 1-2 câu
- key_fields: {{"số giấy tờ":"...","ngày cấp":"...","nơi cấp":"..."}}
- extracted: object trích MỌI thông tin nhìn thấy phục vụ kiểm tra hồ sơ; trường nào không có để chuỗi rỗng "" hoặc mảng rỗng []. Chỉ điền cái nào áp dụng với loại giấy này. Các khoá có thể có:
  ho_ten, ngay_sinh, gioi_tinh, quoc_tich, noi_sinh, que_quan, noi_thuong_tru, noi_o_hien_tai,
  so_giay_to, loai_so ("CMND 9 số"|"CCCD 12 số"|"hộ chiếu"|...), ngay_cap, noi_cap, ngay_het_han, co_gia_tri_den,
  ho_ten_cha, nam_sinh_cha, ho_ten_me, nam_sinh_me, ho_ten_vo_chong, so_cmnd_cu_vo_chong, nguoi_di_khai_sinh,
  thanh_vien_ho_khau ([{{"ho_ten":"","ngay_sinh":"","so_dinh_danh":"","quan_he_voi_chu_ho":""}}]), giay_co_gia_tri_den,
  chu_tai_khoan, so_tai_khoan_hoac_so, so_tien, ky_han, ngay_dao_han, ngay_xac_nhan_so_du, so_du,
  ky_sao_ke_tu, ky_sao_ke_den, ten_cong_ty, ma_so_bhxh, giai_doan_dong_bhxh, ma_the_bhyt, bhyt_gia_tri_tu, bhyt_gia_tri_den,
  tinh_trang_an_tich, la_to_khai (true nếu là tờ tự khai / biểu mẫu khách tự ghi; false nếu là giấy tờ chính thức do cơ quan cấp),
  co_dau_moc (true/false), co_chu_ky (true/false), visual_flags (["ảnh mờ","nghi tẩy xóa ...","thiếu chữ ký","thiếu dấu mộc",...])

Tên file: {filename}

Nếu không đọc được file, vẫn trả JSON với summary_vi mô tả lý do và "extracted": {{}}."""
    payload = {
        "model": GEMINI_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "file", "file": {"filename": filename, "file_data": f"data:{mime};base64,{content_b64}"}},
        ]}],
        "temperature": 0.1,
    }
    with httpx.Client(timeout=120) as client:
        resp = client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            d.setdefault("extracted", {})
            return d
        return {"doc_type": "", "person": [], "summary_vi": str(d)[:300], "key_fields": {}, "extracted": {}}
    except Exception:
        return {"doc_type": "", "person": [], "summary_vi": text[:300], "key_fields": {}, "extracted": {}}


def subject_from_gemini(gem: dict, fallback: str) -> str:
    person = gem.get("person")
    if isinstance(person, list) and person:
        p0 = person[0]
        return (p0.get("full_name") or p0.get("name") or "") if isinstance(p0, dict) else str(p0)
    if isinstance(person, dict):
        return person.get("full_name") or person.get("name") or ""
    if isinstance(person, str):
        return person
    return fallback


# ----------------------------------------------------------------------------
# filename collision handling — never let two distinct source files in the same
# batch collapse to one Drive name (that silently loses a file). Per-run only:
# the Nth file of a given (tag, subject) gets " N" appended (SOP "file thứ N"),
# so re-running the same input reproduces the same names and stays idempotent.
# ----------------------------------------------------------------------------
def dedup_name(name_registry: dict, tag: str, subject_title: str, ext: str,
               is_english: bool, build_filename) -> str:
    key = (tag.lower().strip(), subject_title.lower().strip())
    n = name_registry.get(key, 0) + 1
    name_registry[key] = n
    return build_filename(tag, subject_title, ext, index=(n if n > 1 else None), is_english=is_english)


# ============================================================================
# process one file (with retries)
# ============================================================================
def process_one(path: Path, src_name: str, *, case_folder_id: str, applicant: str,
                case_id: str, retries: int, dry_run: bool, sop, name_registry: dict) -> dict:
    classify_doc_type, build_filename, detect_english, title_case_ascii = sop
    ext = path.suffix.lower()
    can_ocr = ext in OCR_EXT_MIME
    mime = OCR_EXT_MIME.get(ext) or OTHER_EXT_MIME.get(ext) or "application/octet-stream"

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            gem: dict = {}
            if can_ocr and not dry_run:
                gem = gemini_classify_file(path, src_name)
            if not isinstance(gem, dict):
                gem = {"doc_type": "", "person": [], "summary_vi": str(gem)[:300], "key_fields": {}, "extracted": {}}
            gem.setdefault("key_fields", {})
            if not isinstance(gem["key_fields"], dict):
                gem["key_fields"] = {}
            gem.setdefault("extracted", {})
            if not isinstance(gem["extracted"], dict):
                gem["extracted"] = {}

            raw_dt = gem.get("doc_type", "")
            summary = str(gem.get("summary_vi", ""))[:400]
            cls = classify_doc_type(raw_dt, summary, src_name, extracted=gem.get("extracted"))
            needs_review = cls.needs_review or (not can_ocr)
            subject_raw = subject_from_gemini(gem, applicant) or applicant
            subject_title = title_case_ascii(subject_raw) or "Unknown"
            is_eng = detect_english(summary, "")
            # Only the first retry attempt may consume a registry slot per file;
            # build it once on attempt 1 and reuse it on later attempts.
            if attempt == 1 or "new_name" not in locals():
                new_name = dedup_name(name_registry, cls.tag, subject_title, path.suffix, is_eng, build_filename)

            item = {
                "src_name": src_name, "new_name": new_name, "ext": ext,
                "tag": cls.tag, "folder": cls.folder, "subject": subject_title,
                "confidence": cls.confidence if can_ocr else "low",
                "needs_review": needs_review, "is_english": is_eng,
                "ocr": can_ocr, "summary": summary, "extracted": gem.get("extracted") or {},
                "gemini": gem, "case_id": case_id,
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
                md = (f"# {new_name}\n\n"
                      f"**Loại:** {cls.tag} | **Folder:** {cls.folder}\n"
                      f"**Người:** {item['subject']} | **Confidence:** {item['confidence']}{review}{eng}\n"
                      f"**File gốc:** {src_name}\n\n## Tóm tắt\n{summary or '(no OCR)'}\n")
                if gem.get("key_fields"):
                    md += "\n## Thông tin chính\n" + "\n".join(f"- **{k}:** {v}" for k, v in gem["key_fields"].items()) + "\n"
                _ex = gem.get("extracted") or {}
                _ex_lines = [f"- **{k}:** {v}" for k, v in _ex.items() if v not in ("", [], {}, None, False)]
                if _ex_lines:
                    md += "\n## Dữ liệu trích xuất\n" + "\n".join(_ex_lines) + "\n"
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
    # ảnh chân dung / ảnh thẻ 5x7 (phông trắng, 1 người) → "Anh the" (mục 9 FARM)
    assert classify_doc_type("Ảnh thẻ 5x7", "ảnh chân dung phông trắng", "Khac-Mo.jpg").tag == "Anh the"
    assert classify_doc_type("Ảnh chân dung", "", "ID photo-Mo.jpg").tag == "Anh the"
    assert classify_doc_type("Ảnh chụp gia đình", "tiệc sinh nhật", "x.jpg").tag == "Anh gia dinh"
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

        name_registry: dict = {}
        items = []
        for idx, (path, src_name) in enumerate(files, 1):
            log(f"[{idx}/{total}] {src_name}")
            it = process_one(path, src_name, case_folder_id=case_folder_id or "", applicant=applicant,
                             case_id=case_id, retries=args.retries, dry_run=args.dry_run, sop=sop,
                             name_registry=name_registry)
            status = it.get("status", "?")
            log(f"     -> {status}  {it.get('new_name', '')}")
            items.append(it)

        n = {"uploaded": 0, "uploaded-no-ocr": 0, "duplicate": 0, "failed": 0, "dry-run": 0}
        for it in items:
            n[it.get("status", "failed")] = n.get(it.get("status", "failed"), 0) + 1
        manifest = {
            "input": str(in_path) if in_path else None,
            "case_folder_id": case_folder_id,
            "case_id": case_id,
            "applicant": applicant,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_input_files": total,
            "counts": n,
            "ok": n["failed"] == 0,
            "items": items,
        }

        # --- AI checklist cross-check: a bolt-on after OCR/upload; it must NEVER
        #     break the core result, so the whole thing is wrapped. Runs when there's
        #     a checklist-relevant doc in the batch (auto-debounce), or always in --checklist-only.
        if not args.dry_run and case_folder_id and not args.no_checklist:
            try:
                from lib import checklist as _ck
                if args.checklist_only or _ck.should_run_checklist(manifest):
                    log("running AI checklist ...")
                    ck = _ck.run_and_write(case_folder_id, applicant, SHARED_DRIVE_ID,
                                           batch_items=items, today=today_vn)
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
        review = [it for it in items if it.get("needs_review")]
        if review and not args.dry_run:
            print(f"\nNEEDS REVIEW ({len(review)}): " + ", ".join(it["src_name"] for it in review))
        # reconciliation guarantee: every input file is accounted for in the manifest
        assert len(items) == total, f"manifest covers {len(items)} but {total} input files (BUG)"
        print("=" * 60)
        return 0 if n["failed"] == 0 else 1
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2
    finally:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
