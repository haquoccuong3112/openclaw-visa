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

try:
    import PIL.Image  # noqa: F401 — kéo lên top-level để fail-fast khi thiếu Pillow.
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "google/gemini-2.5-flash")
# Model text-only (vd deepseek/deepseek-v4-flash) để re-classify sau Gemini OCR.
# Rỗng = tắt bước này (fallback classify_doc_type() regex như cũ).
CLASSIFY_MODEL = os.environ.get("CLASSIFY_MODEL", "")
# Document AI processor id (từ GOOGLE_DOCUMENTAI_PROCESSOR_ID trong scan-ocr.env).
# Khi có → PDF đi qua flow mới: DocAI OCR toàn bộ → DeepSeek plan → split/upload.
# Rỗng → giữ flow cũ (Gemini page-classify) làm fallback.
DOCAI_PROCESSOR_ID = os.environ.get("GOOGLE_DOCUMENTAI_PROCESSOR_ID", "")
# Model DeepSeek cho planning call (DocAI flow). Mặc định dùng pro (cần reason tốt hơn flash).
DOCAI_PLAN_MODEL = os.environ.get("DOCAI_PLAN_MODEL", "deepseek/deepseek-v4-pro")
# Batch-plan nhiều PDF sau khi DocAI OCR xong để tránh 1 DeepSeek call / PDF nhỏ.
DOCAI_BATCH_PLAN = os.environ.get("DOCAI_BATCH_PLAN", "1").lower() not in {"0", "false", "no"}
DOCAI_BATCH_PLAN_MAX_FILES = int(os.environ.get("DOCAI_BATCH_PLAN_MAX_FILES", "12"))
DOCAI_BATCH_PLAN_MAX_CHARS = int(os.environ.get("DOCAI_BATCH_PLAN_MAX_CHARS", "24000"))
# Schema strict cho response_format khi gọi Gemini OCR (OpenRouter json_schema). Khi model
# không nhận → fallback json_object → fallback off (xem gemini_classify_file).
GEMINI_RESPONSE_SCHEMA = {
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
                    # P3.1: relation của person này với đương đơn (chỉ điền khi văn bản ghi RÕ).
                    "relation": {"type": "string"},  # one of: applicant, cha, me, vo, chong, con, anh_chi_em, khac, ""
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
TOP_FOLDERS = ["Personal Docs", "Education", "Asset", "Employment"]
OCR_META_FOLDER = "_Bot OCR & Metadata"
DA_DUYET_FOLDER = "Đã duyệt"   # staff review folder — bot reads, never writes files here
# Số file được Gemini OCR ĐỒNG THỜI (mỗi file 1 HTTP call độc lập). Phân loại + upload Drive + thẩm định vẫn tuần tự.
OCR_WORKERS = max(1, int(os.environ.get("SCAN_OCR_WORKERS", "5")))

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


if not _HAS_PIL:
    log("⚠️ Pillow chưa cài → multi-page PDF split sẽ bị disable. "
        "Fix: pip3 install --user --break-system-packages Pillow")


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
def gemini_classify_file(path: Path, filename: str, model: str | None = None) -> dict:
    import httpx  # local import so --self-test works without it installed

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"doc_type": "", "person": [], "summary_vi": "(no OPENROUTER_API_KEY)", "key_fields": {}, "extracted": {}}
    mime = OCR_EXT_MIME.get(path.suffix.lower(), "application/pdf")
    content_b64 = base64.b64encode(path.read_bytes()).decode()
    # Inject doc type catalog từ data/doc_types.yaml (Phase 5 data-driven).
    # Helps LLM thấy description đầy đủ của mỗi loại, không chỉ tag name hardcoded.
    try:
        from lib.rule_loader import generate_doc_type_catalog
        _doc_catalog = generate_doc_type_catalog()
    except Exception:  # noqa: BLE001 — graceful: nếu YAML lỗi thì dùng prompt cũ không catalog
        _doc_catalog = ""
    prompt = f"""Đọc trực tiếp file hồ sơ visa Canada đính kèm và trả về JSON MỘT DÒNG, THUẦN (không markdown, không giải thích ngoài JSON).

# DANH MỤC LOẠI GIẤY TỜ BOT NHẬN DIỆN (tham khảo — `doc_type` nên match TÊN tiếng Việt 1 trong các loại bên dưới):
{_doc_catalog or "(catalog không load được)"}

Các trường:
- doc_type: loại giấy tờ tiếng Việt — PHÂN LOẠI THEO BẢN CHẤT GIẤY TỜ, KHÔNG theo các trường/thông tin mà nó nhắc tới.
  • "Căn cước công dân"/"Hộ chiếu"/"Sổ tiết kiệm"/"Lý lịch tư pháp"/"Sao kê ngân hàng"/… CHỈ khi file ĐÚNG LÀ giấy tờ đó
    (vd: CCCD = tấm thẻ in 2 mặt có ảnh chân dung + chip/QR; hộ chiếu = cuốn hộ chiếu; sổ tiết kiệm = cuốn sổ ngân hàng).
  • PHÂN BIỆT ẢNH (chỉ áp dụng khi CẢ FILE LÀ một tấm ảnh — không áp dụng cho ảnh chân dung in TRÊN giấy tờ khác):
    – cả file LÀ một tấm ảnh chân dung CHÍNH THỨC kiểu ảnh dán hồ sơ / hộ chiếu: 1 người, chỉ đầu + vai, phông nền
      ĐƠN SẮC (trắng/xanh), nhìn thẳng, KHÔNG có cảnh vật / hoạt động → doc_type = "Ảnh thẻ" VÀ "extracted.la_anh_the" = true;
    – 1 người đang LÀM VIỆC / làm nông / ở vườn-ruộng-nhà kính / chăm cây-trồng hoa (dù thấy rõ mặt) → doc_type = "Ảnh làm nông" (KHÔNG phải "Ảnh thẻ");
    – ảnh chụp NHIỀU người / gia đình / tiệc / sự kiện → doc_type = "Ảnh gia đình".
    ⚠️ Một tấm ảnh chân dung in TRÊN một giấy tờ khác (thẻ CCCD, cuốn hộ chiếu, bằng cấp, sơ yếu lý lịch…) thì phân
    loại theo giấy tờ đó ("Căn cước công dân" / "Hộ chiếu" / …) — KHÔNG phải "Ảnh thẻ", và la_anh_the = false.
  • Một tờ giấy / biểu mẫu do KHÁCH HÀNG TỰ KHAI / VIẾT TAY / TỰ ĐIỀN thông tin cá nhân (họ tên, ngày sinh, số CCCD,
    địa chỉ, người thân…) → doc_type = "Thông tin cá nhân (tự khai)" (≈ sơ yếu lý lịch), KHÔNG phải "Căn cước công dân"
    chỉ vì có ô "Số CCCD". Tương tự với các loại giấy khác — đừng vì file nhắc đến số/tên gì mà gán nhầm loại.
  • "Sao kê ngân hàng": CHỈ áp dụng khi file có ĐÚNG cấu trúc sao kê tài khoản: SỐ TÀI KHOẢN + KỲ SAO KÊ
    (từ ngày–đến ngày) + DANH SÁCH GIAO DỊCH nhiều dòng (cột nợ/có/số dư) + SỐ DƯ đầu/cuối kỳ. KHÔNG gắn
    "Sao kê ngân hàng" cho: ảnh thẻ visa scan, ảnh trang passport (page có thông tin cá nhân), biên lai đơn
    lẻ, thông báo SMS ngân hàng, hay bảng thông tin có vài hàng. Nếu file là 1 TẤM THẺ (visa card / passport /
    CCCD scan) hay 1 trang giấy thông tin → phân theo loại giấy đó (Hộ chiếu / Căn cước / Thẻ tín dụng / …),
    KHÔNG phải "Sao kê ngân hàng".
- person: [{{"full_name":"...","date_of_birth":"...","relation":"..."}}]
  • relation: quan hệ của người đó với "đương đơn chính" (chủ hồ sơ). 1 trong:
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
  tinh_trang_an_tich, la_to_khai (true nếu là tờ tự khai / biểu mẫu khách tự ghi; false nếu là giấy tờ chính thức do cơ quan cấp),
  la_anh_the (true CHỈ khi cả file LÀ một tấm ảnh chân dung riêng lẻ kiểu ảnh dán hồ sơ — KHÔNG phải ảnh sinh hoạt / làm việc / làm nông / chụp nhóm, và KHÔNG phải ảnh chân dung in trên CCCD / hộ chiếu / bằng cấp),
  co_dau_moc (true/false), co_chu_ky (true/false), visual_flags (["ảnh mờ","nghi tẩy xóa ...","thiếu chữ ký","thiếu dấu mộc",...]),

  // Chỉ điền khi doc_type = "Ảnh thẻ" (file CẢ là 1 tấm ảnh chân dung độc lập):
  la_mat_moc (true nếu mặt mộc — không son phấn, không kẻ mắt đậm; false nếu có trang điểm rõ),
  co_trang_suc (true nếu có trang sức như bông tai, dây chuyền, vòng tay, kính thời trang; false nếu không),
  co_xam_lo (true nếu thấy hình xăm lộ ra trong khung ảnh — cổ, vai, ngực, tay nếu thấy),
  toc_toi_mau (true nếu tóc màu tối tự nhiên: đen/nâu sậm; false nếu nhuộm sáng/highlight/màu lạ),
  phong_nen_trang (true nếu phông nền trắng/xanh đơn sắc, đủ sáng; false nếu phông phức tạp/lộn xộn/tối),

  // Chỉ điền khi doc_type = "Căn cước công dân" và FILE LÀ MẶT SAU CCCD:
  co_2_o_van_tay (true nếu mặt sau có ĐỦ 2 ô dấu vân tay rõ — ngón trỏ trái + ngón trỏ phải; false nếu thiếu 1/2 ô, mờ, hoặc trống),

  // Sub-typing — chỉ điền khi loại tương ứng:
  bang_cap_level (chỉ khi doc_type là bằng cấp; 1 trong: "cap_2"|"cap_3"|"trung_cap"|"cao_dang"|"dai_hoc"|"thac_si"|"tien_si"|"khac"),
  gplx_hang (chỉ khi GPLX; vd "A1"|"A2"|"B"|"B2"|"C"|"D"|"E"|"FC"),
  cccd_mat (chỉ khi doc_type là CCCD/CMND; 1 trong "truoc"|"sau"|"2-mat" — file có cả 2 mặt thì "2-mat"),
  co_dau_xa_phuong (true CHỈ khi đây là Sơ yếu lý lịch / đơn / xác nhận CÓ con dấu mộc tròn của UBND xã/phường — phân biệt với CV viết tay không dấu),

  // MRZ — chỉ điền khi doc_type là CCCD hoặc Hộ chiếu VÀ file có vùng MRZ (3 dòng `<<` dưới đáy CCCD hoặc 2 dòng ở trang dữ liệu hộ chiếu):
  mrz: {{ "raw":"<2-3 dòng MRZ nguyên văn, mỗi dòng 1 chuỗi>", "name":"<tên parse từ MRZ>", "dob":"<DD/MM/YYYY parse từ MRZ>", "doc_no":"<số giấy tờ parse từ MRZ>" }}
  ⚠️ MRZ name LÀ GROUND-TRUTH chủ giấy tờ — KHÔNG được dùng tên ngoài MRZ nếu MRZ rõ ràng đọc được.

QUY TẮC CHỐNG SUY DIỄN (BẮT BUỘC):
- "bs" = "bản sao" (viết tắt thường dùng trên giấy khai sinh bản sao). KHÔNG được dịch thành "ba" / "bố" / coi là họ tên người.
- KHÔNG được tự chèn "vợ" / "chồng" / "con" / "bố" / "mẹ" vào tên người hoặc relation nếu văn bản KHÔNG ghi rõ chữ đó kèm tên cụ thể.
- Một CCCD/giấy tờ thuộc về MỘT chủ thể duy nhất — nếu MRZ đọc được, chủ thể = MRZ name; nếu không, chủ thể = họ tên ngay dưới dòng "Họ tên" / "Full name", KHÔNG phải tên người được nhắc tới trong các trường phụ.

VÍ DỤ output (3 case tham khảo — JSON output thực tế phải khớp file thực):

VD1 (CCCD scan): {{"doc_type":"Căn cước công dân","person":[{{"full_name":"Nguyễn Văn A","date_of_birth":"01/01/1990"}}],"summary_vi":"Thẻ CCCD của Nguyễn Văn A, số 0123...","key_fields":{{"số CCCD":"0123...","ngày cấp":"15/03/2021"}},"extracted":{{"ho_ten":"Nguyễn Văn A","ngay_sinh":"01/01/1990","so_giay_to":"0123456789","loai_so":"CCCD 12 số","ngay_cap":"15/03/2021","la_to_khai":false,"la_anh_the":false}}}}

VD2 (Sổ đất / GCNQSDĐ): {{"doc_type":"Giấy chứng nhận quyền sử dụng đất","person":[],"summary_vi":"Sổ đất của Trần Văn B + vợ, thửa 123 tại huyện X","key_fields":{{"số GCN":"AB-12345"}},"extracted":{{"chu_su_dung":"Trần Văn B và Nguyễn Thị C","dia_chi_thua":"thửa 123 tờ bản đồ 4, xã Y, huyện X","dien_tich":"180 m2"}}}}

VD3 (Khai sinh con — relation): {{"doc_type":"Trích lục khai sinh","person":[{{"full_name":"Trần Văn D","date_of_birth":"05/05/2015"}}],"summary_vi":"Khai sinh của Trần Văn D (con), bố Trần Văn B, mẹ Nguyễn Thị C","key_fields":{{"số khai sinh":"123/2015"}},"extracted":{{"ho_ten":"Trần Văn D","ngay_sinh":"05/05/2015","ho_ten_cha":"Trần Văn B","ho_ten_me":"Nguyễn Thị C","la_to_khai":false}}}}

Tên file: {filename}

Nếu không đọc được file, vẫn trả JSON với summary_vi mô tả lý do và "extracted": {{}}."""
    payload = {
        "model": model or GEMINI_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "file", "file": {"filename": filename, "file_data": f"data:{mime};base64,{content_b64}"}},
        ]}],
        "temperature": 0.1,
        "response_format": {"type": "json_schema", "json_schema": GEMINI_RESPONSE_SCHEMA},
    }
    # 3-tier fallback: json_schema → json_object → no format (vài model không nhận strict
    # mode hoặc json_schema; rớt dần đến khi 2xx).
    with httpx.Client(timeout=120) as client:
        resp = client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        if resp.status_code >= 400:
            payload["response_format"] = {"type": "json_object"}
            resp = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
        if resp.status_code >= 400:
            payload.pop("response_format", None)
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


