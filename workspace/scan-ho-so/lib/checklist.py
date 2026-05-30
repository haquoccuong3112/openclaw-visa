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
CHECKLIST_MODEL = os.environ.get("CHECKLIST_MODEL", "gpt-5-mini")
CHECKLIST_FALLBACK_MODEL = os.environ.get("CHECKLIST_FALLBACK_MODEL", "gpt-5-mini")
# Tầng 1 — model rẻ để gộp/chuẩn hoá summary+extracted của các file thành 1 JSON hồ sơ cô đọng
CHECKLIST_EXTRACT_MODEL = os.environ.get("CHECKLIST_EXTRACT_MODEL", "gpt-5-mini")
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

# ===========================================================================
# Bảng checklist hiển thị A–H (format báo cáo mới)
# ===========================================================================
SECTION_NAMES = {
    "A": "GIẤY TỜ TÙY THÂN",
    "B": "PHÁP LÝ & CƯ TRÚ",
    "C": "HỌC VẤN & NGHỀ NGHIỆP",
    "D": "TÀI CHÍNH",
    "E": "TÀI SẢN",
    "F": "BẢO HIỂM & Y TẾ",
    "G": "HỖ TRỢ NÔNG NGHIỆP (FARM)",
    "H": "GIA ĐÌNH / LIÊN QUAN",
}

# 29 dòng hiển thị: (section, num, label, tags, severity, date_key)
# date_key: key trong doc['key_fields'] hoặc doc['du_lieu'] để lấy ngày hiển thị.
# severity đặc biệt:
#   "hc_cu"         → HC cũ (Passport không phải mới nhất)
#   "display_cha_me"→ CCCD cha/mẹ (quan_he in ['ba','me'])
REPORT_DISPLAY_ROWS: list[tuple] = [
    ("A",  1, "Hộ chiếu (còn hạn ≥2 năm)",                  ["Passport"],                   "bat_buoc",       "ngay_het_han"),
    ("A",  2, "Hộ chiếu cũ (nếu có)",                        ["Passport"],                   "hc_cu",          "ngay_het_han"),
    ("A",  3, "CCCD / CMND (2 mặt)",                         ["CCCD"],                       "bat_buoc",       None),
    ("A",  4, "Giấy khai sinh",                               ["GKS"],                        "bat_buoc",       None),
    ("A",  5, "Ảnh thẻ 5×7 (phông trắng, digital)",          ["Anh the"],                    "bat_buoc",       None),
    ("A",  6, "Giấy đăng ký kết hôn / ly hôn",               ["GKH"],                        "ket_hon",        None),
    ("A",  7, "Giấy khai sinh của con",                       ["GKS"],                        "co_con",         None),
    ("B",  8, "Lý lịch tư pháp số 2 (≤6 tháng)",            ["LLTP"],                       "bat_buoc",       "ngay_cap"),
    ("B",  9, "Xác nhận cư trú CT07",                        ["XNCT"],                       "bat_buoc",       "ngay_het_han"),
    ("B", 10, "Bằng lái xe (nếu có)",                        ["GPLX"],                       "tuy_chon",       None),
    ("C", 11, "Bằng cấp / chứng chỉ",                        ["Bang cap"],                   "bat_buoc",       None),
    ("C", 12, "Chứng minh nghề nông (sổ đỏ NN / ĐKKD HTX)", ["So dat NN", "DKKD"],         "bat_buoc",       None),
    ("C", 13, "Biên lai BHXH tự nguyện (3 tháng gần nhất)", ["BHXH"],                       "bat_buoc",       None),
    ("C", 14, "Sơ yếu lý lịch / thông tin cá nhân & GĐ",   ["CV"],                         "bat_buoc",       None),
    ("D", 15, "Sổ tiết kiệm (≥300–400tr, kỳ hạn ≥6 tháng)", ["STK"],                       "bat_buoc",       "ngay_dao_han"),
    ("D", 16, "Sao kê ngân hàng (3–6 tháng gần nhất)",      ["Sao ke"],                     "bat_buoc",       "ngay_in"),
    ("D", 17, "Xác nhận số dư (EN / song ngữ)",              ["XN so du"],                   "lam_sau",        "ngay_in"),
    ("D", 18, "Thẻ Visa / Mastercard quốc tế (2 mặt)",      ["The Visa-MC"],                "bat_buoc",       None),
    ("E", 19, "Sổ đỏ / GCN QSD đất",                        ["So dat", "HD cho-tang-thua ke"], "bat_buoc",    None),
    ("E", 20, "HĐ cho-tặng-thừa kế (nếu có)",               ["HD cho-tang-thua ke"],        "tuy_chon",       None),
    ("E", 21, "Cà vẹt xe / hoá đơn mua vàng",               ["Ca vet xe", "Vang"],          "tuy_chon",       None),
    ("F", 22, "Biên lai BHYT (3 tháng gần nhất)",            ["BHYT"],                       "bat_buoc",       "ngay_het_han"),
    ("F", 23, "Khám sức khoẻ IOM",                           ["IOM"],                        "lam_sau",        None),
    ("G", 24, "Thông tin 2 đại lý nông sản / phân bón",     ["Dai ly NS"],                  "bat_buoc",       None),
    ("G", 25, "Ảnh chụp gia đình",                           ["Anh gia dinh"],               "bat_buoc",       None),
    ("G", 26, "Ảnh & video làm nông",                        ["Anh-video lam nong"],         "bat_buoc",       None),
    ("G", 27, "Giấy công ích / bằng khen (nếu có)",         ["Bang khen"],                  "tuy_chon",       None),
    ("H", 28, "CCCD / CMND cha, mẹ",                        ["CCCD"],                       "display_cha_me", None),
    ("H", 29, "Giấy xác nhận con đang học",                  ["XN hoc"],                     "co_con",         None),
]

# ─── HTML color palette (matches PDF design) ───────────────────────────────
_C = {
    "primary": "#1B4F6A",
    "sec_bg":  "#EBF5FB",
    "ok_bg":   "#E8F5E9", "ok_fg":   "#1B5E20",
    "miss_bg": "#FFEBEE", "miss_fg": "#B71C1C",
    "warn_bg": "#FFF3E0", "warn_fg": "#E65100",
    "chk_bg":  "#E3F2FD", "chk_fg":  "#0D47A1",
    "na_fg":   "#9E9E9E",
    "alt":     "#F8FAFB",
    "bd":      "#D0D7DE",
}

_LEGEND_ITEMS = [
    ("✓ Đã có",        _C["ok_fg"],   _C["ok_bg"],   "Không cần xử lý"),
    ("✗ Chưa có",      _C["miss_fg"], _C["miss_bg"],  "Bổ sung / làm lại gấp"),
    ("⚠ Hết hạn",      _C["warn_fg"], _C["warn_bg"],  "Gia hạn hoặc cấp lại"),
    ("⏰ Sắp đáo hạn", _C["warn_fg"], "#FFF8E1",      "Theo dõi / nộp sớm"),
    ("? Cần kiểm tra", _C["chk_fg"],  _C["chk_bg"],   "Đối chiếu bản gốc"),
    ("—",              _C["na_fg"],   "#F5F5F5",      "Không áp dụng"),
]


def _e(s) -> str:
    return html.escape(str(s or ""))


def _td(content: str, style: str = "", colspan: int = 1) -> str:
    cs = f' colspan="{colspan}"' if colspan > 1 else ""
    return f'<td{cs} style="padding:4px 8px;border:1px solid {_C["bd"]};{style}">{content}</td>'


def _status_td(status: str) -> str:
    s = status.strip()
    if s.startswith("✓"):
        st = f"background:{_C['ok_bg']};color:{_C['ok_fg']}"
    elif s.startswith("✗"):
        st = f"background:{_C['miss_bg']};color:{_C['miss_fg']}"
    elif s.startswith(("⚠", "⏰")):
        st = f"background:{_C['warn_bg']};color:{_C['warn_fg']}"
    elif s.startswith("?"):
        st = f"background:{_C['chk_bg']};color:{_C['chk_fg']}"
    else:
        st = f"color:{_C['na_fg']};font-style:italic"
    return (f'<td style="{st};font-weight:bold;text-align:center;'
            f'padding:4px 8px;border:1px solid {_C["bd"]}">{_e(s)}</td>')


