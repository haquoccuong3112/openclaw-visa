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
REQUIRED_DOCS = [
    # === 18 mục BẮT BUỘC (tính vào X/18) ===
    ("1. Hộ chiếu đương đơn (gồm HC cũ nếu có)",                  "Passport",                        "bat_buoc"),
    ("2. Giấy khai sinh đương đơn + vợ/chồng",                    "GKS",                             "bat_buoc"),
    ("5. CCCD đương đơn + vợ/chồng/con",                          "CCCD",                            "bat_buoc"),
    ("6. Giấy xác nhận cư trú CT07 (mới nhất)",                   "XNCT",                            "bat_buoc"),
    ("7. Lý lịch tư pháp số 2 (≤6 tháng)",                        "LLTP",                            "bat_buoc"),
    ("9. Ảnh thẻ 5x7 (phông trắng, có bản digital)",              "Anh the",                         "bat_buoc"),
    ("10. Bằng cấp & chứng chỉ",                                  "Bang cap",                        "bat_buoc"),
    ("11/12. Giấy tờ tài sản (sổ đỏ HOẶC HĐ cho/tặng-thừa kế)",   ("So dat", "HD cho-tang-thua ke"), "bat_buoc"),
    ("13/14. Chứng minh nghề nông (sổ đỏ đất NN HOẶC ĐKKD HTX)",  ("So dat NN", "DKKD"),             "bat_buoc"),
    ("15. Sổ tiết kiệm (≥300-400tr, kỳ hạn ≥6 tháng)",            "STK",                             "bat_buoc"),
    ("17. Sao kê ngân hàng (3-6 tháng gần nhất)",                 "Sao ke",                          "bat_buoc"),
    ("19a. Biên lai BHXH tự nguyện (3 tháng gần nhất)",           "BHXH",                            "bat_buoc"),
    ("19b. Biên lai BHYT (3 tháng gần nhất)",                     "BHYT",                            "bat_buoc"),
    ("21. Thông tin cá nhân & gia đình (sơ yếu lý lịch)",         "CV",                              "bat_buoc"),
    ("22. Thẻ Visa/Mastercard quốc tế (ảnh 2 mặt)",              "The Visa-MC",                     "bat_buoc"),
    ("23. Thông tin 2 đại lý nông sản/phân bón",                  "Dai ly NS",                       "bat_buoc"),
    ("25. Ảnh chụp gia đình",                                     "Anh gia dinh",                    "bat_buoc"),
    ("26. Ảnh & video làm nông",                                  "Anh-video lam nong",              "bat_buoc"),
    # === ĐIỀU KIỆN ===
    ("3. Giấy đăng ký kết hôn / giấy ly hôn",                     "GKH",                             "ket_hon"),
    ("2b. Giấy khai sinh của con",                                "GKS_con",                         "co_con"),
    ("4. Giấy xác nhận con đang học",                             "XN hoc",                          "co_con"),
    # === NẾU CÓ / TĂNG HỒ SƠ ===
    ("8. Bằng lái xe ô tô",                                       "GPLX",                            "tuy_chon"),
    ("18. Cà vẹt xe / hoá đơn mua vàng",                          ("Ca vet xe", "Vang"),             "tuy_chon"),
    ("24. Giấy công ích / bằng khen / thư cảm ơn",                "Bang khen",                       "tuy_chon"),
    # === LÀM / BỔ SUNG SAU ===
    ("16. Giấy xác nhận số dư sổ TK (EN/song ngữ)",               "XN so du",                        "lam_sau"),
    ("20. Khám sức khỏe IOM",                                     "IOM",                             "lam_sau"),
]

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
# danh sách 34 đơn vị hành chính cấp tỉnh (đọc từ provinces_34.json)
# ---------------------------------------------------------------------------
def _load_provinces() -> dict:
    p = SCAN_HO_SO_DIR / "provinces_34.json"
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
  `du_lieu` (các trường trích từ OCR), `key_fields`.

# NGUYÊN TẮC LÀM VIỆC BẮT BUỘC