def _ocr_one_with_retry(path: Path, src_name: str, retries: int = 2) -> dict | None:
    """Gọi Gemini OCR 1 file, retry vài lần. Trả dict (kể cả dict fallback rỗng) hoặc None nếu vẫn raise."""
    last_err = None
    for i in range(1, retries + 1):
        try:
            return gemini_classify_file(path, src_name)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < retries:
                time.sleep(min(2 ** i, 15))
    log(f"  OCR prefetch lỗi cho {src_name}: {type(last_err).__name__}: {last_err} — sẽ thử lại tuần tự ở process_one")
    return None


def ocr_prefetch(files: list, *, dry_run: bool, workers: int) -> dict:
    """OCR ĐỒNG THỜI mọi file OCR-được trong batch → {src_name: gem(dict) | None}.
    File không OCR được (ext lạ) hoặc khi --dry-run: bỏ qua (không thêm vào dict). 1 file lỗi không phá batch."""
    todo = [(p, n) for (p, n) in files if (not dry_run) and p.suffix.lower() in OCR_EXT_MIME]
    if not todo:
        return {}
    n_workers = max(1, min(workers, len(todo)))
    log(f"OCR song song: {len(todo)} file, {n_workers} luồng …")
    out: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        fut_to_name = {ex.submit(_ocr_one_with_retry, p, n): n for (p, n) in todo}
        for fut in concurrent.futures.as_completed(fut_to_name):
            name = fut_to_name[fut]
            try:
                out[name] = fut.result()
            except Exception as e:  # noqa: BLE001 — không nên xảy ra (đã bọc bên trong), nhưng cho chắc
                log(f"  OCR prefetch future lỗi cho {name}: {type(e).__name__}: {e}")
                out[name] = None
    ok = sum(1 for v in out.values() if isinstance(v, dict))
    log(f"OCR song song xong: {ok}/{len(out)} ok" + ("" if ok == len(out) else f" ({len(out) - ok} sẽ thử lại tuần tự)"))
    return out


