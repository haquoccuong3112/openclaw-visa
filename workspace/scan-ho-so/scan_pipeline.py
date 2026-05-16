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
  2. Gemini OCR/understanding for every OCR-able file IN PARALLEL (a thread pool,
     SCAN_OCR_WORKERS, default 5 — each file is one independent HTTP call); then,
     per file (sequential): classify (SOP tag + 1 of 4 top folders) → build the
     SOP-compliant filename. (Classify / dedup / Drive upload stay sequential —
     the Drive client isn't thread-safe.)
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
# Model for post-DocAI structure extraction.
OCR_CLASSIFY_MODEL = os.environ.get("OCR_CLASSIFY_MODEL", "gpt-5-mini")
# Document AI processor id — REQUIRED.
DOCAI_PROCESSOR_ID = os.environ.get("GOOGLE_DOCUMENTAI_PROCESSOR_ID", "")
# Batch-plan nhiều PDF để tránh 1 LLM call / PDF nhỏ.
DOCAI_BATCH_PLAN_MAX_FILES = int(os.environ.get("DOCAI_BATCH_PLAN_MAX_FILES", "12"))
DOCAI_BATCH_PLAN_MAX_CHARS = int(os.environ.get("DOCAI_BATCH_PLAN_MAX_CHARS", "24000"))
# Response schema for structure extraction (gpt-5-mini with json_schema).
OCR_RESPONSE_SCHEMA = {
    "name": "ocr_result",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "doc_type":   {"type": "string"},
            "person":     {"type": "array", "items": {"type": "object",
                "properties": {
                    "full_name": {"type": "string"},
                    "date_of_birth": {"type": "string"},
                    "relation": {"type": "string"},
                },
                "additionalProperties": True}},
            "summary_vi": {"type": "string"},
            "key_fields": {"type": "object", "additionalProperties": True},
            "extracted":  {"type": "object", "additionalProperties": True},
        },
        "required": ["doc_type", "summary_vi", "extracted"],
        "additionalProperties": True,
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
# Số file được DocAI OCR ĐỒNG THỜI. Phân loại + upload Drive + thẩm định vẫn tuần tự.
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


def docai_classify(pages_text: list[dict], filename: str,
                   applicant: str = "", model: str | None = None) -> dict:
    """DocAI pages text → gpt-5-mini structure extraction.

    Takes [{page: N, text: str}] from DocAI → returns gem dict:
    {doc_type, person[], summary_vi, key_fields, extracted}
    """
    if not pages_text:
        return {"doc_type": "Khac", "person": [], "summary_vi": "(no text)", "key_fields": {}, "extracted": {}}

    if len(pages_text) == 1:
        text_content = pages_text[0].get("text", "")
    else:
        parts = [f"[Trang {p['page']}]\n{p.get('text', '')}" for p in pages_text]
        text_content = "\n\n".join(parts)

    if not text_content.strip():
        return {"doc_type": "Khac", "person": [], "summary_vi": "(không đọc được text)", "key_fields": {}, "extracted": {}}

    try:
        from lib.rule_loader import generate_doc_type_catalog
        _doc_catalog = generate_doc_type_catalog()
    except Exception:  # noqa: BLE001
        _doc_catalog = ""

    applicant_line = f'Đương đơn chính (applicant): "{applicant}"\n\n' if applicant else ""
    prompt = f"""Đây là text OCR từ hồ sơ visa Canada. Phân tích và trả về JSON MỘT DÒNG, THUẦN (không markdown, không giải thích ngoài JSON).

{applicant_line}# DANH MỤC LOẠI GIẤY TỜ BOT NHẬN DIỆN (tham khảo — `doc_type` nên match TÊN tiếng Việt 1 trong các loại bên dưới):
{_doc_catalog or "(catalog không load được)"}

Các trường:
- doc_type: loại giấy tờ tiếng Việt — PHÂN LOẠI THEO BẢN CHẤT GIẤY TỜ, KHÔNG theo các trường/thông tin mà nó nhắc tới.
  • "Căn cước công dân"/"Hộ chiếu"/"Sổ tiết kiệm"/"Lý lịch tư pháp"/"Sao kê ngân hàng"/… CHỈ khi file ĐÚNG LÀ giấy tờ đó
    (vd: CCCD = tấm thẻ in 2 mặt có ảnh chân dung + chip/QR; hộ chiếu = cuốn hộ chiếu; sổ tiết kiệm = cuốn sổ ngân hàng).
  • PHÂN BIỆT ẢNH (áp dụng khi text OCR rỗng hoặc rất ít — file là 1 tấm ảnh):
    – text rỗng/tên file gợi ý ảnh chân dung 1 người, phông đơn sắc → doc_type = "Ảnh thẻ" VÀ extracted.la_anh_the = true;
    – text/tên file gợi ý người làm nông / làm việc / vườn-ruộng-nhà kính → doc_type = "Ảnh làm nông";
    – text/tên file gợi ý nhiều người / gia đình / tiệc / sự kiện → doc_type = "Ảnh gia đình".
    ⚠️ Ảnh chân dung in TRÊN giấy tờ khác (thẻ CCCD, hộ chiếu, bằng cấp…) → phân loại theo giấy tờ đó, KHÔNG phải "Ảnh thẻ".
  • Một tờ giấy / biểu mẫu do KHÁCH HÀNG TỰ KHAI / VIẾT TAY / TỰ ĐIỀN thông tin cá nhân (họ tên, ngày sinh, số CCCD,
    địa chỉ, người thân…) → doc_type = "Thông tin cá nhân (tự khai)" (≈ sơ yếu lý lịch), KHÔNG phải "Căn cước công dân"
    chỉ vì có ô "Số CCCD". Tương tự với các loại giấy khác — đừng vì file nhắc đến số/tên gì mà gán nhầm loại.
  • "Sao kê ngân hàng": CHỈ áp dụng khi file có ĐÚNG cấu trúc sao kê tài khoản: SỐ TÀI KHOẢN + KỲ SAO KÊ
    (từ ngày–đến ngày) + DANH SÁCH GIAO DỊCH nhiều dòng (cột nợ/có/số dư) + SỐ DƯ đầu/cuối kỳ. KHÔNG gắn
    "Sao kê ngân hàng" cho: ảnh thẻ visa scan, biên lai đơn lẻ, thông báo SMS ngân hàng, hay bảng có vài hàng.
- person: [{{"full_name":"...","date_of_birth":"...","relation":"..."}}]
  • relation: quan hệ của người đó với đương đơn chính. 1 trong:
    "applicant" | "cha" | "me" | "vo" | "chong" | "con" | "anh_chi_em" | "khac" | "".
  • CHỈ ĐIỀN khi văn bản GHI RÕ chữ "cha/bố/mẹ/vợ/chồng/con" kèm tên đó. KHÔNG SUY DIỄN.
    "bs" = bản sao (viết tắt), KHÔNG phải họ tên người và KHÔNG phải "bố".
- summary_vi: tóm tắt 1-2 câu
- key_fields: {{"số giấy tờ":"...","ngày cấp":"...","nơi cấp":"..."}}
- extracted: object trích MỌI thông tin nhìn thấy phục vụ kiểm tra hồ sơ; trường nào không có để chuỗi rỗng "" hoặc mảng rỗng []. Chỉ điền cái nào áp dụng với loại giấy này. Các khoá có thể có:
  ho_ten, ngay_sinh, gioi_tinh, quoc_tich, noi_sinh, que_quan, noi_thuong_tru, noi_o_hien_tai,
  so_giay_to, loai_so ("CMND 9 số"|"CCCD 12 số"|"hộ chiếu"|...), ngay_cap, noi_cap, ngay_het_han, co_gia_tri_den,
  ho_ten_cha, nam_sinh_cha, ngay_sinh_cha (DD/MM/YYYY nếu thấy đầy đủ, nếu chỉ thấy năm thì để rỗng),
  ho_ten_me, nam_sinh_me, ngay_sinh_me (DD/MM/YYYY tương tự),
  ho_ten_vo_chong, so_cmnd_cu_vo_chong, nguoi_di_khai_sinh,
  thanh_vien_ho_khau ([{{"ho_ten":"","ngay_sinh":"","so_dinh_danh":"","quan_he_voi_chu_ho":""}}]), giay_co_gia_tri_den,
  chu_tai_khoan, so_tai_khoan_hoac_so, so_tien, ky_han, ngay_dao_han, ngay_xac_nhan_so_du, so_du,
  ky_sao_ke_tu, ky_sao_ke_den, ten_cong_ty, ma_so_bhxh, giai_doan_dong_bhxh, ma_the_bhyt, bhyt_gia_tri_tu, bhyt_gia_tri_den,
  tinh_trang_an_tich,
  la_to_khai (true nếu là tờ tự khai / biểu mẫu khách tự ghi; false nếu là giấy tờ chính thức do cơ quan cấp),
  la_anh_the (true CHỈ khi cả file LÀ một tấm ảnh chân dung riêng lẻ kiểu ảnh dán hồ sơ — KHÔNG phải ảnh sinh hoạt / làm việc / làm nông / chụp nhóm, và KHÔNG phải ảnh chân dung in trên CCCD / hộ chiếu / bằng cấp),
  co_dau_moc (true/false), co_chu_ky (true/false),
  visual_flags (["ảnh mờ","nghi tẩy xóa","thiếu chữ ký","thiếu dấu mộc",...] — dấu hiệu bất thường đọc được từ text),

  // Chỉ điền khi doc_type = "Ảnh thẻ":
  la_mat_moc, co_trang_suc, co_xam_lo, toc_toi_mau, phong_nen_trang,

  // Chỉ điền khi doc_type = "Căn cước công dân" và là mặt sau:
  co_2_o_van_tay (true nếu text OCR hoặc tên file chỉ ra MẶT SAU có đủ 2 ô vân tay),

  // Sub-typing — chỉ điền khi loại tương ứng:
  bang_cap_level ("cap_2"|"cap_3"|"trung_cap"|"cao_dang"|"dai_hoc"|"thac_si"|"tien_si"|"khac"),
  gplx_hang ("A1"|"A2"|"B"|"B2"|"C"|"D"|"E"|"FC"),
  cccd_mat ("truoc"|"sau"|"2-mat" — file có cả 2 mặt thì "2-mat"),
  co_dau_xa_phuong (true CHỈ khi đây là Sơ yếu lý lịch / đơn / xác nhận CÓ con dấu mộc tròn của UBND xã/phường),

  // MRZ — chỉ điền khi doc_type là CCCD hoặc Hộ chiếu VÀ text OCR có vùng MRZ (dòng chứa "<<"):
  mrz: {{"raw":"<2-3 dòng MRZ nguyên văn>", "name":"<tên parse từ MRZ>", "dob":"<DD/MM/YYYY>", "doc_no":"<số giấy tờ>"}}
  ⚠️ MRZ name LÀ GROUND-TRUTH chủ giấy tờ — KHÔNG được dùng tên ngoài MRZ nếu MRZ rõ ràng đọc được.

QUY TẮC CHỐNG SUY DIỄN (BẮT BUỘC):
- "bs" = "bản sao" (viết tắt thường dùng trên giấy khai sinh bản sao). KHÔNG được dịch thành "ba" / "bố" / coi là họ tên người.
- KHÔNG được tự chèn "vợ" / "chồng" / "con" / "bố" / "mẹ" vào tên người hoặc relation nếu văn bản KHÔNG ghi rõ chữ đó kèm tên cụ thể.
- Một CCCD/giấy tờ thuộc về MỘT chủ thể duy nhất — nếu MRZ đọc được, chủ thể = MRZ name; nếu không, chủ thể = họ tên ngay dưới dòng "Họ tên" / "Full name", KHÔNG phải tên người được nhắc tới trong các trường phụ.

VÍ DỤ output (3 case tham khảo):

VD1 (CCCD): {{"doc_type":"Căn cước công dân","person":[{{"full_name":"Nguyễn Văn A","date_of_birth":"01/01/1990","relation":"applicant"}}],"summary_vi":"Thẻ CCCD của Nguyễn Văn A, số 0123...","key_fields":{{"số CCCD":"0123...","ngày cấp":"15/03/2021"}},"extracted":{{"ho_ten":"Nguyễn Văn A","ngay_sinh":"01/01/1990","so_giay_to":"0123456789","loai_so":"CCCD 12 số","ngay_cap":"15/03/2021","la_to_khai":false,"la_anh_the":false}}}}

VD2 (Sổ đất): {{"doc_type":"Giấy chứng nhận quyền sử dụng đất","person":[],"summary_vi":"Sổ đất của Trần Văn B + vợ, thửa 123 tại huyện X","key_fields":{{"số GCN":"AB-12345"}},"extracted":{{"chu_su_dung":"Trần Văn B và Nguyễn Thị C","dia_chi_thua":"thửa 123 tờ bản đồ 4, xã Y, huyện X","dien_tich":"180 m2"}}}}

VD3 (Khai sinh con): {{"doc_type":"Trích lục khai sinh","person":[{{"full_name":"Trần Văn D","date_of_birth":"05/05/2015","relation":"con"}}],"summary_vi":"Khai sinh của Trần Văn D (con), bố Trần Văn B, mẹ Nguyễn Thị C","key_fields":{{"số khai sinh":"123/2015"}},"extracted":{{"ho_ten":"Trần Văn D","ngay_sinh":"05/05/2015","ho_ten_cha":"Trần Văn B","ho_ten_me":"Nguyễn Thị C","la_to_khai":false}}}}

Tên file: {filename}

TEXT OCR:
{text_content[:10000]}

Nếu không đọc được nội dung, trả JSON với doc_type="Khac", summary_vi mô tả lý do, và "extracted": {{}}."""

    payload = {
        "model": model or OCR_CLASSIFY_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_schema", "json_schema": OCR_RESPONSE_SCHEMA},
    }
    try:
        text = _call_classify_api(payload)
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        d = json.loads(text)
        if isinstance(d, dict):
            d.setdefault("extracted", {})
            return d
        return {"doc_type": "", "person": [], "summary_vi": str(d)[:300], "key_fields": {}, "extracted": {}}
    except Exception as e:  # noqa: BLE001
        return {"doc_type": "", "person": [], "summary_vi": f"(classify lỗi: {type(e).__name__}: {e})", "key_fields": {}, "extracted": {}}


def _docai_ocr_one(path: "Path", src_name: str) -> "list[dict] | None":
    """OCR 1 file bằng DocAI, trả pages_text hoặc None."""
    try:
        from lib.docai_client import ocr_with_docai
        return ocr_with_docai(path)
    except Exception as e:  # noqa: BLE001
        log(f"  DocAI OCR lỗi cho {src_name}: {type(e).__name__}: {e}")
        return None


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


# ============================================================================
# Document AI + DeepSeek unified planning flow (PDF)
# ============================================================================
_EXISTING_DOCS_CACHE: dict[str, list[dict]] = {}  # cleared at run start, keyed by case_folder_id


def _fetch_existing_docs(case_folder_id: str) -> list[dict]:
    """Đọc tất cả .json sidecar trong _Bot OCR & Metadata của case.
    Trả list[{"filename":…, "tag":…, "relation":…}] để inject vào prompt DeepSeek
    giúp nó biết hồ sơ đã có gì (quan trọng khi khách gửi lắc nhắc nhiều lần).
    Kết quả cache trong _EXISTING_DOCS_CACHE để tránh gọi Drive nhiều lần trong 1 batch."""
    if not case_folder_id:
        return []
    if case_folder_id in _EXISTING_DOCS_CACHE:
        return _EXISTING_DOCS_CACHE[case_folder_id]
    try:
        from lib.drive_helpers import get_or_create_folder, list_folder, download_file_text
        meta_id = get_or_create_folder(OCR_META_FOLDER, case_folder_id, drive_id=SHARED_DRIVE_ID)
        files = list_folder(meta_id, drive_id=SHARED_DRIVE_ID)
        existing: list[dict] = []
        for name, fid in files.items():
            if not name.lower().endswith(".json"):
                continue
            # bỏ qua sidecar da-duyet meta (prefix "da-duyet - ")
            if name.startswith("da-duyet - "):
                continue
            try:
                d = json.loads(download_file_text(fid, drive_id=SHARED_DRIVE_ID))
                if isinstance(d, dict) and d.get("new_name"):
                    existing.append({
                        "filename": d["new_name"],
                        "tag": d.get("tag", ""),
                        "relation": d.get("relation", ""),
                    })
            except Exception:  # noqa: BLE001
                continue
        _EXISTING_DOCS_CACHE[case_folder_id] = existing
        return existing
    except Exception as e:  # noqa: BLE001
        log(f"  _fetch_existing_docs lỗi ({type(e).__name__}: {e}) — bỏ qua context cũ")
        return []


def _docai_ocr_pdf(path: Path) -> list[dict] | None:
    """OCR 1 PDF bằng Google Document AI, trả pages_text hoặc None."""
    try:
        from lib.docai_client import ocr_with_docai
        log(f"  DocAI OCR: {path.name} …")
        pages_text = ocr_with_docai(path)
        if not pages_text:
            log("  DocAI OCR: trả [] — skip file")
            return None
        log(f"  DocAI OCR xong: {len(pages_text)} trang")
        return pages_text
    except Exception as e:  # noqa: BLE001
        log(f"  DocAI OCR lỗi ({type(e).__name__}: {e}) — skip")
        return None


@functools.lru_cache(maxsize=1)
def _docai_catalog_context() -> tuple[dict, str, str] | None:
    """Load doc-type catalog + relation whitelist for DocAI planning prompts."""
    try:
        from lib.rule_loader import load_doc_types, generate_doc_type_catalog, load_relations
        _doc_types = load_doc_types()
        tag_to_folder = {dt.tag: dt.folder for dt in _doc_types}
        catalog = generate_doc_type_catalog(_doc_types)
        relations_list = load_relations()
        relations_str = "|".join(r.relation for r in relations_list) + "|applicant|khac"
        return tag_to_folder, catalog, relations_str
    except Exception as e:  # noqa: BLE001
        log(f"  _docai_catalog_context: không load catalog ({e}) — fallback")
        return None


def _docai_existing_block(case_folder_id: str) -> str:
    existing_docs = _fetch_existing_docs(case_folder_id) if case_folder_id else []
    if not existing_docs:
        return ""
    lines = [f"- {d['filename']} (tag={d['tag']}, relation={d['relation'] or 'applicant'})"
             for d in existing_docs]
    return "ĐÃ CÓ TRÊN DRIVE (các lần gửi trước):\n" + "\n".join(lines) + "\n\n"


def _clean_applicant_for_prompt(applicant: str) -> str:
    import re as _re
    _clean_ap = _re.sub(r'\b\d{4}\b', '', applicant or '')
    _clean_ap = _re.sub(r'\b[A-Za-z]+\d+[A-Za-z]*\b', '', _clean_ap)
    return ' '.join(_clean_ap.split()).strip()


def _docai_plan_api_call(prompt: str, *, timeout: int = 180) -> dict | None:
    """Call gpt-5-mini planner. Caller validates schema."""
    import httpx

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    plan_model = OCR_CLASSIFY_MODEL

    if openai_key:
        api_endpoint = "https://api.openai.com/v1/chat/completions"
        api_key = openai_key
    elif openrouter_key:
        api_endpoint = "https://openrouter.ai/api/v1/chat/completions"
        api_key = openrouter_key
    else:
        return None

    payload: dict = {
        "model": plan_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(api_endpoint, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
        if resp.status_code >= 400:
            payload.pop("response_format", None)
            resp = client.post(api_endpoint, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def _build_single_pdf_plan_prompt(path: Path, pages_text: list[dict], applicant: str,
                                  case_folder_id: str, catalog: str, relations_str: str) -> str:
    page_block_lines = []
    for p in pages_text:
        text_trunc = str(p.get("text", ""))[:600]  # ~150 token — đủ nhận dạng loại giấy + tên
        page_block_lines.append(f"[Trang {p['page']}]\n{text_trunc}")
    page_block = "\n\n".join(page_block_lines)
    clean_ap = _clean_applicant_for_prompt(applicant)
    applicant_line = f'Đương đơn chính (applicant): "{clean_ap}"\n\n' if clean_ap else ""
    existing_block = _docai_existing_block(case_folder_id)
    total_pages = len(pages_text)
    return (
        "Bạn là chuyên gia phân loại hồ sơ visa Canada. Đọc text OCR từng trang của một PDF "
        "có thể gộp nhiều loại giấy tờ khác nhau. Nhiệm vụ:\n"
        "1. Xác định mỗi giấy tờ (range trang, loại, chủ thể, quan hệ)\n"
        "2. Đặt tên file SOP chuẩn: TAG-Ho Ten.pdf (TAG từ catalog, họ tên không dấu title-case)\n"
        "3. Đánh dấu needs_vision=true CHỈ cho: Ảnh thẻ, Ảnh gia đình, Ảnh làm nông "
        "(text OCR không đủ để classify ảnh — cần Gemini Vision xem thêm)\n\n"
        f"{applicant_line}{existing_block}"
        f"TỔNG SỐ TRANG PDF: {total_pages}\n\n"
        f"CATALOG TAG (chỉ được dùng tag có trong danh sách):\n{catalog}\n\n"
        f"QUAN HỆ hợp lệ: {relations_str}\n\n"
        "TEXT OCR TỪNG TRANG:\n"
        f"{page_block}\n\n"
        "OUTPUT: JSON object duy nhất, không markdown:\n"
        '{"documents": [\n'
        '  {"pages": [1, 2], "tag": "CCCD", "folder": "Personal Docs",\n'
        '   "subject": "Hoang Thi Mo", "relation": "applicant",\n'
        '   "filename": "CCCD-Hoang Thi Mo.pdf",\n'
        '   "confidence": "high", "needs_vision": false, "reason": "..."}\n'
        "]}\n\n"
        "Quy tắc:\n"
        "- GOM TRANG: nhiều trang liên tiếp cùng loại giấy tờ của cùng người → 1 document duy nhất. "
        "VÍ DỤ: trang 5-8 đều là Sao ke của Nguyen Van A → pages: [5,6,7,8], KHÔNG tạo 4 document riêng.\n"
        "- Chỉ tạo document mới khi loại giấy tờ HOẶC chủ thể (tên người) khác nhau.\n"
        "- pages: list số trang (1-based) — tất cả trang phải được bao phủ\n"
        "- subject: họ tên ASCII không dấu, title-case — PHẢI là chủ thể của giấy tờ đó\n"
        '- relation: "applicant" nếu là của đương đơn chính; rỗng nếu không xác định\n'
        "- filename phải theo SOP: TAG-Subject.pdf hoặc TAG relation-Subject.pdf\n"
        "- Trang bị mờ / trống / không xác định → gom vào trang gần nhất hoặc tag='Khac'\n"
        "- Nếu file đã có trên Drive (trong ĐÃ CÓ) → KHÔNG đặt tên trùng; phân biệt bằng số (2, 3...)"
    )


def _docai_plan_pdf(path: Path, applicant: str, case_folder_id: str) -> dict | None:
    """Phase DocAI: OCR toàn bộ PDF 1 lần qua Document AI → DeepSeek đọc full text →
    trả plan JSON {documents: [{pages, tag, folder, subject, relation, filename,
    confidence, needs_vision, reason}]}.

    Trả None nếu lỗi (caller fallback sang flow cũ Gemini page-classify).
    """
    # 1. Document AI OCR
    pages_text = _docai_ocr_pdf(path)
    if not pages_text:
        return None

    # 2. Load catalog + relations
    ctx = _docai_catalog_context()
    if not ctx:
        return None
    _tag_to_folder, catalog, relations_str = ctx
    prompt = _build_single_pdf_plan_prompt(path, pages_text, applicant, case_folder_id, catalog, relations_str)

    try:
        plan = _docai_plan_api_call(prompt, timeout=120)
        if not isinstance(plan, dict) or "documents" not in plan:
            log(f"  _docai_plan_pdf: response không có 'documents' — fallback")
            return None
        log(f"  _docai_plan_pdf: {len(plan['documents'])} documents planned")
        plan["_pages_text"] = pages_text  # full OCR, dùng lại trong sidecar summary
        return plan
    except Exception as e:  # noqa: BLE001
        log(f"  _docai_plan_pdf DeepSeek lỗi ({type(e).__name__}: {e}) — fallback")
        return None


def _make_docai_batch_chunks(ocr_items: list[dict]) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    max_files = max(1, DOCAI_BATCH_PLAN_MAX_FILES)
    max_chars = max(4000, DOCAI_BATCH_PLAN_MAX_CHARS)
    for item in ocr_items:
        item_chars = sum(len(str(p.get("text", ""))[:600]) for p in item.get("pages_text", []))
        if cur and (len(cur) >= max_files or cur_chars + item_chars > max_chars):
            chunks.append(cur)
            cur = []
            cur_chars = 0
        cur.append(item)
        cur_chars += item_chars
    if cur:
        chunks.append(cur)
    return chunks


def _build_docai_batch_plan_prompt(chunk: list[dict], applicant: str, case_folder_id: str,
                                   catalog: str, relations_str: str) -> str:
    clean_ap = _clean_applicant_for_prompt(applicant)
    applicant_line = f'Đương đơn chính (applicant): "{clean_ap}"\n\n' if clean_ap else ""
    existing_block = _docai_existing_block(case_folder_id)
    file_blocks: list[str] = []
    for item in chunk:
        lines = [f"[FILE {item['file_id']}] filename={item['src_name']} total_pages={len(item['pages_text'])}"]
        for p in item["pages_text"]:
            text_trunc = str(p.get("text", ""))[:600]
            lines.append(f"[FILE {item['file_id']} Trang {p['page']}]\n{text_trunc}")
        file_blocks.append("\n".join(lines))
    files_text = "\n\n".join(file_blocks)
    return (
        "Bạn là chuyên gia phân loại hồ sơ visa Canada. Bạn sẽ nhận OCR của NHIỀU PDF riêng biệt "
        "trong cùng một batch khách gửi. Hãy plan từng file độc lập, nhưng chỉ dùng MỘT JSON trả về.\n\n"
        f"{applicant_line}{existing_block}"
        f"CATALOG TAG (chỉ được dùng tag có trong danh sách):\n{catalog}\n\n"
        f"QUAN HỆ hợp lệ: {relations_str}\n\n"
        "OCR CÁC FILE:\n"
        f"{files_text}\n\n"
        "OUTPUT JSON object duy nhất, không markdown, schema:\n"
        '{"files": [\n'
        '  {"file_id": "F1", "documents": [\n'
        '    {"pages": [1,2], "tag": "CCCD", "folder": "Personal Docs", '
        '"subject": "Hoang Thi Mo", "relation": "applicant", '
        '"filename": "CCCD-Hoang Thi Mo.pdf", "confidence": "high", '
        '"needs_vision": false, "reason": "..."}\n'
        '  ]}\n'
        "]}\n\n"
        "Quy tắc bắt buộc:\n"
        "- Trả đủ mọi file_id được cung cấp. pages là số trang 1-based BÊN TRONG file đó, không cộng dồn qua file khác.\n"
        "- Mỗi file được plan độc lập; KHÔNG gộp trang từ 2 file khác nhau vào 1 document.\n"
        "- Trong cùng 1 file: nhiều trang liên tiếp cùng loại giấy tờ/cùng người → 1 document.\n"
        "- Chỉ tạo document mới khi loại giấy tờ hoặc chủ thể khác nhau.\n"
        "- subject: họ tên ASCII không dấu, title-case — là chủ thể của giấy tờ.\n"
        '- relation: "applicant" nếu là đương đơn chính; rỗng nếu không xác định.\n'
        "- filename theo SOP: TAG-Subject.pdf hoặc TAG relation-Subject.pdf.\n"
        "- Trang mờ/trống/không xác định → gom vào trang gần nhất hoặc tag='Khac'.\n"
        "- Nếu file đã có trên Drive (trong ĐÃ CÓ) → không đặt tên trùng; phân biệt bằng số (2,3...)."
    )


def _docai_ocr_pdfs(pdf_files: list[tuple[Path, str]]) -> list[dict]:
    """DocAI OCR tất cả PDF SONG SONG (ThreadPool), trả list dict {file_id, path, src_name, pages_text}.
    File OCR không ra kết quả (pages_text rỗng) → bỏ qua khỏi list trả về.
    DocAI client tạo mới mỗi call (không shared state) → an toàn thread."""
    if not pdf_files:
        return []
    n_workers = max(1, min(OCR_WORKERS, len(pdf_files)))
    log(f"DocAI OCR song song: {len(pdf_files)} PDF, {n_workers} luồng …")
    results: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        fut_map = {
            ex.submit(_docai_ocr_pdf, path): (i, path, src_name)
            for i, (path, src_name) in enumerate(pdf_files, 1)
        }
        for fut in concurrent.futures.as_completed(fut_map):
            i, path, src_name = fut_map[fut]
            try:
                pages_text = fut.result()
            except Exception as e:  # noqa: BLE001
                log(f"  DocAI OCR future lỗi cho {src_name}: {type(e).__name__}: {e}")
                pages_text = None
            if pages_text:
                results[src_name] = {"file_id": f"F{i}", "path": path,
                                     "src_name": src_name, "pages_text": pages_text}
    ok = len(results)
    log(f"DocAI OCR song song xong: {ok}/{len(pdf_files)} ok")
    # Giữ thứ tự gốc để file_id deterministic và batch-plan prompt nhất quán
    return [results[n] for _, n in pdf_files if n in results]


def _docai_batch_plan_pdfs(ocr_items: list[dict], applicant: str,
                           case_folder_id: str) -> dict[str, dict]:
    """DeepSeek batch-plan nhiều PDF đã DocAI OCR sẵn trong ít DeepSeek calls.

    ocr_items: list[dict] từ _docai_ocr_pdfs() (đã có file_id, path, src_name, pages_text).
    Returns {src_name: {documents: [...], _pages_text: [...]}}. Any missing src_name
    is intentionally handled by caller with the old per-file fallback.
    """
    if len(ocr_items) < 2:
        return {}
    ctx = _docai_catalog_context()
    if not ctx:
        return {}
    _tag_to_folder, catalog, relations_str = ctx

    out: dict[str, dict] = {}
    chunks = _make_docai_batch_chunks(ocr_items)
    log(f"DocAI batch-plan: {len(ocr_items)} PDF → {len(chunks)} DeepSeek call(s)")
    by_file_id = {it["file_id"]: it for it in ocr_items}
    for ci, chunk in enumerate(chunks, 1):
        try:
            prompt = _build_docai_batch_plan_prompt(chunk, applicant, case_folder_id, catalog, relations_str)
            plan = _docai_plan_api_call(prompt, timeout=240)
            files_plan = plan.get("files") if isinstance(plan, dict) else None
            if not isinstance(files_plan, list):
                log(f"  DocAI batch-plan chunk {ci}: response không có files[] — fallback từng PDF cho chunk này")
                continue
            ok = 0
            for fp in files_plan:
                if not isinstance(fp, dict):
                    continue
                fid = str(fp.get("file_id", "")).strip()
                item = by_file_id.get(fid)
                docs = fp.get("documents")
                if not item or not isinstance(docs, list):
                    continue
                out[item["src_name"]] = {"documents": docs, "_pages_text": item["pages_text"], "_batch_plan": True}
                ok += 1
            log(f"  DocAI batch-plan chunk {ci}/{len(chunks)}: {ok}/{len(chunk)} file planned")
        except Exception as e:  # noqa: BLE001
            log(f"  DocAI batch-plan chunk {ci} lỗi ({type(e).__name__}: {e}) — fallback từng PDF")
            continue
    return out


def _validate_plan(plan_docs: list[dict], total_pages: int, tag_to_folder: dict) -> list[dict]:
    """Validate + sanitize plan từ DeepSeek:
    - tag có trong YAML → remap folder
    - pages hợp lệ (1..total_pages)
    - filename không rỗng
    - relation thuộc whitelist
    Trả list docs đã clean (không nhất thiết đủ total_pages — caller log cảnh báo).
    """
    _valid_relations = {"bo", "me", "vo", "chong", "con", "anh_chi_em", "khac", "applicant", ""}
    valid: list[dict] = []
    for doc in plan_docs:
        if not isinstance(doc, dict):
            continue
        tag = str(doc.get("tag", "")).strip()
        if not tag:
            tag = "Khac"
        # Remap folder từ YAML (không tin folder DeepSeek trả)
        folder = tag_to_folder.get(tag, "Personal Docs")
        pages_raw = doc.get("pages", [])
        if isinstance(pages_raw, list):
            pages = [int(p) for p in pages_raw if str(p).isdigit() and 1 <= int(p) <= total_pages]
        elif isinstance(pages_raw, str) and "-" in pages_raw:
            a, b = pages_raw.split("-", 1)
            pages = list(range(int(a), int(b) + 1)) if a.isdigit() and b.isdigit() else []
        else:
            pages = []
        if not pages:
            continue
        subject = str(doc.get("subject", "")).strip()
        filename = str(doc.get("filename", "")).strip()
        if not filename:
            # Tự build filename fallback
            from lib.sop_naming import build_filename as _bf
            try:
                rel = str(doc.get("relation", "")).strip()
                filename = _bf(tag, subject or "Unknown", ".pdf",
                               relation="" if rel == "applicant" else rel)
            except Exception:  # noqa: BLE001
                filename = f"{tag}-{subject or 'Unknown'}.pdf"
        rel = str(doc.get("relation", "")).strip().lower()
        if rel not in _valid_relations:
            rel = ""
        valid.append({
            "pages": pages,
            "tag": tag,
            "folder": folder,
            "subject": subject,
            "relation": rel,
            "filename": filename,
            "confidence": str(doc.get("confidence", "medium")).strip().lower(),
            "needs_vision": bool(doc.get("needs_vision", False)),
            "reason": str(doc.get("reason", "")),
        })
    return valid


def _execute_plan(plan_docs: list[dict], path: Path, src_name: str,
                  case_folder_id: str, applicant: str, case_id: str,
                  retries: int, name_registry: dict, sop,
                  pages_text: list | None = None) -> list[dict]:
    """Thực thi plan: với mỗi doc trong plan → split PDF → OCR (nếu needs_vision) → upload → sidecar.
    pages_text: kết quả DocAI OCR full (list[{"page": N, "text": "..."}]) — dùng làm summary fallback.
    Trả list item (cùng format process_one) để gộp vào manifest."""
    classify_doc_type, build_filename, detect_english, title_case_ascii = sop
    items: list[dict] = []

    for doc in plan_docs:
        pages = doc["pages"]
        tu_trang = min(pages)
        den_trang = max(pages)
        tag = doc["tag"]
        folder = doc["folder"]
        subject = doc["subject"]
        relation = "" if doc["relation"] == "applicant" else doc["relation"]
        filename = doc["filename"]
        needs_vision = doc["needs_vision"]
        confidence = doc["confidence"]

        seg_src = f"{Path(src_name).stem}__split_{tu_trang}-{den_trang}{Path(src_name).suffix or '.pdf'}"

        # DocAI full OCR text cho segment này — dùng làm summary trong sidecar (không cần OCR lại)
        _docai_seg_text = ""
        if pages_text:
            _pg_map = {p["page"]: p.get("text", "") for p in pages_text}
            _docai_seg_text = "\n".join(_pg_map.get(pg, "") for pg in pages).strip()

        seg_path: Path | None = None
        try:
            seg_path = _split_pdf_pages(path, tu_trang, den_trang)

            gem: dict = {}

            if not isinstance(gem, dict):
                gem = {}
            gem.setdefault("extracted", {})

            # Dedup filename trong name_registry
            subject_title = title_case_ascii(subject) or "Unknown"
            is_eng = detect_english(gem.get("summary_vi", ""), "")
            from lib.sop_naming import build_filename as _build_fn
            new_name = dedup_name(name_registry, tag, subject_title,
                                  Path(src_name).suffix or ".pdf", is_eng, _build_fn, relation=relation)

            # Hash dedup
            content_hash = hashlib.sha1(seg_path.read_bytes()).hexdigest()
            existing_dup = _find_sidecar_by_hash(case_folder_id, content_hash)
            if existing_dup:
                _old_name = existing_dup.get("new_name", "")
                _old_tag  = existing_dup.get("tag", "Khac")
                if _old_tag != "Khac":
                    log(f"       seg p{tu_trang}-{den_trang} → duplicate-by-hash ({_old_name!r})")
                    it = {
                        "src_name": seg_src, "split_from": src_name,
                        "split_pages": f"{tu_trang}-{den_trang}",
                        "new_name": _old_name, "ext": Path(src_name).suffix or ".pdf",
                        "tag": _old_tag, "folder": existing_dup.get("folder", folder),
                        "subject": existing_dup.get("subject", subject_title),
                        "relation": existing_dup.get("relation", relation),
                        "confidence": existing_dup.get("confidence", confidence),
                        "needs_review": bool(existing_dup.get("needs_review")),
                        "is_english": bool(existing_dup.get("is_english")),
                        "ocr": True, "summary": existing_dup.get("summary", ""),
                        "extracted": existing_dup.get("extracted", {}),
                        "case_id": case_id,
                        "status": "duplicate-by-hash",
                        "drive_link": existing_dup.get("drive_link", ""),
                        "content_hash": content_hash,
                        "docai_plan": True,
                    }
                    items.append(it)
                    continue

            # Upload
            from lib.drive_helpers import get_or_create_folder, upload_file
            top_id = get_or_create_folder(folder, case_folder_id, drive_id=SHARED_DRIVE_ID)
            mime = "application/pdf"
            up = upload_file(str(seg_path), new_name, top_id, drive_id=SHARED_DRIVE_ID, mime=mime)
            drive_link = up["link"]
            status = "duplicate" if up.get("skipped") else "uploaded-split"

            summary = (str(gem.get("summary_vi", "")) if gem else "") or _docai_seg_text
            summary = summary[:2000]  # checklist đọc tối đa 800 char — cho nhiều hơn để đủ data
            needs_review = (confidence == "low")

            item: dict = {
                "src_name": seg_src, "split_from": src_name,
                "split_pages": f"{tu_trang}-{den_trang}",
                "new_name": new_name, "ext": Path(src_name).suffix or ".pdf",
                "tag": tag, "folder": folder,
                "subject": subject_title, "relation": relation,
                "confidence": confidence, "needs_review": needs_review,
                "is_english": is_eng, "ocr": True,
                "summary": summary, "extracted": gem.get("extracted", {}),
                "content_hash": content_hash, "case_id": case_id,
                "drive_link": drive_link, "status": status,
                "docai_plan": True, "docai_reason": doc.get("reason", ""),
                "pass1_doc_type": tag,
                "pass1_ten_chu_the": subject,
            }

            # Sidecars
            try:
                meta_id = get_or_create_folder(OCR_META_FOLDER, case_folder_id, drive_id=SHARED_DRIVE_ID)
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
                    json.dump(item, fh, ensure_ascii=False, indent=2)
                    jpath = fh.name
                upload_file(jpath, f"{new_name}.json", meta_id, drive_id=SHARED_DRIVE_ID, mime="application/json")
                os.unlink(jpath)
                review_tag = " ⚠️ Cần kiểm tra" if needs_review else ""
                md = (f"# {new_name}\n\n"
                      f"**Loại:** {tag} | **Folder:** {folder}\n"
                      f"**Người:** {subject_title} | **Confidence:** {confidence}{review_tag}\n"
                      f"**File gốc:** {src_name} (trang {tu_trang}-{den_trang})\n"
                      f"**Phân loại bởi:** Document AI + {OCR_CLASSIFY_MODEL}\n\n"
                      f"## Lý do\n{doc.get('reason', '(DocAI plan)')}\n")
                if summary:
                    md += f"\n## Tóm tắt\n{summary}\n"
                with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
                    fh.write(md)
                    mpath = fh.name
                upload_file(mpath, f"{new_name}.md", meta_id, drive_id=SHARED_DRIVE_ID, mime="text/markdown")
                os.unlink(mpath)
            except Exception as side_err:  # noqa: BLE001
                item["sidecar_error"] = str(side_err)

            log(f"       seg p{tu_trang}-{den_trang} -> {status}  {new_name}")
            items.append(item)

        except Exception as e:  # noqa: BLE001
            log(f"       seg p{tu_trang}-{den_trang} lỗi: {type(e).__name__}: {e}")
            items.append({
                "src_name": seg_src, "split_from": src_name,
                "split_pages": f"{tu_trang}-{den_trang}",
                "new_name": "", "ext": Path(src_name).suffix or ".pdf",
                "tag": tag, "folder": folder, "subject": subject, "relation": relation,
                "status": "failed", "error": f"{type(e).__name__}: {e}",
                "drive_link": "", "case_id": case_id, "docai_plan": True,
            })
        finally:
            if seg_path:
                try:
                    seg_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass

    return items


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


def _split_pdf_pages(path: Path, tu_trang: int, den_trang: int) -> Path:
    """Tách trang tu_trang..den_trang (1-based inclusive) của PDF → file temp mới."""
    import pypdf
    reader = pypdf.PdfReader(str(path))
    writer = pypdf.PdfWriter()
    a, b = max(1, int(tu_trang)), min(len(reader.pages), int(den_trang))
    for p in range(a - 1, b):
        writer.add_page(reader.pages[p])
    tmp = path.parent / f".{path.stem}.seg{a}-{b}.pdf"
    with tmp.open("wb") as fh:
        writer.write(fh)
    return tmp


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
        from lib.sop_naming import classify_doc_type, extract_relation
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
                        gem = docai_classify(_pages, filename, applicant) or {}
                finally:
                    tmp_path.unlink(missing_ok=True)
            if not isinstance(gem, dict):
                gem = {}
            gem.setdefault("extracted", {})
            raw_dt = gem.get("doc_type", "")
            summary = str(gem.get("summary_vi", ""))[:400]
            extracted = gem.get("extracted") if isinstance(gem.get("extracted"), dict) else {}
            cls = classify_doc_type(raw_dt, summary, filename, extracted=extracted)
            subject = subject_from_gemini(gem, applicant) or _strip_trailing_year(applicant)
            relation = extract_relation(applicant, subject, summary, doc_tag=cls.tag, extracted=extracted)
            item = {
                "src_name": filename, "new_name": filename, "ext": ext,
                "tag": cls.tag, "folder": DA_DUYET_FOLDER,
                "subject": subject, "relation": relation,
                "confidence": cls.confidence, "needs_review": cls.needs_review,
                "is_english": False, "ocr": ext in DOCAI_OCR_EXTS,
                "summary": summary, "extracted": extracted, "ocr_classify": gem,
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


# ============================================================================
# process one file (with retries)
# ============================================================================
def process_one(path: Path, src_name: str, *, case_folder_id: str, applicant: str,
                case_id: str, retries: int, dry_run: bool, sop, name_registry: dict,
                pages_text: list | None = None, force_rescan: bool = False) -> dict:
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
                _pages = pages_text if isinstance(pages_text, list) else None
                if _pages is None:
                    from lib.docai_client import ocr_with_docai
                    _pages = ocr_with_docai(path)
                gem = docai_classify(_pages, src_name, applicant) if _pages else {}
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
            # Nếu Gemini báo visual_flags chứa "viết tay" → needs_review để checklist không 🔴 false positives.
            _vflags = " ".join(str(f) for f in (gem.get("extracted") or {}).get("visual_flags", []))
            if _vflags and ("viết tay" in _vflags or "viet tay" in _vflags.lower()):
                needs_review = True
            # P2.5 — `applicant` từ group_registry có thể kèm năm sinh ("Nguyen Thi Anh 1999").
            # Tên file phải sạch — strip năm trước khi dùng làm subject fallback.
            subject_raw = subject_from_gemini(gem, applicant) or _strip_trailing_year(applicant)
            # P2.3 — MRZ override: với CCCD / Passport, nếu OCR thấy MRZ → tên parse từ MRZ
            # là GROUND TRUTH chủ thẻ. Diệt bug "stamp tên đương đơn lên CCCD người khác".
            if cls.tag in ("CCCD", "Passport") and isinstance(gem.get("extracted"), dict):
                _mrz = gem["extracted"].get("mrz")
                if isinstance(_mrz, dict) and _mrz.get("name"):
                    _mrz_name = str(_mrz.get("name") or "").strip()
                    if _mrz_name and len(_mrz_name) >= 4:
                        subject_raw = _mrz_name
                else:
                    # Fallback: Gemini có thể không fill `mrz` dict nhưng đưa cụm MRZ vào summary/raw text.
                    # Thử parse từ summary để bắt trường hợp này.
                    try:
                        from lib.mrz import parse_mrz
                        _mrz_fallback = parse_mrz(str(gem.get("summary_vi") or "") + "\n" +
                                                   "\n".join(str(v) for v in (gem.get("extracted") or {}).values()
                                                            if isinstance(v, str)))
                        if _mrz_fallback and _mrz_fallback.get("name"):
                            subject_raw = _mrz_fallback["name"]
                            gem.setdefault("extracted", {})["mrz"] = _mrz_fallback
                    except Exception:  # noqa: BLE001
                        pass
            subject_title = title_case_ascii(subject_raw) or "Unknown"
            is_eng = detect_english(summary, "")
            # Quan hệ với đương đơn — P2.4 siết: whitelist doc_tag + ground truth từ extracted.
            from lib.sop_naming import extract_relation
            relation = extract_relation(applicant, subject_title, summary,
                                        doc_tag=cls.tag, extracted=gem.get("extracted"))
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
                "ocr": can_ocr, "summary": summary, "extracted": gem.get("extracted") or {},
                "content_hash": content_hash,
                "ocr_classify": gem, "case_id": case_id,
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
    assert OCR_RESPONSE_SCHEMA["strict"] is True
    assert "doc_type" in OCR_RESPONSE_SCHEMA["schema"]["properties"]
    assert callable(docai_classify)
    # Fix 7 — _find_sidecar_by_hash callable + miss case folder → None, không raise
    assert callable(_find_sidecar_by_hash)
    assert _find_sidecar_by_hash("", "abc") is None
    assert _find_sidecar_by_hash("nonexistent-folder", "") is None
    # _split_pdf_pages: tạo PDF 4 trang in-memory rồi tách 2-3 → còn 2 trang
    try:
        import pypdf, io as _io
        w = pypdf.PdfWriter()
        for _i in range(4):
            w.add_blank_page(width=72, height=72)
        _tmp = Path(tempfile.gettempdir()) / "_st_pdf_4p.pdf"
        with _tmp.open("wb") as _fh:
            w.write(_fh)
        _seg = _split_pdf_pages(_tmp, 2, 3)
        assert _count_pdf_pages(_seg) == 2, _count_pdf_pages(_seg)
        _seg.unlink(missing_ok=True)
        _tmp.unlink(missing_ok=True)
        print("page-by-page helpers OK (pypdf available)")
    except ImportError:
        print("page-by-page helpers SKIP (pypdf chưa cài)")
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

        _EXISTING_DOCS_CACHE.clear()  # reset per-batch; planning phase đọc Drive 1 lần rồi cache

        # DocAI OCR tất cả file (PDF + ảnh + .doc/.docx) song song; gpt-5-mini classify và plan sau.
        ocr_cache = docai_prefetch(files, dry_run=args.dry_run, workers=OCR_WORKERS)

        # Batch-plan PDF nhiều trang: DocAI OCR items → gpt-5-mini plan
        _pdf_ocr_items = [
            {"file_id": f"F{i}", "path": p, "src_name": n, "pages_text": ocr_cache[n]}
            for i, (p, n) in enumerate(files, 1)
            if n in ocr_cache and ocr_cache[n] and p.suffix.lower() == ".pdf"
        ]
        docai_batch_plans: dict[str, dict] = {}
        if _pdf_ocr_items and len(_pdf_ocr_items) >= 2 and not args.dry_run:
            docai_batch_plans = _docai_batch_plan_pdfs(_pdf_ocr_items, applicant, case_folder_id or "")

        try:
            from lib.rule_loader import load_doc_types as _ldt_main
            _tag_map_main = {dt.tag: dt.folder for dt in _ldt_main()}
        except Exception:  # noqa: BLE001
            _tag_map_main = {}

        name_registry: dict = {}
        items = []
        for idx, (path, src_name) in enumerate(files, 1):
            log(f"[{idx}/{total}] {src_name}")

            # === DocAI flow: PDF → plan via gpt-5-mini ===
            if path.suffix.lower() == ".pdf":
                plan = docai_batch_plans.get(src_name)
                if plan:
                    log(f"     DocAI batch-plan hit: {len(plan.get('documents', []))} document(s)")
                else:
                    plan = _docai_plan_pdf(path, applicant, case_folder_id or "")
                if plan is not None:
                    plan_docs = _validate_plan(
                        plan.get("documents", []),
                        len(plan.get("_pages_text", [])) or _count_pdf_pages(path),
                        _tag_map_main,
                    )
                    _docai_pages_text = plan.get("_pages_text", [])
                    if plan_docs:
                        log(f"     DocAI plan: {len(plan_docs)} document → execute")
                        plan_items = _execute_plan(
                            plan_docs, path, src_name,
                            case_folder_id=case_folder_id or "", applicant=applicant,
                            case_id=case_id, retries=args.retries,
                            name_registry=name_registry, sop=sop,
                            pages_text=_docai_pages_text,
                        )
                        items.extend(plan_items)
                        continue
                    else:
                        log("     DocAI plan: 0 doc hợp lệ sau validate — single-doc fallback")
                # plan is None hoặc validate ra rỗng → fallback sang single-doc

            # Single-doc path (default)
            it = process_one(path, src_name, case_folder_id=case_folder_id or "", applicant=applicant,
                             case_id=case_id, retries=args.retries, dry_run=args.dry_run, sop=sop,
                             name_registry=name_registry, pages_text=ocr_cache.get(src_name),
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
                    ck = _ck.run_and_write(case_folder_id, applicant, SHARED_DRIVE_ID,
                                           batch_items=items, today=today_vn,
                                           vision_compare=vision_results or None)
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
