"""AI Checklist — thẩm định chéo hồ sơ visa Canada (LMIA) sau bước OCR.

Sau khi scan_zip.py OCR + upload từng file, module này gom TOÀN BỘ giấy tờ của một case
(đọc lại các sidecar .json trong `_Bot OCR & Metadata`), gọi 1 lần LLM với PROMPT THẨM ĐỊNH
của Cường làm system prompt, và:
  - tạo / ghi đè một **Google Doc** `Bao cao tham dinh - <KH>` ở gốc case folder — báo cáo
    văn bản 4 phần, viết như chuyên viên thẩm định viết tay (không phải bảng cứng nữa),
  - trả về dòng tóm tắt + phần "TÓM TẮT & KHUYẾN NGHỊ" để bot post vào group Telegram.

Pipeline 2 tầng để tối ưu chi phí:
  Tầng 1 (`extract_profile_data`) — model rẻ `google/gemini-2.5-flash` (env `CHECKLIST_EXTRACT_MODEL`):
    đọc summary+extracted của mọi file → 1 JSON hồ sơ cô đọng, GIỮ NGUYÊN VĂN số/tên/ngày.
  Tầng 2 (`evaluate_profile_logic`) — model reasoning `google/gemini-2.5-pro` (env `CHECKLIST_MODEL`):
    đọc JSON nhỏ đó → báo cáo Markdown 4 phần (đánh giá business-logic LMIA).
`run_and_write` là orchestrator (≈ process_lmia_dossier): build dataset → tầng 1 → tầng 2 → Google Doc.

Gọi API HTTP qua OpenRouter (OPENROUTER_API_KEY) — KHÔNG dùng account ChatGPT/Codex.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import traceback
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
SCAN_HO_SO_DIR = Path(os.environ.get("SCAN_HO_SO_DIR", str(_HERE.parent)))

# Tầng 2 — model reasoning (đánh giá business-logic, sinh báo cáo 4 phần)
CHECKLIST_MODEL = os.environ.get("CHECKLIST_MODEL", "google/gemini-2.5-pro")
CHECKLIST_FALLBACK_MODEL = os.environ.get("CHECKLIST_FALLBACK_MODEL", "google/gemini-2.5-flash")
# Tầng 1 — model rẻ để gộp/chuẩn hoá summary+extracted của các file thành 1 JSON hồ sơ cô đọng
CHECKLIST_EXTRACT_MODEL = os.environ.get("CHECKLIST_EXTRACT_MODEL", "google/gemini-2.5-flash")
OCR_META_FOLDER = "_Bot OCR & Metadata"
MERGE_CUTOFF = date(2025, 6, 12)

# CHECKLIST HỒ SƠ FARM (ALLY) — 26 mục. Mỗi dòng: (nhãn, tag(s), nhóm).
#   - tag: str (thoả nếu tag đó có trong case) | tuple (thoả nếu có ÍT NHẤT 1 tag trong tuple).
#     Tag đặc biệt "GKS_con" = thoả nếu có >=2 giấy khai sinh (đương đơn + ít nhất 1 con).
#   - nhóm:
#       "bat_buoc"  → tính vào mẫu số "X/18 mục bắt buộc"
#       "ket_hon"   → chỉ áp dụng nếu KH đã kết hôn (suy từ có GKH)
#       "co_con"    → chỉ áp dụng nếu KH có con (suy từ >=2 GKS hoặc có "XN hoc")
#       "tuy_chon"  → "nếu có / tăng hồ sơ" — hiện trong bảng, không cộng X/18
#       "lam_sau"   → bổ sung/làm sau (xác nhận số dư, khám IOM) — hiện "— làm sau"
# Tag tham chiếu phải khớp tag do lib.sop_naming sinh ra.
# REQUIRED_DOCS được load TỪ data/rules.yaml (Phase 1 refactor data-driven).
# Cấu trúc (giữ backward compat): list[(label, tags_str_or_tuple, severity)].
def _build_required_docs() -> list[tuple[str, object, str]]:
    # Robust import — work cả khi checklist.py chạy standalone (`python3 lib/checklist.py`)
    # lẫn khi import như package (`from lib.checklist import …`).
    try:
        from .rule_loader import load_checklist
    except ImportError:
        from rule_loader import load_checklist  # type: ignore  # noqa
    out: list[tuple[str, object, str]] = []
    for ci in load_checklist():
        num = ci.code.removeprefix("FARM-")
        label = f"{num}. {ci.name}"
        tags: object = ci.tags[0] if len(ci.tags) == 1 else tuple(ci.tags)
        out.append((label, tags, ci.severity))
    return out

REQUIRED_DOCS = _build_required_docs()

# Đợt gửi có ít nhất một file mang tag thuộc checklist → mới chạy thẩm định (tự debounce ở scan_zip.py).
# Tính tự động từ REQUIRED_DOCS để không lệch nhau.
CHECKLIST_DOC_TAGS = {
    t for (_, tags, _) in REQUIRED_DOCS
    for t in ((tags,) if isinstance(tags, str) else tags) if t != "GKS_con"
}

_GROUP_LABEL = {
    "bat_buoc": "bắt buộc", "ket_hon": "nếu đã kết hôn", "co_con": "nếu có con",
    "tuy_chon": "nếu có / tăng hồ sơ", "lam_sau": "bổ sung sau",
}

_REQUIRED_TOTAL = sum(1 for _, _, nhom in REQUIRED_DOCS if nhom == "bat_buoc")  # = 18


# ---------------------------------------------------------------------------
# danh sách 34 đơn vị hành chính cấp tỉnh (đọc từ data/provinces_34.json)
# ---------------------------------------------------------------------------
def _load_provinces() -> dict:
    p = SCAN_HO_SO_DIR / "data" / "provinces_34.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


PROVINCES = _load_provinces()


def _provinces_text() -> str:
    if not PROVINCES:
        return "(CHƯA CẤU HÌNH provinces_34.json — bỏ qua kiểm tra địa giới hành chính)"
    cities = PROVINCES.get("cities", [])
    provs = PROVINCES.get("provinces", [])
    eff = PROVINCES.get("effective_date", "2025-06-12")
    o2n = PROVINCES.get("old_to_new", {})
    lines = [f"Hiệu lực từ {eff}. {len(cities)} thành phố trực thuộc trung ương: " + ", ".join(cities) + ".",
             f"{len(provs)} tỉnh: " + ", ".join(provs) + "."]
    if o2n:
        lines.append("Một số tên cũ → tên mới: " + "; ".join(f"{k} → {v}" for k, v in o2n.items()) + ".")
    return "\n".join(lines)


# ===========================================================================
# dataset: gom toàn bộ sidecar .json của case
# ===========================================================================
def build_dataset(case_folder_id: str, drive_id: str | None = None) -> list[dict]:
    """Đọc mọi sidecar `*.json` trong `_Bot OCR & Metadata` của case → list dict mô tả từng giấy tờ."""
    from .drive_helpers import get_or_create_folder, list_folder, download_file_text
    meta_id = get_or_create_folder(OCR_META_FOLDER, case_folder_id, drive_id=drive_id)
    files = list_folder(meta_id, drive_id=drive_id)
    out: list[dict] = []
    for name, fid in files.items():
        if not name.lower().endswith(".json"):
            continue
        try:
            d = json.loads(download_file_text(fid, drive_id=drive_id))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        gem = d.get("gemini") if isinstance(d.get("gemini"), dict) else {}
        out.append({
            "ten": d.get("new_name") or name[:-5],
            "loai": d.get("tag", ""),
            "folder": d.get("folder", ""),
            "nguoi": d.get("subject", ""),
            "tom_tat": d.get("summary", ""),
            "du_lieu": d.get("extracted") if isinstance(d.get("extracted"), dict) else {},
            "key_fields": gem.get("key_fields") if isinstance(gem.get("key_fields"), dict) else {},
            "confidence": d.get("confidence", ""),
            "needs_review": bool(d.get("needs_review")),
            "drive_link": d.get("drive_link", ""),
            "ngay_xu_ly": d.get("generated_at") or d.get("case_id", ""),
        })
    out.sort(key=lambda r: (r["loai"], r["ten"]))
    return out


# ===========================================================================
# coverage: điểm danh hồ sơ theo CHECKLIST FARM (deterministic, không tốn token AI)
# ===========================================================================
def _tags_of(tags) -> tuple:
    return (tags,) if isinstance(tags, str) else tuple(tags)


def compute_coverage(dataset: list[dict]) -> dict:
    """Đối chiếu các giấy tờ đã OCR với CHECKLIST FARM. Trả {have, required, missing, items, ...}.
    `have/required` chỉ tính nhóm "bat_buoc" (mẫu số = 18). `items` có `status` cho từng mục
    để hiển thị (✅ đã có / ❌ THIẾU / — không áp dụng / — chưa có (tùy chọn) / — sẽ làm sau)."""
    tags_present = {d["loai"] for d in dataset if d.get("loai")}
    n_gks = sum(1 for d in dataset if d.get("loai") == "GKS")
    has_marriage = "GKH" in tags_present
    has_kids = (n_gks >= 2) or ("XN hoc" in tags_present)
    items, missing = [], []
    have = 0
    for label, tags, nhom in REQUIRED_DOCS:
        if tags == "GKS_con":
            present = n_gks >= 2
        else:
            present = any(t in tags_present for t in _tags_of(tags))
        applicable = True
        if nhom == "ket_hon":
            applicable = has_marriage
        elif nhom == "co_con":
            applicable = has_kids
        if not applicable:
            status = "— không áp dụng"
        elif present:
            status = "✅ đã có"
        elif nhom == "lam_sau":
            status = "— sẽ làm sau"
        elif nhom == "tuy_chon":
            status = "— chưa có (tùy chọn)"
        else:
            status = "❌ THIẾU"
        items.append({"loai": label, "tags": list(_tags_of(tags)), "nhom": nhom,
                      "applicable": applicable, "present": present, "status": status})
        if nhom == "bat_buoc":
            if present:
                have += 1
            else:
                missing.append(label)
    return {
        "have": have,
        "required": _REQUIRED_TOTAL,
        "missing": missing,
        "items": items,
        "has_marriage": has_marriage,
        "n_gks": n_gks,
        "tags_present": sorted(tags_present),
    }


def should_run_checklist(manifest: dict) -> bool:
    items = manifest.get("items") or []
    return any((it.get("tag") in CHECKLIST_DOC_TAGS) for it in items)


def _coverage_block_text(coverage: dict) -> str:
    return "\n".join(f"  - {it['loai']} ({_GROUP_LABEL.get(it['nhom'], it['nhom'])}): {it['status']}"
                     for it in coverage["items"])


def _coverage_md_table(coverage: dict) -> str:
    rows = ["| Mục (checklist FARM) | Nhóm | Trạng thái |", "|---|---|---|"]
    for it in coverage["items"]:
        rows.append(f"| {it['loai']} | {_GROUP_LABEL.get(it['nhom'], it['nhom'])} | {it['status']} |")
    return "\n".join(rows)


# ===========================================================================
# Prompt thẩm định (template — placeholder dạng {{...}} để tránh đụng dấu ngoặc trong markdown)
# ===========================================================================
CHECKLIST_PROMPT_TEMPLATE = """# VAI TRÒ
Bạn là chuyên viên thẩm định hồ sơ visa/di trú cấp cao, chuyên kiểm tra tính
chính xác và đồng nhất của hồ sơ xin Work Permit Canada (LMIA). Bạn có kinh
nghiệm phát hiện sai lệch nhỏ nhất giữa các giấy tờ Việt Nam.