def _html_to_text(h: str) -> str:
    """Strip HTML tags → plain text cho LLM prompt (không dùng thư viện ngoài)."""
    t = re.sub(r"<td[^>]*>", "\t", h)
    t = re.sub(r"<tr[^>]*>", "\n", t)
    t = re.sub(r"<th[^>]*>", "\t", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = t.replace("&middot;", "·").replace("&nbsp;", " ")
    t = re.sub(r"\t+", " | ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _legend_html() -> str:
    cells = "".join(
        f'<td style="background:{bg};padding:7px 4px;border:1px solid {_C["bd"]};'
        f'text-align:center;width:16%">'
        f'<div style="color:{fg};font-weight:bold;font-size:9pt">{_e(sym)}</div>'
        f'<div style="color:#888;font-size:7.5pt">{_e(act)}</div>'
        f'</td>'
        for sym, fg, bg, act in _LEGEND_ITEMS
    )
    return (f'<table style="width:100%;border-collapse:collapse;margin:8px 0 12px 0;'
            f'font-family:Arial,sans-serif">'
            f'<tr>{cells}</tr></table>')


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
# deterministic enrichers from DocAI summaries
# ===========================================================================

def _clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _parse_ct07_from_summary(summary: str) -> dict:
    """Parse common CT07/XNCT fields from DocAI text.

    DocAI-plan sidecars often keep rich OCR text in `summary` but leave
    `extracted` empty. Checklist must still use this text instead of blaming
    the scan as blurry/unreadable. This parser is conservative: it only fills
    fields that are explicitly present in the OCR text.
    """
    text = str(summary or "")
    if not text.strip():
        return {}
    out: dict = {}

    def first(pattern: str, flags: int = re.I) -> str:
        m = re.search(pattern, text, flags)
        return _clean_ws(m.group(1)) if m else ""

    out["so_xac_nhan"] = first(r"Số\s*:\s*([^\n]+)")
    out["nguoi_de_nghi"] = first(r"Theo đề nghị của\s+Ông/Bà\s*:\s*([^\n]+)")
    out["ho_ten"] = first(r"Họ, chữ đệm và tên của\s+Ông/Bà\s*:\s*([^\n]+)")
    out["ngay_sinh"] = first(r"Ngày, tháng, năm sinh\s*:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})")
    out["so_dinh_danh"] = first(r"Số định danh cá nhân\s*:\s*([0-9]{9,14})")
    out["que_quan"] = first(r"Quê quán\s*:\s*([^\n]+)")
    out["thuong_tru"] = first(r"Nơi thường trú\s*:\s*([^\n]+)")
    out["tam_tru"] = first(r"Nơi tạm trú\s*:\s*([^\n]*)")
    out["noi_o_hien_tai"] = first(r"Nơi ở hiện tại\s*:\s*([^\n]+)")
    out["chu_ho"] = first(r"Họ, chữ đệm và tên chủ hộ\s*:\s*([^\n]+?)(?:\s+12\.|$)")
    out["quan_he_voi_chu_ho"] = first(r"Quan hệ với chủ hộ\s*:\s*([^\n]+)")
    valid = first(r"Giấy này có giá trị(?: sử dụng)? đến hết ngày\s+(?:ngày\s+)?([0-9]{1,2}\s+tháng\s+[0-9]{1,2}\s+năm\s+[0-9]{4})")
    if valid:
        out["gia_tri_den"] = valid
    issue = first(r"ngày\s*\.\s*tháng\s*\.\s*năm\s*\.\.\.\s*([0-9]{4,5})")
    if issue:
        out["ngay_cap_raw"] = issue

    members: list[dict] = []
    row_re = re.compile(
        r"(?:^|\n)\s*(\d{1,2})\s*\n\s*"
        r"([^\n]+?)\s*\n\s*"
        r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})\s+"
        r"(Nam|Nữ)\s+([0-9]{9,14})\s*\n\s*([^\n]+)",
        re.I,
    )
    for m in row_re.finditer(text):
        members.append({
            "stt": m.group(1),
            "ho_ten": _clean_ws(m.group(2)),
            "ngay_sinh": m.group(3),
            "gioi_tinh": m.group(4),
            "so_dinh_danh": m.group(5),
            "quan_he_voi_chu_ho": _clean_ws(m.group(6)),
        })
    if members:
        out["thanh_vien_ho_gia_dinh"] = members

    return {k: v for k, v in out.items() if v not in ("", [], None)}


def _enrich_entry_from_summary(entry: dict) -> dict:
    """Fill deterministic fields from `tom_tat` without extra AI calls."""
    tag = str(entry.get("loai") or "").upper()
    name = str(entry.get("ten") or "")
    summary = str(entry.get("tom_tat") or "")
    is_ct07 = tag == "XNCT" or "CT07" in summary.upper() or "Xác nhận thông tin về cư trú" in summary or "XNCT" in name.upper()
    if is_ct07:
        parsed = _parse_ct07_from_summary(summary)
        if parsed:
            data = dict(entry.get("du_lieu") or {})
            for k, v in parsed.items():
                data.setdefault(k, v)
            entry["du_lieu"] = data
            entry["ocr_quality_note"] = (
                "DocAI summary có nội dung đọc được; không coi là scan mờ chỉ vì extracted/du_lieu ban đầu trống."
            )
    return entry


# ===========================================================================
# dataset: gom toàn bộ sidecar .json của case
# ===========================================================================
def build_dataset(case_folder_id: str, drive_id: str | None = None) -> list[dict]:
    """Đọc mọi sidecar `*.json` trong `_Bot OCR & Metadata` của case → list dict mô tả từng giấy tờ.

    P3.4 — dedup theo `content_hash`: nếu 2 sidecar cùng hash (do remnant từ lần quét cũ
    hoặc oldfile-rescan), giữ entry mới nhất (theo `generated_at`). Đảm bảo `Phụ lục file`
    đếm đúng N file thực, không 40 entry phù phiếm.
    Cũng skip sidecar nội bộ `_vision_compare.json` (không phải giấy tờ KH).
    """
    from .drive_helpers import get_or_create_folder, list_folder, download_file_text
    meta_id = get_or_create_folder(OCR_META_FOLDER, case_folder_id, drive_id=drive_id)
    files = list_folder(meta_id, drive_id=drive_id)
    # P3.4 — gom theo content_hash; nếu trùng → giữ entry có generated_at mới nhất.
    by_hash: dict = {}
    no_hash: list = []
    for name, fid in files.items():
        if not name.lower().endswith(".json"):
            continue
        if name.startswith("_") or name.startswith("."):
            # Sidecars nội bộ (_vision_compare.json, _processed.json…) — không phải giấy KH.
            continue
        try:
            d = json.loads(download_file_text(fid, drive_id=drive_id))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        gem = d.get("gemini") if isinstance(d.get("gemini"), dict) else {}
        entry = {
            "ten": d.get("new_name") or name[:-5],
            "loai": d.get("tag", ""),
            "folder": d.get("folder", ""),
            "nguoi": d.get("subject", ""),
            "quan_he": d.get("relation", ""),  # quan hệ với đương đơn: "me","ba","con","vo","chong",""
            "tom_tat": d.get("md_content") or d.get("summary", ""),   # md_content richer when available
            "du_lieu": d.get("extracted") if isinstance(d.get("extracted"), dict) else {},
            "key_fields": gem.get("key_fields") if isinstance(gem.get("key_fields"), dict) else {},
            "confidence": d.get("confidence", ""),
            "needs_review": bool(d.get("needs_review")),
            "drive_link": d.get("drive_link", ""),
            "ngay_xu_ly": d.get("generated_at") or d.get("case_id", ""),
            "content_hash": d.get("content_hash", ""),
            "source": d.get("source", "bot"),   # "bot" | "da-duyet" (staff-verified)
        }
        entry = _enrich_entry_from_summary(entry)
        h = entry["content_hash"]
        if h:
            prev = by_hash.get(h)
            # Giữ entry mới nhất (ngay_xu_ly sort lexicographic vì format YYYY-mm-dd HH:MM:SS)
            if prev is None or (entry["ngay_xu_ly"] or "") >= (prev["ngay_xu_ly"] or ""):
                by_hash[h] = entry
        else:
            no_hash.append(entry)
    out = list(by_hash.values()) + no_hash
    out.sort(key=lambda r: (r["loai"], r["ten"]))

    # Da-duyet override: nếu staff đã put file vào "Đã duyệt/", dùng bản đó → bỏ bản bot cùng loai+nguoi.
    # Chỉ override khi da-duyet file đã classify được (không Khac) để tránh mất dữ liệu bot vô ích.
    da_duyet_keys = {
        (e["loai"].lower(), (e.get("nguoi") or "").lower())
        for e in out
        if e.get("source") == "da-duyet" and e.get("loai") and e["loai"] != "Khac"
    }
    if da_duyet_keys:
        out = [
            e for e in out
            if e.get("source") == "da-duyet"
            or (e.get("loai", "").lower(), (e.get("nguoi") or "").lower()) not in da_duyet_keys
        ]

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
# Helpers cho bảng checklist A–H (deterministic, không tốn LLM)
# ===========================================================================

def _fmt_date(val) -> str:
    """Chuẩn hoá ngày về DD/MM/YYYY."""
    if not val:
        return ""
    s = str(val).strip()
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", s):
        return s
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2})$", s)
    if m:
        yy = int(m.group(3))
        year = 2000 + yy if yy < 50 else 1900 + yy
        return f"{m.group(1)}/{m.group(2)}/{year}"
    return s


def _add_months_to_date(date_str: str, months: int) -> str:
    """Cộng N tháng vào DD/MM/YYYY."""
    try:
        import calendar as _cal
        d, m, y = map(int, date_str.split("/"))
        m += months
        while m > 12:
            m -= 12
            y += 1
        d = min(d, _cal.monthrange(y, m)[1])
        return f"{d:02d}/{m:02d}/{y}"
    except Exception:
        return ""


def _find_by_tags(docs_by_tag: dict, tags) -> list:
    """Trả tất cả docs có loai thuộc tags (không trùng tên file)."""
    if isinstance(tags, str):
        tags = [tags]
    seen: set = set()
    result = []
    for t in tags:
        for d in docs_by_tag.get(t, []):
            n = d.get("ten", "")
            if n not in seen:
                seen.add(n)
                result.append(d)
    return result


def _row_status(matching: list[dict], errors_by_tag: dict, tags: list, severity: str) -> str:
    """Xác định ký hiệu trạng thái cho một dòng có docs."""
    has_reject = any(
        e.get("severity") == "reject"
        for t in tags for e in errors_by_tag.get(t, [])
    )
    if has_reject:
        return "⚠ Hết hạn"
    if any(d.get("needs_review") for d in matching):
        return "? Cần kiểm tra"
    has_warn = any(
        e.get("severity") == "warn"
        for t in tags for e in errors_by_tag.get(t, [])
    )
    if has_warn:
        return "⏰ Sắp đáo hạn"
    return "✓ Đã có"