def _deepseek_batch_classify(ocr_cache: dict, applicant: str = "") -> dict:
    """1 batch call CLASSIFY_MODEL (text-only) để re-classify tất cả file sau Gemini OCR.

    Input : {src_name: gem_dict} (ocr_cache sau prefetch)
    Output: {src_name: {"tag": "...", "subject": "...", "folder": "..."}}
    Fail-safe: trả {} nếu lỗi → caller dùng classify_doc_type() regex như cũ.
    """
    if not CLASSIFY_MODEL or not ocr_cache:
        return {}
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if deepseek_key:
        api_endpoint = "https://api.deepseek.com/v1/chat/completions"
        api_key = deepseek_key
        classify_model = CLASSIFY_MODEL.split("/", 1)[1] if CLASSIFY_MODEL.startswith("deepseek/") else CLASSIFY_MODEL
    elif openrouter_key:
        api_endpoint = "https://openrouter.ai/api/v1/chat/completions"
        api_key = openrouter_key
        classify_model = CLASSIFY_MODEL
    else:
        return {}

    try:
        from lib.rule_loader import load_doc_types, generate_doc_type_catalog
        _doc_types = load_doc_types()
        tag_to_folder = {dt.tag: dt.folder for dt in _doc_types}
        catalog = generate_doc_type_catalog(_doc_types)
    except Exception as e:  # noqa: BLE001
        log(f"_deepseek_batch_classify: không load doc_types ({e}) — skip")
        return {}

    # Build danh sách file gọn cho prompt
    file_entries = []
    names_in_order = []
    for src_name, gem in ocr_cache.items():
        if not isinstance(gem, dict):
            continue
        person = gem.get("person", [])
        person_name = ""
        gemini_relation = ""
        if isinstance(person, list) and person and isinstance(person[0], dict):
            person_name = person[0].get("full_name", "")
            gemini_relation = person[0].get("relation", "")
        ext_brief = {k: v for k, v in (gem.get("extracted") or {}).items()
                     if k in ("ho_ten", "chu_tai_khoan", "ten_cong_ty", "la_to_khai", "la_anh_the",
                               "ho_ten_cha", "ho_ten_me", "ho_ten_vo_chong")}
        file_entries.append(
            f'- file: {json.dumps(src_name, ensure_ascii=False)}\n'
            f'  doc_type_raw: {json.dumps(gem.get("doc_type", ""), ensure_ascii=False)}\n'
            f'  summary: {json.dumps(str(gem.get("summary_vi", ""))[:300], ensure_ascii=False)}\n'
            f'  person: {json.dumps(person_name, ensure_ascii=False)}\n'
            f'  gemini_relation: {json.dumps(gemini_relation, ensure_ascii=False)}\n'
            f'  extracted: {json.dumps(ext_brief, ensure_ascii=False)}'
        )
        names_in_order.append(src_name)

    if not file_entries:
        return {}

    _applicant_line = f'Đương đơn chính (applicant): "{applicant}"\n\n' if applicant else ""
    prompt = (
        "Bạn là chuyên gia phân loại hồ sơ visa Canada.\n"
        "Dựa vào thông tin OCR của từng file, xác định TAG, họ tên chủ thẻ, và quan hệ với đương đơn chính.\n\n"
        f"{_applicant_line}"
        f"# CATALOG TAG (chỉ được chọn tag có trong danh sách này):\n{catalog or '(không load được)'}\n\n"
        "# CÁC FILE CẦN PHÂN LOẠI:\n" + "\n".join(file_entries) + "\n\n"
        "# OUTPUT: JSON object, key = tên file (giữ nguyên ký tự), value = object:\n"
        '  {"tag": "<tag SOP>", "subject": "<họ tên chủ thẻ ASCII không dấu, rỗng nếu không rõ>",\n'
        '   "relation": "<quan hệ với đương đơn: bo|me|vo|chong|con|anh_chi_em|khac|applicant|rỗng>"}\n'
        "Quy tắc relation:\n"
        '  - "applicant": file là của chính đương đơn (subject gần = tên đương đơn)\n'
        '  - "bo"/"me"/"vo"/"chong"/"con"/"anh_chi_em": chỉ khi summary/extracted ghi RÕ quan hệ\n'
        '  - "": không xác định được\n'
        "Chỉ trả JSON thuần không markdown, không giải thích."
    )

    import httpx
    try:
        payload: dict = {
            "model": classify_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                api_endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            if resp.status_code >= 400:
                payload.pop("response_format", None)
                resp = client.post(
                    api_endpoint,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        result = json.loads(text)
        if not isinstance(result, dict):
            return {}
        out: dict = {}
        for src_name, hint in result.items():
            if not isinstance(hint, dict):
                continue
            tag = str(hint.get("tag", "")).strip()
            if not tag or tag not in tag_to_folder:
                continue
            _valid_relations = {"bo","me","vo","chong","con","anh_chi_em","khac","applicant",""}
            raw_rel = str(hint.get("relation", "")).strip().lower()
            out[src_name] = {
                "tag": tag,
                "subject": str(hint.get("subject", "")).strip(),
                "folder": tag_to_folder[tag],
                "relation": raw_rel if raw_rel in _valid_relations else "",
            }
        log(f"DeepSeek batch classify ({CLASSIFY_MODEL}): {len(out)}/{len(names_in_order)} file OK")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"_deepseek_batch_classify lỗi ({type(e).__name__}: {e}) — fallback classify_doc_type()")
        return {}


# ============================================================================
# Document AI + DeepSeek unified planning flow (PDF)
# ============================================================================
def _fetch_existing_docs(case_folder_id: str) -> list[dict]:
    """Đọc tất cả .json sidecar trong _Bot OCR & Metadata của case.
    Trả list[{"filename":…, "tag":…, "relation":…}] để inject vào prompt DeepSeek
    giúp nó biết hồ sơ đã có gì (quan trọng khi khách gửi lắc nhắc nhiều lần)."""
    if not case_folder_id:
        return []
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
        return existing
    except Exception as e:  # noqa: BLE001
        log(f"  _fetch_existing_docs lỗi ({type(e).__name__}: {e}) — bỏ qua context cũ")
        return []


def _docai_ocr_pdf(path: Path) -> list[dict] | None:
    """OCR 1 PDF bằng Google Document AI, trả pages_text hoặc None để caller fallback."""
    try:
        from lib.docai_client import ocr_pdf_with_docai
        log(f"  DocAI OCR: {path.name} …")
        pages_text = ocr_pdf_with_docai(path)
        if not pages_text:
            log("  DocAI OCR: trả [] — fallback sang Gemini flow")
            return None
        log(f"  DocAI OCR xong: {len(pages_text)} trang")
        return pages_text
    except Exception as e:  # noqa: BLE001
        log(f"  DocAI OCR lỗi ({type(e).__name__}: {e}) — fallback sang Gemini flow")
        return None


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
    """Call DeepSeek/OpenRouter planner. Caller validates schema."""
    import httpx

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if deepseek_key:
        api_endpoint = "https://api.deepseek.com/v1/chat/completions"
        api_key = deepseek_key
        plan_model = os.environ.get("DOCAI_PLAN_DIRECT_MODEL", "deepseek-v4-pro")
    elif openrouter_key:
        api_endpoint = "https://openrouter.ai/api/v1/chat/completions"
        api_key = openrouter_key
        plan_model = DOCAI_PLAN_MODEL
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
    """DocAI OCR từng PDF, trả list dict {file_id, path, src_name, pages_text}.
    File OCR không ra kết quả (pages_text rỗng) → bỏ qua khỏi list trả về."""
    ocr_items: list[dict] = []
    for i, (path, src_name) in enumerate(pdf_files, 1):
        log(f"[DocAI batch OCR {i}/{len(pdf_files)}] {src_name}")
        pages_text = _docai_ocr_pdf(path)
        if pages_text:
            ocr_items.append({
                "file_id": f"F{i}",
                "path": path,
                "src_name": src_name,
                "pages_text": pages_text,
            })
    return ocr_items


def _docai_batch_plan_pdfs(ocr_items: list[dict], applicant: str,
                           case_folder_id: str) -> dict[str, dict]:
    """DeepSeek batch-plan nhiều PDF đã DocAI OCR sẵn trong ít DeepSeek calls.

    ocr_items: list[dict] từ _docai_ocr_pdfs() (đã có file_id, path, src_name, pages_text).
    Returns {src_name: {documents: [...], _pages_text: [...]}}. Any missing src_name
    is intentionally handled by caller with the old per-file fallback.
    """
    if not DOCAI_BATCH_PLAN or len(ocr_items) < 2:
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

            # Nếu needs_vision → Gemini Vision để xác nhận classify + extract
            gem: dict = {}
            if needs_vision:
                try:
                    log(f"       [DocAI plan] {seg_src}: needs_vision → Gemini Vision classify")
                    gem = gemini_classify_file(seg_path, filename)
                    if isinstance(gem, dict) and gem.get("doc_type"):
                        from lib.sop_naming import Classification as _Cls
                        from lib.rule_loader import load_doc_types as _ldt
                        _tag_map = {dt.tag: dt.folder for dt in _ldt()}
                        from lib.sop_naming import classify_doc_type as _cdt
                        _cls = _cdt(gem.get("doc_type", ""), str(gem.get("summary_vi", "")), filename,
                                    extracted=gem.get("extracted"))
                        if _cls.tag != "Khac":
                            tag = _cls.tag
                            folder = _cls.folder
                        subject = subject or (gem.get("person", [{}]) or [{}])[0].get("full_name", subject)
                except Exception as e:  # noqa: BLE001
                    log(f"       [DocAI plan] Gemini Vision lỗi ({type(e).__name__}: {e}) — giữ plan tag")
                    gem = {}

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
            needs_review = (confidence == "low") or needs_vision

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
                      f"**Phân loại bởi:** Document AI + {DOCAI_PLAN_MODEL}\n\n"
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


# ============================================================================
# Fix 5 — Page-by-page 2-pass OCR cho multi-page PDF (multi-doc).
# Pass 1: rasterize từng trang → flash classify (cheap, chỉ doc_type + ten_chu_the).
# Group: trang liên tiếp cùng (doc_type, ten_chu_the) → segment.
# Pass 2: split PDF + full OCR per segment (do caller chạy qua process_one).
# ============================================================================
PAGE_CLASSIFY_MODEL = os.environ.get("PAGE_CLASSIFY_MODEL", "google/gemini-2.5-flash")
# P1.1: escalate model — chạy lại các trang Khac/low-conf để diệt bug "11 trang Passport".
PAGE_CLASSIFY_PRO_MODEL = os.environ.get("PAGE_CLASSIFY_PRO_MODEL", "google/gemini-2.5-pro")
# Ngưỡng escalate: nếu ≥30% trang có confidence low → batch escalate qua pro.
PAGE_CLASSIFY_PRO_RATIO = float(os.environ.get("PAGE_CLASSIFY_PRO_RATIO", "0.20"))
# Fix D — Force Pro cho Pass 1 ngay từ đầu nếu PDF ≥N trang. Flash confident-wrong trên batch lớn
# (vd 53 trang multi-doc bị nhận là 1 XNCT). Pro chậm hơn + tốn $0.16/53-trang nhưng chính xác.
PAGE_CLASSIFY_FORCE_PRO_MIN_PAGES = int(os.environ.get("PAGE_CLASSIFY_FORCE_PRO_MIN_PAGES", "10"))
SCAN_DPI = int(os.environ.get("SCAN_DPI", "150"))
PAGE_CLASSIFY_SCHEMA = {
    "name": "page_classify",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "doc_type":    {"type": "string"},
            "ten_chu_the": {"type": "string"},
            "confidence":  {"type": "string"},  # P1.1: high|medium|low
        },
        "required": ["doc_type"],
        "additionalProperties": True,
    },
}


