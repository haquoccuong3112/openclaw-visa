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
    (r"hien mau|chung nhan hien mau|giay chung nhan hien mau|don hien mau|so hien mau", "Hien mau", "Personal Docs"),
    (r"\bly hon\b|don ly hon|quyet dinh ly hon|ban an ly hon|thoa thuan ly hon|don thuan tinh ly hon", "Ly hon", "Personal Docs"),
    (r"giay phep lai xe|gplx|bang lai", "GPLX", "Personal Docs"),
    (r"anh the|hinh the|the\s*\d\s*x\s*\d|anh\s*\d\s*x\s*\d|anh\s*ho\s*chieu|id\s*photo|passport\s*photo", "Anh the", "Personal Docs"),
    (r"\bbhxh\b|bao hiem xa hoi", "BHXH", "Personal Docs"),
    (r"\bbhyt\b|bao hiem y te|the bao hiem y te", "BHYT", "Personal Docs"),
    (r"\biom\b", "IOM", "Personal Docs"),
    (r"\bcv\b|curriculum vitae|so yeu ly lich|so yeu|syll|thong tin ca nhan|thong tin gia dinh|tu khai|to khai|phieu thong tin|phieu khai|bieu mau", "CV", "Personal Docs"),
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
    (r"\banh\s*the\b|id\s*photo|passport\s*photo|\bportrait\b|\b\d\s*x\s*\d\b", "Anh the", "Personal Docs"),
    (r"\bxnct\b|cu\s*tru", "XNCT", "Personal Docs"),
    (r"\bhien\s*mau\b", "Hien mau", "Personal Docs"),
    (r"\bly\s*hon\b|\bqd\s*ly\s*hon\b", "Ly hon", "Personal Docs"),
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
