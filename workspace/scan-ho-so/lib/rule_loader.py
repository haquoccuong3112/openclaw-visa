#!/usr/bin/env python3
"""rule_loader — load + validate `data/rules.yaml`, `data/doc_types.yaml`, `data/relations.yaml`.

Cách dùng:
  from lib.rule_loader import load_checklist, load_validations
  items = load_checklist()    # → list[ChecklistItem] (26 mục FARM)
  rules = load_validations()  # → list[ValidationRule] (56 rule kiểm tra)

Phase 1: chỉ checklist + validations skeleton (Phase 2 mới migrate v1.1 rules).
Phase 4/5: thêm load_doc_types() + load_relations().

Schema validation fail-fast khi YAML malformed — bot start báo lỗi rõ ràng.
"""
from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RULES_YAML = DATA_DIR / "rules.yaml"

# Severity hợp lệ cho checklist (mục FARM): bat_buoc | ket_hon | co_con | tuy_chon | lam_sau
CHECKLIST_SEVERITIES = {"bat_buoc", "ket_hon", "co_con", "tuy_chon", "lam_sau"}
# Severity hợp lệ cho validation rule
VALIDATION_SEVERITIES = {"reject", "warn", "info"}


@dataclass(frozen=True)
class ChecklistItem:
    """1 mục trong CHECKLIST FARM — KH phải có giấy tờ thuộc `tags`."""
    code: str          # "FARM-1", "FARM-11-12"…
    name: str          # Tên mục hiển thị
    tags: tuple[str, ...]   # 1+ tag SOP; KH có ≥1 trong số đó là OK
    severity: str      # bat_buoc | ket_hon | co_con | tuy_chon | lam_sau

    @property
    def is_required(self) -> bool:
        """Mục bắt buộc — tính vào "X/18"."""
        return self.severity == "bat_buoc"


@dataclass(frozen=True)
class ValidationRule:
    """1 rule kiểm tra giấy tờ (vd "LLTP ≤ 6 tháng", "Sổ đỏ không thế chấp")."""
    code: str                  # "1.1", "13.3"… (theo v1.1 doc)
    category: str              # ho_tich | tai_san | tai_chinh | cong_viec | rang_buoc
    severity: str              # reject | warn | info
    applies_to: tuple[str, ...]   # tag(s) áp dụng
    rule: str                  # mô tả rule
    action: str                # hành động khi vi phạm
    condition: str | None      # expression deterministic (None = LLM only)
    needs_llm: bool            # True nếu LLM phải kiểm reasoning


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"rule_loader: file YAML không tồn tại — {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"rule_loader: parse YAML lỗi ({path.name}): {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"rule_loader: {path.name} root phải là dict")
    return data


@lru_cache(maxsize=1)
def load_rules_yaml() -> dict:
    """Load rules.yaml — cached in-process. Caller nên dùng load_checklist / load_validations."""
    return _load_yaml(RULES_YAML)


@lru_cache(maxsize=1)
def load_checklist() -> tuple[ChecklistItem, ...]:
    """Trả tuple 26 ChecklistItem từ rules.yaml. Fail-fast nếu schema sai."""
    data = load_rules_yaml()
    raw = data.get("checklist") or []
    if not isinstance(raw, list):
        raise ValueError("rules.yaml: 'checklist' phải là list")
    out: list[ChecklistItem] = []
    seen_codes: set[str] = set()
    for i, r in enumerate(raw, 1):
        if not isinstance(r, dict):
            raise ValueError(f"rules.yaml checklist[{i}]: phải là dict, gặp {type(r).__name__}")
        try:
            code = str(r["code"]).strip()
            name = str(r["name"]).strip()
            tags_raw = r["tags"]
            severity = str(r["severity"]).strip()
        except KeyError as e:
            raise ValueError(f"rules.yaml checklist[{i}]: thiếu field {e}") from e
        if code in seen_codes:
            raise ValueError(f"rules.yaml checklist[{i}]: code '{code}' bị trùng")
        seen_codes.add(code)
        if severity not in CHECKLIST_SEVERITIES:
            raise ValueError(
                f"rules.yaml checklist[{i}] code={code}: severity '{severity}' không hợp lệ. "
                f"Phải là 1 trong {sorted(CHECKLIST_SEVERITIES)}"
            )
        if not isinstance(tags_raw, list) or not tags_raw or not all(isinstance(t, str) for t in tags_raw):
            raise ValueError(f"rules.yaml checklist[{i}] code={code}: tags phải là list[str] non-empty")
        out.append(ChecklistItem(
            code=code, name=name,
            tags=tuple(t.strip() for t in tags_raw),
            severity=severity,
        ))
    if not out:
        raise ValueError("rules.yaml: checklist rỗng")
    return tuple(out)