1. **KHÔNG SUY ĐOÁN**: Chỉ kết luận dựa trên dữ liệu OCR có thật. Nếu OCR mờ/
   thiếu/không rõ → ghi "KHÔNG ĐỌC ĐƯỢC - cần kiểm tra bản gốc".

2. **ĐỐI CHIẾU CHÉO TRIỆT ĐỂ**: Mọi thông tin trùng lặp giữa các giấy tờ
   (họ tên, ngày sinh, số CMND/CCCD, địa chỉ, tên cha mẹ...) phải được so
   khớp ký tự với ký tự. Một dấu cách thừa, một chữ lót thiếu = LỖI.

3. **TÍNH TOÁN NGÀY THÁNG**: Mọi thời hạn phải tính từ ngày kiểm tra ở trên.
   Hiển thị rõ phép tính (VD: "Cấp 22/01/2026, đến {{TODAY}} = 3 tháng 20
   ngày, còn hạn 2 tháng 10 ngày").

4. **CẢNH BÁO ĐỊA GIỚI HÀNH CHÍNH**: Từ 12/06/2025, Việt Nam chỉ còn 34 đơn
   vị hành chính cấp tỉnh (28 tỉnh + 6 thành phố trực thuộc TW).
   - Giấy tờ cấp SAU 12/06/2025 mà ghi tên tỉnh cũ (đã sáp nhập) → BÁO LỖI
   - Giấy tờ cấp TRƯỚC 12/06/2025 ghi tên tỉnh cũ → HỢP LỆ, ghi chú "đã sáp nhập"
   Danh sách 34 đơn vị hiện hành:
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
- [ ] Số CMND/CCCD ghi trong HC: là số 9 hay 12? Có khớp giấy tờ hiện hành?
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
- [ ] Số CMND/CCCD trên LLTP khớp số hiện hành
- [ ] Tên cha, mẹ, vợ/chồng khớp Khai sinh + Đăng ký kết hôn
- [ ] Tình trạng: "Không có án tích"

**A5. Xác nhận cư trú CT07:**
- [ ] Còn hiệu lực ("Giấy này có giá trị đến hết ngày...")
- [ ] Địa chỉ thường trú + nơi ở hiện tại khớp CCCD/LLTP
- [ ] Bảng thành viên hộ: đầy đủ vợ/chồng/con? Mã định danh 12 số + ngày sinh
  từng người khớp CCCD/KS của họ?
- [ ] Quan hệ với chủ hộ có logic không

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

1. **Mâu thuẫn CMND 9 số ↔ CCCD 12 số**: Nếu HC/Kết hôn dùng số 9 cũ, hiện
   tại dùng số 12 → khách CẦN có Giấy xác nhận số CMND hoặc số 9 phải quét
   được từ QR/chip CCCD hiện tại. → CẢNH BÁO

2. **Sai lệch địa chỉ giữa giấy cũ và mới**: Logic không? Đã update đúng
   trên CT07?

3. **Chữ lót/tên gọi**: Bất kỳ sai khác dù 1 chữ lót → BÁO LỖI NGAY

4. **Tên công ty trên BHXH ↔ Sao kê lương**: PHẢI trùng khớp tuyệt đối

5. **Địa giới hành chính sau 12/06/2025**: Đối chiếu danh sách 34 đơn vị mới

# THAM KHẢO — ĐIỂM DANH HỒ SƠ THEO CHECKLIST FARM (ALLY)
(đếm tự động từ dữ liệu OCR, coi là CHUẨN — KHÔNG được mâu thuẫn; trạng thái mỗi mục: "✅ đã có" /
"❌ THIẾU" / "— không áp dụng" / "— chưa có (tùy chọn)" / "— sẽ làm sau"):
{{COVERAGE_BLOCK}}
→ Khách đã nộp {{HAVE}}/{{REQUIRED}} mục BẮT BUỘC trong CHECKLIST HỒ SƠ FARM. {{MISSING_NOTE}}
Mục đánh dấu "— không áp dụng" / "— tùy chọn" / "— sẽ làm sau" thì ĐỪNG liệt kê là "thiếu" trong PHẦN 3.

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


def _build_prompt(today: str, applicant: str, coverage: dict) -> str:
    missing_note = ("Còn thiếu (bắt buộc): " + ", ".join(coverage["missing"]) + "."
                    if coverage["missing"] else f"Đã đủ {coverage['required']} mục bắt buộc.")
    repl = {
        "{{TODAY}}": today,
        "{{APPLICANT}}": applicant or "(không rõ tên)",
        "{{PROVINCES}}": _provinces_text(),
        "{{COVERAGE_BLOCK}}": _coverage_block_text(coverage),
        "{{HAVE}}": str(coverage["have"]),
        "{{REQUIRED}}": str(coverage["required"]),
        "{{MISSING_NOTE}}": missing_note,
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
        })
    return out


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
`nguoi`, `tom_tat`, `du_lieu`, `key_fields`.