def _format_date_for_row(docs: list[dict], date_key: str | None, row_num: int) -> str:
    """Định dạng cột 'Hạn / ngày cấp' cho một dòng."""
    if not date_key or not docs:
        return ""
    results = []
    for doc in docs:
        kf = doc.get("key_fields") or {}
        ext = doc.get("du_lieu") or {}
        raw = kf.get(date_key) or ext.get(date_key)
        if not raw and date_key == "ngay_het_han":
            raw = kf.get("han_su_dung") or kf.get("gia_tri_den") or ext.get("gia_tri_den")
        if not raw and date_key == "ngay_in":
            raw = kf.get("ngay_xac_nhan") or ext.get("ngay_xac_nhan")
        date = _fmt_date(raw) if raw else ""
        if not date:
            continue
        if date_key == "ngay_het_han":
            results.append(f"HH {date}")
        elif date_key == "ngay_cap" and row_num == 8:  # LLTP: hiển thị cấp + tính hết hạn 6 tháng
            expiry = _add_months_to_date(date, 6)
            s = f"Cấp {date}"
            if expiry:
                s += f"  ·  HH {expiry}"
            results.append(s)
        elif date_key == "ngay_dao_han":
            prefix = (doc.get("ten") or "").split("-")[0].strip()
            results.append(f"{prefix} đáo hạn {date}" if prefix else f"đáo hạn {date}")
        elif date_key == "ngay_in":
            results.append(f"In {date}")
        else:
            results.append(date)
    return "  ·  ".join(results)


def _format_ghi_chu_col(docs: list[dict]) -> str:
    """Định dạng cột 'File / Ghi chú' từ danh sách docs."""
    names = []
    for doc in docs:
        name = doc.get("ten", "")
        if not name:
            continue
        kf = doc.get("key_fields") or {}
        ext = doc.get("du_lieu") or {}
        ident = (kf.get("so_passport") or kf.get("so_cccd") or kf.get("so_dinh_danh") or
                 kf.get("so_the") or kf.get("so_so") or
                 ext.get("so_passport") or ext.get("so_cccd") or ext.get("so_dinh_danh") or "")
        names.append(f"{name} ({ident})" if ident else name)
    return " + ".join(names)


def _build_main_table(dataset: list[dict], coverage: dict, rule_errors: list[dict],
                      today: str, has_marriage: bool, has_kids: bool) -> str:
    """Build bảng checklist A–H dạng HTML (deterministic, không gọi LLM)."""
    docs_by_tag: dict[str, list[dict]] = {}
    for d in dataset:
        t = d.get("loai") or ""
        if t:
            docs_by_tag.setdefault(t, []).append(d)

    errors_by_tag: dict[str, list[dict]] = {}
    for e in (rule_errors or []):
        t = e.get("tag") or ""
        if t:
            errors_by_tag.setdefault(t, []).append(e)

    matched_files: set[str] = set()
    _CELL = f"padding:4px 8px;border:1px solid {_C['bd']};font-family:Arial,sans-serif;font-size:9pt"
    _HDR  = f"background:{_C['primary']};color:#fff;font-weight:bold;{_CELL}"
    _SEC  = f"background:{_C['sec_bg']};color:{_C['primary']};font-weight:bold;{_CELL}"
    _NUM  = f"text-align:center;color:#555;{_CELL}"
    _GHI  = f"color:#444;font-size:8.5pt;{_CELL}"
    _DATE = f"color:#333;font-size:8.5pt;{_CELL}"
    _LABEL = f"color:#222;{_CELL}"

    rows: list[str] = []
    # Header row
    rows.append(
        f'<tr>'
        f'<th style="{_HDR};width:3%;text-align:center">#</th>'
        f'<th style="{_HDR};width:28%">Mục hồ sơ</th>'
        f'<th style="{_HDR};width:13%;text-align:center">Trạng thái</th>'
        f'<th style="{_HDR};width:17%">Hạn / ngày cấp</th>'
        f'<th style="{_HDR}">File / Ghi chú</th>'
        f'</tr>'
    )
    current_section = None

    for row_idx, row in enumerate(REPORT_DISPLAY_ROWS):
        sec, num, label, tags, severity, date_key = row
        if sec != current_section:
            current_section = sec
            sec_name = f"{sec} — {SECTION_NAMES.get(sec, sec)}"
            rows.append(
                f'<tr><td colspan="5" style="{_SEC}">{_e(sec_name)}</td></tr>'
            )

        ghi_chu_note = ""
        matching: list[dict] = []

        if severity == "hc_cu":
            passports = sorted(docs_by_tag.get("Passport", []),
                               key=lambda d: (d.get("key_fields") or {}).get("ngay_cap", "") or "",
                               reverse=True)
            if len(passports) < 2:
                status_label = "—"
                ghi_chu_note = "Không có HC cũ"
            else:
                matching = passports[1:]
                status_label = _row_status(matching, errors_by_tag, ["Passport"], "tuy_chon")

        elif severity == "display_cha_me":
            matching = [d for d in docs_by_tag.get("CCCD", []) if d.get("quan_he") in ("ba", "me")]
            if not matching:
                status_label = "? Cần kiểm tra"
                ghi_chu_note = "Chưa scan CCCD cha/mẹ — đối chiếu bản gốc"
            else:
                status_label = _row_status(matching, errors_by_tag, ["CCCD"], "bat_buoc")

        elif severity == "ket_hon" and not has_marriage:
            status_label = "—"
            ghi_chu_note = "Không áp dụng"

        elif severity == "co_con" and not has_kids:
            status_label = "—"
            ghi_chu_note = "Không áp dụng"

        elif severity == "lam_sau":
            matching = _find_by_tags(docs_by_tag, tags)
            if matching:
                status_label = _row_status(matching, errors_by_tag, list(tags), severity)
            else:
                status_label = "—"
                ghi_chu_note = "Sẽ làm sau"

        elif num == 1:  # HC mới: chỉ lấy passport mới nhất
            passports = sorted(docs_by_tag.get("Passport", []),
                               key=lambda d: (d.get("key_fields") or {}).get("ngay_cap", "") or "",
                               reverse=True)
            matching = passports[:1]
            status_label = (_row_status(matching, errors_by_tag, ["Passport"], severity)
                            if matching else "✗ Chưa có")
            if not matching:
                ghi_chu_note = "CHƯA CÓ — cần bổ sung gấp"

        elif num == 3:  # CCCD đương đơn (không phải cha/mẹ)
            matching = [d for d in docs_by_tag.get("CCCD", [])
                        if d.get("quan_he") not in ("ba", "me")]
            status_label = (_row_status(matching, errors_by_tag, ["CCCD"], severity)
                            if matching else "✗ Chưa có")
            if not matching:
                ghi_chu_note = "CHƯA CÓ — cần bổ sung gấp"

        elif num == 7:  # GKS con: GKS thứ 2 trở đi
            all_gks = docs_by_tag.get("GKS", [])
            matching = all_gks[1:] if len(all_gks) >= 2 else []
            if not matching and has_kids:
                status_label = "✗ Chưa có"
                ghi_chu_note = "CHƯA CÓ — cần bổ sung gấp"
            elif not matching:
                status_label = "—"
                ghi_chu_note = "Không áp dụng"
            else:
                status_label = _row_status(matching, errors_by_tag, ["GKS"], severity)

        elif num == 19:  # Sổ đỏ: chỉ tag So dat; HD cho-tang-thua ke ở row 20
            so_dat = docs_by_tag.get("So dat", [])
            hd = docs_by_tag.get("HD cho-tang-thua ke", [])
            matching = so_dat  # row 19 hiển thị So dat
            combined_ok = bool(so_dat or hd)  # FARM-11-12: either satisfies
            if combined_ok:
                status_label = _row_status(matching if matching else hd,
                                           errors_by_tag, ["So dat", "HD cho-tang-thua ke"], severity)
            else:
                status_label = "✗ Chưa có"
                ghi_chu_note = "CHƯA CÓ — cần bổ sung gấp"

        else:
            tag_list = list(tags) if not isinstance(tags, str) else [tags]
            matching = _find_by_tags(docs_by_tag, tag_list)
            if not matching:
                if severity == "bat_buoc":
                    status_label = "✗ Chưa có"
                    ghi_chu_note = "CHƯA CÓ — cần bổ sung gấp"
                elif severity == "ket_hon":
                    status_label = "—"
                    ghi_chu_note = "CHƯA CÓ" if has_marriage else "Không áp dụng"
                else:
                    status_label = "—"
                    ghi_chu_note = "Tùy chọn"
            else:
                tag_list_e = list(tags) if not isinstance(tags, str) else [tags]
                status_label = _row_status(matching, errors_by_tag, tag_list_e, severity)

        for d in matching:
            matched_files.add(d.get("ten", ""))

        date_str = _format_date_for_row(matching, date_key, num)
        ghi_chu = ghi_chu_note if ghi_chu_note else _format_ghi_chu_col(matching)
        row_bg = _C["alt"] if row_idx % 2 == 0 else "#fff"
        rows.append(
            f'<tr style="background:{row_bg}">'
            f'<td style="{_NUM}">{num}</td>'
            f'<td style="{_LABEL}">{_e(label)}</td>'
            + _status_td(status_label)
            + f'<td style="{_DATE}">{_e(date_str)}</td>'
            f'<td style="{_GHI}">{_e(ghi_chu)}</td>'
            f'</tr>'
        )

    # TÀI LIỆU BỔ SUNG: files không khớp bất kỳ dòng nào
    unmatched = [d for d in dataset if d.get("ten", "") not in matched_files]
    if unmatched:
        rows.append(
            f'<tr><td colspan="5" style="{_SEC}">TÀI LIỆU BỔ SUNG (ngoài checklist chuẩn)</td></tr>'
        )
        for d in unmatched:
            rows.append(
                f'<tr>'
                f'<td style="{_NUM}">*</td>'
                f'<td style="{_LABEL}">{_e(d.get("ten", ""))}</td>'
                + _status_td("✓ Đã có")
                + f'<td style="{_DATE}">—</td>'
                f'<td style="{_GHI};font-style:italic;color:{_C["na_fg"]}">(ngoài checklist chuẩn)</td>'
                f'</tr>'
            )

    inner = "\n".join(rows)
    return (
        f'<table style="width:100%;border-collapse:collapse;'
        f'font-family:Arial,sans-serif;font-size:9pt;margin-bottom:16px">'
        f'{inner}</table>'
    )