def _count_pdf_pages(path: Path) -> int:
    """Đếm số trang PDF. Trả 0 nếu lỗi (file corrupt / không phải PDF)."""
    try:
        import pypdf
        return len(pypdf.PdfReader(str(path)).pages)
    except Exception as e:  # noqa: BLE001
        log(f"  _count_pdf_pages({path.name}) lỗi: {type(e).__name__}: {e}")
        return 0


def _rasterize_page_to_jpg_b64(path: Path, page_idx: int, dpi: int | None = None) -> str:
    """Render trang `page_idx` (0-based) thành JPEG, trả base64. Cho Pass 1 cheap classify."""
    if not _HAS_PIL:
        raise RuntimeError(
            "Pillow chưa cài — bỏ qua multi-page split. "
            "Fix: pip3 install --user --break-system-packages Pillow"
        )
    import io
    import pypdfium2 as pdfium
    _dpi = dpi if dpi is not None else SCAN_DPI
    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[page_idx]
        bitmap = page.render(scale=_dpi / 72.0)
        img = bitmap.to_pil()
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    finally:
        pdf.close()


def _gemini_quick_classify_page(img_b64: str, page_no: int,
                                 model: str | None = None) -> dict:
    """Pass 1 — classify 1 trang ảnh: trả {doc_type, ten_chu_the, confidence}.
    P1.1 fix: KHÔNG được trả `doc_type=""`. Khi không chắc → trả `"Khac"` + confidence="low".
    Caller (`detect_pdf_segments`) sẽ escalate batch Khac/low qua PAGE_CLASSIFY_PRO_MODEL."""
    import httpx
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"doc_type": "Khac", "ten_chu_the": "", "confidence": "low"}
    prompt = (
        f"Đây là TRANG {page_no} của một file PDF có thể gộp nhiều loại giấy tờ visa Canada. "
        "Phân loại CHỈ trang này theo BẢN CHẤT (vd: 'Căn cước công dân' / 'Hộ chiếu' / "
        "'Trích lục khai sinh' / 'Giấy đăng ký kết hôn' / 'Quyết định ly hôn' / "
        "'Giấy chứng nhận quyền sử dụng đất' / 'Hợp đồng cho tặng đất' / 'Bằng cấp' / "
        "'Học bạ' / 'Chứng chỉ tin học' / 'Trích lục cải chính hộ tịch' / "
        "'Chứng nhận đăng ký xe' / 'Chứng nhận hiến máu' / 'Sao kê ngân hàng' / 'Sổ tiết kiệm' / "
        "'Xác nhận số dư' / 'Hợp đồng lao động' / 'Hợp đồng thuê mặt bằng' / "
        "'Đăng ký kinh doanh' / 'BHYT' / 'BHXH' / 'Lý lịch tư pháp' / "
        "'Xác nhận cư trú' / 'Xác nhận tình trạng hôn nhân' / 'Xác nhận đất nông nghiệp' / "
        "'Giấy xác nhận học sinh' / 'Sơ yếu lý lịch' / 'Khám sức khỏe' / "
        "'Ảnh thẻ' / 'Ảnh gia đình' / 'Khac'). "
        'Trả JSON 1 dòng: {"doc_type":"<loại>","ten_chu_the":"<họ tên người trên giấy đó nếu thấy>","confidence":"high|medium|low"}. '
        "Nếu trang là continuation của giấy ở trang trước (cùng loại, cùng người) → trả loại + tên đó + confidence=high. "
        "⚠️ KHÔNG được trả doc_type rỗng. Khi không chắc / trang trắng / mờ → trả doc_type='Khac' + confidence='low'. "
        "Lưu ý: tài liệu hành chính VN thường có dấu đỏ tròn/oval ghi tên cơ quan cấp — đọc kỹ dấu để xác định loại giấy khi chữ mờ."
    )
    payload = {
        "model": model or PAGE_CLASSIFY_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        ]}],
        "temperature": 0.1,
        "response_format": {"type": "json_schema", "json_schema": PAGE_CLASSIFY_SCHEMA},
    }
    with httpx.Client(timeout=60) as client:
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
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            dt = str(d.get("doc_type", "")).strip() or "Khac"   # P1.1: rỗng → Khac
            conf = str(d.get("confidence", "")).strip().lower()
            if conf not in ("high", "medium", "low"):
                # Suy ra confidence: nếu Khac → low; nếu rõ loại → medium (flash dùng medium default).
                conf = "low" if dt.lower() == "khac" else "medium"
            return {"doc_type": dt,
                    "ten_chu_the": str(d.get("ten_chu_the", "")).strip(),
                    "confidence": conf}
    except Exception:  # noqa: BLE001
        pass
    return {"doc_type": "Khac", "ten_chu_the": "", "confidence": "low"}


