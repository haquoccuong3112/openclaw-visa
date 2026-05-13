"""SOP §10 + §27 compliant naming + folder classifier.

Maps Gemini OCR output → standardized filename + 1 of 4 top-level folders
per Cường's SOP (Personal Docs / Education / Asset / Employment).

No subfolders are created — files go directly into the top-level folder.
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional


# ============================================================
# 1. doc_type (free-text VN from Gemini) → SOP tag (no diacritics)
# ============================================================
# SOP §10.6 abbreviations + extensions
# Order matters: more specific / multi-word patterns FIRST.
def _load_doc_types_from_yaml():
    """Load doc_types từ data/doc_types.yaml và build DOC_TYPE_PATTERNS + FILENAME_HINTS.
    Robust import — work cả khi sop_naming.py chạy standalone lẫn import như package."""
    try:
        from .rule_loader import load_doc_types
    except ImportError:
        from rule_loader import load_doc_types  # type: ignore  # noqa
    dt_pats: list[tuple[str, str, str]] = []
    fn_hints: list[tuple[str, str, str]] = []
    for dt in load_doc_types():
        if dt.doc_type_patterns:
            # Gom mọi pattern của 1 tag thành 1 regex `a|b|c` để khớp DOC_TYPE_PATTERNS cũ.
            combined = "|".join(dt.doc_type_patterns)
            dt_pats.append((combined, dt.tag, dt.folder))
        for p in dt.filename_patterns:
            fn_hints.append((p, dt.tag, dt.folder))
    return dt_pats, fn_hints

DOC_TYPE_PATTERNS, _FILENAME_HINTS_FROM_YAML = _load_doc_types_from_yaml()

# Default fallback when nothing matches
DEFAULT_TAG = "Khac"
DEFAULT_FOLDER = "Personal Docs"


# ============================================================
# 2. Vietnamese → ASCII (no diacritics, no special chars)
# ============================================================
def strip_diacritics(s: str) -> str:
    """Remove Vietnamese diacritics + đ/Đ → d/D."""
    if not s:
        return ""
    s = s.replace("đ", "d").replace("Đ", "D")
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def title_case_ascii(s: str) -> str:
    """Title Case Each Word, ASCII only, single spaces."""
    s = strip_diacritics(s)
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(w.capitalize() for w in s.split())


# ============================================================
# 3. Relation extraction (SOP §10.4)
# ============================================================
RELATION_MAP = {
    # canonical: list of triggers (already diacritic-stripped, lowered)
    "ba": ["cha", "bo ", "bo,", "bo.", " bo", "ong bo", "ba ruot"],  # cha
    "me": ["me ", "me,", "me.", " me", "ba me", "me ruot"],
    "vo": ["vo ", "vo,", "vo.", " vo", "ba xa"],
    "chong": ["chong"],
    "con": ["con trai", "con gai", "con ruot", " con "],
    "ong ba": ["ong ba", "ong noi", "ba noi", "ong ngoai", "ba ngoai"],
    "anh chi em": ["anh ruot", "chi ruot", "em ruot", "anh trai", "chi gai"],
    "co di chu bac": ["co ruot", "di ruot", "chu ruot", "bac ruot"],
}


def extract_relation(applicant: str, subject: str, summary: str = "") -> Optional[str]:
    """Return SOP-format relation tag if subject != applicant.

    Heuristic: if subject_name == applicant_name → no relation.
    Otherwise try to detect from summary text mentions like "mẹ là ...", "bố ...".
    """
    if not subject or not applicant:
        return None
    a = strip_diacritics(applicant).lower().strip()
    s = strip_diacritics(subject).lower().strip()
    if not a or not s or a == s:
        return None
    # Check summary for explicit relation mentions about subject
    text = strip_diacritics(summary or "").lower()
    for relation, triggers in RELATION_MAP.items():
        for t in triggers:
            if t.strip() in text:
                # crude: assume mention applies to subject if subject also appears
                if s.split()[-1] in text or len(s.split()) > 1 and s.split()[0] in text:
                    return relation
    # Unknown relation → return None, caller decides whether to mark needs_review
    return None


# ============================================================
# 4. Classify Gemini output → SOP tag + folder
# ============================================================
@dataclass
class Classification:
    tag: str  # SOP doc type tag (e.g. "CCCD", "Sao ke")
    folder: str  # one of 4 top-level
    confidence: str  # "high" | "medium" | "low"
    needs_review: bool


# FILENAME_HINTS — load từ data/doc_types.yaml ở module-import (Phase 4 data-driven).
# Strong filename hints: tag keyword trong tên file thường win, kể cả khi Gemini doc_type khác.
FILENAME_HINTS: list[tuple[str, str, str]] = _FILENAME_HINTS_FROM_YAML


def classify_doc_type(
    raw_doc_type: str,
    summary: str = "",
    original_filename: str = "",
    extracted: Optional[dict] = None,
) -> Classification:
    """Map Gemini's free-text doc_type → SOP tag + 1 of 4 top folders.

    Strategy:
      0. Guard: a self-filled / hand-written form (Gemini's `extracted.la_to_khai`, or a "tự khai / viết tay /
         thông tin gia đình …" wording that also mentions CCCD/personal info) is NOT a CCCD card / official doc
         → tag CV, needs_review. This beats the filename hint (a file *named* "CCCD-…" can still be a tự-khai form).
      1. doc_type alone (high confidence) when Gemini gave a clear answer.
      2. STRONG filename hint (medium confidence) — always tried before falling
         to summary, so noisy summaries can't override an obvious filename.
      2b. `extracted.la_anh_the` (Gemini saw a standalone portrait/ID photo file) → `Anh the`. Checked AFTER
          doc_type + filename so a CCCD/passport/diploma (whose face-photo is just printed on it) isn't mistaken
          for an `Anh the`.
      3. doc_type + filename loose match (medium).
      4. Summary as last resort (low, needs_review).
    """
    # Pass 0: tờ tự khai / viết tay → CV, KHÔNG phải CCCD/giấy chính thức (kể cả khi tên file/doc_type nói "CCCD").
    _la_to_khai = bool(isinstance(extracted, dict) and extracted.get("la_to_khai"))
    _g_hay = strip_diacritics(f"{raw_doc_type or ''} {summary or ''}").lower()
    if _la_to_khai or (
        re.search(r"tu\s*khai|to\s*khai|viet\s*tay|bieu\s*mau|tu\s*dien|tu\s*ghi|tu\s*viet|"
                  r"thong\s*tin\s*gia\s*dinh|so\s*yeu|phieu\s*khai|khai\s*bao\s*thong\s*tin", _g_hay)
        and re.search(r"\bcccd\b|can\s*cuoc|chung\s*minh|\bcmnd\b|thong\s*tin\s*ca\s*nhan", _g_hay)
    ):
        return Classification(tag="CV", folder="Personal Docs", confidence="medium", needs_review=True)

    is_unclear = (not raw_doc_type) or any(
        bad in (raw_doc_type or "").lower()
        for bad in ["chưa phân loại", "không xác định", "unknown"]
    )

    # Pass 1: doc_type only (high confidence)
    if not is_unclear:
        dt_hay = strip_diacritics(str(raw_doc_type)).lower()
        for pattern, tag, folder in DOC_TYPE_PATTERNS:
            if re.search(pattern, dt_hay):
                return Classification(tag=tag, folder=folder, confidence="high", needs_review=False)

    # Pass 2: STRONG filename hint (per Cường · "bổ sung theo tên file")
    if original_filename:
        fn_hay = strip_diacritics(str(original_filename)).lower()
        for pattern, tag, folder in FILENAME_HINTS:
            if re.search(pattern, fn_hay):
                return Classification(tag=tag, folder=folder, confidence="medium", needs_review=False)

    # Pass 2b: Gemini cờ "cả file LÀ một tấm ảnh chân dung riêng lẻ kiểu ảnh dán hồ sơ" → Anh the (mục 9 FARM).
    # Đặt SAU doc_type + tên file: nếu là CCCD / hộ chiếu / bằng cấp… (ảnh chân dung chỉ in trên giấy đó) thì đã
    # được nhận đúng ở trên; cờ này chỉ cứu trường hợp file đúng là ảnh thẻ mà doc_type/tên file mù mờ.
    if isinstance(extracted, dict) and extracted.get("la_anh_the"):
        return Classification(tag="Anh the", folder="Personal Docs", confidence="medium", needs_review=False)

    # Pass 3: filename + doc_type loose (medium)
    fn_hay_parts = [strip_diacritics(str(s)).lower() for s in (raw_doc_type, original_filename) if s]
    fn_hay = " | ".join(fn_hay_parts)
    if fn_hay.strip():
        for pattern, tag, folder in DOC_TYPE_PATTERNS:
            if re.search(pattern, fn_hay):
                return Classification(tag=tag, folder=folder, confidence="medium", needs_review=False)

    # Pass 4: summary as last resort (low, needs_review)
    if summary:
        sm_hay = strip_diacritics(str(summary)).lower()
        for pattern, tag, folder in DOC_TYPE_PATTERNS:
            if re.search(pattern, sm_hay):
                return Classification(tag=tag, folder=folder, confidence="low", needs_review=True)

    return Classification(
        tag=DEFAULT_TAG,
        folder=DEFAULT_FOLDER,
        confidence="low",
        needs_review=True,
    )


# ============================================================
# 5. Filename builder (SOP §10.1-10.4)
# ============================================================
def build_filename(
    tag: str,
    subject_name: str,
    extension: str,
    relation: Optional[str] = None,
    index: Optional[int] = None,
    is_english: bool = False,
) -> str:
    """[Tag] [relation?] [index?]-[Subject][_ENG?].ext

    Examples:
      CCCD-Bui Van Huan.pdf
      CCCD me-Bui Van Huan.pdf
      Sao ke 2-Bui Van Huan.pdf
      GKS-Bui Van Huan_ENG.pdf
    """
    parts = [tag.strip()]
    if relation:
        parts.append(relation.strip())
    if index is not None and index > 0:
        parts.append(str(index))
    left = " ".join(parts)

    subject_clean = title_case_ascii(subject_name) if subject_name else "Unknown"
    eng_suffix = "_ENG" if is_english else ""

    ext = extension.lstrip(".").lower()
    return f"{left}-{subject_clean}{eng_suffix}.{ext}"


# ============================================================
# 6. ENG detection
# ============================================================
def detect_english(summary: str = "", text_sample: str = "") -> bool:
    """Heuristic: if summary or text contains noticeable English content."""
    sample = (summary + " " + text_sample[:500]).lower()
    en_markers = [
        "translation", "translated", "certified true copy", "republic of",
        "ministry of", "this is to certify", "english version", "bilingual",
    ]
    return any(m in sample for m in en_markers)


# ============================================================
# 7. Quick self-test
# ============================================================
if __name__ == "__main__":
    cases = [
        ("Căn cước công dân", "HOÀNG THỊ MƠ", "pdf", "", ""),
        ("Trích lục khai sinh", "Hoàng Thị Mơ", "pdf", "Mẹ là PHAN THỊ BÍNH", ""),
        ("Hộ chiếu", "Bùi Văn Huân", "pdf", "", ""),
        ("Giấy chứng nhận quyền sử dụng đất", "Nguyễn Bá Thắng", "pdf", "", "BIA DAT.pdf"),
        ("Chứng nhận đăng ký xe", "Nguyễn Bá Thắng", "pdf", "", "DK XE.pdf"),
        ("Sao kê ngân hàng", "Hoàng Thị Mơ", "pdf", "", ""),
        ("Chưa phân loại", "Hoàng Thị Mơ", "jpg", "ảnh chăm sóc vườn hoa cúc nhà kính", ""),
        ("Không xác định", "Hoàng Thị Mơ", "jpg", "tiệc sinh nhật happy full moon", ""),
        ("Hợp đồng cho tặng đất", "Hoàng Thị Mơ", "pdf", "", ""),
        ("Sổ tiết kiệm", "Hoàng Thị Mơ", "pdf", "", ""),
        ("BHYT", "Hoàng Thị Mơ", "pdf", "", ""),
        ("PHIẾU LÝ LỊCH TƯ PHÁP SỐ 2", "Hoàng Thị Mơ", "pdf", "", ""),
        ("Giấy chứng nhận đăng ký HTX", "HTX Mơ", "pdf", "", ""),
    ]
    for raw_dt, subj, ext, summ, fn in cases:
        c = classify_doc_type(raw_dt, summ, fn)
        name = build_filename(c.tag, subj, ext)
        print(f"{c.confidence:6}  {c.folder:14}  {name:50}  ← {raw_dt!r}")
    # tờ tự khai / "thông tin gia đình" viết tay → CV, KHÔNG phải CCCD — kể cả khi tên file là "CCCD-…"
    assert classify_doc_type("Căn cước công dân", "thẻ căn cước 2 mặt có chip", "CCCD-Hoang Thi Mo.jpg").tag == "CCCD"
    assert classify_doc_type("Căn cước công dân", "tờ giấy có ô số CCCD", "CCCD-Hoang Thi Mo.jpg",
                             extracted={"la_to_khai": True}).tag == "CV"
    assert classify_doc_type("Thông tin gia đình", "khách tự ghi tay họ tên các thành viên", "CCCD-Hoang Thi Mo.jpg").tag == "CV"
    # ảnh thẻ 5x7 (ảnh dán hồ sơ, 1 người) → "Anh the" (mục 9 FARM); ảnh người làm nông → "Anh-video lam nong"; nhóm/tiệc → "Anh gia dinh"
    assert classify_doc_type("Ảnh thẻ 5x7", "ảnh chân dung phông trắng", "Khac-Hoang Thi Mo.jpg").tag == "Anh the"
    assert classify_doc_type("Ảnh", "", "x.jpg", extracted={"la_anh_the": True}).tag == "Anh the"   # cờ Gemini (không có dấu hiệu khác)
    assert classify_doc_type("Ảnh chân dung", "", "ID photo-Hoang Thi Mo.jpg").tag == "Anh the"      # round-trip qua tên file
    assert classify_doc_type("Hình ảnh", "", "Anh the-Hoang Thi Mo.jpg").tag == "Anh the"            # round-trip qua tên file
    assert classify_doc_type("Ảnh chân dung người làm nông trong nhà kính", "đang chăm cây", "x.jpg").tag == "Anh-video lam nong"  # KHÔNG nuốt ảnh làm nông
    assert classify_doc_type("Ảnh chụp gia đình", "tiệc sinh nhật", "x.jpg").tag == "Anh gia dinh"    # KHÔNG nuốt ảnh gia đình
    # CCCD (ảnh chân dung chỉ in TRÊN thẻ) → vẫn CCCD, KHÔNG bị cờ la_anh_the kéo thành "Anh the"
    assert classify_doc_type("Căn cước công dân", "thẻ căn cước có ảnh chân dung và chip", "CCCD.pdf", extracted={"la_anh_the": True}).tag == "CCCD"
    assert classify_doc_type("", "", "CCCD.pdf", extracted={"la_anh_the": True}).tag == "CCCD"        # tên file CCCD thắng cờ la_anh_the
    # Fix 2 — 2 pattern mới + KHÔNG regress GKH
    assert classify_doc_type("Giấy chứng nhận hiến máu", "Đã hiến máu lần thứ 3", "").tag == "Hien mau"
    assert classify_doc_type("Quyết định ly hôn", "Tòa án nhân dân huyện X", "QD-Ly hon.pdf").tag == "Ly hon"
    assert classify_doc_type("Giấy đăng ký kết hôn", "", "").tag == "GKH"
    # Fix 1 — extract_relation + build_filename slot relation
    assert extract_relation("Chu Thi Le", "Nguyen Van A", "Bố của đương đơn là Nguyễn Văn A") == "ba"
    assert extract_relation("Chu Thi Le", "Chu Thi Le", "") is None
    assert build_filename("CCCD", "Nguyen Van A", ".pdf", relation="ba") == "CCCD ba-Nguyen Van A.pdf"
    assert build_filename("CCCD", "Chu Thi Le", ".pdf") == "CCCD-Chu Thi Le.pdf"  # no-relation form không đổi
    print("classify guards OK")