# ===========================================================================
# Prompt thẩm định — chỉ sinh phần NHẬN XÉT (thay thế báo cáo 4 phần cũ)
# ===========================================================================
NHAN_XET_PROMPT = """# VAI TRÒ
Chuyên viên thẩm định hồ sơ visa Canada (LMIA) của Đồng Hành — kinh nghiệm
phát hiện sai lệch nhỏ nhất giữa các giấy tờ Việt Nam.

# NHIỆM VỤ
Bot đã build sẵn bảng checklist A–H (xem {{CHECKLIST_TABLE}} ở dưới).
Dựa trên hồ sơ OCR + bảng đó, CHỈ viết phần NHẬN XÉT HỒ SƠ theo format cuối prompt.
KHÔNG tạo lại bảng. KHÔNG thêm phần khác ngoài NHẬN XÉT.

# DỮ LIỆU
- Ngày kiểm tra: {{TODAY}}
- Khách hàng: {{APPLICANT}}
- Điểm danh: {{HAVE}}/{{REQUIRED}} mục bắt buộc. {{MISSING_NOTE}}
- Địa giới hành chính hiện hành (ground-truth): {{PROVINCES}}

Bảng checklist (deterministic — đã tính sẵn, COI LÀ ĐÚNG):
{{CHECKLIST_TABLE}}

Lỗi bot phát hiện (deterministic, COI LÀ ĐÚNG — phải đưa vào ! bullets):
{{DETERMINISTIC_ERRORS}}

Rule tham chiếu:
{{RULES_BLOCK}}

# NGUYÊN TẮC BẮT BUỘC

1. **KHÔNG hallucinate**: chỉ kết luận từ dữ liệu OCR thật. `needs_review=true`/`confidence=low` → chỉ
   ghi "? cần kiểm tra bản gốc", KHÔNG kết luận lỗi cứng.
   CẤM nói "scan mờ" khi `confidence=high` và `tom_tat` có nội dung đọc được.
2. **Đối chiếu chéo**: họ tên, ngày sinh, số CCCD, cha mẹ/vợ chồng trên mọi giấy chính thức phải
   khớp ký tự với ký tự. Giấy `needs_review=true`/tự khai (`loai=CV`)/viết tay → KHÔNG dùng làm chuẩn
   để bắt lỗi giấy chính thức khác.
3. **Địa giới 2025**: Từ 12/06/2025 chỉ còn 34 đơn vị cấp tỉnh; từ 01/07/2025 xã/phường đã sáp nhập.
   Dùng `_dia_gioi` (nếu có trong hồ sơ JSON) làm ground-truth — tên cũ↔mới CÙNG nơi = KHÔNG mâu thuẫn.
   Giấy cấp SAU mốc mà ghi tên đơn vị đã sáp nhập → BÁO trong ! bullet.
4. **Vision compare** (`_vision_compare`): `same_person=false` (confidence=high) → ! nghiêm trọng;
   `age_diff_months>6` → ! cần ảnh mới. Coi là ground-truth.
5. **Photo flags** (ảnh thẻ — `du_lieu.photo_flags`): `la_mat_moc/co_trang_suc/co_xam_lo/toc_toi_mau/
   phong_nen_trang` — báo ! nếu không đạt tiêu chuẩn.
6. **Lỗi deterministic** ({{DETERMINISTIC_ERRORS}}): BẮT BUỘC đưa vào ! bullets, KHÔNG bỏ qua.
7. **Mục bắt buộc thiếu** (từ bảng): liệt kê trong ✗ bullet, đủ tên.
8. **Số CCCD cũ (9 số) vs mới (12 số)**: chuyển đổi lịch sử → BÌNH THƯỜNG, KHÔNG báo lỗi.
9. **Sổ đất cùng chủ sở hữu**: nếu có ≥2 sổ đất cùng tên chủ → đối chiếu năm sinh và số CMND/CCCD
   của từng chủ giữa các sổ. Nếu lệch → báo "! cần đối chiếu bản gốc: thông tin chủ sở hữu
   không nhất quán giữa các sổ đất (năm sinh/số CMND/CCCD khác nhau)".

# FORMAT ĐẦU RA BẮT BUỘC

Chỉ xuất đúng khối dưới đây (kể cả dấu ---), không thêm gì khác:

---

## 📋 NHẬN XÉT HỒ SƠ & ƯU TIÊN BỔ SUNG

Nhận xét tổng thể
[1 dòng: "Sẵn sàng nộp — hồ sơ đầy đủ" HOẶC "Cần bổ sung trước khi nộp — còn X mục bắt buộc chưa có" HOẶC "Cần xử lý gấp — có lỗi nghiêm trọng"]

✓  [điểm mạnh 1 — nêu cụ thể (giấy tờ nào, số tiền, diện tích…)]
✓  [điểm mạnh 2]
!  [cảnh báo 1 — kèm tên file, số liệu cụ thể, hành động đề xuất]
!  [cảnh báo 2 (nếu có)]
✗  [X mục FARM bắt buộc chưa có: tên mục 1, tên mục 2, ...]  ← chỉ xuất dòng này nếu có mục thiếu

   Ưu tiên bổ sung
1.  [Tên mục / hành động]  —  [hướng dẫn cụ thể (tên giấy cần nộp, yêu cầu đặc biệt)]
2.  [...]
(liệt kê TẤT CẢ mục cần làm theo thứ tự ưu tiên — từ bắt buộc thiếu đến cảnh báo cần xử lý)

---

VIẾT NGAY. KHÔNG HỎI THÊM."""


def _build_nhan_xet_prompt(today: str, applicant: str, coverage: dict,
                            checklist_table_md: str = "",
                            deterministic_errors: list[dict] | None = None) -> str:
    """Build system prompt cho tầng 2: chỉ sinh phần NHẬN XÉT HỒ SƠ."""
    missing_note = ("Còn thiếu (bắt buộc): " + ", ".join(coverage["missing"]) + "."
                    if coverage["missing"] else f"Đã đủ {coverage['required']} mục bắt buộc.")
    try:
        from .rule_loader import generate_rules_block
    except ImportError:
        from rule_loader import generate_rules_block  # type: ignore  # noqa
    rules_block = generate_rules_block()
    if deterministic_errors:
        det_lines = [f"- [{e['code']}] file `{e.get('ten','?')}` (tag {e.get('tag','?')}): "
                     f"{e['msg']} → {e.get('action','')}"
                     for e in deterministic_errors]
        det_block = "\n".join(det_lines)
    else:
        det_block = "(không phát hiện lỗi tự động)"
    repl = {
        "{{TODAY}}": today,
        "{{APPLICANT}}": applicant or "(không rõ tên)",
        "{{PROVINCES}}": _provinces_text(),
        "{{HAVE}}": str(coverage["have"]),
        "{{REQUIRED}}": str(coverage["required"]),
        "{{MISSING_NOTE}}": missing_note,
        "{{CHECKLIST_TABLE}}": _html_to_text(checklist_table_md) if checklist_table_md else "(chưa build được bảng checklist)",
        "{{RULES_BLOCK}}": rules_block,
        "{{DETERMINISTIC_ERRORS}}": det_block,
    }
    s = NHAN_XET_PROMPT
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
            "quan_he": d.get("quan_he", ""),  # quan hệ với đương đơn: "me"=mẹ,"ba"=bố,"con"=con,"vo"=vợ,"chong"=chồng
            "tom_tat": (d.get("tom_tat") or "")[:800],
            "du_lieu": d.get("du_lieu") or {},
            "key_fields": d.get("key_fields") or {},
            "confidence": d.get("confidence", ""),       # "high"|"medium"|"low" — độ tin cậy OCR/phân loại
            "needs_review": bool(d.get("needs_review")),  # True = scan mờ / viết tay / phân loại chưa chắc
            "ocr_quality_note": d.get("ocr_quality_note", ""),
        })
    return out


