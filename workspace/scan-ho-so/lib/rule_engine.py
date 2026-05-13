#!/usr/bin/env python3
"""rule_engine — deterministic eval cho rule có condition trong data/rules.yaml.

Mỗi rule với `condition` (KHÔNG `needs_llm`) sẽ được eval qua simpleeval với
context restricted (chỉ doc + helper functions). Bot phát hiện vi phạm TRƯỚC
khi gọi LLM → đỡ token + giảm false-negative (LLM không thể bỏ sót rule).

Helpers exposed cho condition:
  - doc.tag                  : SOP tag của giấy (str)
  - doc.extracted.<field>    : field từ Gemini OCR; missing → None
  - doc.summary              : tóm tắt OCR (str lowercase)
  - today                    : datetime.date hôm nay
  - years_until(date_str)    : (date_str - today) / năm; None nếu parse fail
  - years_since(date_str)    : (today - date_str) / năm
  - months_since(date_str)   : (today - date_str) / tháng
  - days_since(date_str)     : (today - date_str) / ngày
  - lower(s)                 : s.lower() (handle None)
  - contains(haystack, needle): case-insensitive `needle` in `haystack`
  - any_in_text(text, items) : any item in items found in text (lowercase)

Vietnamese date format hỗ trợ: dd/mm/yyyy, dd-mm-yyyy, yyyy-mm-dd, "ngày X tháng Y năm Z".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

try:
    from simpleeval import EvalWithCompoundTypes, NameNotDefined, AttributeDoesNotExist
except ImportError as e:
    raise ImportError("rule_engine cần simpleeval. Cài: pip install --break-system-packages simpleeval") from e

# ============================================================================
# Date parsing (VN format)
# ============================================================================
_DATE_PATTERNS = [
    re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})"),       # dd/mm/yyyy hoặc dd-mm-yyyy
    re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})"),       # yyyy-mm-dd
    re.compile(r"ng[aà]y\s+(\d{1,2})\s+th[aá]ng\s+(\d{1,2})\s+n[aă]m\s+(\d{4})", re.IGNORECASE),
]


def _parse_vn_date(s: Any) -> date | None:
    """Parse string thành date. Trả None nếu không parse được."""
    if isinstance(s, date):
        return s
    if not isinstance(s, str) or not s.strip():
        return None
    txt = s.strip()
    for p in _DATE_PATTERNS:
        m = p.search(txt)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g[0]) == 4:   # yyyy-mm-dd
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            else:                # dd-mm-yyyy
                d, mo, y = int(g[0]), int(g[1]), int(g[2])
            return date(y, mo, d)
        except (ValueError, IndexError):
            continue
    return None


# ============================================================================
# Doc wrapper — cho condition truy cập doc.tag / doc.extracted.<field>
# ============================================================================
class _AttrDict:
    """dict wrap thành object để truy cập `obj.field`; missing field → None."""
    def __init__(self, d: dict | None):
        self.__dict__ = d if isinstance(d, dict) else {}

    def __getattr__(self, name: str) -> Any:
        return None    # missing field → None (không AttributeError)


@dataclass
class DocContext:
    tag: str
    summary: str
    extracted: _AttrDict


def _make_doc_context(doc: dict) -> DocContext:
    return DocContext(
        tag=str(doc.get("loai") or doc.get("tag") or ""),
        summary=str(doc.get("tom_tat") or doc.get("summary") or "").lower(),
        extracted=_AttrDict(doc.get("du_lieu") or doc.get("extracted") or {}),
    )


# ============================================================================
# Condition helper functions (cho YAML condition)
# ============================================================================
def _years_until(date_str: Any) -> float | None:
    d = _parse_vn_date(date_str)
    if d is None:
        return None
    return (d - date.today()).days / 365.25


def _years_since(date_str: Any) -> float | None:
    d = _parse_vn_date(date_str)
    if d is None:
        return None
    return (date.today() - d).days / 365.25


def _months_since(date_str: Any) -> float | None:
    d = _parse_vn_date(date_str)
    if d is None:
        return None
    return (date.today() - d).days / 30.44


def _days_since(date_str: Any) -> float | None:
    d = _parse_vn_date(date_str)
    if d is None:
        return None
    return float((date.today() - d).days)


def _lower(s: Any) -> str:
    if s is None:
        return ""
    return str(s).lower()


def _contains(haystack: Any, needle: Any) -> bool:
    if haystack is None or needle is None:
        return False
    return str(needle).lower() in str(haystack).lower()


def _any_in_text(text: Any, items: list | tuple) -> bool:
    if text is None or not items:
        return False
    t = str(text).lower()
    for it in items:
        if it and str(it).lower() in t:
            return True
    return False


# ============================================================================
# Eval rule condition cho 1 doc
# ============================================================================
def evaluate_rule(condition: str, doc: dict) -> bool | None:
    """Eval `condition` (chuỗi expression Python-like) trên `doc` (dict 1 giấy).
    Trả:
      True  → rule vi phạm
      False → rule OK
      None  → không eval được (condition lỗi syntax, dữ liệu thiếu — bỏ qua deterministic, để LLM xử)
    KHÔNG raise — eval lỗi → trả None để không phá pipeline.
    """
    if not condition or not isinstance(doc, dict):
        return None
    ctx = _make_doc_context(doc)
    se = EvalWithCompoundTypes(
        # `true`/`false`/`null` cho-phép viết YAML kiểu YAML thay vì kiểu Python `True`/`False`/`None`.
        names={"doc": ctx, "today": date.today(),
               "true": True, "false": False, "null": None,
               "True": True, "False": False, "None": None},
        functions={
            "years_until":  _years_until,
            "years_since":  _years_since,
            "months_since": _months_since,
            "days_since":   _days_since,
            "lower":        _lower,
            "contains":     _contains,
            "any_in_text":  _any_in_text,
        },
    )
    try:
        result = se.eval(condition)
        if result is None:
            return None
        return bool(result)
    except (NameNotDefined, AttributeDoesNotExist, TypeError, ValueError, ZeroDivisionError):
        return None
    except Exception:  # noqa: BLE001 — defensive; bất kỳ lỗi nào → None
        return None


def detect_deterministic_errors(rules: list, dataset: list[dict]) -> list[dict]:
    """Chạy mọi rule có `condition` (KHÔNG `needs_llm`) qua từng doc trong dataset.
    Trả list[{code, severity, msg, tag, ten, action}] cho mọi vi phạm phát hiện.
    """
    out: list[dict] = []
    for r in rules:
        if not r.condition or r.needs_llm:
            continue
        for doc in dataset:
            tag = doc.get("loai") or doc.get("tag") or ""
            # Skip nếu rule có applies_to mà doc.tag không thuộc
            if r.applies_to and tag and tag not in r.applies_to:
                continue
            hit = evaluate_rule(r.condition, doc)
            if hit is True:
                out.append({
                    "code": r.code,
                    "severity": r.severity,
                    "msg": r.rule,
                    "action": r.action,
                    "tag": tag,
                    "ten": doc.get("ten") or doc.get("new_name") or "",
                })
    return out


# ============================================================================
# self-test
# ============================================================================
if __name__ == "__main__":
    # Date parsing
    assert _parse_vn_date("15/09/2025") == date(2025, 9, 15)
    assert _parse_vn_date("2025-09-15") == date(2025, 9, 15)
    assert _parse_vn_date("ngày 15 tháng 9 năm 2025") == date(2025, 9, 15)
    assert _parse_vn_date("garbage") is None
    assert _parse_vn_date(None) is None
    assert _parse_vn_date("") is None

    # Helpers
    today = date.today()
    far_past = f"01/01/{today.year - 5}"
    assert _years_since(far_past) > 4.5
    assert _months_since(far_past) > 50
    assert _contains("Hello WORLD", "world") is True
    assert _contains(None, "x") is False
    assert _any_in_text("đây là thẻ Ever-Link của Vietcombank", ["ever-link", "agribank"]) is True
    assert _any_in_text("thẻ Techcombank", ["ever-link", "agribank"]) is False

    # evaluate_rule
    doc_thechap = {"loai": "So dat", "du_lieu": {"tinh_trang_the_chap": True}}
    assert evaluate_rule("doc.extracted.tinh_trang_the_chap == True", doc_thechap) is True
    doc_clean = {"loai": "So dat", "du_lieu": {"tinh_trang_the_chap": False}}
    assert evaluate_rule("doc.extracted.tinh_trang_the_chap == True", doc_clean) is False
    doc_missing = {"loai": "So dat", "du_lieu": {}}
    assert evaluate_rule("doc.extracted.tinh_trang_the_chap == True", doc_missing) is False
    # LLTP hết hạn: ngày cấp 10 tháng trước → months_since > 6
    lltp_old = {"loai": "LLTP", "du_lieu": {"ngay_cap": f"01/{(today.month - 8) % 12 + 1:02d}/{today.year - (1 if today.month <= 8 else 0)}"}}
    # Đơn giản hơn: hardcode 10 tháng trước
    lltp_old = {"loai": "LLTP", "du_lieu": {"ngay_cap": "01/01/2025" if today >= date(2025, 11, 1) else f"01/01/{today.year - 1}"}}
    assert evaluate_rule("months_since(doc.extracted.ngay_cap) > 6", lltp_old) is True
    lltp_fresh = {"loai": "LLTP", "du_lieu": {"ngay_cap": today.strftime("%d/%m/%Y")}}
    assert evaluate_rule("months_since(doc.extracted.ngay_cap) > 6", lltp_fresh) is False
    # 12.1 NH cấm
    doc_ever = {"loai": "The Visa-MC", "tom_tat": "Thẻ Ever-Link Vietcombank xài được không?"}
    assert evaluate_rule("any_in_text(doc.summary, ['ever-link', 'agribank', 'mb hybrid'])", doc_ever) is True
    # Bad syntax → None
    assert evaluate_rule("doc.x.y.z == foo bar baz syntax error here", {"loai": "X"}) is None
    # Bad condition không raise
    assert evaluate_rule("", {"loai": "X"}) is None
    assert evaluate_rule("x == 1", None) is None

    # detect_deterministic_errors
    try:
        from .rule_loader import load_validations
    except ImportError:
        from rule_loader import load_validations  # type: ignore
    rules = list(load_validations())
    dataset = [
        {"loai": "So dat", "ten": "So dat-A.pdf", "du_lieu": {"tinh_trang_the_chap": True}},
        {"loai": "LLTP",   "ten": "LLTP-A.pdf",  "du_lieu": {"ngay_cap": "01/01/2024"}},
        {"loai": "The Visa-MC", "ten": "The Visa-A.jpg", "tom_tat": "thẻ Ever-Link của Vietcombank"},
    ]
    errors = detect_deterministic_errors(rules, dataset)
    codes = {e["code"] for e in errors}
    print(f"detect_deterministic_errors: phát hiện {len(errors)} lỗi từ {len(dataset)} doc")
    for e in errors:
        print(f"  [{e['code']}] {e['severity']:6}  {e['ten']}: {e['msg'][:60]}")
    assert "13.3" in codes, "Rule 13.3 (sổ đỏ thế chấp) phải fire"
    assert "6.2" in codes, "Rule 6.2 (LLTP hết hạn) phải fire"
    assert "12.1" in codes, "Rule 12.1 (NH cấm) phải fire"
    print("OK")