# NGỮ CẢNH
Tôi sẽ cung cấp cho bạn nội dung đã OCR từ bộ hồ sơ của khách hàng. Nhiệm vụ
của bạn là rà soát TOÀN BỘ hồ sơ theo checklist bên dưới, đối chiếu chéo
giữa các giấy tờ, và xuất báo cáo theo đúng format yêu cầu.

# THÔNG TIN ĐẦU VÀO
- Ngày kiểm tra: {{TODAY}}
- Tên khách hàng: {{APPLICANT}}
- Nội dung OCR hồ sơ: cung cấp ở message tiếp theo dưới dạng JSON — mỗi phần tử là một giấy tờ với
  `ten` (tên file đã chuẩn hoá), `loai` (mã loại giấy tờ), `nguoi` (người trên giấy), `tom_tat`,
  `du_lieu` (các trường trích từ OCR), `key_fields`, `confidence` ("high"|"medium"|"low"),
  `needs_review` (true ⇒ scan mờ / viết tay / phân loại chưa chắc — KHÔNG dùng giấy đó làm chuẩn để bắt lỗi giấy khác).

# NGUYÊN TẮC LÀM VIỆC BẮT BUỘC

1. **KHÔNG SUY ĐOÁN**: Chỉ kết luận dựa trên dữ liệu OCR có thật. Nếu OCR mờ/
   thiếu/không rõ → ghi "KHÔNG ĐỌC ĐƯỢC - cần kiểm tra bản gốc".

2. **ĐỐI CHIẾU CHÉO TRIỆT ĐỂ**: Mọi thông tin trùng lặp giữa các giấy tờ
   (họ tên, ngày sinh, số CMND/CCCD, địa chỉ, tên cha mẹ...) phải được so
   khớp ký tự với ký tự. Một dấu cách thừa, một chữ lót thiếu = LỖI.
   — **TRỪ KHI** một bên là giấy `needs_review=true` / `confidence`="low" / tờ TỰ KHAI / viết tay (`loai`="CV", biểu
   mẫu khách tự ghi): khác biệt nhỏ kiểu đó nhiều khả năng chỉ là OCR đọc sai chữ viết tay → ghi 🟢/🟡 "OCR thấp tin
   cậy — cần đối chiếu bản gốc", **KHÔNG phải lỗi 🔴**. Một tờ TỰ KHAI / viết tay KHÔNG phải CCCD / giấy chính thức kể
   cả khi nó có ghi số CCCD — đừng dùng nó làm chuẩn để bắt lỗi giấy khác.

