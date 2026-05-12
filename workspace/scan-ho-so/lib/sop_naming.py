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
DOC_TYPE_PATTERNS: list[tuple[str, str, str]] = [
    # ---- HIGH-PRIORITY DISAMBIGUATION (multi-token, must come first) ----
    # Đất nông nghiệp → Employment (SOP §9.4)
    (r"so do nong nghiep|so dat nong nghiep|dat nong nghiep|dat canh tac|gcn quyen su dung dat nong nghiep", "So dat NN", "Employment"),
    # DKKD / HTX / doanh nghiệp (phải trước CCCD vì cùng "chung nhan")
    (r"dkkd|dang ky kinh doanh|chung nhan dang ky kinh doanh|gcn dang ky doanh nghiep|gcn dang ky htx|dang ky htx|chung nhan dang ky htx|hop tac xa|dang ky doanh nghiep", "DKKD", "Employment"),

    # ---- Personal Docs ----
    (r"\bcccd\b|can cuoc cong dan|chung minh nhan dan|cmnd", "CCCD", "Personal Docs"),
    (r"ho chieu|passport", "Passport", "Personal Docs"),
    (r"khai sinh|trich luc khai sinh|\bgks\b", "GKS", "Personal Docs"),
    (r"ket hon|hon thu|dang ky ket hon", "GKH", "Personal Docs"),
    (r"xac nhan hoc|xn hoc|giay xn hoc", "XN hoc", "Personal Docs"),
    (r"cu tru|xac nhan cu tru|xnct", "XNCT", "Personal Docs"),
    (r"ly lich tu phap|lltp|phieu ll", "LLTP", "Personal Docs"),
    (r"giay phep lai xe|gplx|bang lai", "GPLX", "Personal Docs"),
    (r"anh the|hinh the|the 4x6|the 3x4", "Anh the", "Personal Docs"),
    (r"\bbhxh\b|bao hiem xa hoi", "BHXH", "Personal Docs"),
    (r"\bbhyt\b|bao hiem y te|the bao hiem y te", "BHYT", "Personal Docs"),
    (r"\biom\b", "IOM", "Personal Docs"),
    (r"\bcv\b|curriculum vitae|so yeu ly lich|syll|thong tin ca nhan|tu khai|phieu thong tin|bieu mau", "CV", "Personal Docs"),
    (r"the tin dung|credit card|the visa|the mc|mastercard|visa card|the ngan hang", "The Visa-MC", "Personal Docs"),
    (r"bang khen|giay khen|huy chuong", "Bang khen", "Personal Docs"),
    (r"anh gia dinh|anh chup gia dinh|family photo|tiec sinh nhat|tiec day thang|happy full moon|happy 1 month|day thang", "Anh gia dinh", "Personal Docs"),

    # ---- Education ----
    (r"bang cap|bang tot nghiep|chung chi|bang dai hoc|bang trung cap|bang cao dang|diploma", "Bang cap", "Education"),

    # ---- Asset ----
    (r"so do|so hong|quyen su dung dat|gcn quyen su dung dat|bia dat|giay chung nhan quyen su dung dat", "So dat", "Asset"),
    (r"cho tang|tang cho|thua ke|hd cho|hd tang", "HD cho-tang-thua ke", "Asset"),
    (r"so tiet kiem|\bstk\b|sotk", "STK", "Asset"),
    (r"xac nhan so du|xn so du|xnsd", "XN so du", "Asset"),
    (r"ca vet|cavet|dang ky xe|dk xe|chung nhan dang ky xe|chung nhan dang ky xe mo to", "Ca vet xe", "Asset"),
    (r"\bvang\b|vang mieng|\bsjc\b", "Vang", "Asset"),

    # ---- Employment (extras) ----
    (r"dai ly nong san|dai ly phan bon|dai ly thuc an|nong san", "Dai ly NS", "Employment"),
    (r"anh.*lam nong|video.*lam nong|lam nong|cham soc cay|nha kinh|trong hoa|cay trong|vuon hoa|hoa cuc", "Anh-video lam nong", "Employment"),
    (r"sao ke|sao ke ngan hang", "Sao ke", "Employment"),
    (r"hop dong lao dong|hdld|hop dong lam viec", "HDLD", "Employment"),
    (r"bien lai|hoa don thu tien", "Bien lai", "Employment"),
]

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