def _names_clearly_differ(a: str, b: str) -> bool:
    """RÕ RÀNG 2 người khác nhau (cùng doc_type nhưng KHÁC PERSON) → buộc split.

    Đừng nhầm: Gemini OCR có thể đọc khác chữ giữa 2 trang của CÙNG 1 người (vd
    đánh máy → "Hoàng Thị Mơ" vs "Hoang Thi Mo"). Chỉ coi là KHÁC NGƯỜI khi:
    - Cả 2 tên non-empty
    - HỌ (token đầu) ≠ — vd "Hoang" vs "Au" → khác họ → chắc chắn khác người
    HOẶC
    - Tên CUỐI (token cuối) khác RÕ — vd "Mơ" (Mo) vs "Huyền" (Huyen)
    """
    try:
        from lib.sop_naming import strip_diacritics
    except ImportError:
        from sop_naming import strip_diacritics  # type: ignore  # noqa
    a = strip_diacritics(a or "").lower().strip()
    b = strip_diacritics(b or "").lower().strip()
    if not a or not b or a == b:
        return False
    at = a.split()
    bt = b.split()
    if not at or not bt:
        return False
    # Khác họ (token đầu) → CHẮC CHẮN khác người
    if at[0] != bt[0]:
        return True
    # Cùng họ, khác tên cuối + tên cuối đủ dài (≥3 chars để tránh OCR typo 1-2 char) → khác người
    if len(at) >= 2 and len(bt) >= 2:
        if at[-1] != bt[-1] and min(len(at[-1]), len(bt[-1])) >= 3:
            return True
    return False


def _group_consecutive(pages_class: list[dict]) -> list[dict]:
    """Gom các trang liên tiếp cùng doc_type → segment. Cắt khi:
    - doc_type khác (loại giấy đổi)
    - HOẶC cùng doc_type nhưng `ten_chu_the` rõ ràng KHÁC NGƯỜI (vd 1 PDF gộp CCCD KH +
      CCCD bố mẹ — cùng tag CCCD nhưng khác họ → phải split để tránh sidecar trộn dữ liệu).

    P1.1 fix: page với doc_type="Khac" KHÔNG còn được xem là continuation. Mỗi run trang
    Khac liên tiếp trở thành 1 segment riêng — caller sẽ escalate qua pro model để xác định
    đúng loại. Cách này phá bug "11 trang Passport gộp tất cả CCCD/sổ đất/khai sinh"."""
    if not pages_class:
        return []
    def _dt(p: dict) -> str:
        return (p.get("doc_type") or "").strip().lower()
    segments: list[dict] = []
    cur_dt = _dt(pages_class[0]) or "khac"   # P1.1: safety
    cur_name = (pages_class[0].get("ten_chu_the") or "").strip()
    cur_start = 1
    for i, p in enumerate(pages_class[1:], start=2):
        k = _dt(p) or "khac"
        nm = (p.get("ten_chu_the") or "").strip()
        # P1.1: bất kỳ thay đổi doc_type nào (kể cả vào/ra "khac") đều split.
        diff_type = k != cur_dt
        diff_person = k == cur_dt and k not in ("khac", "") and _names_clearly_differ(cur_name, nm)
        if diff_type or diff_person:
            seed = pages_class[cur_start - 1]
            segments.append({"tu_trang": cur_start, "den_trang": i - 1,
                             "doc_type": seed.get("doc_type", "") or "Khac",
                             "ten_chu_the": seed.get("ten_chu_the", "")})
            cur_dt = k
            cur_name = nm
            cur_start = i
    seed = pages_class[cur_start - 1]
    segments.append({"tu_trang": cur_start, "den_trang": len(pages_class),
                     "doc_type": seed.get("doc_type", "") or "Khac",
                     "ten_chu_the": seed.get("ten_chu_the", "")})
    return segments


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