NHIỆM VỤ: gom toàn bộ dữ liệu thành MỘT JSON object hồ sơ thống nhất, theo schema dưới.

QUY TẮC TUYỆT ĐỐI:
- GIỮ NGUYÊN VĂN mọi giá trị (họ tên, ngày, số CMND/CCCD, địa chỉ, tên cha/mẹ/vợ/chồng, tên công ty,
  số tiền…) — copy chính xác từng ký tự, KHÔNG tóm tắt, KHÔNG diễn giải, KHÔNG tự "sửa" cho đẹp.
- Nếu một thông tin xuất hiện khác nhau ở các giấy → giữ CẢ HAI dạng và ghi vào `notes` (vd "tên chồng:
  LLTP='Nguyễn Bá Thắng' vs CT07='Nguyễn Bá Thẳng' (dấu hỏi)").
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
                           model: str | None = None, n_docs: int | None = None) -> dict:
    """TẦNG 2: đọc hồ sơ đã chuẩn hoá (dict) — hoặc dataset thô (list, fallback) — + prompt thẩm định
    → báo cáo Markdown 4 phần. Trả {report_text, model_used, n_docs} hoặc {report_text:None, error}."""
    model = model or CHECKLIST_MODEL
    if n_docs is None:
        n_docs = len(profile) if isinstance(profile, list) else len((profile or {}).get("documents") or [])
    system = _build_prompt(today, applicant, coverage)
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
    from .drive_helpers import find_file_by_name, delete_file
    DOC_MIME = "application/vnd.google-apps.document"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(md_text)
        mpath = fh.name
    try:
        old = find_file_by_name(name, case_folder_id, drive_id, mime_type=DOC_MIME)
        if old:
            try:
                delete_file(old, drive_id)
            except Exception:
                pass
        media = MediaFileUpload(mpath, mimetype="text/markdown", resumable=False)
        body = {"name": name, "mimeType": DOC_MIME, "parents": [case_folder_id]}
        kwargs = dict(body=body, media_body=media, fields="id, webViewLink")
        if drive_id:
            kwargs["supportsAllDrives"] = True
        f = drive().files().create(**kwargs).execute()
        return f.get("webViewLink") or f"https://docs.google.com/document/d/{f['id']}/edit"
    finally:
        try:
            os.unlink(mpath)
        except OSError:
            pass


# ===========================================================================
# Orchestrator (≈ process_lmia_dossier): dataset → tầng 1 → tầng 2 → Google Doc
# ===========================================================================
def run_and_write(case_folder_id: str, applicant: str, drive_id: str | None,
                  batch_items: list | None = None, today: str | None = None,
                  model: str | None = None) -> dict:
    """Chạy toàn bộ bước thẩm định cho một case (2 tầng: trích xuất rẻ → reasoning);
    trả về dict để gắn vào manifest['checklist']."""
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

    # --- Tầng 2: đánh giá business-logic (model reasoning) → báo cáo Markdown -
    res = evaluate_profile_logic(eval_input, applicant, today, coverage, model=model, n_docs=n_docs)
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
    assert "VAI TRÒ" in p and "PHẦN 4" in p and "12/05/2026" in p and "Nguyen Van Test" in p and "{{" not in p and "CHECKLIST HỒ SƠ FARM" in p
    print("prompt len:", len(p))
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
