#!/usr/bin/env python3
"""Tạo folder 'Đã duyệt' cho tất cả khách hàng cũ trong group_registry.json.
Chạy 1 lần: python3 setup_da_duyet.py
"""
import json, os, sys
from pathlib import Path

# Load env từ scan-ocr.env (cùng parent dir)
env_path = Path(__file__).parent.parent / "scan-ocr.env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent))
from lib.drive_helpers import get_or_create_folder
from scan_pipeline import SHARED_DRIVE_ID, DA_DUYET_FOLDER

reg_path = Path(__file__).parent / "group_registry.json"
if not reg_path.exists():
    print("group_registry.json không tìm thấy — chạy từ thư mục scan-ho-so/")
    sys.exit(1)

reg: dict = json.loads(reg_path.read_text())
seen: set = set()
ok = fail = skip = 0

for chat_id, info in reg.items():
    fid = info.get("folder_id", "")
    if not fid or fid in seen:
        skip += 1
        continue
    seen.add(fid)
    name = info.get("applicant") or chat_id
    try:
        da_id = get_or_create_folder(DA_DUYET_FOLDER, fid, drive_id=SHARED_DRIVE_ID)
        print(f"  ✓ {name}  →  {DA_DUYET_FOLDER}/ (id: {da_id})")
        ok += 1
    except Exception as e:
        print(f"  ✗ {name}: {e}")
        fail += 1

print(f"\nXong: {ok} tạo/xác nhận, {fail} lỗi, {skip} skip (trùng folder_id)")
