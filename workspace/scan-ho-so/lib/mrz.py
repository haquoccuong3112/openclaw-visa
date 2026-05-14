"""MRZ parser cho CCCD VN (TD1, 3 dòng × 30 ký tự) và Hộ chiếu (TD3, 2 dòng × 44 ký tự).

P2.3 — MRZ là GROUND-TRUTH chủ thẻ. Khi OCR trên mặt sau CCCD hoặc trang bio-data hộ chiếu
đọc được khối MRZ, name parse từ MRZ phải override `subject_raw` (loại bỏ bug "stamp tên
đương đơn lên CCCD của người khác" — case Mai Lan / Đặng Thị Hà).

Format reference:
- TD1 (CCCD VN mặt sau): 3 dòng, mỗi dòng 30 ký tự
    Line 1: `I<VNM<doc_no>...` (doc số), padding `<`
    Line 2: `YYMMDD-`gender`-YYMMDD-` (DOB + expiry), padding
    Line 3: `SURNAME<<GIVEN<NAMES<<<<<<...` (họ tên, `<<` tách họ và tên)
- TD3 (Passport bio page): 2 dòng × 44 ký tự
    Line 1: `P<VNM<SURNAME<<GIVEN<NAMES<<<<<<<...`
    Line 2: `<doc_no><check><VNM><YYMMDD><check><gender><YYMMDD>...`

API:
    parse_mrz(text: str) -> dict | None  # {raw, name, dob, doc_no, type}
"""
from __future__ import annotations
import re
from typing import Optional


# Regex bắt dòng MRZ: 30 hoặc 44 ký tự A-Z 0-9 < ở vị trí đầu dòng.
_MRZ_LINE = re.compile(r"^[A-Z0-9<]{28,46}$", re.MULTILINE)


def _candidate_lines(text: str) -> list[str]:
    """Lấy mọi dòng OCR khả nghi là MRZ — uppercase, có chứa `<<` hoặc `VNM`."""
    if not text:
        return []
    out = []
    for ln in text.split("\n"):
        s = ln.strip().replace(" ", "")
        if not s:
            continue
        # Phải là chữ in hoa + chữ số + `<`, độ dài 28-46.
        if not (28 <= len(s) <= 46):
            continue
        if not _MRZ_LINE.match(s):
            continue
        # Filter false-positives: phải có `<` hoặc `VNM` hoặc bắt đầu bằng `I<` / `P<`.
        if "<" not in s and "VNM" not in s:
            continue
        out.append(s)
    return out


def _parse_name(field: str) -> str:
    """Convert MRZ name field (SURNAME<<GIVEN<NAMES<<<<...) sang dạng `Surname Given Names`.
    Không bỏ dấu — MRZ vốn không có dấu. Title-case từng từ."""
    # Tách Surname << Given names.
    parts = field.split("<<", 1)
    surname = parts[0].replace("<", " ").strip()
    given = parts[1].replace("<", " ").strip() if len(parts) > 1 else ""
    full = f"{given} {surname}".strip() if given else surname
    full = re.sub(r"\s+", " ", full)
    return full.title()


def _parse_dob(yymmdd: str) -> str:
    """YYMMDD → DD/MM/YYYY. Heuristic year: <30 → 20YY, ≥30 → 19YY (đủ cho năm sinh KH visa)."""
    if not (len(yymmdd) == 6 and yymmdd.isdigit()):
        return ""
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    year = 2000 + yy if yy < 30 else 1900 + yy
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return ""
    return f"{dd:02d}/{mm:02d}/{year}"


