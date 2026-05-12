#!/usr/bin/env python3
"""One-off: convert admin_mapping_old_to_new.xlsx (from VietMap) → old_to_new_wards.json.
Not used at runtime. Needs `openpyxl` (use a throwaway venv: python3 -m venv /tmp/v && /tmp/v/bin/pip install openpyxl).
Re-run this whenever you drop in a newer admin_mapping_*.xlsx from VietMap (rename it to admin_mapping_old_to_new.xlsx)."""
import json, pathlib, openpyxl
HERE = pathlib.Path(__file__).resolve().parent
src = HERE / "admin_mapping_old_to_new.xlsx"
dst = HERE / "old_to_new_wards.json"
ws = openpyxl.load_workbook(src, read_only=True, data_only=True)["admin_mapping"]
rows = list(ws.iter_rows(values_only=True))
cols = [str(c).strip() for c in rows[0]]
data = [[("" if v is None else (str(v).strip() if isinstance(v, str) else v)) for v in r]
        for r in rows[1:] if r is not None and not all(v is None for v in r)]
dst.write_text(json.dumps({
    "_source": "vietmap-company/vietnam_administrative_address — admin_mapping_old_to_new.xlsx; JSON-ified verbatim. See SOURCES.md.",
    "columns": cols, "rows": data,
}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"wrote {dst} · {len(data)} rows · {dst.stat().st_size} bytes")