3. **TÍNH TOÁN NGÀY THÁNG**: Mọi thời hạn phải tính từ ngày kiểm tra ở trên.
   Hiển thị rõ phép tính (VD: "Cấp 22/01/2026, đến {{TODAY}} = 3 tháng 20
   ngày, còn hạn 2 tháng 10 ngày").

4. **CẢNH BÁO ĐỊA GIỚI HÀNH CHÍNH (cải cách 2025)**: Từ 12/06/2025 chỉ còn 34 đơn vị
   cấp tỉnh (28 tỉnh + 6 TP trực thuộc TW); từ 01/07/2025 các xã/phường cũng đã sáp nhập (≈10.000 → 3.321).
   - Hồ sơ có sẵn trường **`_dia_gioi`** = KẾT QUẢ TRA CỨU DETERMINISTIC từ bảng địa giới chính thức (cũ↔mới,
     tới cấp xã). **COI ĐÓ LÀ GROUND-TRUTH — KHÔNG tự dò lại, KHÔNG đoán.** Cách dùng:
     (a) hai địa chỉ TEXT khác nhau nhưng `_dia_gioi.doi_chieu` ghi `same` (hoặc cùng `don_vi_moi`) → **KHÔNG báo
         mâu thuẫn** trong PHẦN 3 (chỉ là tên trước/sau cải cách);
     (b) giấy cấp SAU mốc cải cách mà ghi đơn vị `la_ten_cu=true` (đã sáp nhập) → BÁO LỖI ở PHẦN 3;
     (c) giấy cấp TRƯỚC mốc đó → HỢP LỆ, ghi chú "đã sáp nhập thành …";
     (d) `do_tin`=`unknown`/`fuzzy` (bảng chưa phủ hết / chuỗi mờ) → tự đánh giá thêm như bình thường.
     (Nếu hồ sơ KHÔNG có `_dia_gioi` thì tự kiểm dựa trên danh sách dưới như trước.)
   Danh sách 34 đơn vị cấp tỉnh hiện hành:
   {{PROVINCES}}

# QUY TRÌNH KIỂM TRA (Thực hiện tuần tự, không bỏ bước)

Bộ hồ sơ FARM của ALLY gồm 26 mục, trong đó 18 mục BẮT BUỘC — bảng điểm danh chi tiết từng mục (đã có /
THIẾU / không áp dụng / tùy chọn / làm sau) ở phần "THAM KHẢO" bên dưới. Mục BẮT BUỘC nào (đang áp dụng)
chưa có → ghi vào PHẦN 3 dạng "THIẾU: [tên mục]".

## BƯỚC 1: Liệt kê inventory
Liệt kê tất cả giấy tờ phát hiện trong hồ sơ OCR, đánh số thứ tự.

## BƯỚC 2: Trích xuất dữ liệu chuẩn
Tạo một "Bảng dữ liệu gốc" tổng hợp các trường thông tin then chốt từ TẤT CẢ
giấy tờ, dạng:

| Trường | Hộ chiếu | CCCD | Khai sinh | LLTP | CT07 | Kết hôn | KS con |
|--------|----------|------|-----------|------|------|---------|--------|
| Họ tên | ... | ... | ... | ... | ... | ... | ... |
| Ngày sinh | ... | ... | ... | ... | ... | ... | ... |
| Số CMND/CCCD | ... | ... | ... | ... | ... | ... | ... |
| Địa chỉ TT | ... | ... | ... | ... | ... | ... | ... |
| Tên cha | ... | ... | ... | ... | ... | ... | ... |
| Tên mẹ | ... | ... | ... | ... | ... | ... | ... |

## BƯỚC 3: Kiểm tra từng giấy tờ theo checklist chi tiết

### A. GIẤY TỜ CÁ NHÂN

**A1. Hộ chiếu:**
- [ ] Đã scan đủ MỌI trang có thông tin/dấu/visa của cả hộ chiếu cũ (nếu có) lẫn hộ chiếu mới?
- [ ] Họ tên không dấu khớp 100% với CCCD/Khai sinh
- [ ] Ngày sinh, giới tính khớp tuyệt đối
- [ ] Nơi sinh (tỉnh) khớp với Khai sinh
- [ ] Số CMND/CCCD ghi trong HC: số 9 cũ hay 12 mới — chỉ GHI NHẬN (lịch sử chuyển đổi giấy tờ), KHÔNG phải lỗi
- [ ] Còn hạn ≥ 2 năm tính từ ngày dự kiến sử dụng?
- [ ] Hình ảnh/chữ ký không tẩy xóa

**A2. CCCD:**
- [ ] Số định danh đủ 12 số
- [ ] Toàn bộ thông tin cá nhân khớp HC + Khai sinh
- [ ] Ghi nhận Quê quán + Nơi thường trú để đối chiếu CT07/LLTP
- [ ] Còn hạn (lưu ý mốc đổi thẻ: 25, 40, 60 tuổi)

**A3. Khai sinh khách hàng:**
- [ ] Họ tên, ngày sinh, cha mẹ ruột khớp tất cả giấy tờ khác

**A4. Lý lịch tư pháp (LLTP):**
- [ ] Còn hạn 6 tháng tính từ ngày cấp đến NGÀY KIỂM TRA
- [ ] Số CMND/CCCD trên LLTP: nếu là số 9 cũ (LLTP cấp đã lâu) thì bình thường — KHÔNG yêu cầu giải trình
- [ ] Tên cha, mẹ, vợ/chồng khớp Khai sinh + Đăng ký kết hôn
- [ ] Tình trạng: "Không có án tích"

**A5. Xác nhận cư trú CT07:**
- [ ] Còn hiệu lực ("Giấy này có giá trị đến hết ngày...")
- [ ] Địa chỉ thường trú + nơi ở hiện tại khớp CCCD/LLTP (áp dụng quy tắc địa giới ở mục 4 — tên cũ↔mới của CÙNG MỘT
  nơi KHÔNG phải mâu thuẫn)
- [ ] Đương đơn là NGƯỜI YÊU CẦU / người khai (ghi ở phần đầu giấy). Bảng "Các thành viên khác trong hộ gia đình" chỉ
  liệt kê những NGƯỜI KHÁC trong hộ — **KHÔNG có tên đương đơn trong bảng đó là BÌNH THƯỜNG**, KHÔNG phải lỗi, KHÔNG
  làm giấy "vô giá trị". Chỉ kiểm: mã định danh 12 số + ngày sinh từng người TRONG BẢNG có khớp CCCD/KS của họ không.
- [ ] CHỦ HỘ có thể là bố/mẹ ruột, bố/mẹ chồng (vợ), anh/chị/em, hoặc chính vợ/chồng đương đơn — **đừng mặc định chủ
  hộ là vợ/chồng của đương đơn**, và đừng báo "mâu thuẫn tên chồng/vợ" chỉ vì tên chủ hộ khác tên vợ/chồng ghi trên
  giấy khác. Chỉ báo mâu thuẫn vợ/chồng khi MỘT trường ghi RÕ "vợ/chồng" (trên LLTP, ĐKKH, CCCD…) xung đột với một
  trường ghi RÕ "vợ/chồng" khác.

**A6. Đăng ký kết hôn (nếu có):**
- [ ] Họ tên, ngày sinh vợ/chồng khớp 100% giấy tờ tùy thân
- [ ] Số CMND trên giấy này (thường là số cũ 9 số) → ghi nhận lịch sử
- [ ] Đủ chữ ký 2 vợ chồng + dấu cơ quan cấp

**A7. Khai sinh con cái:**
- [ ] Họ tên, ngày sinh con khớp bảng CT07
- [ ] Họ tên + năm sinh cha mẹ trên KS con = trên ĐKKH + CCCD khách hàng
- [ ] Người đi khai sinh: nếu là ông/bà → tên có khớp KS gốc của khách hàng?

### B. GIẤY TỜ TÀI CHÍNH & BẢO HIỂM

**B1. Sổ tiết kiệm:**
- [ ] Chủ sổ khớp CCCD/HC
- [ ] Số tiền gốc ≥ ~300–400 triệu? Kỳ hạn ≥ 6 tháng? Số dư nên KHÔNG tròn (vd 301/315/390tr) — báo nếu không đạt
- [ ] Đã đáo hạn chưa? Đang phong tỏa/duy trì?
- [ ] Đủ mộc đỏ + chữ ký giao dịch viên

**B2. Xác nhận số dư:**
- [ ] Ngày in xác nhận ≤ 30 ngày tính từ ngày kiểm tra
- [ ] Số TK + số dư khớp sổ tiết kiệm/sao kê
- [ ] Bản gốc có mộc đỏ hay chỉ in điện tử?

**B3. Biên lai BHXH (tự nguyện):**
- [ ] Tên + Số sổ/Mã BHXH khớp khách hàng
- [ ] Mức đóng ~200k/tháng? Có đủ 3 tháng gần nhất? (yêu cầu duy trì đóng 3 tháng/lần) — báo nếu thiếu/ngắt quãng
- [ ] Có dấu đỏ + chữ ký người thu/nộp tiền? Biên lai hợp lệ?

**B4. Biên lai BHYT:**
- [ ] Tên + Mã thẻ
- [ ] Còn hiệu lực tính đến ngày kiểm tra

**B5. Sao kê ngân hàng:**
- [ ] Chủ TK + số TK khớp
- [ ] Đủ 3-6 tháng gần nhất, không thiếu tháng
- [ ] Đóng dấu giáp lai + mộc đỏ trang cuối
- [ ] Số dư cuối kỳ khớp xác nhận số dư

## BƯỚC 4: Rà soát trường hợp đặc biệt (BẮT BUỘC)

1. **Số định danh cũ (9 số) và CCCD mới (12 số)**: hồ sơ có thể có cả hai (giấy cũ ghi số 9, giấy mới ghi số 12) — đây
   là chuyện BÌNH THƯỜNG của quá trình chuyển đổi giấy tờ, **KHÔNG báo lỗi, KHÔNG yêu cầu khách bổ sung giấy xác nhận**.
   (Chỉ lưu ý nếu trên CÙNG một giấy hiện hành lại có hai số khác nhau không ăn khớp — hiếm.)

2. **Sai lệch địa chỉ giữa giấy cũ và mới**: dùng `_dia_gioi` (mục 4) — tên TRƯỚC vs SAU cải cách của CÙNG MỘT nơi
   KHÔNG phải lỗi; chỉ báo nếu giấy cấp SAU mốc cải cách còn ghi đơn vị đã sáp nhập.

3. **Chữ lót/tên gọi**: sai khác giữa các GIẤY CHÍNH THỨC (CCCD, hộ chiếu, khai sinh, LLTP, CT07, ĐKKH) → BÁO LỖI;
   nhưng nếu một bên là giấy `needs_review`/tự khai/viết tay → chỉ 🟢/🟡 "cần đối chiếu bản gốc" (xem mục 2 NGUYÊN TẮC).

4. **Tên công ty trên BHXH ↔ Sao kê lương**: PHẢI trùng khớp tuyệt đối.

5. **Địa giới hành chính sau 12/06/2025 (tỉnh) / 01/07/2025 (xã/phường)**: đối chiếu `_dia_gioi` / danh sách 34 đơn vị mới.

6. **VISION COMPARE (Mức 3)**: phần `_vision_compare` (nếu có) là kết quả gemini-2.5-pro SO SÁNH ảnh chân
   dung trên ảnh thẻ với ảnh trên hộ chiếu / GPLX / CCCD — COI LÀ GROUND-TRUTH, KHÔNG tự dò lại.
   - `same_person`=false (confidence=high) → **🔴 LỖI NGHIÊM TRỌNG**: 2 ảnh KHÁC NGƯỜI → ghi rule code `[8.3+]`
     vào PHẦN 3, đề xuất kiểm tra hồ sơ khẩn cấp.
   - `phau_thuat_signs` non-empty → **🔴 [1.2]**: nghi phẫu thuật thẩm mỹ — báo người phụ trách kèm danh sách
     bộ phận nghi vấn (lấy từ field).
   - `same_person`=true AND `age_diff_months` > 6 → **🟡 [8.3]**: ảnh thẻ trùng ảnh trên giấy khác cách quá
     6 tháng → yêu cầu ảnh thẻ mới hơn.
   - `anomalies` non-empty → ghi vào PHẦN 3 với mức độ phù hợp.
   - `confidence`=low → đề xuất "cần đối chiếu thủ công bản gốc", KHÔNG báo lỗi cứng.

# THAM KHẢO — ĐIỂM DANH HỒ SƠ THEO CHECKLIST FARM (ALLY)
(đếm tự động từ dữ liệu OCR, coi là CHUẨN — KHÔNG được mâu thuẫn; trạng thái mỗi mục: "✅ đã có" /
"❌ THIẾU" / "— không áp dụng" / "— chưa có (tùy chọn)" / "— sẽ làm sau"):
{{COVERAGE_BLOCK}}
→ Khách đã nộp {{HAVE}}/{{REQUIRED}} mục BẮT BUỘC trong CHECKLIST HỒ SƠ FARM. {{MISSING_NOTE}}
Mục đánh dấu "— không áp dụng" / "— tùy chọn" / "— sẽ làm sau" thì ĐỪNG liệt kê là "thiếu" trong PHẦN 3.

# THAM KHẢO — DANH SÁCH RULE KIỂM TRA GIẤY TỜ (theo HƯỚNG DẪN CHECK HỒ SƠ v1.1)
Mỗi rule có MÃ CODE (vd `13.3`, `19.4`). Khi báo lỗi ở PHẦN 3, ghi rõ code: "Lỗi #N [13.3]: …".
🔴 = reject (giấy KHÔNG dùng được, BÁO ngay) · 🟡 = warn (cần làm rõ) · 🟢 = info (ghi nhận / nhắc)
Áp dụng đúng `áp dụng:` của mỗi rule — KHÔNG áp dụng rule lên giấy không thuộc tag đó.

{{RULES_BLOCK}}

{{DETERMINISTIC_ERRORS}}

# FORMAT ĐẦU RA BẮT BUỘC

Xuất kết quả theo đúng 4 phần dưới đây, không thêm bớt, viết bằng tiếng Việt như một chuyên viên
thẩm định viết tay — câu văn tự nhiên, đủ ý, dùng được ngay cho nhân viên gửi khách:

---

## 📋 BÁO CÁO THẨM ĐỊNH HỒ SƠ
**Khách hàng:** {{APPLICANT}}
**Ngày kiểm tra:** {{TODAY}}
**Tổng số giấy tờ rà soát:** [số]

---

## ✅ PHẦN 1: GIẤY TỜ CHUẨN XÁC
Liệt kê các giấy tờ đã pass toàn bộ check, ghi rõ tên giấy tờ + 1 dòng tóm tắt.

| STT | Tên giấy tờ | Ghi chú |
|-----|-------------|---------|
| 1 | ... | ... |

---

## ⏰ PHẦN 2: GIẤY TỜ SẮP / ĐÃ HẾT HẠN
Liệt kê kèm tính toán cụ thể.

| STT | Tên giấy tờ | Ngày cấp/hết hạn | Tình trạng | Hành động |
|-----|-------------|------------------|------------|-----------|
| 1 | ... | ... | Còn X ngày / Đã hết Y ngày | Cấp lại / Gia hạn |

---

## ⚠️ PHẦN 3: ĐIỂM MÂU THUẪN CẦN LÀM RÕ
Mỗi điểm trình bày theo cấu trúc:

**Lỗi #[N]: [Tiêu đề ngắn]**
- **Mức độ:** 🔴 Nghiêm trọng / 🟡 Cần làm rõ / 🟢 Ghi chú
- **Vị trí:** Giấy tờ A vs Giấy tờ B
- **Chi tiết:** Trên [A] ghi "X", trên [B] ghi "Y"
- **Nguyên nhân khả dĩ:** ...
- **Hành động đề xuất:** Yêu cầu khách bổ sung / xin cấp lại / xin xác nhận...

(Nếu OCR thiếu một loại giấy tờ bắt buộc → ghi vào đây dạng **"Lỗi: THIẾU GIẤY TỜ [tên loại]"** chứ không dừng lại.)

---

## 📌 PHẦN 4: TÓM TẮT & KHUYẾN NGHỊ
- **Tình trạng tổng thể:** ✅ Sẵn sàng nộp / ⚠️ Cần bổ sung / 🔴 Cần xử lý gấp
- **Số lỗi nghiêm trọng:** [N]
- **Số điểm cần làm rõ:** [N]
- **Hành động ưu tiên (theo thứ tự):**
  1. ...
  2. ...
  3. ...

---

BẮT ĐẦU RÀ SOÁT NGAY KHI NHẬN ĐƯỢC HỒ SƠ OCR. KHÔNG HỎI THÊM TRƯỚC KHI CÓ KẾT QUẢ BƯỚC 1-4.
NẾU OCR THIẾU MỘT LOẠI GIẤY TỜ → GHI VÀO PHẦN 3 DẠNG "THIẾU GIẤY TỜ" CHỨ KHÔNG DỪNG LẠI."""


def _build_prompt(today: str, applicant: str, coverage: dict,
                  deterministic_errors: list[dict] | None = None) -> str:
    missing_note = ("Còn thiếu (bắt buộc): " + ", ".join(coverage["missing"]) + "."
                    if coverage["missing"] else f"Đã đủ {coverage['required']} mục bắt buộc.")
    # Inject rule references từ rules.yaml (data-driven Phase 2 + 3).
    try:
        from .rule_loader import generate_rules_block
    except ImportError:
        from rule_loader import generate_rules_block  # type: ignore  # noqa
    rules_block = generate_rules_block()
    # Tin do bot pre-check deterministic — LLM phải tin tưởng và đưa vào báo cáo PHẦN 3.
    det_block = ""
    if deterministic_errors:
        lines = [
            "## ⚠️ LỖI BOT ĐÃ PHÁT HIỆN (deterministic check, COI LÀ ĐÚNG — phải đưa vào PHẦN 3 báo cáo):",
        ]
        for e in deterministic_errors:
            lines.append(f"- 🔴 [{e['code']}] file `{e.get('ten','?')}` (tag {e.get('tag','?')}): "
                         f"{e['msg']} → {e.get('action','')}")
        det_block = "\n".join(lines)
    repl = {
        "{{TODAY}}": today,
        "{{APPLICANT}}": applicant or "(không rõ tên)",
        "{{PROVINCES}}": _provinces_text(),
        "{{COVERAGE_BLOCK}}": _coverage_block_text(coverage),
        "{{HAVE}}": str(coverage["have"]),
        "{{REQUIRED}}": str(coverage["required"]),
        "{{MISSING_NOTE}}": missing_note,
        "{{RULES_BLOCK}}": rules_block,
        "{{DETERMINISTIC_ERRORS}}": det_block,
    }
    s = CHECKLIST_PROMPT_TEMPLATE
    for k, v in repl.items():
        s = s.replace(k, v)
    return s


# ===========================================================================
# Gọi LLM (OpenRouter) — trả về văn bản markdown (không ép JSON)
# ===========================================================================
def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _trim_dataset_for_llm(dataset: list[dict]) -> list[dict]:
    out = []
    for d in dataset:
        out.append({
            "ten": d.get("ten", ""),
            "loai": d.get("loai", ""),
            "nguoi": d.get("nguoi", ""),
            "tom_tat": (d.get("tom_tat") or "")[:800],
            "du_lieu": d.get("du_lieu") or {},
            "key_fields": d.get("key_fields") or {},
            "confidence": d.get("confidence", ""),       # "high"|"medium"|"low" — độ tin cậy OCR/phân loại
            "needs_review": bool(d.get("needs_review")),  # True = scan mờ / viết tay / phân loại chưa chắc
        })
    return out


async def _call_openrouter_stream(model: str, system: str, user: str, on_chunk,
                                   timeout: int = 300) -> str:
    """Gọi OpenRouter với stream=True. Yield từng delta qua callback `on_chunk(text_delta)`
    (async function). Trả về full text cuối cùng. Caller dùng on_chunk để edit Telegram tin.

    SSE format (OpenAI-compatible):
        data: {"choices":[{"delta":{"content":"..."}}]}
        data: [DONE]

    Lỗi → raise. Caller có thể fallback _call_openrouter (non-stream)."""
    import httpx
    import json as _json
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY chưa được cấu hình")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "stream": True,
    }
    buf: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"}, json=payload,
        ) as resp:
            if resp.status_code >= 400:
                txt = (await resp.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenRouter stream HTTP {resp.status_code}: {txt[:200]}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                chunk = line[6:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    d = _json.loads(chunk)
                    choices = d.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if delta:
                        buf.append(delta)
                        try:
                            await on_chunk(delta)
                        except Exception:  # noqa: BLE001 — on_chunk lỗi không phá stream
                            pass
                except _json.JSONDecodeError:
                    continue
    return _strip_fences("".join(buf))


def _call_openrouter(model: str, system: str, user: str, timeout: int = 300,
                     json_mode: bool = False) -> str:
    """Gọi OpenRouter chat/completions. Trả về content (đã strip fences).
    json_mode=True → thêm response_format json_object; nếu HTTP ≥400 thì retry KHÔNG kèm
    response_format (vài model không nhận). Caller tự json.loads nếu cần."""
    import httpx
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY chưa được cấu hình")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post("https://openrouter.ai/api/v1/chat/completions",
                           headers={"Authorization": f"Bearer {api_key}"}, json=payload)
        if json_mode and resp.status_code >= 400:
            payload.pop("response_format", None)
            resp = client.post("https://openrouter.ai/api/v1/chat/completions",
                               headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _strip_fences(content or "")


# ===========================================================================
# TẦNG 1 — trích xuất & chuẩn hoá hồ sơ (model rẻ) → 1 JSON cô đọng
# ===========================================================================
_PROFILE_EXTRACT_SYSTEM = """Bạn là trợ lý trích xuất & chuẩn hoá hồ sơ visa Canada (LMIA).
Đầu vào (message kế tiếp): JSON liệt kê các giấy tờ đã OCR — mỗi phần tử có `ten`, `loai`,
`nguoi`, `tom_tat`, `du_lieu`, `key_fields`, `confidence` ("high"|"medium"|"low") và `needs_review` (true = scan mờ /
viết tay / phân loại chưa chắc).

NHIỆM VỤ: gom toàn bộ dữ liệu thành MỘT JSON object hồ sơ thống nhất, theo schema dưới.

QUY TẮC TUYỆT ĐỐI:
- GIỮ NGUYÊN VĂN mọi giá trị (họ tên, ngày, số CMND/CCCD, địa chỉ, tên cha/mẹ/vợ/chồng, tên công ty,
  số tiền…) — copy chính xác từng ký tự, KHÔNG tóm tắt, KHÔNG diễn giải, KHÔNG tự "sửa" cho đẹp.
- Nếu một thông tin xuất hiện khác nhau ở các giấy → giữ CẢ HAI dạng và ghi vào `notes` (vd "tên chồng:
  LLTP='Nguyễn Bá Thắng' vs CT07='Nguyễn Bá Thẳng' (dấu hỏi)").
- Giấy nào `needs_review=true` / `confidence`="low" / là tờ TỰ KHAI (`loai`="CV", hoặc tiêu đề kiểu "Thông tin cá
  nhân / gia đình (tự khai)") → vẫn copy giá trị nhưng GHI RÕ trong `notes`: "(OCR thấp tin cậy / tự khai — cần đối
  chiếu bản gốc)". TUYỆT ĐỐI không coi giấy đó là nguồn chuẩn cho họ tên / số giấy tờ / địa chỉ khi nó lệch với một
  giấy CHÍNH THỨC do cơ quan cấp (CCCD thật, hộ chiếu, khai sinh, LLTP, CT07…). Một tờ tự khai/viết tay KHÔNG phải
  CCCD/giấy chính thức kể cả khi nó có ghi số CCCD.
- Nếu OCR mờ/thiếu → để chuỗi rỗng "" hoặc mảng rỗng [], và ghi lý do vào `notes`.
- Không bịa. Chỉ điền cái thật sự đọc được.
- Trả về JSON object MỘT DÒNG, THUẦN (không markdown, không chữ ngoài JSON).

SCHEMA (khoá nào không có để "" hoặc []):
{
 "personal_info": {"fullname":"","dob":"","gender":"","nationality":"","place_of_birth":"",
   "permanent_address":"","current_address":"","id_number":"","id_type":"","old_cmnd":"",
   "father_name":"","father_birth_year":"","mother_name":"","mother_birth_year":"",
   "spouse_name":"","spouse_old_id":""},
 "documents_found": ["tên loại giấy tờ phát hiện được"],
 "passport": {"number":"","expiry_date":"","issue_place":"","id_number_on_doc":""},
 "criminal_record": {"issue_date":"","status":"","id_number_used":"","father_name":"","mother_name":"","spouse_name":""},
 "residence_ct07": {"valid_until":"","permanent_address":"","current_address":"",
   "household_members":[{"name":"","dob":"","id":"","relation":""}]},
 "marriage": {"has_marriage":"có/không/không rõ","husband_name":"","wife_name":"","husband_dob":"","wife_dob":"",
   "ids_on_cert":"","signatures_ok":"","seal_ok":""},
 "children": [{"name":"","dob":"","parents_on_cert":"","registered_by":""}],
 "financial": {"savings_owner":"","savings_amount":"","savings_term":"","savings_maturity":"",
   "balance_confirm_date":"","balance_amount":"","statement_period":"","seal_ok":""},
 "insurance": {"bhxh_id":"","bhxh_period":"","bhxh_company":"","bhyt_id":"","bhyt_valid_from":"","bhyt_valid_to":""},
 "documents": [{"ten":"","loai":"","nguoi":"","key_facts":{},"needs_review":false}],
 "visual_flags": ["ảnh mờ / nghi tẩy xoá / thiếu chữ ký / thiếu dấu mộc ..."],
 "notes": ["mọi điều nghi vấn, biến thể, sai lệch đáng để bước thẩm định để ý"]
}"""


def extract_profile_data(dataset: list[dict], applicant: str, today: str,
                         model: str | None = None) -> dict:
    """TẦNG 1: gộp dataset (summary+extracted của các file) → 1 JSON hồ sơ cô đọng.
    Trả về dict hồ sơ; nếu lỗi → {"_error": "..."} (không raise)."""
    model = model or CHECKLIST_EXTRACT_MODEL
    user = (f"KHÁCH HÀNG: {applicant or '(không rõ tên)'}\nNgày: {today}\n"
            f"Số giấy tờ: {len(dataset)}\n\nDỮ LIỆU GIẤY TỜ ĐÃ OCR (JSON):\n"
            f"{json.dumps(_trim_dataset_for_llm(dataset), ensure_ascii=False)}")
    try:
        raw = _call_openrouter(model, _PROFILE_EXTRACT_SYSTEM, user, json_mode=True)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("kết quả không phải JSON object")
        data.setdefault("documents", [])
        return data
    except Exception as e:  # noqa: BLE001
        print(f"extract_profile_data: model={model} failed: {type(e).__name__}: {e}", flush=True)
        return {"_error": f"{type(e).__name__}: {e}"}


# ===========================================================================
# TẦNG 2 — đánh giá business-logic LMIA → báo cáo Markdown 4 phần (model reasoning)
# ===========================================================================
def evaluate_profile_logic(profile, applicant: str, today: str, coverage: dict,
                           model: str | None = None, n_docs: int | None = None,
                           dataset: list[dict] | None = None) -> dict:
    """TẦNG 2: đọc hồ sơ đã chuẩn hoá (dict) — hoặc dataset thô (list, fallback) — + prompt thẩm định
    → báo cáo Markdown 4 phần. Trả {report_text, model_used, n_docs} hoặc {report_text:None, error}.

    `dataset` (Phase 3 data-driven): dataset thô từ build_dataset() — cho phép rule_engine chạy
    deterministic pre-check (vd thế chấp sổ đỏ, LLTP hết hạn) NGOÀI LLM rồi đưa kết quả vào prompt.
    """
    model = model or CHECKLIST_MODEL
    if n_docs is None:
        n_docs = len(profile) if isinstance(profile, list) else len((profile or {}).get("documents") or [])
    # Pre-check deterministic — chạy 11 rule có condition trong rules.yaml, đưa kết quả cho LLM tin tưởng.
    det_errors: list[dict] = []
    try:
        try:
            from .rule_loader import load_validations
            from .rule_engine import detect_deterministic_errors
        except ImportError:
            from rule_loader import load_validations          # type: ignore  # noqa
            from rule_engine import detect_deterministic_errors  # type: ignore  # noqa
        # Ưu tiên dataset thô (có du_lieu nguyên trạng); fallback dùng profile.documents
        eval_dataset = dataset if isinstance(dataset, list) else (
            profile if isinstance(profile, list) else (profile or {}).get("documents") or []
        )
        if eval_dataset:
            det_errors = detect_deterministic_errors(list(load_validations()), eval_dataset)
            if det_errors:
                print(f"evaluate_profile_logic: deterministic check phát hiện {len(det_errors)} lỗi "
                      f"({', '.join(e['code'] for e in det_errors)})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"evaluate_profile_logic: rule_engine lỗi: {type(e).__name__}: {e} — bỏ qua deterministic",
              flush=True)
    system = _build_prompt(today, applicant, coverage, deterministic_errors=det_errors)
    if isinstance(profile, list):
        label = "NỘI DUNG OCR HỒ SƠ (JSON — mỗi phần tử một giấy tờ)"
    else:
        label = ("HỒ SƠ ĐÃ TRÍCH XUẤT & CHUẨN HOÁ (JSON — dùng đúng các giá trị verbatim trong đây; "
                 "đặc biệt chú ý các trường `notes` và `visual_flags`)")
    user = (f"KHÁCH HÀNG: {applicant or '(không rõ tên)'}\nNgày kiểm tra: {today}\n"
            f"Số giấy tờ trong hồ sơ: {n_docs}\n\n{label}:\n"
            f"{json.dumps(profile, ensure_ascii=False)}")
    candidates = [model] + ([CHECKLIST_FALLBACK_MODEL] if model != CHECKLIST_FALLBACK_MODEL else [])
    last_err = None
    for attempt, mdl in enumerate(candidates, 1):
        try:
            text = _call_openrouter(mdl, system, user)
            if not text.strip():
                raise RuntimeError("LLM trả về rỗng")
            return {"report_text": text.strip(), "model_used": mdl, "n_docs": n_docs, "error": None}
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"evaluate_profile_logic: attempt {attempt} model={mdl} failed: {type(e).__name__}: {e}", flush=True)
    return {"report_text": None, "model_used": None, "n_docs": n_docs,
            "error": f"{type(last_err).__name__}: {last_err}"}


# ===========================================================================
# Báo cáo: ghép văn bản LLM + phụ lục → markdown đầy đủ → Google Doc
# ===========================================================================
def render_doc_md(report_text: str, applicant: str, today: str, model: str,
                  coverage: dict, dataset: list[dict]) -> str:
    parts = [(report_text or "").strip(), ""]
    parts.append("---\n")
    parts.append(f"## ⓿ Điểm danh hồ sơ theo CHECKLIST FARM (đếm tự động) — đã có {coverage['have']}/{coverage['required']} mục bắt buộc")
    if coverage["missing"]:
        parts.append(f"**Còn thiếu (bắt buộc):** {', '.join(coverage['missing'])}\n")
    else:
        parts.append(f"**Đã đủ {coverage['required']} mục bắt buộc.** (xem các mục điều kiện / tùy chọn / làm sau ở bảng dưới)\n")
    parts.append(_coverage_md_table(coverage))
    parts.append("")
    parts.append("## 📎 Phụ lục — danh sách file đã OCR trong hồ sơ")
    parts.append("| # | Tên file | Loại | Người | Tóm tắt |")
    parts.append("|---|---|---|---|---|")
    for i, d in enumerate(dataset, 1):
        tt = (d.get("tom_tat") or "").replace("\n", " ").replace("|", "/")[:220]
        parts.append(f"| {i} | {d.get('ten','')} | {d.get('loai','')} | {d.get('nguoi','')} | {tt} |")
    parts.append("")
    parts.append(f"_Báo cáo do bot tạo tự động · model {model} · {today} · {len(dataset)} giấy tờ. "
                 f"Đây là bản rà soát máy — nhân viên cần đối chiếu lại bản gốc trước khi nộp._")
    return "\n".join(parts) + "\n"


def _write_google_doc(case_folder_id: str, name: str, md_text: str, drive_id: str | None) -> str:
    """Tạo (hoặc ghi đè) Google Doc tên `name` ở case folder từ nội dung markdown.
    Google Drive tự convert text/markdown → Google Doc khi mimeType đích là Google Doc.
    Trả về webViewLink (hoặc URL dựng từ id)."""
    import tempfile
    from googleapiclient.http import MediaFileUpload
    from .google_clients import drive
    from .drive_helpers import find_file_by_name
    DOC_MIME = "application/vnd.google-apps.document"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(md_text)
        mpath = fh.name
    try:
        existing = find_file_by_name(name, case_folder_id, drive_id, mime_type=DOC_MIME)
        media = MediaFileUpload(mpath, mimetype="text/markdown", resumable=False)
        if existing:
            # Fix B: update content in place → giữ Doc ID + webViewLink → link cũ
            # trong tin Telegram vẫn click được. Drive auto-lưu version history (File →
            # Version history) — anh xem revision trước qua UI Google Docs.
            update_kwargs = dict(fileId=existing, media_body=media, fields="id, webViewLink")
            if drive_id:
                update_kwargs["supportsAllDrives"] = True
            f = drive().files().update(**update_kwargs).execute()
        else:
            body = {"name": name, "mimeType": DOC_MIME, "parents": [case_folder_id]}
            create_kwargs = dict(body=body, media_body=media, fields="id, webViewLink")
            if drive_id:
                create_kwargs["supportsAllDrives"] = True
            f = drive().files().create(**create_kwargs).execute()
        return f.get("webViewLink") or f"https://docs.google.com/document/d/{f['id']}/edit"
    finally:
        try:
            os.unlink(mpath)
        except OSError:
            pass


# ===========================================================================
# Địa giới hành chính: tra cứu deterministic (lib.diadia) → gắn vào hồ sơ cho tầng 2
# ===========================================================================
_ADDR_KEYS = ("noi_thuong_tru", "noi_o_hien_tai", "que_quan", "noi_sinh", "dia_chi", "dia_chi_thuong_tru")


def _diadia():
    """Import lib.diadia robustly (works both as a package and when checklist.py is run standalone)."""
    try:
        from . import diadia as _dd  # type: ignore
    except (ImportError, ValueError):
        import diadia as _dd  # lib/ on sys.path (standalone self-check)
    return _dd


def _gather_addresses(dataset: list, profile) -> list[tuple]:
    """[(nhãn nguồn, chuỗi địa chỉ thô), …] — gom từ profile (tầng 1) + `du_lieu`/`key_fields` mỗi file; dedup."""
    items = []
    if isinstance(profile, dict):
        pi = profile.get("personal_info") or {}
        ct = profile.get("residence_ct07") or {}
        for src, k in (("giấy tờ tuỳ thân — thường trú", "permanent_address"),
                       ("giấy tờ tuỳ thân — nơi ở hiện tại", "current_address"),
                       ("nơi sinh (khai sinh / HC / CCCD)", "place_of_birth")):
            v = (pi.get(k) or "").strip()
            if v:
                items.append((src, v))
        for src, k in (("CT07 — thường trú", "permanent_address"), ("CT07 — nơi ở hiện tại", "current_address")):
            v = (ct.get(k) or "").strip()
            if v:
                items.append((src, v))
    for d in (dataset or []):
        loai = d.get("loai") or d.get("tag") or "?"
        for bag in (d.get("du_lieu"), d.get("key_fields")):
            if not isinstance(bag, dict):
                continue
            for k in _ADDR_KEYS:
                v = bag.get(k)
                if isinstance(v, str) and v.strip() and len(v.strip()) > 4:
                    items.append((f"{loai} — {k}", v.strip()))
    try:
        _dd = _diadia()
    except Exception:
        return items[:12]
    seen, out = set(), []
    for label, raw in items:
        f = _dd._fold(raw)
        if not f or f in seen:
            continue
        seen.add(f)
        out.append((label, raw))
        if len(out) >= 12:
            break
    return out


def build_dia_gioi(dataset: list, profile) -> dict | None:
    """Tra cứu địa giới (lib.diadia) cho mọi địa chỉ trong hồ sơ → block ground-truth cho tầng 2.
    Trả None nếu không có địa chỉ nào / lib.diadia không nạp được. Không bao giờ raise (wrap ở caller)."""
    try:
        _dd = _diadia()
    except Exception as e:  # noqa: BLE001
        return {"_help": "lib.diadia không nạp được — bỏ qua tra cứu địa giới deterministic", "loi": str(e)}
    addrs = _gather_addresses(dataset, profile)
    if not addrs:
        return None
    resolved = []
    for label, raw in addrs:
        try:
            r = _dd.resolve_address(raw)
        except Exception:
            continue
        if r:
            resolved.append((label, raw, r))
    if not resolved:
        return None
    dia_chi = []
    for label, raw, r in resolved:
        if r["xa_moi"]:
            moi = f"{r['xa_moi']}, {r['tinh_moi']}"
        elif r["candidates"]:
            moi = f"{r['tinh_moi']} (cấp xã: nhiều ứng viên — {len(r['candidates'])})"
        else:
            moi = r["tinh_moi"] or "(không xác định)"
        dia_chi.append({"nguon": label, "goc": raw, "don_vi_moi": moi,
                        "la_ten_cu": bool(r["is_old_province"] or r["is_old_ward"]),
                        "do_tin": r["confidence"], "ghi_chu": r["ghi_chu"]})
    doi_chieu = []
    for i in range(len(resolved)):
        for j in range(i + 1, len(resolved)):
            if len(doi_chieu) >= 30:
                break
            la, ra_, _ = resolved[i]
            lb, rb_, _ = resolved[j]
            try:
                v, why = _dd.same_place(ra_, rb_)
            except Exception:
                continue
            doi_chieu.append({"a": la, "b": lb, "ket_qua": v, "ghi_chu": why})
        if len(doi_chieu) >= 30:
            break
    return {
        "_help": ("Kết quả TRA CỨU DETERMINISTIC từ bảng địa giới hành chính chính thức 2025 (data/admin/, tới cấp "
                  "xã/phường). COI LÀ GROUND-TRUTH — đừng tự dò lại / đừng đoán. (a) hai địa chỉ TEXT khác nhau nhưng "
                  "`doi_chieu`=`same` hoặc cùng `don_vi_moi` → KHÔNG phải mâu thuẫn (chỉ tên trước/sau cải cách); "
                  "(b) giấy cấp SAU mốc cải cách (tỉnh 12/06/2025, xã 01/07/2025) mà ghi đơn vị `la_ten_cu=true` → "
                  "BÁO LỖI ở PHẦN 3; (c) cấp TRƯỚC mốc → HỢP LỆ, ghi chú 'đã sáp nhập'; (d) `do_tin`=`unknown`/`fuzzy` "
                  "→ tự đánh giá thêm như bình thường (bảng có thể chưa phủ hết)."),
        "dia_chi_da_tra": dia_chi,
        "doi_chieu": doi_chieu,
    }


# ===========================================================================
# Orchestrator (≈ process_lmia_dossier): dataset → tầng 1 → (địa giới) → tầng 2 → Google Doc
# ===========================================================================
def run_and_write(case_folder_id: str, applicant: str, drive_id: str | None,
                  batch_items: list | None = None, today: str | None = None,
                  model: str | None = None,
                  vision_compare: list | None = None) -> dict:
    """Chạy toàn bộ bước thẩm định cho một case (2 tầng: trích xuất rẻ → reasoning);
    trả về dict để gắn vào manifest['checklist'].

    `vision_compare` (Mức 3 vision): list[{file_a, file_b, result}] từ
    lib/vision_check.evaluate_pairs() — inject vào eval_input như `_dia_gioi`.
    """
    try:
        from .sop_naming import title_case_ascii
    except Exception:
        def title_case_ascii(s):  # fallback thô
            return (s or "").strip() or "Unknown"
    today = today or time.strftime("%d/%m/%Y")
    _ = batch_items  # giữ tham số cho tương thích (scan_zip.py truyền vào); không cần dùng riêng
    dataset = build_dataset(case_folder_id, drive_id)
    coverage = compute_coverage(dataset)
    n_docs = len(dataset)
    if not dataset:
        return {"ran": False, "error": "không có sidecar nào trong _Bot OCR & Metadata", "coverage": coverage}

    # --- Tầng 1: trích xuất & chuẩn hoá (model rẻ) → JSON hồ sơ cô đọng -----
    prof = extract_profile_data(dataset, applicant, today)
    if not isinstance(prof, dict) or prof.get("_error"):
        print(f"checklist: tầng trích xuất lỗi ({prof.get('_error') if isinstance(prof, dict) else prof}) "
              f"→ fallback dùng dataset thô cho bước thẩm định", flush=True)
        eval_input = _trim_dataset_for_llm(dataset)
        extract_model = None
        profile_out = None
    else:
        eval_input = prof
        extract_model = CHECKLIST_EXTRACT_MODEL
        profile_out = prof

    # --- Vision compare (Mức 3): case-level từ Drive (auto-trigger nếu caller chưa pass) ---
    # Logic:
    #   • Nếu caller (scan_pipeline.py /oldfile) đã chạy vision với local files → dùng kết quả đó.
    #   • Nếu không (vd /check, hoặc batch chỉ có Anh thẻ mà Passport ở Drive cũ) → tự download
    #     từ Drive + run + cache sidecar `_vision_compare.json` trong _Bot OCR & Metadata.
    if not vision_compare:
        try:
            try:
                from .vision_check import compare_pairs_for_case
            except ImportError:
                from vision_check import compare_pairs_for_case  # type: ignore  # noqa
            vision_compare = compare_pairs_for_case(case_folder_id, dataset, drive_id=drive_id)
            if vision_compare:
                print(f"checklist: vision_compare case-level chạy ({len(vision_compare)} pairs, "
                      f"{sum(1 for v in vision_compare if not v.get('cached'))} mới)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"checklist: vision_compare case-level lỗi: {type(e).__name__}: {e}", flush=True)
    if vision_compare:
        try:
            if isinstance(eval_input, dict):
                eval_input["_vision_compare"] = vision_compare
            else:
                eval_input = list(eval_input) + [{"_vision_compare": vision_compare}]
        except Exception as e:  # noqa: BLE001
            print(f"checklist: gắn _vision_compare lỗi: {type(e).__name__}: {e}", flush=True)

    # --- Địa giới hành chính: tra cứu deterministic (cũ↔mới, tới cấp xã) → gắn vào hồ sơ làm ground-truth ---
    try:
        _dg = build_dia_gioi(dataset, profile_out)
        if _dg:
            if isinstance(eval_input, dict):
                eval_input["_dia_gioi"] = _dg          # cùng object với profile_out khi tầng 1 OK
            else:
                eval_input = list(eval_input) + [{"_dia_gioi": _dg}]
    except Exception as e:  # noqa: BLE001
        print(f"checklist: tra cứu địa giới (_dia_gioi) lỗi — bỏ qua: {type(e).__name__}: {e}", flush=True)

    # --- Tầng 2: đánh giá business-logic (model reasoning) → báo cáo Markdown -
    # Truyền `dataset` thô để rule_engine chạy deterministic pre-check (thế chấp, hết hạn…).
    res = evaluate_profile_logic(eval_input, applicant, today, coverage, model=model, n_docs=n_docs,
                                 dataset=dataset)
    if not res.get("report_text"):
        return {"ran": False, "error": res.get("error") or "evaluate_profile_logic không trả về báo cáo",
                "coverage": coverage, "model": res.get("model_used"), "extract_model": extract_model,
                "n_docs": n_docs}
    report_text = res["report_text"]
    model_used = res["model_used"]
    doc_name = f"Bao cao tham dinh - {title_case_ascii(applicant) or 'Unknown'}"
    full_md = render_doc_md(report_text, applicant, today, model_used, coverage, dataset)

    report_link = ""
    try:
        report_link = _write_google_doc(case_folder_id, doc_name, full_md, drive_id)
    except Exception as e:  # noqa: BLE001
        print(f"checklist: ghi Google Doc thất bại: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()

    return {"ran": True, "model": model_used, "extract_model": extract_model, "n_docs": n_docs,
            "coverage": coverage, "report_link": report_link, "doc_link": report_link,
            "md_link": report_link, "sheet_link": "", "report": report_text, "report_text": report_text,
            "profile": profile_out, "error": None}


# ===========================================================================
# Tóm tắt cho Telegram
# ===========================================================================
def summarize_for_telegram(report_text, coverage, model, link):
    """Trả (line_main, detail) cho Telegram.

    - line_main: dòng "📋 Điểm danh (checklist FARM): X/18 …" (text thường — caller tự escape).
    - detail   : tin xác nhận NGẮN dạng Telegram-HTML — "✅ Đã thẩm định hồ sơ — <a …>xem báo cáo
      thẩm định</a>" (không còn dump PHẦN 4). `report_text`/`model` giữ trong chữ ký cho tương thích
      nhưng không dùng nữa."""
    coverage = coverage or {}
    have, req = coverage.get("have", 0), coverage.get("required", 0)
    miss = coverage.get("missing") or []
    if miss:
        shown = "; ".join(m.split(".", 1)[-1].strip().split(" (")[0] for m in miss[:6])
        miss_txt = f" — thiếu: {shown}" + (f" … (+{len(miss) - 6})" if len(miss) > 6 else "")
    else:
        miss_txt = " ✔ đủ"
    l1 = f"📋 Điểm danh (checklist FARM): {have}/{req} mục bắt buộc{miss_txt}"
    if link:
        detail = f'✅ Đã thẩm định hồ sơ — <a href="{html.escape(link, quote=True)}">xem báo cáo thẩm định</a>'
    else:
        detail = "✅ Đã thẩm định hồ sơ. (chưa tạo được file báo cáo — kiểm tra log)"
    return l1, detail


# ===========================================================================
# self-check khi chạy trực tiếp
# ===========================================================================
if __name__ == "__main__":
    print("CHECKLIST_MODEL (tầng 2):", CHECKLIST_MODEL, "| fallback:", CHECKLIST_FALLBACK_MODEL)
    print("CHECKLIST_EXTRACT_MODEL (tầng 1):", CHECKLIST_EXTRACT_MODEL)
    print("CHECKLIST_DOC_TAGS:", len(CHECKLIST_DOC_TAGS), "| REQUIRED_DOCS:", len(REQUIRED_DOCS))
    print("provinces loaded:", bool(PROVINCES), "| cities:", len(PROVINCES.get("cities", [])),
          "| provinces:", len(PROVINCES.get("provinces", [])))
    assert callable(extract_profile_data) and callable(evaluate_profile_logic) and callable(run_and_write)
    assert _PROFILE_EXTRACT_SYSTEM and "GIỮ NGUYÊN VĂN" in _PROFILE_EXTRACT_SYSTEM and CHECKLIST_EXTRACT_MODEL
    assert len(REQUIRED_DOCS) == 26 and _REQUIRED_TOTAL == 18
    assert {"Passport", "Sao ke", "Anh gia dinh", "Dai ly NS", "So dat NN", "The Visa-MC"} <= CHECKLIST_DOC_TAGS
    assert "GKS_con" not in CHECKLIST_DOC_TAGS
    ds = [{"loai": "CCCD", "ten": "CCCD-Test.pdf", "nguoi": "Test", "tom_tat": "x", "du_lieu": {}, "key_fields": {}},
          {"loai": "Passport", "ten": "Passport-Test.pdf", "nguoi": "Test", "tom_tat": "y", "du_lieu": {}, "key_fields": {}}]
    cov = compute_coverage(ds)
    assert cov["required"] == 18 and cov["have"] == 2
    _i3 = next(i for i in cov["items"] if i["loai"].startswith("3."))
    assert _i3["applicable"] is False and "không áp dụng" in _i3["status"]
    cov2 = compute_coverage([{"loai": "GKH", "ten": "GKH-x.pdf", "nguoi": "x"}])
    _i3b = next(i for i in cov2["items"] if i["loai"].startswith("3."))
    assert _i3b["applicable"] is True and _i3b["present"] is True
    print("coverage:", cov["have"], "/", cov["required"], "| missing:", len(cov["missing"]), "mục")
    p = _build_prompt("12/05/2026", "Nguyen Van Test", cov)
    assert "VAI TRÒ" in p and "PHẦN 4" in p and "12/05/2026" in p and "Nguyen Van Test" in p and "{{" not in p and "CHECKLIST HỒ SƠ FARM" in p and "_dia_gioi" in p
    _plc = re.sub(r"\s+", " ", p.lower())  # gộp khoảng trắng để khỏi vướng wrap dòng
    assert "đừng mặc định chủ hộ" in _plc and "không có tên đương đơn trong bảng đó" in _plc and "ocr thấp tin cậy" in _plc
    assert "không yêu cầu khách bổ sung giấy xác nhận" in _plc  # bỏ quy tắc "Giấy xác nhận số CMND"
    _t = _trim_dataset_for_llm([{"loai": "CV", "ten": "x", "needs_review": True, "confidence": "low",
                                 "du_lieu": {}, "key_fields": {}, "tom_tat": "t"}])
    assert _t[0].get("needs_review") is True and _t[0].get("confidence") == "low"
    print("prompt len:", len(p))
    # địa giới: build_dia_gioi qua lib.diadia
    _dg = build_dia_gioi(
        [{"loai": "CCCD", "ten": "x", "du_lieu": {"noi_thuong_tru": "Phường Liên Bảo, TP Vĩnh Yên, Tỉnh Vĩnh Phúc"}},
         {"loai": "XNCT", "ten": "y", "du_lieu": {"noi_thuong_tru": "..., Phú Thọ"}}],
        {"personal_info": {"permanent_address": "Phường Liên Bảo, TP Vĩnh Yên, Tỉnh Vĩnh Phúc"}})
    assert _dg and _dg.get("dia_chi_da_tra") and any(x.get("la_ten_cu") for x in _dg["dia_chi_da_tra"])
    assert _dg.get("doi_chieu") and any(x["ket_qua"] == "same" for x in _dg["doi_chieu"])
    print("dia_gioi: addrs", len(_dg["dia_chi_da_tra"]), "| doi_chieu", len(_dg["doi_chieu"]))
    assert should_run_checklist({"items": [{"tag": "CCCD"}]}) is True
    assert should_run_checklist({"items": [{"tag": "Khac"}]}) is False
    md = render_doc_md("## 📋 BÁO CÁO THẨM ĐỊNH HỒ SƠ\n**Khách hàng:** Test\n\n## ⚠️ PHẦN 3: ...\n\n## 📌 PHẦN 4: TÓM TẮT & KHUYẾN NGHỊ\n- **Tình trạng tổng thể:** ✅ Sẵn sàng nộp",
                      "Test", "12/05/2026", "test-model", cov, ds)
    assert "Phụ lục" in md and "Điểm danh" in md and "CHECKLIST FARM" in md
    print("render_doc_md len:", len(md))
    l1, det = summarize_for_telegram("## 📌 PHẦN 4: TÓM TẮT & KHUYẾN NGHỊ\n- **Tình trạng tổng thể:** ✅ Sẵn sàng nộp\n- **Số lỗi nghiêm trọng:** 0",
                                     cov, "test-model", "http://x/doc")
    assert det and "Đã thẩm định" in det and 'href="http://x/doc"' in det and "PHẦN 4" not in det
    l1b, detb = summarize_for_telegram("", cov, "test-model", "")
    assert detb and "Đã thẩm định" in detb and "<a " not in detb
    print("telegram l1:", l1)
    print("telegram detail:", det)
    print("OK")