def detect_pdf_segments(path: Path) -> list[dict]:
    """Pass 1: classify từng trang của PDF nhiều trang → trả list segments.
    Nếu PDF ≤1 trang hoặc chỉ 1 segment (file đơn doc): trả [] (caller dùng single-doc flow).

    P1.1: nếu ≥30% trang ra "Khac"/low-conf, escalate những trang đó qua
    PAGE_CLASSIFY_PRO_MODEL (gemini-2.5-pro 1 batch) để re-classify. Diệt bug
    "11 trang gộp Passport" khi Flash bị choke ở giữa file đa loại.

    Fix D: PDF dài (≥PAGE_CLASSIFY_FORCE_PRO_MIN_PAGES) dùng Pro ngay từ Pass 1, không qua Flash.
    Lý do: Flash hay confident-wrong trên batch nhiều trang cùng phông (vd 53 trang multi-doc
    bị Flash classify là 1 XNCT → P1.1 escalate không trigger vì không có trang low-conf).
    """
    n_pages = _count_pdf_pages(path)
    if n_pages <= 1:
        return []
    # Fix D — chọn model cho Pass 1 dựa vào độ dài PDF.
    use_pro_for_pass1 = n_pages >= PAGE_CLASSIFY_FORCE_PRO_MIN_PAGES
    pass1_model = PAGE_CLASSIFY_PRO_MODEL if use_pro_for_pass1 else None
    if use_pro_for_pass1:
        log(f"  page-classify Pass 1 FORCE-PRO: {n_pages} trang ≥ {PAGE_CLASSIFY_FORCE_PRO_MIN_PAGES} → {PAGE_CLASSIFY_PRO_MODEL}")
    # Rasterize 1 lần, dùng lại nếu phải escalate.
    rendered: list[str | None] = []
    for i in range(n_pages):
        try:
            rendered.append(_rasterize_page_to_jpg_b64(path, i))
        except Exception as e:  # noqa: BLE001
            log(f"  rasterize lỗi page={i+1}: {type(e).__name__}: {e}")
            rendered.append(None)
            # Nếu Pillow thiếu hoặc lỗi hệ thống chung → khỏi spam N dòng, bail out.
            if isinstance(e, (RuntimeError, ModuleNotFoundError, ImportError)):
                log(f"  rasterize bỏ qua {n_pages - i - 1} trang còn lại → multi-page split disabled cho file này")
                rendered.extend([None] * (n_pages - i - 1))
                break
    pages_class: list[dict] = []
    for i in range(n_pages):
        img_b64 = rendered[i]
        if img_b64 is None:
            pages_class.append({"doc_type": "Khac", "ten_chu_the": "", "confidence": "low"})
            continue
        try:
            c = _gemini_quick_classify_page(img_b64, page_no=i + 1, model=pass1_model)
        except Exception as e:  # noqa: BLE001
            log(f"  page-classify lỗi page={i+1}: {type(e).__name__}: {e}")
            c = {"doc_type": "Khac", "ten_chu_the": "", "confidence": "low"}
        pages_class.append(c)

    # P1.1 escalation: re-classify trang Khac / low qua pro model.
    # Fix D — skip nếu Pass 1 đã dùng Pro (không escalate lên cùng model).
    if not use_pro_for_pass1:
        low_idx = [i for i, p in enumerate(pages_class)
                   if (p.get("doc_type") or "").strip().lower() in ("khac", "") or p.get("confidence") == "low"]
        if low_idx and len(low_idx) >= max(2, int(n_pages * PAGE_CLASSIFY_PRO_RATIO)):
            log(f"  page-classify escalate: {len(low_idx)}/{n_pages} trang low-conf → {PAGE_CLASSIFY_PRO_MODEL}")
            for i in low_idx:
                if rendered[i] is None:
                    continue
                try:
                    c = _gemini_quick_classify_page(rendered[i], page_no=i + 1, model=PAGE_CLASSIFY_PRO_MODEL)
                    # Chỉ overwrite nếu pro cho confidence ≥ medium (tránh thay Khac/low bằng Khac/low khác)
                    if c.get("confidence") in ("high", "medium") and (c.get("doc_type") or "").strip().lower() not in ("", "khac"):
                        pages_class[i] = c
                except Exception as e:  # noqa: BLE001
                    log(f"  page-classify pro lỗi page={i+1}: {type(e).__name__}: {e}")

    segments = _group_consecutive(pages_class)
    return segments if len(segments) >= 2 else []


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
        if ext not in OCR_EXT_MIME and ext not in OTHER_EXT_MIME:
            continue
        log(f"  da-duyet: OCR {filename}")
        try:
            data = download_file_bytes(fid, drive_id=SHARED_DRIVE_ID)
            content_hash = hashlib.sha1(data).hexdigest()
            gem: dict = {}
            if ext in OCR_EXT_MIME:
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    tmp.write(data)
                    tmp_path = Path(tmp.name)
                try:
                    gem = gemini_classify_file(tmp_path, filename) or {}
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
                "is_english": False, "ocr": ext in OCR_EXT_MIME,
                "summary": summary, "extracted": extracted, "gemini": gem,
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
                prefetched_gem: dict | None = None, force_rescan: bool = False,
                ds_hint: dict | None = None) -> dict:
    classify_doc_type, build_filename, detect_english, title_case_ascii = sop
    ext = path.suffix.lower()
    can_ocr = ext in OCR_EXT_MIME
    mime = OCR_EXT_MIME.get(ext) or OTHER_EXT_MIME.get(ext) or "application/octet-stream"

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
                # dùng kết quả OCR đã prefetch song song nếu có; nếu prefetch lỗi (None) thì gọi lại tuần tự (có retry).
                gem = prefetched_gem if isinstance(prefetched_gem, dict) else gemini_classify_file(path, src_name)
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
            # DeepSeek batch classify hint — bypass classify_doc_type() regex khi tag hợp lệ + không phải Khac.
            # ds_hint được tính 1 lần cho cả batch trước vòng lặp process_one (text-only, không cần vision).
            _ds_tag = (ds_hint or {}).get("tag", "")
            _ds_subject = (ds_hint or {}).get("subject", "")
            _ds_folder = (ds_hint or {}).get("folder", "")
            _ds_relation = (ds_hint or {}).get("relation", None)  # None = chưa có → dùng extract_relation()
            if _ds_tag and _ds_tag != "Khac" and _ds_folder:
                from lib.sop_naming import Classification as _Cls
                cls = _Cls(tag=_ds_tag, folder=_ds_folder, confidence="medium", needs_review=False)
                log(f"  {src_name}: DeepSeek tag={_ds_tag!r} (bypass classify_doc_type regex)")
            else:
                cls = classify_doc_type(raw_dt, summary, src_name, extracted=gem.get("extracted"))
                _ds_subject = ""  # không dùng subject từ ds nếu tag fallback
            # P2.2 — escalate mọi file rơi về `Khac` (bất kể confidence) lên gemini-2.5-pro 1 call.
            # Trước chỉ escalate "Khac+low". Mở rộng vì user feedback: nhiều file Medical/XNCT/HĐ
            # bị Pass 4 bắt nhầm thành Khac với confidence=low — escalate-all diệt CV/Khac mặc định.
            if can_ocr and not dry_run and attempt == 1 and cls.tag == "Khac":
                try:
                    gem2 = gemini_classify_file(path, src_name, model="google/gemini-2.5-pro")
                    if isinstance(gem2, dict):
                        gem2.setdefault("extracted", {})
                        if not isinstance(gem2["extracted"], dict):
                            gem2["extracted"] = {}
                        raw_dt2 = gem2.get("doc_type", "")
                        summary2 = str(gem2.get("summary_vi", ""))[:400]
                        cls2 = classify_doc_type(raw_dt2, summary2, src_name, extracted=gem2.get("extracted"))
                        if cls2.tag != "Khac" or cls2.confidence != "low":
                            log(f"  {src_name}: escalated low-conf → {cls2.tag} ({cls2.confidence})")
                            gem, raw_dt, summary, cls = gem2, raw_dt2, summary2, cls2
                            gem["_escalated"] = True
                except Exception as e:  # noqa: BLE001
                    log(f"  {src_name}: escalation failed ({type(e).__name__}: {e})")
            needs_review = cls.needs_review or (not can_ocr)
            # Nếu Gemini báo visual_flags chứa "viết tay" → needs_review để checklist không 🔴 false positives.
            _vflags = " ".join(str(f) for f in (gem.get("extracted") or {}).get("visual_flags", []))
            if _vflags and ("viết tay" in _vflags or "viet tay" in _vflags.lower()):
                needs_review = True
            # P2.5 — `applicant` từ group_registry có thể kèm năm sinh ("Nguyen Thi Anh 1999").
            # Tên file phải sạch — strip năm trước khi dùng làm subject fallback.
            # _ds_subject: họ tên từ DeepSeek (nếu có và tag đã dùng); ưu tiên trên Gemini person[]
            subject_raw = (_ds_subject if _ds_subject else None) or subject_from_gemini(gem, applicant) or _strip_trailing_year(applicant)
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
            if _ds_relation is not None and _ds_tag and _ds_tag != "Khac":
                # DeepSeek đã đọc full context → dùng relation của nó; "applicant" → "" (không stamp vào tên)
                relation = "" if _ds_relation == "applicant" else _ds_relation
            else:
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
    # OCR song song: prefetch bỏ qua file ext-lạ / dry-run / list rỗng (không chạm API), process_one nhận prefetched_gem
    import inspect as _inspect
    assert callable(ocr_prefetch) and "prefetched_gem" in _inspect.signature(process_one).parameters
    assert ocr_prefetch([], dry_run=False, workers=3) == {}
    assert ocr_prefetch([(Path("/nope/a.docx"), "a.docx")], dry_run=False, workers=2) == {}
    assert ocr_prefetch([(Path("/nope/a.jpg"), "a.jpg")], dry_run=True, workers=2) == {}
    print(f"OCR_WORKERS={OCR_WORKERS} | ocr_prefetch OK")
    # Fix 0 — model + schema sanity
    assert GEMINI_MODEL.endswith("flash") or "pro" in GEMINI_MODEL, GEMINI_MODEL
    assert GEMINI_RESPONSE_SCHEMA["strict"] is True
    assert "doc_type" in GEMINI_RESPONSE_SCHEMA["schema"]["properties"]
    # Fix 6 — gemini_classify_file accepts `model` kwarg (giúp escalation)
    import inspect as _inspect
    assert "model" in _inspect.signature(gemini_classify_file).parameters
    # Fix 7 — _find_sidecar_by_hash callable + miss case folder → None, không raise
    assert callable(_find_sidecar_by_hash)
    assert _find_sidecar_by_hash("", "abc") is None
    assert _find_sidecar_by_hash("nonexistent-folder", "") is None
    # P1.1 — _group_consecutive: trang doc_type "" / "Khac" KHÔNG còn coi là continuation.
    # Mỗi run pages "Khac" → segment riêng để caller (detect_pdf_segments) escalate qua pro model.
    # Diệt bug "11 trang Passport gộp cả CCCD/sổ đất khi Flash trả rỗng pages 3-11".
    g = _group_consecutive([
        {"doc_type": "CCCD", "ten_chu_the": "A"},
        {"doc_type": "CCCD", "ten_chu_the": "A"},
        {"doc_type": "Bằng cấp", "ten_chu_the": "A"},
        {"doc_type": "Bằng cấp", "ten_chu_the": "A"},
        {"doc_type": "", "ten_chu_the": ""},                # trang trắng → segment Khac riêng
        {"doc_type": "Trích lục khai sinh", "ten_chu_the": "B"},
    ])
    assert len(g) == 4, g    # P1.1 fix: 4 segments (CCCD/Bang/Khac/Trich luc)
    assert g[0]["tu_trang"] == 1 and g[0]["den_trang"] == 2 and g[0]["doc_type"] == "CCCD"
    assert g[1]["tu_trang"] == 3 and g[1]["den_trang"] == 4 and g[1]["doc_type"] == "Bằng cấp"
    assert g[2]["tu_trang"] == 5 and g[2]["den_trang"] == 5 and g[2]["doc_type"] == "Khac"
    assert g[3]["tu_trang"] == 6 and g[3]["den_trang"] == 6 and g[3]["doc_type"] == "Trích lục khai sinh"
    # 1 doc duy nhất → 1 segment
    g1 = _group_consecutive([{"doc_type": "CCCD"}] * 5)
    assert len(g1) == 1 and g1[0]["den_trang"] == 5
    # PDF rỗng → 0 segment
    assert _group_consecutive([]) == []

    # Bug fix #4: cùng doc_type nhưng KHÁC PERSON → buộc split
    # Tình huống: 1 PDF gộp CCCD KH + CCCD bố/mẹ (đều CCCD nhưng khác họ tên)
    g_multi_person = _group_consecutive([
        {"doc_type": "Căn cước công dân", "ten_chu_the": "Hoàng Thị Mơ"},
        {"doc_type": "Căn cước công dân", "ten_chu_the": "Hoàng Thị Mơ"},
        {"doc_type": "Căn cước công dân", "ten_chu_the": "Âu Thị Huyền"},  # khác họ → split
        {"doc_type": "Căn cước công dân", "ten_chu_the": "Âu Thị Huyền"},
    ])
    assert len(g_multi_person) == 2, g_multi_person
    assert g_multi_person[0]["ten_chu_the"] == "Hoàng Thị Mơ"
    assert g_multi_person[1]["ten_chu_the"] == "Âu Thị Huyền"

    # Đừng false-positive: OCR đọc khác chữ giữa 2 trang cùng NGƯỜI → KHÔNG split
    g_same_person_typo = _group_consecutive([
        {"doc_type": "Hộ chiếu", "ten_chu_the": "Hoàng Thị Mơ"},
        {"doc_type": "Hộ chiếu", "ten_chu_the": "Hoang Thi Mo"},   # ascii vs unicode same person
    ])
    assert len(g_same_person_typo) == 1, g_same_person_typo

    # Cùng họ + tên cuối chỉ khác 1-2 char (OCR typo) → KHÔNG split
    g_typo = _group_consecutive([
        {"doc_type": "CCCD", "ten_chu_the": "Nguyễn Văn A"},
        {"doc_type": "CCCD", "ten_chu_the": "Nguyễn Văn"},        # OCR cắt cuối, vẫn cùng người
    ])
    assert len(g_typo) == 1, g_typo

    # _names_clearly_differ direct test
    assert _names_clearly_differ("Hoàng Thị Mơ", "Âu Thị Huyền") is True
    assert _names_clearly_differ("Hoàng Văn Thành", "Phan Thị Bính") is True
    assert _names_clearly_differ("Hoàng Thị Mơ", "Hoang Thi Mo") is False     # ascii vs unicode
    assert _names_clearly_differ("Hoàng Thị Mơ", "") is False                 # empty → không khẳng định
    assert _names_clearly_differ("Hoàng Văn Thành", "Hoàng Văn Thanh") is False  # 1 char diff cùng họ-đệm
    print("_group_consecutive multi-person split OK")
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
        # rasterize 1 trang → bắt sớm vụ thiếu Pillow / pypdfium2 hỏng.
        if _HAS_PIL:
            _b64 = _rasterize_page_to_jpg_b64(_tmp, 0)
            assert isinstance(_b64, str) and len(_b64) > 100, f"rasterize b64 quá ngắn: {len(_b64)}"
            assert base64.b64decode(_b64)[:3] == b"\xff\xd8\xff", "rasterize không phải JPEG"
            print("rasterize page → JPEG OK (Pillow available)")
        else:
            print("rasterize SKIP (Pillow chưa cài — multi-page split sẽ bị disable)")
        _tmp.unlink(missing_ok=True)
        print("page-by-page helpers OK (pypdf + pypdfium2 available)")
    except ImportError:
        print("page-by-page helpers SKIP (pypdf/pypdfium2 chưa cài)")
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
    ap.add_argument("--no-docai", action="store_true",
                    help="Bỏ qua Document AI flow; dùng Gemini page-classify như cũ (debug).")
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

        use_docai = bool(DOCAI_PROCESSOR_ID) and not args.dry_run and not getattr(args, "no_docai", False)
        # OCR mọi file OCR-được ĐỒNG THỜI trước (Gemini call/file là độc lập); phần dưới (phân loại + upload + sidecar) vẫn tuần tự.
        # Khi DocAI bật: PDF đi qua DocAI flow riêng → bỏ qua khỏi Gemini prefetch để tránh double OCR.
        _ocr_files = [(p, n) for (p, n) in files
                      if not (use_docai and p.suffix.lower() == ".pdf")] if use_docai else files

        docai_batch_plans: dict[str, dict] = {}
        if use_docai:
            _pdf_files = [(p, n) for (p, n) in files if p.suffix.lower() == ".pdf"]
            # Chạy Gemini OCR ảnh/non-PDF và DocAI OCR PDF SONG SONG để tránh
            # DeepSeek batch classify chặn start của DocAI.
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=2) as _ex:
                _fut_gemini = _ex.submit(ocr_prefetch, _ocr_files, dry_run=args.dry_run, workers=OCR_WORKERS) if _ocr_files else None
                _fut_docai = _ex.submit(_docai_ocr_pdfs, _pdf_files) if _pdf_files else None
                ocr_cache = _fut_gemini.result() if _fut_gemini else {}
                _docai_ocr_items = _fut_docai.result() if _fut_docai else []
            # Sau cả 2 OCR xong → DeepSeek classify ảnh + plan PDF
            ds_cache = _deepseek_batch_classify(ocr_cache, applicant=applicant) if (CLASSIFY_MODEL and not args.dry_run and ocr_cache) else {}
            if _docai_ocr_items and len(_docai_ocr_items) >= 2:
                docai_batch_plans = _docai_batch_plan_pdfs(_docai_ocr_items, applicant, case_folder_id or "")
        else:
            ocr_cache = ocr_prefetch(_ocr_files, dry_run=args.dry_run, workers=OCR_WORKERS) if _ocr_files else {}
            ds_cache = _deepseek_batch_classify(ocr_cache, applicant=applicant) if (CLASSIFY_MODEL and not args.dry_run and ocr_cache) else {}

        name_registry: dict = {}
        items = []
        for idx, (path, src_name) in enumerate(files, 1):
            log(f"[{idx}/{total}] {src_name}")

            # === DocAI flow: PDF + DOCAI_PROCESSOR_ID có → 1 call DocAI OCR + DeepSeek plan ===
            if use_docai and path.suffix.lower() == ".pdf":
                plan = docai_batch_plans.get(src_name)
                if plan:
                    log(f"     DocAI batch-plan hit: {len(plan.get('documents', []))} document(s)")
                else:
                    plan = _docai_plan_pdf(path, applicant, case_folder_id or "")
                if plan is not None:
                    try:
                        from lib.rule_loader import load_doc_types as _ldt_main
                        _tag_map_main = {dt.tag: dt.folder for dt in _ldt_main()}
                    except Exception:  # noqa: BLE001
                        _tag_map_main = {}
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
                        log("     DocAI plan: 0 doc hợp lệ sau validate — fallback Gemini flow")
                # plan is None hoặc validate ra rỗng → fallback sang flow cũ bên dưới

            # === Gemini flow (cũ): page-classify Pass 1 → segments hoặc single-doc ===
            segments: list[dict] = []
            if (not args.dry_run) and path.suffix.lower() == ".pdf":
                try:
                    segments = detect_pdf_segments(path)
                except Exception as e:  # noqa: BLE001
                    log(f"     detect_pdf_segments lỗi: {type(e).__name__}: {e} — single-doc fallback")
            if segments:
                log(f"     multi-doc PDF: {len(segments)} segment → split + OCR per segment")
                stem = Path(src_name).stem
                ext_orig = Path(src_name).suffix or ".pdf"
                for seg in segments:
                    a, b = seg["tu_trang"], seg["den_trang"]
                    seg_src = f"{stem}__split_{a}-{b}{ext_orig}"
                    try:
                        seg_path = _split_pdf_pages(path, a, b)
                    except Exception as e:  # noqa: BLE001
                        log(f"     split p{a}-{b} lỗi: {type(e).__name__}: {e} — bỏ qua segment")
                        items.append({
                            "src_name": seg_src, "split_from": src_name, "split_pages": f"{a}-{b}",
                            "new_name": "", "ext": ext_orig, "tag": "", "folder": "",
                            "subject": "", "relation": None, "status": "failed",
                            "error": f"PDF split lỗi: {type(e).__name__}: {e}",
                            "drive_link": "", "case_id": case_id,
                        })
                        continue
                    try:
                        seg_it = process_one(seg_path, seg_src,
                                             case_folder_id=case_folder_id or "", applicant=applicant,
                                             case_id=case_id, retries=args.retries, dry_run=args.dry_run,
                                             sop=sop, name_registry=name_registry, prefetched_gem=None,
                                             force_rescan=args.force_rescan)
                    finally:
                        try:
                            seg_path.unlink(missing_ok=True)
                        except Exception:  # noqa: BLE001
                            pass
                    # đánh dấu thuộc segment + parent file
                    seg_it["split_from"] = src_name
                    seg_it["split_pages"] = f"{a}-{b}"
                    seg_it["pass1_doc_type"] = seg.get("doc_type", "")
                    seg_it["pass1_ten_chu_the"] = seg.get("ten_chu_the", "")
                    if seg_it.get("status") == "uploaded":
                        seg_it["status"] = "uploaded-split"
                    log(f"       seg p{a}-{b} -> {seg_it.get('status','?')}  {seg_it.get('new_name','')}")
                    items.append(seg_it)
                continue
            # Single-doc path (default)
            it = process_one(path, src_name, case_folder_id=case_folder_id or "", applicant=applicant,
                             case_id=case_id, retries=args.retries, dry_run=args.dry_run, sop=sop,
                             name_registry=name_registry, prefetched_gem=ocr_cache.get(src_name),
                             force_rescan=args.force_rescan, ds_hint=ds_cache.get(src_name))
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