async def _call_openrouter_stream(model: str, system: str, user: str, on_chunk,
                                   timeout: int = 300, temperature: float = 0.1) -> str:
    """Gọi LLM streaming qua OpenAI, DeepSeek, hoặc OpenRouter.
    - model bắt đầu bằng "gpt-" hoặc "openai/" + OPENAI_API_KEY → api.openai.com
    - model bắt đầu bằng "deepseek/" + DEEPSEEK_API_KEY → api.deepseek.com
    - Còn lại → openrouter.ai
    Yield từng delta qua callback `on_chunk(text_delta)` (async function).
    Trả về full text cuối cùng. Caller dùng on_chunk để edit Telegram tin.

    SSE format (OpenAI-compatible):
        data: {"choices":[{"delta":{"content":"..."}}]}
        data: [DONE]

    Lỗi → raise. Caller có thể fallback _call_openrouter (non-stream)."""
    import httpx
    import json as _json
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    ollama_key = os.environ.get("WIKI_API_KEY", "")
    ollama_base = os.environ.get("WIKI_BASE_URL", "https://ollama.com/v1")
    is_openai_model = model.startswith("gpt-") or model.startswith("openai/")
    is_deepseek_model = model.startswith("deepseek/")
    is_ollama_model = model.startswith("ollama/")
    if is_ollama_model:
        api_endpoint = f"{ollama_base.rstrip('/')}/chat/completions"
        api_key = ollama_key
        direct_model = model.split("/", 1)[1]
    elif is_openai_model and openai_key:
        api_endpoint = "https://api.openai.com/v1/chat/completions"
        api_key = openai_key
        direct_model = model.removeprefix("openai/")
    elif is_deepseek_model and deepseek_key:
        api_endpoint = "https://api.deepseek.com/v1/chat/completions"
        api_key = deepseek_key
        direct_model = model.split("/", 1)[1]
    else:
        api_endpoint = "https://openrouter.ai/api/v1/chat/completions"
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        direct_model = model
        if not api_key:
            raise RuntimeError("Thiếu OPENAI_API_KEY, DEEPSEEK_API_KEY, và OPENROUTER_API_KEY")
    payload = {
        "model": direct_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
    }
    # gpt-5-* only accepts default temperature (1); skip for those models
    if not direct_model.startswith("gpt-5"):
        payload["temperature"] = temperature
    buf: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", api_endpoint,
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
                    delta_obj = choices[0].get("delta") or {}
                    content_delta = delta_obj.get("content") or ""
                    # Fix A — DeepSeek V4 reasoning model: emit `delta.reasoning` (chain-of-thought)
                    # ~30-60s TRƯỚC khi xuất `delta.content`. Trước khi fix, on_chunk không gọi
                    # trong giai đoạn reasoning → Telegram ack đứng yên cả phút, user tưởng treo.
                    # Giải pháp: callback heartbeat (delta="") cho mỗi reasoning chunk →
                    # _setup_streaming.on_chunk hiển thị "🤖 đang suy nghĩ… ⏳".
                    reasoning_delta = delta_obj.get("reasoning") or ""
                    if reasoning_delta:
                        try:
                            await on_chunk("")   # heartbeat — caller dùng làm spinner
                        except Exception:  # noqa: BLE001
                            pass
                    if content_delta:
                        buf.append(content_delta)
                        try:
                            await on_chunk(content_delta)
                        except Exception:  # noqa: BLE001 — on_chunk lỗi không phá stream
                            pass
                except _json.JSONDecodeError:
                    continue
    return _strip_fences("".join(buf))