def parse_mrz(text: str) -> Optional[dict]:
    """Parse text (OCR raw) → MRZ dict, hoặc None nếu không nhận diện được khối MRZ hợp lệ.

    Trả {raw, name, dob, doc_no, type ∈ {TD1, TD3}}.
    """
    if not text:
        return None
    lines = _candidate_lines(text)
    if not lines:
        return None

    # ---- TD3 (Passport): 2 dòng × 44 (chấp nhận 42-46 cho OCR noise) ----
    td3 = [ln for ln in lines if 42 <= len(ln) <= 46]
    if len(td3) >= 2:
        l1, l2 = td3[0], td3[1]
        if l1.startswith("P"):
            # Line 1: P<VNM<SURNAME<<GIVEN<<<...
            name_field = l1[5:].rstrip("<")
            name = _parse_name(name_field)
            # Line 2: doc_no[9] check[1] nat[3] dob[6] check[1] sex[1] exp[6]
            doc_no = l2[:9].replace("<", "").strip()
            dob = _parse_dob(l2[13:19])
            return {"raw": "\n".join([l1, l2]), "name": name, "dob": dob,
                    "doc_no": doc_no, "type": "TD3"}

    # ---- TD1 (CCCD VN): 3 dòng × 30 ----
    td1 = [ln for ln in lines if len(ln) == 30]
    if len(td1) >= 3:
        l1, l2, l3 = td1[0], td1[1], td1[2]
        # Line 1: I<VNM<<doc_no...
        # Line 2: YYMMDD-gender-YYMMDD (DOB + expiry)
        # Line 3: SURNAME<<GIVEN<NAMES<<<<...
        if l1.startswith(("I<", "ID")) and ("VNM" in l1 or l1[2:5] == "VNM"):
            # Doc_no: bytes 5-14 (skip I<VNM<)
            doc_no_raw = l1[5:14].replace("<", "")
            doc_no = doc_no_raw
            dob = _parse_dob(l2[:6])
            name = _parse_name(l3)
            return {"raw": "\n".join([l1, l2, l3]), "name": name, "dob": dob,
                    "doc_no": doc_no, "type": "TD1"}

    # ---- Fallback: nếu có 2 dòng `<<` (passport) hoặc 3 dòng (CCCD) bất kể prefix ----
    if len(lines) >= 2 and any("<<" in ln for ln in lines):
        # Tìm dòng có `<<` (tên) → parse name. DOB/doc_no có thể không lấy được → bỏ trống.
        name_line = next((ln for ln in lines if "<<" in ln), "")
        if name_line:
            # Loại prefix `P<VNM<` hoặc tương tự ở đầu dòng tên
            content = name_line
            content = re.sub(r"^[A-Z]{1,2}<", "", content)
            content = re.sub(r"^VNM<+", "", content)
            return {"raw": "\n".join(lines[:3]), "name": _parse_name(content),
                    "dob": "", "doc_no": "", "type": "unknown"}

    return None


if __name__ == "__main__":
    # Self-test với mẫu MRZ thực tế.
    # CCCD TD1 mẫu (3 dòng × 30 — đã giả lập từ một CCCD VN demo):
    cccd_sample = """I<VNM0123456789<<<<<<<<<<<<<<<
9001011M3001017VNM<<<<<<<<<<<6
DANG<<THI<HA<<<<<<<<<<<<<<<<<<"""
    r = parse_mrz(cccd_sample)
    assert r is not None, "TD1 phải parse được"
    assert r["type"] == "TD1", r
    assert r["name"] == "Thi Ha Dang", r["name"]
    assert r["dob"] == "01/01/1990", r["dob"]
    print(f"TD1 OK: name={r['name']!r} dob={r['dob']!r} doc_no={r['doc_no']!r}")

    # Passport TD3 mẫu (2 dòng × 44):
    pp_sample = """P<VNMNGUYEN<<VAN<A<<<<<<<<<<<<<<<<<<<<<<<<<<
B12345678<7VNM9001011M3001011<<<<<<<<<<<<<<<06"""
    r = parse_mrz(pp_sample)
    assert r is not None, "TD3 phải parse được"
    assert r["type"] == "TD3", r
    assert r["name"] == "Van A Nguyen", r["name"]
    assert r["doc_no"] == "B12345678", r["doc_no"]
    print(f"TD3 OK: name={r['name']!r} dob={r['dob']!r} doc_no={r['doc_no']!r}")

    # No MRZ trong text → None
    assert parse_mrz("Không có MRZ trong đoạn text này") is None
    assert parse_mrz("") is None
    print("All MRZ self-tests OK")
