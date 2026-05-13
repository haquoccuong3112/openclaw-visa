#!/usr/bin/env python3
"""Golden test harness — verify rule_engine detect đúng rule mong đợi.

Cách dùng:
  cd ~/.openclaw/workspace/scan-ho-so
  python3 tests/run_golden.py                          # chạy tất cả case
  python3 tests/run_golden.py tests/golden/sample.yaml # chạy 1 case

Mỗi file YAML golden case có:
  - applicant + today (context)
  - dataset: list[doc] mock (giống output build_dataset())
  - expected_errors: list rule code BẮT BUỘC phải detect (deterministic)
  - forbidden_errors: list rule code KHÔNG được fire (false-positive guard)

Test chỉ chạy deterministic rule (rule_engine) — KHÔNG gọi LLM → 0 token,
chạy <1s. Phase 6 data-driven sprint.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
WS_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(WS_DIR))

from lib.rule_loader import load_validations    # noqa: E402
from lib.rule_engine import detect_deterministic_errors  # noqa: E402


def run_case(case_path: Path) -> tuple[bool, str]:
    """Chạy 1 golden case. Trả (passed, summary)."""
    data = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    applicant = data.get("applicant", "?")
    dataset = data.get("dataset") or []
    expected = set(str(c) for c in (data.get("expected_errors") or []))
    forbidden = set(str(c) for c in (data.get("forbidden_errors") or []))

    rules = list(load_validations())
    errors = detect_deterministic_errors(rules, dataset)
    detected = {e["code"] for e in errors}

    missing = expected - detected
    false_positive = forbidden & detected

    lines = [f"== {case_path.name} :: {applicant} =="]
    lines.append(f"  dataset: {len(dataset)} doc")
    lines.append(f"  expected: {sorted(expected)}")
    lines.append(f"  detected: {sorted(detected)}")
    for e in errors:
        lines.append(f"    [{e['code']}] {e['severity']:6}  {e.get('ten','?'):45}  {e['msg'][:70]}")
    passed = True
    if missing:
        lines.append(f"  ❌ MISSING (expected but not detected): {sorted(missing)}")
        passed = False
    if false_positive:
        lines.append(f"  ❌ FALSE-POSITIVE (forbidden but detected): {sorted(false_positive)}")
        passed = False
    lines.append("  ✅ PASS" if passed else "  ❌ FAIL")
    return passed, "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        cases = [Path(p) for p in argv[1:]]
    else:
        cases = sorted((SCRIPT_DIR / "golden").glob("*.yaml"))
    if not cases:
        print("Không tìm thấy golden case nào trong tests/golden/")
        return 1
    n_pass = n_fail = 0
    for case in cases:
        if not case.exists():
            print(f"⚠️  Skip {case} — file không tồn tại")
            continue
        ok, summary = run_case(case)
        print(summary)
        print()
        if ok:
            n_pass += 1
        else:
            n_fail += 1
    print(f"=== TOTAL: {n_pass} pass, {n_fail} fail ===")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