# Strong filename hints — if a tag keyword is in the filename it usually wins,
# even when Gemini's doc_type disagrees (per Cường's note: "thiếu cứ bổ sung
# theo tên file"). Order: most specific first.
FILENAME_HINTS: list[tuple[str, str, str]] = [
    (r"\bbhxh\b", "BHXH", "Personal Docs"),
    (r"\bbhyt\b", "BHYT", "Personal Docs"),
    (r"\bcccd\b|\bcmnd\b", "CCCD", "Personal Docs"),
    (r"\bgks\b|giay\s*ks|khai\s*sinh", "GKS", "Personal Docs"),
    (r"\bgkh\b|ket\s*hon|hon\s*thu", "GKH", "Personal Docs"),
    (r"\blltp\b|ly\s*lich\s*tu\s*phap", "LLTP", "Personal Docs"),
    (r"\bgplx\b|bang\s*lai|giay\s*phep\s*lai\s*xe", "GPLX", "Personal Docs"),
    (r"\bxnct\b|cu\s*tru", "XNCT", "Personal Docs"),
    (r"\biom\b", "IOM", "Personal Docs"),
    (r"\bsyll\b|so\s*yeu\s*ly\s*lich|thong\s*tin\s*ca\s*nhan|to\s*khai", "CV", "Personal Docs"),
    (r"ho\s*chieu|passport", "Passport", "Personal Docs"),
    (r"the\s*tin\s*dung|credit\s*card|mastercard|visa\s*card|the\s*visa|the\s*mc", "The Visa-MC", "Personal Docs"),
    (r"\bdkkd\b|dang\s*ky\s*kinh\s*doanh|\bhtx\b", "DKKD", "Employment"),
    (r"\bhdld\b|hop\s*dong\s*lao\s*dong", "HDLD", "Employment"),
    (r"sao\s*ke", "Sao ke", "Employment"),
    (r"\bstk\b|so\s*tiet\s*kiem", "STK", "Asset"),
    (r"\bxnsd\b|xn\s*so\s*du", "XN so du", "Asset"),
    (r"so\s*do|so\s*hong|bia\s*dat|qsdd", "So dat", "Asset"),
    (r"\bdk\s*xe\b|cavet|ca\s*vet|dang\s*ky\s*xe", "Ca vet xe", "Asset"),
    (r"\bvang\b|\bsjc\b", "Vang", "Asset"),
]


def classify_doc_type(
    raw_doc_type: str,
    summary: str = "",
    original_filename: str = "",
) -> Classification:
    """Map Gemini's free-text doc_type → SOP tag + 1 of 4 top folders.

    Strategy:
      0. Guard: a self-filled / hand-written form that only *mentions* CCCD info
         (số CCCD, họ tên, địa chỉ…) is NOT a CCCD card → tag CV, needs_review.
      1. doc_type alone (high confidence) when Gemini gave a clear answer.
      2. STRONG filename hint (medium confidence) — always tried before falling
         to summary, so noisy summaries can't override an obvious filename.
      3. doc_type + filename loose match (medium).
      4. Summary as last resort (low, needs_review).
    """
    # Pass 0: "tự khai/viết tay" + CCCD-ish wording → it's a personal-info form (CV), not the CCCD card.
    _g_hay = strip_diacritics(f"{raw_doc_type or ''} {summary or ''}").lower()
    if re.search(r"tu\s*khai|to\s*khai|viet\s*tay|bieu\s*mau|tu\s*dien|tu\s*ghi|tu\s*viet", _g_hay) and \
       re.search(r"\bcccd\b|can\s*cuoc|chung\s*minh|\bcmnd\b|thong\s*tin\s*ca\s*nhan", _g_hay):
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