def _call_openrouter(model: str, system: str, user: str, timeout: int = 300,
                     json_mode: bool = False, temperature: float = 0.1) -> str:
    """Gọi LLM qua OpenAI, DeepSeek, hoặc OpenRouter.
    - model bắt đầu bằng "gpt-" hoặc "openai/" + OPENAI_API_KEY → api.openai.com
    - model bắt đầu bằng "deepseek/" + DEEPSEEK_API_KEY → api.deepseek.com
    - Còn lại → openrouter.ai
    json_mode=True → thêm response_format json_object; retry không kèm nếu HTTP ≥400."""
    import httpx
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    ollama_key = os.environ.get("WIKI_API_KEY", "")
    ollama_base = os.environ.get("WIKI_BASE_URL", "https://ollama.com/v1")
    is_openai_model = model.startswith("gpt-") or model.startswith("openai/")
    is_deepseek_model = model.startswith("deepseek/")
    is_ollama_model = model.startswith("ollama/")
    if is_ollama_model:
        api_endpoint = f"{ollama_base.rstrip('/')}/chat/completions"
        api_key = ollama_key
        direct_model = model.split("/", 1)[1]
    elif is_openai_model and openai_key:
        api_endpoint = "https://api.openai.com/v1/chat/completions"
        api_key = openai_key
        direct_model = model.removeprefix("openai/")
    elif is_deepseek_model and deepseek_key:
        api_endpoint = "https://api.deepseek.com/v1/chat/completions"
        api_key = deepseek_key
        direct_model = model.split("/", 1)[1]
    else:
        api_endpoint = "https://openrouter.ai/api/v1/chat/completions"
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        direct_model = model
        if not api_key:
            raise RuntimeError("Thiếu OPENAI_API_KEY, DEEPSEEK_API_KEY, và OPENROUTER_API_KEY")
    payload = {
        "model": direct_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # gpt-5-* only accepts default temperature (1); skip for those models
    if not direct_model.startswith("gpt-5"):
        payload["temperature"] = temperature
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(api_endpoint, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
        if json_mode and resp.status_code >= 400:
            payload.pop("response_format", None)
            resp = client.post(api_endpoint, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _strip_fences(content or "")


# ===========================================================================
# TẦNG 1 — trích xuất & chuẩn hoá hồ sơ (model rẻ) → 1 JSON cô đọng
# ===========================================================================
_PROFILE_EXTRACT_SYSTEM = """Bạn là trợ lý trích xuất & chuẩn hoá hồ sơ visa Canada (LMIA).
Đầu vào (message kế tiếp): JSON liệt kê các giấy tờ đã OCR — mỗi phần tử có `ten`, `loai`,
`nguoi`, `quan_he` (quan hệ của người trên giấy với đương đơn: "me"=mẹ, "ba"=bố, "con"=con, "vo"=vợ, "chong"=chồng, ""=chính đương đơn),
`tom_tat`, `du_lieu`, `key_fields`, `confidence` ("high"|"medium"|"low") và `needs_review` (true = scan mờ /
viết tay / phân loại chưa chắc — chỉ copy giá trị, KHÔNG dùng làm chuẩn).

NHIỆM VỤ: gom toàn bộ dữ liệu thành MỘT JSON object hồ sơ thống nhất, theo schema dưới.

QUY TẮC TUYỆT ĐỐI:
- GIỮ NGUYÊN VĂN mọi giá trị (họ tên, ngày, số CMND/CCCD, địa chỉ, tên cha/mẹ/vợ/chồng, tên công ty,
  số tiền…) — copy chính xác từng ký tự, KHÔNG tóm tắt, KHÔNG diễn giải, KHÔNG tự "sửa" cho đẹp.
- Nếu một thông tin xuất hiện khác nhau ở các giấy → giữ CẢ HAI dạng và ghi vào `notes` (vd "tên chồng:
  LLTP='Nguyễn Bá Thắng' vs CT07='Nguyễn Bá Thẳng' (dấu hỏi)").
- `criminal_record.issue_date` = NGÀY CẤP ghi trên chính tờ Lý lịch tư pháp — lấy từ file loai="LLTP",
  KHÔNG phải ngày cấp CCCD, passport hay giấy tờ khác. Nếu đọc được ngày trên tờ LLTP thì điền vào đây;
  nếu không đọc được → để "".
- Giấy nào `needs_review=true` / `confidence`="low" / là tờ TỰ KHAI (`loai`="CV", hoặc tiêu đề kiểu "Thông tin cá
  nhân / gia đình (tự khai)") → vẫn copy giá trị nhưng GHI RÕ trong `notes`: "(OCR thấp tin cậy / tự khai — cần đối
  chiếu bản gốc)". TUYỆT ĐỐI không coi giấy đó là nguồn chuẩn cho họ tên / số giấy tờ / địa chỉ khi nó lệch với một
  giấy CHÍNH THỨC do cơ quan cấp (CCCD thật, hộ chiếu, khai sinh, LLTP, CT07…). Một tờ tự khai/viết tay KHÔNG phải
  CCCD/giấy chính thức kể cả khi nó có ghi số CCCD.
- Nếu `du_lieu` trống nhưng `tom_tat` có nội dung OCR chi tiết thì PHẢI đọc và copy từ `tom_tat`; không được tự kết luận file mờ.
- Chỉ ghi scan mờ/OCR không đọc được khi `needs_review=true`, `confidence`="low", hoặc `tom_tat` quá ngắn/không có dữ liệu. Nếu thiếu structured field nhưng summary đọc được, ghi "chưa bóc được trường cấu trúc".
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
                           dataset: list[dict] | None = None,
                           case_profile: dict | None = None,
                           checklist_table_md: str = "") -> dict:
    """TẦNG 2: đọc hồ sơ đã chuẩn hoá + bảng checklist → sinh NHẬN XÉT HỒ SƠ (markdown).
    Trả {report_text, model_used, n_docs} hoặc {report_text:None, error}.

    `dataset`: thô từ build_dataset() — cho rule_engine chạy deterministic per-doc.
    `case_profile` (P3.3): cross-doc profile từ build_case_profile() — cho rule 2.4 + 5.3.
    `checklist_table_md`: bảng A–H đã build deterministic — nhúng vào system prompt.
    """
    model = model or CHECKLIST_MODEL
    if n_docs is None:
        n_docs = len(profile) if isinstance(profile, list) else len((profile or {}).get("documents") or [])
    # Pre-check deterministic — chạy mọi rule có condition trong rules.yaml.
    det_errors: list[dict] = []
    try:
        try:
            from .rule_loader import load_validations
            from .rule_engine import detect_deterministic_errors
        except ImportError:
            from rule_loader import load_validations          # type: ignore  # noqa
            from rule_engine import detect_deterministic_errors  # type: ignore  # noqa
        eval_dataset = dataset if isinstance(dataset, list) else (
            profile if isinstance(profile, list) else (profile or {}).get("documents") or []
        )
        if eval_dataset or case_profile:
            det_errors = detect_deterministic_errors(list(load_validations()), eval_dataset or [],
                                                     profile=case_profile)
            if det_errors:
                print(f"evaluate_profile_logic: deterministic check phát hiện {len(det_errors)} lỗi "
                      f"({', '.join(e['code'] for e in det_errors)})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"evaluate_profile_logic: rule_engine lỗi: {type(e).__name__}: {e} — bỏ qua deterministic",
              flush=True)
    system = _build_nhan_xet_prompt(today, applicant, coverage,
                                     checklist_table_md=checklist_table_md,
                                     deterministic_errors=det_errors)
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
def _nhan_xet_html(text: str) -> str:
    """Parse LLM NHẬN XÉT text → HTML 2 cột (tổng thể | ưu tiên bổ sung)."""
    import re as _re
    text = (text or "").strip()
    # Tách tại dòng "Ưu tiên bổ sung"
    split_pat = _re.compile(r"(ưu tiên bổ sung)", _re.IGNORECASE)
    parts = split_pat.split(text, maxsplit=1)
    left_raw = parts[0]
    right_raw = (parts[1] + parts[2]) if len(parts) == 3 else ""

    def _render_left(raw: str) -> str:
        lines = raw.splitlines()
        out: list[str] = []
        verdict_done = False
        for ln in lines:
            ln = ln.strip()
            if not ln or "NHẬN XÉT HỒ SƠ" in ln or "Nhận xét tổng thể" in ln.replace("​", ""):
                if "Nhận xét tổng thể" in ln:
                    out.append(f'<p style="font-weight:bold;color:{_C["primary"]};margin:0 0 6px 0">'
                                f'Nhận xét tổng thể</p>')
                continue
            if ln.startswith("✓"):
                out.append(f'<p style="color:{_C["ok_fg"]};margin:2px 0">{_e(ln)}</p>')
            elif ln.startswith("✗"):
                out.append(f'<p style="color:{_C["miss_fg"]};margin:2px 0">{_e(ln)}</p>')
            elif ln.startswith("!"):
                out.append(f'<p style="color:{_C["warn_fg"]};margin:2px 0">{_e(ln)}</p>')
            elif not verdict_done:
                # first non-empty line after header = verdict
                verdict_done = True
                out.append(f'<p style="font-weight:bold;color:{_C["miss_fg"]};margin:0 0 8px 0">'
                            f'{_e(ln)}</p>')
            else:
                out.append(f'<p style="margin:2px 0">{_e(ln)}</p>')
        return "".join(out)

    def _render_right(raw: str) -> str:
        lines = raw.splitlines()
        out: list[str] = []
        item_pat = _re.compile(r"^(\d+)[.\)]?\s+(.+)")
        for ln in lines:
            ln = ln.strip()
            if not ln or _re.match(r"ưu tiên bổ sung", ln, _re.IGNORECASE):
                continue
            m = item_pat.match(ln)
            if m:
                num_s = m.group(1)
                body = m.group(2)
                # bold tên mục, italic mô tả sau " — "
                if " — " in body:
                    item_name, desc = body.split(" — ", 1)
                    body_html = (f'<b>{_e(item_name.strip())}</b>'
                                 f'<span style="color:#555;font-style:italic"> — {_e(desc.strip())}</span>')
                else:
                    body_html = f'<b>{_e(body)}</b>'
                out.append(f'<p style="margin:3px 0"><span style="color:{_C["primary"]};'
                            f'font-weight:bold">{_e(num_s)}.</span> {body_html}</p>')
            else:
                out.append(f'<p style="margin:2px 0;font-size:8.5pt;color:#555">{_e(ln)}</p>')
        return "".join(out)

    banner_style = (f"background:{_C['primary']};color:#fff;font-family:Arial,sans-serif;"
                    f"font-weight:bold;font-size:11pt;padding:7px 12px;margin:16px 0 0 0")
    left_html  = _render_left(left_raw)
    right_html = _render_right(right_raw)
    cell_style = (f"width:50%;vertical-align:top;padding:10px 14px;"
                  f"border:1px solid {_C['bd']};font-family:Arial,sans-serif;font-size:9pt")
    return (
        f'<div style="{banner_style}">📋 NHẬN XÉT HỒ SƠ &amp; ƯU TIÊN BỔ SUNG</div>'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:16px">'
        f'<tr>'
        f'<td style="{cell_style}">'
        f'<p style="font-weight:bold;color:{_C["primary"]};margin:0 0 8px 0">Nhận xét tổng thể</p>'
        f'{left_html}</td>'
        f'<td style="{cell_style};border-left:none">'
        f'<p style="font-weight:bold;color:{_C["primary"]};margin:0 0 8px 0">Ưu tiên bổ sung</p>'
        f'{right_html}</td>'
        f'</tr></table>'
    )


def render_doc_md(nhan_xet_text: str, applicant: str, today: str, model: str,
                  coverage: dict, dataset: list[dict],
                  checklist_table_md: str = "", birth_year: str = "") -> str:
    """Sinh báo cáo HTML đầy đủ: header + legend + bảng A–H + nhận xét 2 cột + phụ lục."""
    n = len(dataset)
    by_part = f" — {birth_year}" if birth_year else ""
    P = f"font-family:Arial,sans-serif"

    # ── Page-level header (company name + subtitle) ───────────────────────────
    doc_header = (
        f'<table style="width:100%;border-collapse:collapse;{P};margin-bottom:4px">'
        f'<tr style="background:{_C["primary"]};color:#fff">'
        f'<td style="padding:5px 10px;font-size:8.5pt;font-weight:bold">'
        f'Donghanh Investment and Immigration Consultation</td>'
        f'<td style="padding:5px 10px;font-size:8.5pt;text-align:right">'
        f'Checklist &middot; {_e(applicant)}</td>'
        f'</tr></table>'
    )

    # ── Title banner ─────────────────────────────────────────────────────────
    title_banner = (
        f'<div style="background:{_C["primary"]};color:#fff;{P};'
        f'font-size:12pt;font-weight:bold;padding:8px 12px;margin-bottom:8px">'
        f'CHECKLIST HỒ SƠ &nbsp;{_e(applicant)}{_e(by_part)}'
        f'&nbsp; &middot; &nbsp;Ngày kiểm tra: <b>{_e(today)}</b>'
        f'&nbsp; &middot; &nbsp;Tài liệu rà soát: <b>{n}</b>'
        f'</div>'
    )

    # ── Legend ────────────────────────────────────────────────────────────────
    legend = _legend_html()

    # ── Bảng A–H (HTML đã build) ──────────────────────────────────────────────
    table_html = checklist_table_md or f'<p style="color:red">(bảng checklist chưa build được)</p>'

    # ── NHẬN XÉT (LLM, 2 cột) ────────────────────────────────────────────────
    nhan_xet = _nhan_xet_html(nhan_xet_text)

    # ── PHỤ LỤC ──────────────────────────────────────────────────────────────
    _HDR = f"background:{_C['primary']};color:#fff;font-weight:bold;padding:4px 8px;border:1px solid {_C['bd']};{P};font-size:9pt"
    _ROW = f"padding:4px 8px;border:1px solid {_C['bd']};{P};font-size:8.5pt"
    appendix_rows = [
        f'<tr style="background:{_C["primary"]};color:#fff">'
        f'<th style="{_HDR};width:3%;text-align:center">#</th>'
        f'<th style="{_HDR};width:22%">Tên file</th>'
        f'<th style="{_HDR};width:9%">Loại</th>'
        f'<th style="{_HDR};width:12%">Người</th>'
        f'<th style="{_HDR}">Tóm tắt</th>'
        f'</tr>'
    ]
    for i, d in enumerate(dataset, 1):
        tt = (d.get("tom_tat") or "").replace("\n", " ")[:220]
        row_bg = _C["alt"] if i % 2 == 0 else "#fff"
        appendix_rows.append(
            f'<tr style="background:{row_bg}">'
            f'<td style="{_ROW};text-align:center;color:#555">{i}</td>'
            f'<td style="{_ROW};font-weight:bold">{_e(d.get("ten", ""))}</td>'
            f'<td style="{_ROW}">{_e(d.get("loai", ""))}</td>'
            f'<td style="{_ROW}">{_e(d.get("nguoi", ""))}</td>'
            f'<td style="{_ROW};font-style:italic;color:#444">{_e(tt)}</td>'
            f'</tr>'
        )
    appendix_banner = (
        f'<div style="background:{_C["primary"]};color:#fff;{P};'
        f'font-size:11pt;font-weight:bold;padding:7px 12px;margin:16px 0 0 0">'
        f'&#128273; PHỤ LỤC — DANH SÁCH FILE ĐÃ OCR</div>'
    )
    appendix = (
        appendix_banner
        + f'<table style="width:100%;border-collapse:collapse;margin-bottom:16px">'
        + "\n".join(appendix_rows)
        + f'</table>'
    )

    # ── Footer ────────────────────────────────────────────────────────────────
    footer = (
        f'<p style="{P};font-size:8pt;color:#888;text-align:center;'
        f'border-top:1px solid {_C["bd"]};padding-top:6px;margin-top:4px">'
        f'Báo cáo do bot tạo tự động &middot; {_e(today)} &middot; {n} tài liệu '
        f'&middot; Nhân viên đối chiếu bản gốc trước khi nộp.</p>'
    )

    body = "\n".join([doc_header, title_banner, legend, table_html, nhan_xet, appendix, footer])
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<style>body{margin:20px;font-family:Arial,sans-serif}</style>'
        f'</head><body>{body}</body></html>'
    )


def _write_google_doc(case_folder_id: str, name: str, md_text: str, drive_id: str | None) -> str:
    """Tạo (hoặc ghi đè) Google Doc tên `name` ở case folder từ nội dung markdown.
    Google Drive tự convert text/markdown → Google Doc khi mimeType đích là Google Doc.
    Trả về webViewLink (hoặc URL dựng từ id)."""
    import tempfile
    from googleapiclient.http import MediaFileUpload
    from .google_clients import drive
    from .drive_helpers import find_file_by_name
    DOC_MIME = "application/vnd.google-apps.document"
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
        fh.write(md_text)
        mpath = fh.name
    try:
        existing = find_file_by_name(name, case_folder_id, drive_id, mime_type=DOC_MIME)
        media = MediaFileUpload(mpath, mimetype="text/html", resumable=False)
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
# ===========================================================================
# P3.2 — build_case_profile: aggregator cross-doc identities cho thẩm định + rule_engine
# ===========================================================================
def _name_norm(s: object) -> str:
    """Normalize tên VN để so sánh: bỏ dấu, lowercase, trim, dồn space."""
    if not isinstance(s, str) or not s:
        return ""
    try:
        from .sop_naming import strip_diacritics
    except ImportError:
        from sop_naming import strip_diacritics  # type: ignore  # noqa
    return re.sub(r"\s+", " ", strip_diacritics(s).lower().strip())


def build_case_profile(dataset: list[dict]) -> dict:
    """Aggregate identities chéo các giấy tờ trong case → 1 profile để inject vào LLM tầng 2
    HOẶC rule_engine deterministic (xem P3.3 — parent_dob_mismatch, children_missing_from_xnct).

    Trả:
      {
        "applicant": {"name", "dob_candidates":[(value, source)], "cccd": "", "evidence":[doc_names]},
        "parents":   {"cha": {"name", "dob_candidates":[(value, source)]},
                      "me":  {"name", "dob_candidates":[(value, source)]}},
        "spouse":    {"name", "dob", "evidence":[]},
        "children":  [{"name", "dob", "evidence":[]}],
        "residence_members": [{"ho_ten","ngay_sinh","quan_he"}]   # từ XNCT
      }
    """
    profile: dict = {
        "applicant": {"name": "", "dob_candidates": [], "cccd": "", "evidence": []},
        "parents":   {"cha": {"name": "", "dob_candidates": []},
                      "me":  {"name": "", "dob_candidates": []}},
        "spouse":    {"name": "", "dob": "", "evidence": []},
        "children":  [],
        "residence_members": [],
    }
    # Helper: gom dob candidate (deduped, giữ thứ tự).
    def _add_dob(target: list, value: str, source: str):
        if not value or not source:
            return
        for v, _ in target:
            if v == value:
                return
        target.append((value, source))

    children_by_norm: dict = {}   # name_norm → child dict

    for d in dataset:
        loai = d.get("loai", "")
        du = d.get("du_lieu") if isinstance(d.get("du_lieu"), dict) else {}
        doc_name = d.get("ten", "")
        nguoi = d.get("nguoi", "")

        # === Applicant identity (CCCD/Passport/LLTP của đương đơn — best heuristic: subject==applicant) ===
        if loai in ("CCCD", "Passport", "LLTP") and nguoi:
            if not profile["applicant"]["name"]:
                profile["applicant"]["name"] = nguoi
            profile["applicant"]["evidence"].append(doc_name)
            if du.get("so_giay_to") and loai == "CCCD":
                profile["applicant"]["cccd"] = profile["applicant"]["cccd"] or str(du.get("so_giay_to"))
            if du.get("ngay_sinh"):
                _add_dob(profile["applicant"]["dob_candidates"], str(du["ngay_sinh"]), doc_name)

        # === Parents (cha/mẹ) — extracted từ MỌI loại giấy có ho_ten_cha/me ===
        ho_cha = du.get("ho_ten_cha") or ""
        ho_me = du.get("ho_ten_me") or ""
        if ho_cha:
            if not profile["parents"]["cha"]["name"]:
                profile["parents"]["cha"]["name"] = ho_cha
            # DOB candidates: ưu tiên ngay_sinh_cha (DD/MM/YYYY), fallback nam_sinh_cha (chỉ năm)
            full_dob = du.get("ngay_sinh_cha")
            year_only = du.get("nam_sinh_cha")
            if full_dob:
                _add_dob(profile["parents"]["cha"]["dob_candidates"], str(full_dob), doc_name)
            elif year_only:
                _add_dob(profile["parents"]["cha"]["dob_candidates"], str(year_only), doc_name)
        if ho_me:
            if not profile["parents"]["me"]["name"]:
                profile["parents"]["me"]["name"] = ho_me
            full_dob = du.get("ngay_sinh_me")
            year_only = du.get("nam_sinh_me")
            if full_dob:
                _add_dob(profile["parents"]["me"]["dob_candidates"], str(full_dob), doc_name)
            elif year_only:
                _add_dob(profile["parents"]["me"]["dob_candidates"], str(year_only), doc_name)

        # === Spouse — từ GKH (giấy kết hôn) ===
        if loai == "GKH":
            spouse_name = du.get("ho_ten_vo_chong") or ""
            if spouse_name and not profile["spouse"]["name"]:
                profile["spouse"]["name"] = spouse_name
                profile["spouse"]["evidence"].append(doc_name)
            # Đồng thời extracted có thể có ngay_sinh_vo_chong (nếu Gemini fill)
            if du.get("ngay_sinh_vo_chong"):
                profile["spouse"]["dob"] = profile["spouse"]["dob"] or str(du["ngay_sinh_vo_chong"])

        # === Children — từ GKS con (subject ≠ applicant + có ho_ten_cha/me match applicant) + từ XN hoc ===
        if loai == "GKS":
            child_name = du.get("ho_ten") or nguoi
            child_dob = du.get("ngay_sinh") or ""
            cn = _name_norm(child_name)
            if cn and cn not in children_by_norm:
                # Loại trừ trường hợp GKS đương đơn (đã hint qua loai="GKS" cho applicant)
                # bằng cách so applicant name (nếu đã set).
                appl_norm = _name_norm(profile["applicant"]["name"])
                if not appl_norm or cn != appl_norm:
                    entry = {"name": child_name, "dob": child_dob, "evidence": [doc_name]}
                    children_by_norm[cn] = entry
                    profile["children"].append(entry)
        elif loai == "XN hoc":
            # XN hoc thường có subject là tên con, ngày sinh trong extracted
            child_name = nguoi or du.get("ho_ten") or ""
            child_dob = du.get("ngay_sinh") or ""
            cn = _name_norm(child_name)
            if cn and cn not in children_by_norm:
                entry = {"name": child_name, "dob": child_dob, "evidence": [doc_name]}
                children_by_norm[cn] = entry
                profile["children"].append(entry)
            elif cn:
                # Đã có → gắn doc làm evidence
                children_by_norm[cn]["evidence"].append(doc_name)
                if not children_by_norm[cn].get("dob") and child_dob:
                    children_by_norm[cn]["dob"] = child_dob

        # === Residence members — từ XNCT extracted.thanh_vien_ho_khau ===
        if loai == "XNCT":
            members = du.get("thanh_vien_ho_khau")
            if isinstance(members, list):
                for mem in members:
                    if isinstance(mem, dict) and mem.get("ho_ten"):
                        profile["residence_members"].append({
                            "ho_ten": mem.get("ho_ten") or "",
                            "ngay_sinh": mem.get("ngay_sinh") or "",
                            "quan_he": mem.get("quan_he_voi_chu_ho") or "",
                        })
    return profile


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

    # --- P3.2: cross-doc profile (parent dob candidates, children, XNCT members) → ground truth ---
    try:
        _case_profile = build_case_profile(dataset)
        if _case_profile:
            if isinstance(eval_input, dict):
                eval_input["_doi_chieu_cheo"] = _case_profile
            else:
                eval_input = list(eval_input) + [{"_doi_chieu_cheo": _case_profile}]
    except Exception as e:  # noqa: BLE001
        print(f"checklist: build_case_profile lỗi — bỏ qua: {type(e).__name__}: {e}", flush=True)
        _case_profile = None

    # --- Build bảng checklist A–H (deterministic) — trước khi gọi LLM ---
    _det_for_table: list[dict] = []
    try:
        try:
            from .rule_loader import load_validations
            from .rule_engine import detect_deterministic_errors
        except ImportError:
            from rule_loader import load_validations  # type: ignore  # noqa
            from rule_engine import detect_deterministic_errors  # type: ignore  # noqa
        _det_for_table = detect_deterministic_errors(list(load_validations()), dataset,
                                                      profile=_case_profile)
    except Exception as e:  # noqa: BLE001
        print(f"checklist: det_errors for table lỗi: {type(e).__name__}: {e}", flush=True)
    _has_marriage = coverage.get("has_marriage", False)
    _has_kids = (coverage.get("n_gks", 0) >= 2) or ("XN hoc" in coverage.get("tags_present", []))
    checklist_table_md = _build_main_table(dataset, coverage, _det_for_table, today,
                                            _has_marriage, _has_kids)

    # --- Tầng 2: sinh NHẬN XÉT HỒ SƠ (model reasoning) ---
    res = evaluate_profile_logic(eval_input, applicant, today, coverage, model=model, n_docs=n_docs,
                                 dataset=dataset, case_profile=_case_profile,
                                 checklist_table_md=checklist_table_md)
    if not res.get("report_text"):
        return {"ran": False, "error": res.get("error") or "evaluate_profile_logic không trả về báo cáo",
                "coverage": coverage, "model": res.get("model_used"), "extract_model": extract_model,
                "n_docs": n_docs}
    report_text = res["report_text"]
    model_used = res["model_used"]

    # Trích birth_year từ profile để hiển thị trong header
    birth_year = ""
    if isinstance(profile_out, dict):
        dob = (profile_out.get("personal_info") or {}).get("dob", "")
        m_yr = re.search(r"(\d{4})", dob) if dob else None
        if m_yr:
            birth_year = m_yr.group(1)

    doc_name = f"Bao cao tham dinh - {title_case_ascii(applicant) or 'Unknown'}"
    full_md = render_doc_md(report_text, applicant, today, model_used, coverage, dataset,
                             checklist_table_md=checklist_table_md, birth_year=birth_year)

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


def run_from_md_contents(
    md_contents: list[str],
    case_folder_id: str,
    applicant: str,
    today: str,
    dataset: list[dict],
    drive_id: str | None = None,
    vision_compare: list | None = None,
) -> dict:
    """Fresh run: dùng md_content in-memory từ per-doc classify → tầng 2 thẩm định.

    Bỏ qua tầng 1 extract_profile_data (md_content đã là nội dung trích xuất).
    Dùng cho batch Telegram / /oldfile. /check dùng run_and_write() (đọc Drive sidecars).
    """
    try:
        from .sop_naming import title_case_ascii
    except Exception:
        def title_case_ascii(s):
            return (s or "").strip() or "Unknown"
    today = today or time.strftime("%d/%m/%Y")
    coverage = compute_coverage(dataset)
    n_docs = len(md_contents)
    if not md_contents:
        return {"ran": False, "error": "không có md_content nào", "coverage": coverage}

    # Vision compare — caller thường đã pass kết quả; fallback download từ Drive nếu cần.
    if not vision_compare:
        try:
            try:
                from .vision_check import compare_pairs_for_case
            except ImportError:
                from vision_check import compare_pairs_for_case  # type: ignore  # noqa
            vision_compare = compare_pairs_for_case(case_folder_id, dataset, drive_id=drive_id)
            if vision_compare:
                print(f"checklist: vision_compare case-level ({len(vision_compare)} pairs)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"checklist: vision_compare case-level lỗi: {type(e).__name__}: {e}", flush=True)

    vc_block = ""
    if vision_compare:
        lines = ["## Kết quả so sánh ảnh (vision compare):"]
        for vc in vision_compare:
            r = vc.get("result") or {}
            lines.append(
                f"- {vc.get('file_a','?')} × {vc.get('file_b','?')}: "
                f"same_person={r.get('same_person')} confidence={r.get('confidence')} "
                f"phau_thuat={r.get('phau_thuat_signs')}"
            )
        vc_block = "\n".join(lines) + "\n\n"

    # Deterministic pre-checks (rule_engine — chạy TRƯỚC LLM).
    det_errors: list[dict] = []
    try:
        try:
            from .rule_loader import load_validations
            from .rule_engine import detect_deterministic_errors
        except ImportError:
            from rule_loader import load_validations  # type: ignore  # noqa
            from rule_engine import detect_deterministic_errors  # type: ignore  # noqa
        det_errors = detect_deterministic_errors(list(load_validations()), dataset)
        if det_errors:
            print(f"run_from_md_contents: deterministic {len(det_errors)} lỗi "
                  f"({', '.join(e['code'] for e in det_errors)})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"run_from_md_contents: rule_engine lỗi: {type(e).__name__}: {e}", flush=True)

    # Build bảng checklist A–H (deterministic) — trước khi gọi LLM
    _has_marriage = coverage.get("has_marriage", False)
    _has_kids = (coverage.get("n_gks", 0) >= 2) or ("XN hoc" in coverage.get("tags_present", []))
    checklist_table_md = _build_main_table(dataset, coverage, det_errors, today,
                                            _has_marriage, _has_kids)

    system = _build_nhan_xet_prompt(today, applicant, coverage,
                                     checklist_table_md=checklist_table_md,
                                     deterministic_errors=det_errors)
    content_block = "\n\n---\n\n".join(
        f"### Giấy tờ {i + 1}\n{mc}" for i, mc in enumerate(md_contents) if mc.strip()
    )
    user = (
        f"KHÁCH HÀNG: {applicant or '(không rõ tên)'}\n"
        f"Ngày kiểm tra: {today}\n"
        f"Số giấy tờ trong hồ sơ: {n_docs}\n\n"
        f"{vc_block}"
        f"NỘI DUNG HỒ SƠ (mỗi giấy tờ 1 block markdown — trích xuất từ OCR + vision):\n\n"
        f"{content_block}"
    )

    model = CHECKLIST_MODEL
    candidates = [model] + ([CHECKLIST_FALLBACK_MODEL] if model != CHECKLIST_FALLBACK_MODEL else [])
    report_text = None
    model_used = None
    last_err = None
    for attempt, mdl in enumerate(candidates, 1):
        try:
            text = _call_openrouter(mdl, system, user)
            if text.strip():
                report_text = text.strip()
                model_used = mdl
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"run_from_md_contents: attempt {attempt} model={mdl} failed: {type(e).__name__}: {e}", flush=True)

    if not report_text:
        return {"ran": False,
                "error": f"{type(last_err).__name__}: {last_err}" if last_err else "LLM trả về rỗng",
                "coverage": coverage, "model": model_used}

    doc_name = f"Bao cao tham dinh - {title_case_ascii(applicant) or 'Unknown'}"
    full_md = render_doc_md(report_text, applicant, today, model_used, coverage, dataset,
                             checklist_table_md=checklist_table_md)
    report_link = ""
    try:
        report_link = _write_google_doc(case_folder_id, doc_name, full_md, drive_id)
    except Exception as e:  # noqa: BLE001
        print(f"run_from_md_contents: ghi Google Doc thất bại: {type(e).__name__}: {e}", flush=True)

    return {"ran": True, "model": model_used, "extract_model": None, "n_docs": n_docs,
            "coverage": coverage, "report_link": report_link, "doc_link": report_link,
            "md_link": report_link, "sheet_link": "", "report": report_text, "report_text": report_text,
            "profile": None, "error": None}


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
    assert len(REPORT_DISPLAY_ROWS) == 29
    assert {"Passport", "Sao ke", "Anh gia dinh", "Dai ly NS", "So dat NN", "The Visa-MC"} <= CHECKLIST_DOC_TAGS
    assert "GKS_con" not in CHECKLIST_DOC_TAGS
    ds = [{"loai": "CCCD", "ten": "CCCD-Test.pdf", "nguoi": "Test", "quan_he": "", "tom_tat": "x", "du_lieu": {}, "key_fields": {}, "needs_review": False},
          {"loai": "Passport", "ten": "Passport-Test.pdf", "nguoi": "Test", "quan_he": "", "tom_tat": "y", "du_lieu": {}, "key_fields": {}, "needs_review": False}]
    cov = compute_coverage(ds)
    assert cov["required"] == 18 and cov["have"] == 2
    _i3 = next(i for i in cov["items"] if i["loai"].startswith("3."))
    assert _i3["applicable"] is False and "không áp dụng" in _i3["status"]
    cov2 = compute_coverage([{"loai": "GKH", "ten": "GKH-x.pdf", "nguoi": "x"}])
    _i3b = next(i for i in cov2["items"] if i["loai"].startswith("3."))
    assert _i3b["applicable"] is True and _i3b["present"] is True
    print("coverage:", cov["have"], "/", cov["required"], "| missing:", len(cov["missing"]), "mục")
    # Test bảng checklist A–H
    tbl = _build_main_table(ds, cov, [], "19/05/2026", False, False)
    assert "A — GIẤY TỜ TÙY THÂN" in tbl
    assert "✗ Chưa có" in tbl      # nhiều mục bắt buộc thiếu với ds nhỏ
    assert "✓ Đã có" in tbl        # CCCD + Passport có
    assert "—" in tbl              # mục ket_hon/co_con không áp dụng
    print("checklist table rows:", tbl.count("\n"))
    # Test prompt NHẬN XÉT
    p = _build_nhan_xet_prompt("19/05/2026", "Nguyen Van Test", cov, checklist_table_md=tbl)
    assert "VAI TRÒ" in p and "NHẬN XÉT HỒ SƠ" in p and "19/05/2026" in p and "Nguyen Van Test" in p
    assert "{{" not in p  # không còn placeholder chưa fill
    print("prompt len:", len(p))
    _t = _trim_dataset_for_llm([{"loai": "CV", "ten": "x", "needs_review": True, "confidence": "low",
                                 "du_lieu": {}, "key_fields": {}, "tom_tat": "t"}])
    assert _t[0].get("needs_review") is True and _t[0].get("confidence") == "low"
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
    md = render_doc_md("## 📋 NHẬN XÉT HỒ SƠ & ƯU TIÊN BỔ SUNG\nNhận xét tổng thể\nSẵn sàng nộp",
                      "Test", "12/05/2026", "test-model", cov, ds,
                      checklist_table_md=tbl, birth_year="1990")
    assert "CHECKLIST HỒ SƠ" in md and "PHỤ LỤC" in md and "NHẬN XÉT HỒ SƠ" in md
    print("render_doc_md len:", len(md))
    l1, det = summarize_for_telegram("## 📌 PHẦN 4: TÓM TẮT & KHUYẾN NGHỊ\n- **Tình trạng tổng thể:** ✅ Sẵn sàng nộp\n- **Số lỗi nghiêm trọng:** 0",
                                     cov, "test-model", "http://x/doc")
    assert det and "Đã thẩm định" in det and 'href="http://x/doc"' in det and "PHẦN 4" not in det
    l1b, detb = summarize_for_telegram("", cov, "test-model", "")
    assert detb and "Đã thẩm định" in detb and "<a " not in detb
    print("telegram l1:", l1)
    print("telegram detail:", det)
    print("OK")