@lru_cache(maxsize=1)
def load_validations() -> tuple[ValidationRule, ...]:
    """Trả tuple ValidationRule từ rules.yaml. Phase 1: có thể rỗng (Phase 2 migrate v1.1)."""
    data = load_rules_yaml()
    raw = data.get("validations") or []
    if not isinstance(raw, list):
        raise ValueError("rules.yaml: 'validations' phải là list")
    out: list[ValidationRule] = []
    seen_codes: set[str] = set()
    for i, r in enumerate(raw, 1):
        if not isinstance(r, dict):
            raise ValueError(f"rules.yaml validations[{i}]: phải là dict")
        try:
            code = str(r["code"]).strip()
            category = str(r["category"]).strip()
            severity = str(r["severity"]).strip()
            applies_to_raw = r["applies_to"]
            rule_text = str(r["rule"]).strip()
            action = str(r["action"]).strip()
        except KeyError as e:
            raise ValueError(f"rules.yaml validations[{i}]: thiếu field {e}") from e
        if code in seen_codes:
            raise ValueError(f"rules.yaml validations[{i}]: code '{code}' bị trùng")
        seen_codes.add(code)
        if severity not in VALIDATION_SEVERITIES:
            raise ValueError(
                f"rules.yaml validations[{i}] code={code}: severity '{severity}' không hợp lệ"
            )
        if not isinstance(applies_to_raw, list):
            raise ValueError(f"rules.yaml validations[{i}] code={code}: applies_to phải là list")
        condition = r.get("condition")
        condition = str(condition).strip() if condition is not None and condition != "" else None
        needs_llm = bool(r.get("needs_llm", condition is None))
        out.append(ValidationRule(
            code=code, category=category, severity=severity,
            applies_to=tuple(str(t).strip() for t in applies_to_raw),
            rule=rule_text, action=action,
            condition=condition, needs_llm=needs_llm,
        ))
    return tuple(out)


_CAT_LABEL = {
    "ho_tich": "I. HỒ TỊCH",
    "tai_san": "II. TÀI SẢN",
    "tai_chinh": "II.b TÀI CHÍNH",
    "cong_viec": "III. CÔNG VIỆC",
    "rang_buoc": "IV. RÀNG BUỘC",
}

_SEV_ICON = {"reject": "🔴", "warn": "🟡", "info": "🟢"}


def generate_rules_block(rules: tuple[ValidationRule, ...] | None = None) -> str:
    """Sinh section "RULES REFERENCE" cho prompt thẩm định tầng 2.

    Nhóm rule theo category + đánh dấu reject/warn/info, kèm mã code để LLM
    output báo cáo `Lỗi #N [13.3]:` có rule code traceable.
    """
    rules = rules or load_validations()
    if not rules:
        return ""
    # Top section: HARD-REJECT cho rule severity=reject (ưu tiên kiểm tra)
    rejects = [r for r in rules if r.severity == "reject"]
    others = [r for r in rules if r.severity != "reject"]
    lines: list[str] = []
    if rejects:
        lines.append("# 🛑 HARD-REJECT CHECKS (giấy KHÔNG dùng được, BÁO NGAY)")
        lines.append("")
        for r in rejects:
            tags = ", ".join(r.applies_to) if r.applies_to else "(mọi)"
            lines.append(f"- [{r.code}] {r.rule}")
            lines.append(f"    áp dụng: {tags} · hành động: {r.action}")
        lines.append("")
    # Nhóm rule còn lại theo category
    by_cat: dict[str, list[ValidationRule]] = {}
    for r in others:
        by_cat.setdefault(r.category, []).append(r)
    for cat in ("ho_tich", "tai_san", "tai_chinh", "cong_viec", "rang_buoc"):
        if cat not in by_cat:
            continue
        lines.append(f"# {_CAT_LABEL.get(cat, cat.upper())}")
        lines.append("")
        for r in sorted(by_cat[cat], key=lambda x: x.code):
            tags = ", ".join(r.applies_to) if r.applies_to else "(mọi)"
            icon = _SEV_ICON.get(r.severity, "·")
            lines.append(f"- {icon} [{r.code}] {r.rule}")
            lines.append(f"    áp dụng: {tags}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _invalidate_caches() -> None:
    """Dùng cho test: clear @lru_cache để reload YAML."""
    load_rules_yaml.cache_clear()
    load_checklist.cache_clear()
    load_validations.cache_clear()


# ============================================================================
# self-test
# ============================================================================
if __name__ == "__main__":
    cl = load_checklist()
    print(f"loaded {len(cl)} checklist items")
    bat_buoc = [c for c in cl if c.is_required]
    print(f"  bat_buoc: {len(bat_buoc)}/18 (kỳ vọng 18)")
    by_sev = {}
    for c in cl:
        by_sev[c.severity] = by_sev.get(c.severity, 0) + 1
    print(f"  by severity: {by_sev}")
    # Sanity asserts cho Phase 1
    assert len(cl) == 26, f"checklist phải có 26 mục, đang có {len(cl)}"
    assert len(bat_buoc) == 18, f"bat_buoc phải = 18, đang = {len(bat_buoc)}"
    assert {"Passport", "CCCD", "GKS", "Sao ke", "Anh gia dinh"} <= {t for c in cl for t in c.tags}
    vs = load_validations()
    print(f"loaded {len(vs)} validation rules")
    assert len(vs) >= 30, f"validations phải có ≥30 rule (Phase 2), đang có {len(vs)}"
    by_sev = {}
    for v in vs:
        by_sev[v.severity] = by_sev.get(v.severity, 0) + 1
    print(f"  by severity: {by_sev}")
    det = [v for v in vs if v.condition]
    print(f"  deterministic (có condition): {len(det)}")
    assert len(det) >= 8, "Cần ≥8 rule deterministic (13.3, 19.4, 19.6, 6.2, 12.1…)"
    # Spot-check rule codes quan trọng có mặt
    codes = {v.code for v in vs}
    for c in ("13.3", "19.4", "19.6", "12.1", "6.2"):
        assert c in codes, f"rule code '{c}' phải có trong rules.yaml"
    # Test generate prompt block
    block = generate_rules_block(vs)
    assert "HARD-REJECT" in block, "prompt block phải có section HARD-REJECT"
    assert "[13.3]" in block, "rule code 13.3 phải xuất hiện trong block"
    assert "[19.4]" in block, "rule code 19.4 phải xuất hiện trong block"
    print(f"  generate_rules_block: {len(block)} chars OK")
    print("OK")
