#!/usr/bin/env python3
"""
Dong Hanh Processing Bot v2 - @donghanhprocessingbot

Luồng tự động khi bot được add vào 2 nhóm:

  Nhóm Pro (staff): có chữ "Pro" trong title — vd:
    - "DH Pro WP10m - Hoàng Thị Mơ TEST7 1991"
    - "DH Pro HighSkilled - Lê Văn Hậu 1991"
    - "DH Pro WP2Y – Trần Đăng Sự 2006"        (em/en-dash cũng được)

  Nhóm KH (khách): KHÔNG có chữ "Pro" — vd:
    - "Hoàng Thị Mơ TEST7 1991 WP10m - C Liên"
    - "DongHanh WP2Y - KH Trần Đăng Sự 2006"
    - "DongHanh HighSkilled - KH Lê Văn Hậu 1991"

Parser trích: tên KH (kèm năm sinh nếu có), chương trình (visa).
Nếu tên thiếu → vẫn đăng ký nhóm, ô tên/năm sinh để trống trong sheet.
Pair KH↔Pro: cùng (applicant.lower(), visa.upper()).

Khi bot join cặp nhóm mới:
  1. Tạo Drive folder cho case
  2. Tạo/cập nhật Sheet riêng cho nhân viên phụ trách
  3. Cấp quyền Drive cho nhân viên (email Google)

Khi nhóm KH gửi file:
  1. Im lặng (không reply vào nhóm KH)
  2. OCR → classify → rename theo SOP
  3. Upload Drive
  4. Forward file đã xử lý vào nhóm Pro (không gửi link)
  5. Xóa file tạm ngay

Nhóm Pro: nhân viên có thể hỏi bot về hồ sơ
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from telegram import Update, Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import BadRequest, ChatMigrated, TimedOut, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ── Env ── load scan-ocr.env BEFORE importing lib (lib modules read env vars at import time) ──
ENV_FILE = Path(__file__).resolve().parent.parent / "scan-ocr.env"   # = <workspace>/scan-ocr.env
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# ── Project lib ──────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.google_clients import drive
from lib.drive_helpers import get_or_create_folder, list_folder, download_file_bytes, move_file, invalidate_list_cache
from lib import chat as chatmod

REGISTRY_LOCK = asyncio.Lock()
_OLDFILE_LOCKS: dict[str, asyncio.Lock] = {}   # per-case lock cho lệnh /oldfile (key = folder_id)

# ── Debounce / batching cho file upload từ nhóm KH ───────────────────────────
# Telegram giao mỗi file của một lần "gửi nhiều file" thành 1 update; concurrent_updates(16)
# còn cho nhiều handle_file của cùng chat chạy đan xen. Gom các file đến gần nhau thành
# MỘT lần chạy scan_pipeline.py → MỘT manifest → MỘT tin tóm tắt (+≤1 tin checklist).
SCAN_DEBOUNCE_SECONDS  = float(os.environ.get("SCAN_DEBOUNCE_SECONDS", "8"))    # chờ "im lặng" sau file cuối
SCAN_DEBOUNCE_MAX_WAIT = float(os.environ.get("SCAN_DEBOUNCE_MAX_WAIT", "90"))  # trần tổng chờ kể từ file đầu
SCAN_DEBOUNCE_ACK = os.environ.get("SCAN_DEBOUNCE_ACK", "1") not in ("0", "", "false", "False")
_PENDING_BATCHES: dict = {}   # key = KH chat_id (str) -> _PendingBatch
_DOWNLOAD_SEM = asyncio.Semaphore(int(os.environ.get("SCAN_MAX_DOWNLOADS", "8")))  # tránh quá tải event loop

async def send_message_handle_migration(bot: Bot, chat_id: int | str, text: str, **kwargs) -> str:
    """Send a message and return the effective chat id if Telegram migrated it."""
    try:
        await bot.send_message(chat_id=int(chat_id), text=text, **kwargs)
        return str(chat_id)
    except ChatMigrated as e:
        new_chat_id = str(e.new_chat_id)
        logger.warning(f"Chat migrated while sending message: {chat_id} -> {new_chat_id}")
        await bot.send_message(chat_id=int(new_chat_id), text=text, **kwargs)
        return new_chat_id


async def send_html(bot, chat_id, html_text: str, *, plain_fallback: str | None = None, **kwargs):
    """Send `html_text` with parse_mode=HTML. We build the HTML ourselves (every dynamic value
    escaped) so it's always valid; the BadRequest fallback is just defensive — it resends a
    plain-text version (tags stripped) so the message never gets lost."""
    try:
        await bot.send_message(chat_id=int(chat_id), text=html_text, parse_mode=ParseMode.HTML, **kwargs)
    except BadRequest as e:
        logger.warning(f"HTML send failed ({e}); resending plain")
        await bot.send_message(chat_id=int(chat_id),
                               text=plain_fallback or re.sub(r"<[^>]+>", "", html_text), **kwargs)

SHARED_DRIVE_ID   = "0AIYOQpLqtMPvUk9PVA"
OPENCLAW_FOLDER_ID = "1VUpoBV3fAudONv5mMFXYguRThKfOLyz7"
MASTER_SHEET_ID   = "1Qv4gdxNKgS7EsDPvInFsR1rnob_Qgv9HCaays_al6io"
STAFF_SHEET_TEMPLATE_ID = "1S0kr9nBuTJHLbTwa_O8XfYVvaadviVyamUBiUKMwWJM"
SUMMARY_SHEET_ID = "1bPNea4i86yVqwTGx0IuKGv1t3PrDlKoPAg16yPNOnCs"
TOP_FOLDERS       = ["Personal Docs", "Education", "Asset", "Employment"]
OCR_META_FOLDER   = "_Bot OCR & Metadata"
OLD_FILE_FOLDER   = "Old File"          # inbox cho hồ sơ cũ trên Drive (xử lý bằng lệnh /oldfile)
OLD_FILE_PROCESSED = "_processed"       # subfolder của Old File: lưu bản gốc sau khi đã xử lý
DA_DUYET_FOLDER   = "Đã duyệt"          # staff review folder — bot đọc (OCR), không ghi file vào đây
EXT_MIME = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
ZIP_EXTS = {".zip"}

# ── Registry ─────────────────────────────────────────────────────────────────
REGISTRY_PATH    = Path(__file__).parent / "group_registry.json"
_CHECKPOINT_DIR  = Path(__file__).parent / "_checkpoints"   # persist batch state across restarts

def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {}

def save_registry(reg: dict):
    REGISTRY_PATH.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")

# ── Recent cases (DM /case picker) ──────────────────────────────────────────
# Mỗi staff phụ trách hàng trăm case → flat list 100+ button không khả thi. Giữ 8 case
# vừa được tương tác gần nhất per staff, hiển thị làm shortcut khi gõ /case không args.
RECENT_CASES_PATH = Path(__file__).parent / "recent_cases.json"
RECENT_MAX = 8

def _load_recent() -> dict:
    if RECENT_CASES_PATH.exists():
        try:
            return json.loads(RECENT_CASES_PATH.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — file corrupt → reset, không crash bot
            print(f"recent_cases.json load lỗi ({e}); reset", flush=True)
    return {}

def _save_recent(data: dict) -> None:
    try:
        tmp = RECENT_CASES_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(RECENT_CASES_PATH)
    except Exception as e:  # noqa: BLE001
        print(f"recent_cases.json save lỗi: {e}", flush=True)

_RECENT_BY_STAFF: dict = _load_recent()   # str(user_id) -> [{folder_id, applicant, ts}, ...]

def _push_recent(user_id, info: dict) -> None:
    """Đẩy case `info` lên đầu deque recent của staff (maxlen RECENT_MAX), persist ngay."""
    uid = str(user_id)
    folder_id = info.get("folder_id", "")
    applicant = info.get("applicant", "?")
    if not folder_id:
        return
    arr = [e for e in (_RECENT_BY_STAFF.get(uid) or []) if e.get("folder_id") != folder_id]
    arr.insert(0, {"folder_id": folder_id, "applicant": applicant, "ts": time.time()})
    _RECENT_BY_STAFF[uid] = arr[:RECENT_MAX]
    _save_recent(_RECENT_BY_STAFF)

# Map (chat_id:message_id) → folder_id, để staff reply tin bot cũ → biết case đó là gì.
# In-process only (mất khi restart); chỉ dùng cho DM bot trả lời câu hỏi chat.
_BOT_MSG_TO_FOLDER: dict = {}

# ── Parse group title ─────────────────────────────────────────────────────────
# Phân biệt KH vs Pro bằng chữ "Pro" (case-insensitive) trong title.
# Hỗ trợ nhiều prefix: "DH Pro", "DongHanh", "Đồng Hành Pro", "Đồng Hành"; cả
# em-dash/en-dash; cả "KH" token trên nhánh KH. Visa/chương trình giữ casing đẹp
# qua _canon_visa(); pair_key vẫn dùng .upper() nội bộ.
VISA_RE = re.compile(
    r'\b(WP\d+[mMyY]?|WP|SP|VP|PR|SUV|TRV|LMIA|SOWP|IEC|PNP'
    r'|High\s*Skilled|FARM)\b',
    re.IGNORECASE,
)

_DASH_RE = re.compile(r'[‐-―−]')          # ‐ ‑ ‒ – — ― −
_PRO_RE = re.compile(r'\bpro\b', re.IGNORECASE)
_PREFIX_MARKER_RE = re.compile(
    r'\b(?:DH|DongHanh|Đồng\s*Hành)\s*Pro\b'             # "DH Pro" / "DongHanh Pro" / "Đồng Hành Pro"
    r'|\b(?:DongHanh|Đồng\s*Hành)\b',                    # "DongHanh" / "Đồng Hành" (KH side)
    re.IGNORECASE,
)
_KH_MARKER_RE = re.compile(r'(?<![A-Za-zÀ-ỹ])KH(?![A-Za-zÀ-ỹ])')
_YEAR_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')


def _canon_visa(raw: str) -> str:
    """Chuẩn hoá form hiển thị: 'wp10m' → 'WP10M', 'high skilled' → 'HighSkilled', 'farm' → 'FARM'."""
    s = re.sub(r'\s+', '', raw)
    if re.fullmatch(r'(?i)highskilled', s):
        return "HighSkilled"
    return s.upper()


def parse_group_title(title: str) -> dict | None:
    """
    Returns dict with keys: kind ('kh'|'pro'), applicant, visa, raw_title
    or None if not recognized (cần có visa để pair KH↔Pro).
    """
    t0 = (title or "").strip()
    if not t0:
        return None
    t = _DASH_RE.sub('-', t0)                            # em/en-dash → hyphen
    is_pro = bool(_PRO_RE.search(t))                     # 'Pro' = staff group
    vm = VISA_RE.search(t)
    if not vm:
        return None                                      # không có visa → không pair được
    visa = _canon_visa(vm.group(0))
    # Bỏ visa + prefix marker + KH token → còn lại là tên (có thể nhiều segment).
    working = t[:vm.start()] + " " + t[vm.end():]
    working = _PREFIX_MARKER_RE.sub(' ', working)
    working = _KH_MARKER_RE.sub(' ', working)
    working = re.sub(r'\s+', ' ', working).strip(' -')
    # Split theo '-'; chọn segment chứa năm sinh; nếu không có thì segment dài nhất.
    segs = [s.strip(' -') for s in re.split(r'\s*-\s*', working) if s.strip(' -')]
    applicant = ""
    for seg in segs:
        if _YEAR_RE.search(seg):
            applicant = seg
            break
    if not applicant and segs:
        applicant = max(segs, key=len)
    return {
        "kind": "pro" if is_pro else "kh",
        "applicant": applicant,                          # có thể "" — sheet để trống ô tên
        "visa": visa,
        "raw_title": title,
    }

def make_pair_key(applicant: str, visa: str) -> str:
    """Normalize for matching KH↔Pro."""
    a = re.sub(r'\s+', ' ', applicant.strip().lower())
    return f"{a}|{visa.upper()}"

# ── Staff helpers ─────────────────────────────────────────────────────────────
def load_staff() -> list[dict]:
    try:
        from lib.google_clients import sheets
        r = sheets().spreadsheets().values().get(
            spreadsheetId=MASTER_SHEET_ID, range="Staff!A2:E"
        ).execute()
        staff = []
        for row in r.get("values", []):
            if not row: continue
            staff.append({
                "tele_id": str(row[0]).strip() if row[0] else "",
                "name":    row[1].strip() if len(row) > 1 else "",
                "role":    row[2].strip() if len(row) > 2 else "",
                "email":   row[3].strip() if len(row) > 3 else "",
                "sheet_id": row[4].strip() if len(row) > 4 else "",
            })
        return staff
    except Exception as e:
        logger.warning(f"load_staff: {e}")
        return []

STAFF_TELE_IDS = {
    "5359705508","5177183171","6717503907","5174487713",
    "2075661481","6760657726","7379468455","6714043460",
    "7320385885","8793633276","8768112274",
}

async def get_group_staff(bot: Bot, chat_id: int, all_staff: list[dict]) -> list[dict]:
    """Return known Staff-tab members who are in the group.

    Telegram bots cannot list all members of a group. This works without admin
    by checking known Telegram IDs from the Staff tab one-by-one. If Telegram
    refuses a specific lookup we just skip it.
    """
    staff_map = {s["tele_id"]: s for s in all_staff if s["tele_id"]}
    found = []
    for tid in list(staff_map.keys()):
        try:
            member = await bot.get_chat_member(chat_id, int(tid))
            if member.status not in ("left", "kicked", "banned"):
                found.append(staff_map[tid])
        except Exception:
            pass
    return found

def merge_registry_staff(reg: dict, pro_chat_id: str, staff_ids: list[str]):
    """Merge detected staff IDs into registry for a Pro group."""
    if not staff_ids or pro_chat_id not in reg:
        return
    old = set(reg[pro_chat_id].get("staff") or [])
    new = old | {str(x) for x in staff_ids if x}
    reg[pro_chat_id]["staff"] = sorted(new)

def staff_by_telegram_id(tele_id: str) -> dict | None:
    for s in load_staff():
        if s.get("tele_id") == str(tele_id):
            return s
    return None

async def remember_staff_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback staff detection without admin.

    If a known Staff-tab user joins or sends any message in a KH/Pro group, record
    them in registry and ensure their Drive access + personal Sheet exists.
    """
    msg = update.message or update.channel_post
    if not msg or not msg.chat:
        return
    chat_id = str(msg.chat.id)
    async with REGISTRY_LOCK:
        reg = load_registry()
        info = reg.get(chat_id)
        if not info or info.get("kind") not in ("kh", "pro") or not info.get("case_setup"):
            return

    detected_ids = []
    if msg.from_user:
        detected_ids.append(str(msg.from_user.id))
    for u in (msg.new_chat_members or []):
        detected_ids.append(str(u.id))

    known = []
    for tid in detected_ids:
        s = staff_by_telegram_id(tid)
        if s:
            known.append(s)
    if not known:
        return

    async with REGISTRY_LOCK:
        reg = load_registry()
        info = reg.get(chat_id)
        if not info:
            return
        kh_chat_id_for_merge = chat_id if info.get("kind") == "kh" else info.get("kh_chat_id", "")
        pro_chat_id_for_merge = chat_id if info.get("kind") == "pro" else info.get("pro_chat_id", "")
        if kh_chat_id_for_merge:
            merge_registry_staff(reg, kh_chat_id_for_merge, [s["tele_id"] for s in known])
        if pro_chat_id_for_merge:
            merge_registry_staff(reg, pro_chat_id_for_merge, [s["tele_id"] for s in known])
        save_registry(reg)

    case_id = re.sub(r'\s+','-', info["applicant"].upper()[:20]) + f"-{info['visa']}"
    kh_chat_id = chat_id if info.get("kind") == "kh" else info.get("kh_chat_id", "")
    pro_chat_id = chat_id if info.get("kind") == "pro" else info.get("pro_chat_id", "")
    kh_title = reg.get(kh_chat_id, {}).get("raw_title", info.get("raw_title", "")) if kh_chat_id else info.get("raw_title", "")
    all_staff = load_staff()
    current_staff_ids = set(reg.get(kh_chat_id, {}).get("staff") or []) | set(reg.get(pro_chat_id, {}).get("staff") or [])
    current_staff = [s for s in all_staff if s.get("tele_id") in current_staff_ids]
    manager_name, staff_name = split_responsible_staff(current_staff)
    for s in known:
        if s.get("email"):
            share_drive_folder(info["folder_id"], s["email"])
        get_or_create_staff_sheet(
            s, case_id, info["applicant"], info["visa"], info["drive_link"], info["folder_id"],
            kh_chat_id=kh_chat_id, pro_chat_id=pro_chat_id, kh_title=kh_title,
            manager_name=manager_name, staff_name=staff_name,
        )
    update_summary_sheet(
        case_id, info["applicant"], info["visa"], info["drive_link"],
        kh_chat_id, pro_chat_id, kh_title, manager_name, staff_name,
    )
    logger.info(f"Remembered staff activity in {chat_id}: {', '.join(s['name'] for s in known)}")

# ── Drive setup for case ──────────────────────────────────────────────────────
def setup_drive_folder(case_name: str) -> tuple[str, str]:
    """Create case folder + sub-folders. Returns (folder_id, folder_link)."""
    folder_id = get_or_create_folder(case_name, OPENCLAW_FOLDER_ID, drive_id=SHARED_DRIVE_ID)
    for f in TOP_FOLDERS:
        get_or_create_folder(f, folder_id, drive_id=SHARED_DRIVE_ID)
    get_or_create_folder(OCR_META_FOLDER, folder_id, drive_id=SHARED_DRIVE_ID)
    get_or_create_folder(OLD_FILE_FOLDER, folder_id, drive_id=SHARED_DRIVE_ID)   # inbox cho hồ sơ cũ
    get_or_create_folder(DA_DUYET_FOLDER, folder_id, drive_id=SHARED_DRIVE_ID)   # staff review
    link = f"https://drive.google.com/drive/folders/{folder_id}"
    return folder_id, link

def share_drive_folder(folder_id: str, email: str):
    """Share Drive folder with staff email (reader)."""
    try:
        drive().permissions().create(
            fileId=folder_id,
            supportsAllDrives=True,
            body={"type": "user", "role": "writer", "emailAddress": email},
            sendNotificationEmail=False,
        ).execute()
    except Exception as e:
        logger.warning(f"share_drive_folder {email}: {e}")

def split_responsible_staff(staff_list: list[dict]) -> tuple[str, str]:
    """Return (manager_names, staff_names) from detected group members."""
    managers, staffs = [], []
    for s in staff_list:
        role = s.get("role", "").strip().lower()
        if role == "manager":
            managers.append(s.get("name", ""))
        elif role == "staff":
            staffs.append(s.get("name", ""))
    return ", ".join([x for x in managers if x]), ", ".join([x for x in staffs if x])

def build_case_row_for_headers(headers: list[str], *, case_id: str, applicant: str,
                               visa: str, drive_link: str, kh_chat_id: str,
                               pro_chat_id: str, kh_title: str,
                               manager_name: str = "", staff_name: str = "") -> list[str]:
    """Build a row matching current Cases headers, including Manager/Staff if present."""
    name_no_year = applicant
    birth_year = ""
    m = re.search(r"\b(19\d{2}|20\d{2})\b\s*$", applicant)
    if m:
        birth_year = m.group(1)
        name_no_year = applicant[:m.start()].strip()
    agent = ""
    if " - " in kh_title:
        agent = kh_title.rsplit(" - ", 1)[1].strip()
    program = re.sub(r"^[A-Za-z]+", "", visa).strip() or visa
    today = time.strftime("%d/%m/%Y")
    row = [""] * len(headers)
    values = {
        "TÊN KHÁCH HÀNG": name_no_year,
        "NĂM SINH KH": birth_year,
        "NGÀY NHẬN HS": today,
        "TÊN AGENT/ SALES": agent,
        "MANAGER": manager_name,
        "STAFF": staff_name,
        "LOẠI HS": "NEW",
        "CHƯƠNG TRÌNH": program,
        "Số ngày PRO soạn HSCV": "0",
        "Số ngày KH đủ hồ sơ": "0",
        "Số ngày PRO gom Review": "0",
        "Tổng thời gian PRO xử lý kể từ đóng đợt 2": "0",
        "Case ID": case_id,
        "Visa": visa,
        "Drive": drive_link,
        "Chat ID KH": kh_chat_id,
        "Chat ID Pro": pro_chat_id,
    }
    for i, h in enumerate(headers):
        if h in values:
            row[i] = values[h]
    return row

def upsert_case_to_sheet(sheet_id: str, *, case_id: str, applicant: str, visa: str,
                         drive_link: str, kh_chat_id: str = "", pro_chat_id: str = "",
                         kh_title: str = "", manager_name: str = "", staff_name: str = ""):
    """Upsert a case into the Cases tab, using first visible empty case row."""
    from lib.google_clients import sheets as get_sheets
    svc = get_sheets()
    headers = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="Cases!A1:AB1"
    ).execute().get("values", [[]])[0]
    if not headers:
        raise RuntimeError("Cases header not found")
    row = build_case_row_for_headers(
        headers, case_id=case_id, applicant=applicant, visa=visa, drive_link=drive_link,
        kh_chat_id=kh_chat_id, pro_chat_id=pro_chat_id, kh_title=kh_title,
        manager_name=manager_name, staff_name=staff_name,
    )
    case_idx = headers.index("Case ID") if "Case ID" in headers else 21
    identity_names = {"TÊN KHÁCH HÀNG", "NĂM SINH KH", "NGÀY NHẬN HS", "TÊN AGENT/ SALES", "LOẠI HS", "Case ID", "Visa", "Drive", "Chat ID KH", "Chat ID Pro"}
    identity_idxs = [i for i, h in enumerate(headers) if h in identity_names]
    existing = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"Cases!A2:AB1000"
    ).execute().get("values", [])
    target_row = None
    first_empty_row = None
    for idx, old in enumerate(existing, start=2):
        padded = (old + [""] * len(headers))[:len(headers)]
        if len(padded) > case_idx and padded[case_idx].strip() == case_id:
            target_row = idx
            break
        if first_empty_row is None and not any(str(padded[i]).strip() for i in identity_idxs if i < len(padded)):
            first_empty_row = idx
    write_row = target_row or first_empty_row
    if write_row:
        end_col = "AB" if len(headers) > 26 else "Z"
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"Cases!A{write_row}:{end_col}{write_row}",
            valueInputOption="RAW",
            body={"values": [row]},
        ).execute()
    else:
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="Cases!A:AB",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

def update_summary_sheet(case_id: str, applicant: str, visa: str, drive_link: str,
                         kh_chat_id: str, pro_chat_id: str, kh_title: str,
                         manager_name: str, staff_name: str):
    """Update private all-cases summary sheet. Do not share this file."""
    try:
        upsert_case_to_sheet(
            SUMMARY_SHEET_ID,
            case_id=case_id, applicant=applicant, visa=visa, drive_link=drive_link,
            kh_chat_id=kh_chat_id, pro_chat_id=pro_chat_id, kh_title=kh_title,
            manager_name=manager_name, staff_name=staff_name,
        )
    except Exception as e:
        logger.warning(f"update summary sheet: {e}")

# ── Staff personal Sheet ──────────────────────────────────────────────────────
def get_or_create_staff_sheet(staff: dict, case_id: str, applicant: str,
                               visa: str, drive_link: str, folder_id: str,
                               kh_chat_id: str = "", pro_chat_id: str = "",
                               kh_title: str = "", manager_name: str = "",
                               staff_name: str = "") -> str:
    """Find/create staff Sheet from the sample template, then upsert case row."""
    from lib.google_clients import sheets as get_sheets
    svc = get_sheets()

    sheet_id = staff.get("sheet_id", "")

    def sheet_exists(sid: str) -> bool:
        if not sid:
            return False
        try:
            meta = svc.spreadsheets().get(spreadsheetId=sid, fields="sheets.properties.title").execute()
            titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
            if "Cases" not in titles:
                logger.warning(f"staff sheet {sid} missing Cases tab; recreating from template")
                return False
            return True
        except Exception:
            return False

    def update_staff_master_sheet_id(new_sheet_id: str):
        try:
            r = svc.spreadsheets().values().get(
                spreadsheetId=MASTER_SHEET_ID, range="Staff!A2:E"
            ).execute()
            for i, row in enumerate(r.get("values", []), start=2):
                if row and str(row[0]).strip() == staff["tele_id"]:
                    svc.spreadsheets().values().update(
                        spreadsheetId=MASTER_SHEET_ID,
                        range=f"Staff!E{i}",
                        valueInputOption="RAW",
                        body={"values": [[new_sheet_id]]},
                    ).execute()
                    break
        except Exception as e:
            logger.warning(f"update staff sheet_id: {e}")

    # Create/copy if missing or stale — inside Shared Drive so service account has quota.
    if not sheet_exists(sheet_id):
        title = f"Đồng Hành - {staff['name']}"
        import google.auth
        from googleapiclient.discovery import build as gbuild
        creds, _ = google.auth.default(scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ])
        drive_svc = gbuild("drive", "v3", credentials=creds)
        created = drive_svc.files().copy(
            fileId=STAFF_SHEET_TEMPLATE_ID,
            supportsAllDrives=True,
            body={
            "name": title,
            "parents": [OPENCLAW_FOLDER_ID],
            },
            fields="id",
        ).execute()
        sheet_id = created["id"]
        # Share with staff email
        if staff.get("email"):
            try:
                drive_svc.permissions().create(
                    fileId=sheet_id,
                    body={"type":"user","role":"writer","emailAddress": staff["email"]},
                    supportsAllDrives=True,
                    sendNotificationEmail=False,
                ).execute()
            except Exception as e:
                logger.warning(f"share sheet to {staff['email']}: {e}")
        update_staff_master_sheet_id(sheet_id)

    try:
        upsert_case_to_sheet(
            sheet_id,
            case_id=case_id, applicant=applicant, visa=visa, drive_link=drive_link,
            kh_chat_id=kh_chat_id, pro_chat_id=pro_chat_id, kh_title=kh_title,
            manager_name=manager_name, staff_name=staff_name,
        )
    except Exception as e:
        logger.warning(f"upsert staff sheet: {e}")

    return sheet_id

# ── On bot joins a group ──────────────────────────────────────────────────────
async def on_bot_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fired when bot is added to a group."""
    mcm = update.my_chat_member
    if not mcm: return
    new_status = mcm.new_chat_member.status
    if new_status not in ("member", "administrator"): return

    chat = mcm.chat
    chat_id = str(chat.id)
    title   = chat.title or ""
    info    = parse_group_title(title)

    if not info:
        logger.info(f"Joined unrecognized group: {title}")
        return

    logger.info(f"Joined {info['kind'].upper()} group: {title} ({chat_id})")

    async with REGISTRY_LOCK:
        reg = load_registry()
        pair_key = make_pair_key(info["applicant"], info["visa"])

        # Store this group
        reg[chat_id] = {
            "kind":       info["kind"],
            "pair_key":   pair_key,
            "applicant":  info["applicant"],
            "visa":       info["visa"],
            "raw_title":  title,
            "chat_id":    chat_id,
            "joined_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Check if paired group already registered
        paired = None
        for cid, data in reg.items():
            if cid == chat_id: continue
            if data.get("pair_key") == pair_key and data.get("kind") != info["kind"]:
                paired = data
                break

        if paired:
            # Both groups present → setup case
            kh_data  = reg[chat_id] if info["kind"] == "kh" else paired
            pro_data = reg[chat_id] if info["kind"] == "pro" else paired
            kh_chat_id  = chat_id if info["kind"] == "kh" else paired["chat_id"]
            pro_chat_id = chat_id if info["kind"] == "pro" else paired["chat_id"]
            await _setup_case(context.bot, reg, pair_key,
                              kh_data, pro_data, kh_chat_id, pro_chat_id)
        else:
            logger.info(f"Waiting for paired group (pair_key={pair_key})")

        save_registry(reg)

async def _setup_case(bot: Bot, reg: dict, pair_key: str,
                       kh_data: dict, pro_data: dict,
                       kh_chat_id: str, pro_chat_id: str):
    """Full setup: Drive folder + Staff sheet + permissions."""
    applicant = kh_data["applicant"]
    visa      = kh_data["visa"]
    case_name = f"{applicant} {visa}"

    logger.info(f"Setting up case: {case_name}")

    # 1. Drive folder
    folder_id, drive_link = setup_drive_folder(case_name)

    # 2. Find responsible staff — query both KH and Pro group so staff who are
    # only in the Pro group at setup time are not silently missed.
    all_staff = load_staff()
    staff_in_kh  = await get_group_staff(bot, int(kh_chat_id), all_staff)
    staff_in_pro = await get_group_staff(bot, int(pro_chat_id), all_staff)
    seen_staff_ids = {s["tele_id"] for s in staff_in_kh if s.get("tele_id")}
    staff_in_group = list(staff_in_kh)
    for s in staff_in_pro:
        if s.get("tele_id") and s["tele_id"] not in seen_staff_ids:
            staff_in_group.append(s)
            seen_staff_ids.add(s["tele_id"])
    # Directors always included regardless of group membership detection.
    for s in all_staff:
        if s.get("role", "").strip().lower() == "director" and s.get("tele_id") and s.get("tele_id") not in seen_staff_ids:
            staff_in_group.append(s)
            seen_staff_ids.add(s.get("tele_id"))

    manager_name, staff_name = split_responsible_staff(staff_in_group)
    case_id = re.sub(r'\s+', '-', applicant.upper()[:20]) + f"-{visa}"

    # 3. Share Drive + create/update personal Sheet for each staff
    for s in staff_in_group:
        if s.get("email"):
            share_drive_folder(folder_id, s["email"])
        get_or_create_staff_sheet(
            s, case_id, applicant, visa, drive_link, folder_id,
            kh_chat_id=kh_chat_id, pro_chat_id=pro_chat_id,
            kh_title=kh_data.get("raw_title", ""),
            manager_name=manager_name, staff_name=staff_name,
        )

    # 3b. Private all-cases summary (not shared with staff)
    update_summary_sheet(
        case_id, applicant, visa, drive_link,
        kh_chat_id, pro_chat_id, kh_data.get("raw_title", ""),
        manager_name, staff_name,
    )

    # 4. Update registry with case info
    reg[kh_chat_id].update({"folder_id": folder_id, "drive_link": drive_link,
                              "pro_chat_id": pro_chat_id, "case_setup": True,
                              "staff": [s["tele_id"] for s in staff_in_group]})
    reg[pro_chat_id].update({"folder_id": folder_id, "drive_link": drive_link,
                              "kh_chat_id": kh_chat_id,  "case_setup": True,
                              "staff": [s["tele_id"] for s in staff_in_group]})

    # 5. Notify Pro group
    staff_names = ", ".join(s["name"] for s in staff_in_group) or "chưa xác định"
    pro_text = (f"✅ Case đã được setup\n"
                f"👤 Khách: {applicant}\n"
                f"📋 Visa: {visa}\n"
                f"📁 Drive: {drive_link}\n"
                f"👥 Nhân viên: {staff_names}")
    effective_pro_chat_id = await send_message_handle_migration(bot, pro_chat_id, pro_text)
    if effective_pro_chat_id != str(pro_chat_id):
        reg[effective_pro_chat_id] = {**reg.get(pro_chat_id, {}), **reg[pro_chat_id], "chat_id": effective_pro_chat_id}
        reg[effective_pro_chat_id].update({"folder_id": folder_id, "drive_link": drive_link,
                                           "kh_chat_id": kh_chat_id, "case_setup": True,
                                           "staff": [s["tele_id"] for s in staff_in_group]})
        reg[kh_chat_id]["pro_chat_id"] = effective_pro_chat_id
        pro_chat_id = effective_pro_chat_id
    await send_message_handle_migration(bot, kh_chat_id, "✅")
    logger.info(f"Case setup done: {case_name} | staff: {staff_names}")

# ── Document pipeline (scan_pipeline.py) ─────────────────────────────────────
# The actual unzip → OCR → rename → upload work is delegated to scan_pipeline.py
# (a sibling file; also exposed to the OpenClaw agent via the scan-ho-so-pipeline
# skill, which is just docs). Run as a subprocess so behaviour stays consistent.
# The pipeline:
#   * processes EVERY file in a zip/dir (keeps non pdf/jpg/png too, no OCR)
#   * retries each file, is idempotent, writes a manifest covering all inputs
#   * exits non-zero if anything still failed → we re-run (safe; uploads skip dups)
SCAN_PIPELINE_SCRIPT = os.environ.get(
    "SCAN_PIPELINE_SCRIPT",
    str(Path(__file__).resolve().parent / "scan_pipeline.py"),
)
SCAN_RUN_CONCURRENCY = int(os.environ.get("SCAN_RUN_CONCURRENCY", "2"))
SCAN_RUN_SEMAPHORE = asyncio.Semaphore(SCAN_RUN_CONCURRENCY)
SCAN_MAX_ATTEMPTS = int(os.environ.get("SCAN_MAX_ATTEMPTS", "3"))
# One scan at a time *per case folder* — different cases run in parallel (up to
# SCAN_RUN_CONCURRENCY) but a given case is never processed by two scan_pipeline.py
# runs at once (avoids racing on the same Drive folder / duplicate-named files).
_CASE_LOCKS: dict[str, asyncio.Lock] = {}

def _case_lock(case_key: str) -> asyncio.Lock:
    lk = _CASE_LOCKS.get(case_key)
    if lk is None:
        lk = _CASE_LOCKS[case_key] = asyncio.Lock()
    return lk
# Extensions we accept from a KH group (the pipeline can upload anything; this just
# stops random junk). Mirrors scan_pipeline.py's OCR + "other" extension sets.
ACCEPTED_EXTS = (
    set(EXT_MIME) | ZIP_EXTS
    | {".mov", ".mp4", ".m4v", ".avi", ".heic", ".heif", ".tif", ".tiff",
       ".webp", ".doc", ".docx", ".xls", ".xlsx"}
)


async def run_scan_pipeline(input_path, chat_id: str, case_key: str = "", checklist_only: bool = False,
                            force_rescan: bool = False, sweep_meta: bool = False,
                            case_folder_id: str = "", applicant: str = ""):
    """Run scan_pipeline.py; return the manifest dict.

    Normal mode: process input_path (.zip / dir / file) then run the AI checklist.
    checklist_only=True: skip OCR/upload, only (re)run the checklist for the case
      (input_path is ignored / may be None) — used by the /check command.
    force_rescan=True: bypass hash-dedup so OCR runs lại — dùng cho /oldfile khi cần
      bot reclassify file cũ với rule mới (P1.3).
    sweep_meta=True: trước khi xử lý, dọn .md/.json lạc khỏi 4 folder khách (P1.4).
    case_folder_id + applicant: dùng --case-folder-id thay vì --from-registry (DM mode).

    Serialized per case (case_key) so one case is never processed concurrently;
    different cases run in parallel up to SCAN_RUN_CONCURRENCY. In normal mode
    re-runs idempotently up to SCAN_MAX_ATTEMPTS while files are still failed.
    Returns the last manifest, or None if the script couldn't run.
    """
    if not Path(SCAN_PIPELINE_SCRIPT).exists():
        logger.error(f"scan_pipeline.py not found: {SCAN_PIPELINE_SCRIPT}")
        return None
    man_dir = Path(tempfile.mkdtemp(prefix="scan_manifest_"))
    man_path = man_dir / "manifest.json"
    last_manifest = None
    max_attempts = 1 if checklist_only else SCAN_MAX_ATTEMPTS
    label = "checklist-only" if checklist_only else (Path(input_path).name if input_path else "?")
    async with _case_lock(case_key or str(chat_id)), SCAN_RUN_SEMAPHORE:
        for attempt in range(1, max_attempts + 1):
            cmd = [sys.executable, SCAN_PIPELINE_SCRIPT]
            if checklist_only:
                cmd.append("--checklist-only")
            else:
                cmd.append(str(input_path))
            if case_folder_id:
                cmd += ["--case-folder-id", case_folder_id,
                        "--applicant", applicant or "?",
                        "--manifest", str(man_path)]
            else:
                cmd += ["--from-registry", str(chat_id), "--manifest", str(man_path)]
            if force_rescan:
                cmd.append("--force-rescan")
            if sweep_meta:
                cmd.append("--sweep-meta")
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=os.environ.copy(),
            )
            out, _ = await proc.communicate()
            tail = out.decode(errors="replace")[-4000:] if out else ""
            logger.info(f"scan_pipeline.py attempt {attempt}/{max_attempts} rc={proc.returncode} "
                        f"({label})\n{tail}")
            if man_path.exists():
                try:
                    last_manifest = json.loads(man_path.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"manifest parse failed: {e}")
            if proc.returncode == 0:
                break
            if attempt < max_attempts:
                await asyncio.sleep(min(2 ** attempt, 20))
    try:
        man_path.unlink(missing_ok=True)
        man_dir.rmdir()
    except Exception:
        pass
    return last_manifest


def _checklist_telegram_lines(manifest: dict) -> tuple[str | None, str | None]:
    """From a manifest's checklist block → (line_main, detail) for Telegram, or (None, None)."""
    ck = (manifest or {}).get("checklist") or {}
    if not ck.get("ran"):
        if ck.get("error"):
            logger.info(f"checklist not produced: {ck.get('error')}")
        return None, None
    link = ck.get("report_link") or ck.get("md_link") or ck.get("sheet_link") or ""
    try:
        from lib import checklist as _ck
        return _ck.summarize_for_telegram(ck.get("report") or "", ck.get("coverage") or {},
                                          ck.get("model") or "AI", link)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"checklist summary failed: {e}")
        return (f"🔎 Báo cáo thẩm định: {link}".strip(), None)


def _a(url: str, text) -> str:
    """Telegram-HTML anchor. Empty url → just the escaped text (no link)."""
    txt = html.escape(str(text))
    return f'<a href="{html.escape(str(url), quote=True)}">{txt}</a>' if url else txt


def summarize_manifest(m: dict, drive_link: str = "") -> list[str]:
    """Post-processing summary as Telegram HTML — split into ≤2 messages at folder boundary if too long."""
    c = m.get("counts", {}) or {}
    items = m.get("items", []) or []
    total = m.get("total_input_files", len(items))
    no_ocr = c.get("uploaded-no-ocr", 0)
    split_n = c.get("uploaded-split", 0)
    dup = c.get("duplicate", 0)
    dup_hash = c.get("duplicate-by-hash", 0)
    failed = c.get("failed", 0)
    dropped = list(m.get("dropped_files", []) or [])
    n_out = m.get("total_output_items", len(items))
    review = [it for it in items if it.get("needs_review")]

    # --- Shared helpers ---
    mark = {"uploaded": "•", "uploaded-no-ocr": "▫", "duplicate": "↺",
            "uploaded-split": "✂", "duplicate-by-hash": "🔁", "failed": "✗"}
    failed_items = [it for it in items if it.get("status") == "failed"]
    ok_items = [it for it in items if it.get("status") != "failed"]
    from collections import OrderedDict
    grouped: dict[str, list] = OrderedDict()
    for it in ok_items:
        folder = it.get("folder", "Khac") or "Khac"
        grouped.setdefault(folder, []).append(it)
    bottom = {"Personal Docs", "Khac"}
    order = sorted(grouped.keys(), key=lambda f: (f in bottom, f))

    def _build_header() -> list[str]:
        h: list[str] = []
        if failed == 0 and not dropped:
            h.append(html.escape(f"✅ Đã xử lý {total} file"))
        else:
            h.append(html.escape(f"⚠️ Đã xử lý {total - failed - len(dropped)}/{total} file — "
                                 f"{failed} LỖI, {len(dropped)} BỊ MẤT"))
        new_n = c.get("uploaded", 0) + c.get("uploaded-split", 0) + c.get("uploaded-no-ocr", 0)
        h.append(html.escape(f"📥 Nhận: {total} · 📄 Kết quả: {n_out} item ({n_out - split_n} file gốc + {split_n} tách) · 🆕 Mới: {new_n}") if split_n else
                 html.escape(f"📥 Nhận: {total} · 📄 Kết quả: {n_out} file · 🆕 Mới: {new_n}"))
        if dropped:
            h.append(html.escape(f"⛔ {len(dropped)} FILE BỊ MẤT: ")
                     + ", ".join(html.escape(nm) for nm in dropped[:20])
                     + (f" … (+{len(dropped)-20} nữa)" if len(dropped) > 20 else ""))
        if review:
            h.append(f"⚠️ <b>{len(review)} file cần kiểm tra thủ công</b>")
        extra = []
        if dup_hash: extra.append(f"{dup_hash} trùng nội dung")
        if dup:      extra.append(f"{dup} đã có sẵn")
        if split_n:  extra.append(f"{split_n} tách từ PDF")
        if no_ocr:   extra.append(f"{no_ocr} không OCR")
        if extra:    h.append(html.escape("   (" + " · ".join(extra) + ")"))
        h.append("")
        return h

    def _render_group(folder: str, f_items: list) -> list[str]:
        g: list[str] = []
        n = len(f_items)
        n_rv = sum(1 for it in f_items if it.get("needs_review"))
        rv_note = f" · {n_rv} ⚠️" if n_rv else ""
        g.append(f"📂 <b>{html.escape(folder)}</b> — {n} file{rv_note}")
        for it in f_items:
            st = it.get("status", "?")
            name = it.get("new_name") or it.get("src_name") or "?"
            rv_prefix = "⚠️ " if it.get("needs_review") else ""
            tag = it.get("tag", "")
            suffix = ""
            if it.get("needs_review") and (tag or "").lower() == "khac":
                suffix = " (không nhận diện được)"
            g.append(f"  {mark.get(st, '•')} {rv_prefix}"
                     + _a(it.get("drive_link", ""), name) + html.escape(suffix))
        g.append("")
        return g

    def _build_failed() -> list[str]:
        if not failed_items:
            return []
        g: list[str] = ["❌ <b>Lỗi:</b>"]
        for it in failed_items:
            g.append(f"  ✗ " + html.escape(str(it.get("src_name") or "?"))
                     + " — " + html.escape(str(it.get("error", "?"))))
        g.append("")
        return g

    def _build_summary() -> list[str]:
        parts = []
        for folder in order:
            n = len(grouped[folder])
            n_rv = sum(1 for it in grouped[folder] if it.get("needs_review"))
            rv_flag = " ⚠️" if n_rv else ""
            parts.append(f"{html.escape(folder)}: {n}{rv_flag}")
        return ["📊 <b>Tổng folder:</b> " + " · ".join(parts)]

    def _build_footer() -> list[str]:
        f: list[str] = []
        if review:
            f.append("")
            f.append("⚠️ Cần kiểm tra: " + ", ".join(
                _a(it.get("drive_link", ""), it.get("src_name") or it.get("new_name") or "?") for it in review))
        if drive_link:
            f.append("")
            f.append("📁 " + _a(drive_link, "Thư mục hồ sơ trên Drive"))
        l_main, _ = _checklist_telegram_lines(m)
        if l_main:
            f.append("")
            f.append(html.escape(l_main))
        return f

    header = _build_header()
    failed_block = _build_failed()
    footer = _build_footer()
    summary = _build_summary()
    groups = [(folder, _render_group(folder, grouped[folder])) for folder in order]

    # Try single message
    all_lines = header + [ln for _, gl in groups for ln in gl] + failed_block + summary + footer
    LIMIT = 3900
    full = "\n".join(all_lines)
    if len(full) <= LIMIT:
        return [full]

    # Need to split at folder boundary. Accumulate groups until hitting 2500 chars (room for part2).
    part1 = list(header)
    split_at = 0
    for idx, (fname, glines) in enumerate(groups):
        candidate = "\n".join(part1 + glines + ["(còn tiếp…)"])
        if len(candidate) > LIMIT and idx > 0:
            split_at = idx
            break
        part1.extend(glines)
        split_at = idx + 1

    if split_at >= len(groups) or split_at == 0:
        # Can't split cleanly — return truncated single message
        return [full[:LIMIT - 20] + "\n…(rút gọn)"]

    # Build part2: remaining groups + failed + summary + footer
    remaining_groups = [ln for _, gl in groups[split_at:] for ln in gl]
    part2_lines = ["📋 <b>Tiếp theo</b> — " + ", ".join(
        html.escape(f) for f, _ in groups[split_at:])] + [""] + remaining_groups
    # Only include failed + summary + footer in part2
    if failed_items:
        part2_lines.extend(failed_block)
    part2_lines.extend(summary)
    part2_lines.extend(footer)

    # Add continuation note to part1
    part1_str = "\n".join(part1 + ["📋 <b>Còn tiếp…</b>"])
    if len(part1_str) > LIMIT:
        part1_str = part1_str[:LIMIT - 20] + "\n…(rút gọn)"

    return [part1_str, "\n".join(part2_lines)]


async def _send_summary(bot, chat_id, parts: list[str], **kwargs):
    """Send summary parts as separate Telegram messages."""
    for part in parts:
        await send_html(bot, int(chat_id), part, **kwargs)


# ── Debounce buffer: gom file của một nhóm KH đến gần nhau → một lần scan ─────
class _PendingBatch:
    """Các file từ MỘT nhóm KH đến gần nhau → một lần scan."""
    __slots__ = ("chat_id", "workdir", "names", "first_monotonic", "gen", "task",
                 "flushing", "pro_chat_id", "drive_link", "folder_id")
    def __init__(self, chat_id, workdir, pro_chat_id, drive_link, folder_id):
        self.chat_id = chat_id
        self.workdir = workdir                 # 1 tempdir cho cả đợt (prefix donghanh_batch_)
        self.names: list = []                  # tên file trong workdir, ĐÃ reserve trước khi tải
        self.first_monotonic = time.monotonic()
        self.gen = 0                           # tăng mỗi khi có file mới gia nhập batch
        self.task = None                       # flush-task đang chờ (asyncio.Task | None)
        self.flushing = False                  # True khi _flush_batch_after đã chốt batch
        self.pro_chat_id = str(pro_chat_id)
        self.drive_link  = drive_link or ""
        self.folder_id   = folder_id or ""


def _unique_in_dir(workdir: Path, taken: list, src_name: str) -> str:
    """Tên file không trùng trong workdir VÀ không trùng `taken` (2 handle_file đan xen
    reserve tên trước khi tải xong). Giữ basename gốc (scan_pipeline.py còn dùng làm gợi ý phân loại);
    nếu trùng thì chèn ' (2)', ' (3)' … trước phần mở rộng."""
    base = Path(src_name).name or "file.bin"
    suf  = Path(base).suffix
    stem = base[:-len(suf)] if suf else base
    seen = set(taken); cand = base; i = 2
    while cand in seen or (workdir / cand).exists():
        cand = f"{stem} ({i}){suf}"; i += 1
    return cand


def _get_or_create_batch(chat_id, pro_chat_id, drive_link, folder_id):
    """Trả (batch, created). Dùng lại batch sống của chat_id trừ khi nó đã bắt đầu flush.
    KHÔNG có await ⇒ chạy nguyên khối, 2 handle_file của cùng chat thấy cùng một batch."""
    b = _PENDING_BATCHES.get(chat_id)
    if b is not None and not b.flushing:
        return b, False
    b = _PendingBatch(chat_id, Path(tempfile.mkdtemp(prefix="donghanh_batch_")),
                      pro_chat_id, drive_link, folder_id)
    _PENDING_BATCHES[chat_id] = b
    return b, True


def _extract_zip_flat(zip_path: Path, dest_dir: Path) -> int:
    """Giải nén 1 cấp các member 'thật' của zip_path vào dest_dir (giữ basename gốc,
    dedupe ' (2)', ' (3)' … nếu trùng), bỏ qua __MACOSX/._*/.DS_Store. Trả số file đã giải nén."""
    n = 0
    with zipfile.ZipFile(zip_path) as zf:
        for m in zf.infolist():
            if m.is_dir():
                continue
            rel = Path(m.filename)
            if (not rel.name or "__MACOSX" in rel.parts
                    or rel.name.startswith("._") or rel.name == ".DS_Store"):
                continue
            dest = _unique_in_dir(dest_dir, [], rel.name)
            with zf.open(m) as src, (dest_dir / dest).open("wb") as fh:
                shutil.copyfileobj(src, fh)
            n += 1
    return n


def _expand_zips_in_dir(workdir: Path) -> None:
    """Giải nén tại chỗ mọi .zip trong workdir (1 cấp) rồi xoá file zip — để scan_pipeline.py
    xử lý từng file bên trong (collect_from_dir chỉ glob file, không tự mở zip)."""
    for zp in [p for p in sorted(workdir.iterdir()) if p.is_file() and p.suffix.lower() == ".zip"]:
        try:
            n_ext = _extract_zip_flat(zp, workdir)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"zip extract failed {zp.name}: {e}; để nguyên file zip")
            continue
        if n_ext > 0:
            zp.unlink(missing_ok=True)
            logger.info(f"unzipped {zp.name} → {n_ext} file(s)")


async def _flush_batch_after(context, chat_id, batch, my_gen: int, delay: float):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return                                  # bị file mới reschedule; task mới sẽ lo
    # chỉ chốt nếu mình là lần hẹn mới nhất và batch chưa bị chốt
    if batch.flushing or batch.gen != my_gen or _PENDING_BATCHES.get(chat_id) is not batch:
        return
    batch.flushing = True
    _PENDING_BATCHES.pop(chat_id, None)         # synchronous, trước mọi await ⇒ file mới mở batch mới
    pro_chat_id = batch.pro_chat_id
    # Ghi checkpoint trước khi pipeline — nếu bot bị kill, startup retry sẽ dọn
    _CHECKPOINT_DIR.mkdir(exist_ok=True)
    _ckpt = _CHECKPOINT_DIR / f"{chat_id}.json"
    _ckpt.write_text(json.dumps({
        "chat_id": chat_id, "pro_chat_id": pro_chat_id,
        "drive_link": batch.drive_link, "created_at": time.time(),
    }), encoding="utf-8")
    try:
        if batch.workdir.exists():
            _expand_zips_in_dir(batch.workdir)          # giải nén .zip tại chỗ trước khi scan
        files = (sorted(p.name for p in batch.workdir.iterdir() if p.is_file())
                 if batch.workdir.exists() else [])
        if not files:
            logger.info(f"debounce flush {chat_id}: no files, nothing to do")
            _ckpt.unlink(missing_ok=True)
            return
        manifest = await run_scan_pipeline(batch.workdir, chat_id, case_key=batch.folder_id)
        try:  # giấy tờ vừa đổi → chat phải thấy data mới
            chatmod.invalidate_case_cache(batch.folder_id)
        except Exception:
            pass
        if manifest is None:
            logger.error(f"scan pipeline could not run for batch {chat_id} ({len(files)} file)")
            return  # stay silent in Telegram; logs have the details (checkpoint persists for retry)
        if not manifest.get("items"):
            await context.bot.send_message(chat_id=int(pro_chat_id),
                                           text="⚠️ Lô file vừa gửi không có gì để xử lý.")
            _ckpt.unlink(missing_ok=True)
            return
        await _send_summary(context.bot, pro_chat_id, summarize_manifest(manifest, batch.drive_link),
                           disable_web_page_preview=True)
        # AI checklist — a short second message confirming "đã thẩm định" + link
        _, ck_detail = _checklist_telegram_lines(manifest)
        if ck_detail:
            try:
                await send_html(context.bot, pro_chat_id, ck_detail, disable_web_page_preview=True)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"checklist detail send failed: {e}")
        c = manifest.get("counts", {}) or {}
        logger.info(f"Scan done batch {chat_id} ({len(files)} file): "
                    f"total={manifest.get('total_input_files')} uploaded={c.get('uploaded',0)} "
                    f"split={c.get('uploaded-split',0)} no_ocr={c.get('uploaded-no-ocr',0)} "
                    f"dup={c.get('duplicate',0)} dup_hash={c.get('duplicate-by-hash',0)} "
                    f"failed={c.get('failed',0)}")
        _ckpt.unlink(missing_ok=True)   # Telegram gửi xong → xóa checkpoint
    except Exception as e:
        logger.error(f"_flush_batch_after {chat_id}: {e}", exc_info=True)
    finally:
        shutil.rmtree(batch.workdir, ignore_errors=True)


# ── Handle file from KH group ─────────────────────────────────────────────────
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return

    chat_id = str(msg.chat.id)
    logger.info(f"MSG from {chat_id} ({msg.chat.title!r}) type={msg.chat.type} doc={bool(msg.document)} photo={bool(msg.photo)}")

    reg = load_registry()
    info = reg.get(chat_id)
    if not info:
        logger.info(f"Chat {chat_id} not in registry")
        return

    # Only process files from KH group
    if info.get("kind") != "kh": return

    # Must be setup
    if not info.get("case_setup") or not info.get("folder_id"):
        return  # silently ignore — not setup yet

    pro_chat_id = info.get("pro_chat_id")
    if not pro_chat_id: return

    # Determine the file (keep its real name so filename hints work)
    tg_file = None; src_name = ""
    if msg.document:
        doc = msg.document
        src_name = Path(doc.file_name).name if doc.file_name else f"file_{doc.file_id[:8]}.bin"
        if Path(src_name).suffix.lower() not in ACCEPTED_EXTS:
            logger.info(f"Skipping unsupported file: {src_name}")
            return
        tg_file = await doc.get_file()
    elif msg.photo:
        src_name = f"photo_{int(time.time()*1000)}.jpg"   # ms ⇒ phân biệt trong một đợt
        tg_file  = await msg.photo[-1].get_file()
    if not tg_file: return

    # ── gia nhập (hoặc mở) batch debounce của chat này ───────────────────────
    batch, created = _get_or_create_batch(chat_id, pro_chat_id,
                                          info.get("drive_link", ""), info.get("folder_id", ""))
    batch.gen += 1                                          # đánh dấu: file này thuộc batch
    dest = _unique_in_dir(batch.workdir, batch.names, src_name)
    batch.names.append(dest)                                # reserve SYNCHRONOUS (không await ở giữa)
    in_path = batch.workdir / dest

    if created and SCAN_DEBOUNCE_ACK:
        try:
            await context.bot.send_message(
                chat_id=int(pro_chat_id),
                text="📥 Đang nhận hồ sơ, sẽ kiểm tra & báo cáo sau khi gửi xong…",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"debounce ack send failed: {e}")

    try:
        async with _DOWNLOAD_SEM:
            await tg_file.download_to_drive(str(in_path))
    except Exception as e:
        logger.error(f"download failed {src_name} ({chat_id}): {e}", exc_info=True)
        # tên vẫn reserve; thiếu file trong dir thì collect_from_dir đơn giản bỏ qua, không sao
    finally:
        # luôn (re)hẹn flush — kể cả khi tải lỗi — để batch chắc chắn được chốt
        if batch.task is not None and not batch.task.done():
            batch.task.cancel()
        my_gen  = batch.gen
        elapsed = time.monotonic() - batch.first_monotonic
        delay   = min(SCAN_DEBOUNCE_SECONDS, max(0.0, SCAN_DEBOUNCE_MAX_WAIT - elapsed))
        batch.task = asyncio.create_task(_flush_batch_after(context, chat_id, batch, my_gen, delay))

# ── Main ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

async def debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if msg:
        logger.info(f"DEBUG update chat={msg.chat.id} title={msg.chat.title!r} text={bool(msg.text)} doc={bool(msg.document)} photo={bool(msg.photo)}")


# ── /check — re-run the AI checklist on demand (Pro group hoặc DM) ───────────────
async def on_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    chat_id = str(chat.id)
    user_id = msg.from_user.id if msg.from_user else None

    # ── DM mode ──────────────────────────────────────────────────────────
    if chat.type == "private":
        if not user_id or not (staff_by_telegram_id(str(user_id)) or str(user_id) in STAFF_TELE_IDS):
            return
        reg = load_registry()
        my_cases = chatmod.cases_for_staff(reg, user_id)
        sess = chatmod.dm_session(user_id)
        folder_id = sess.get("folder")
        if not folder_id:
            await context.bot.send_message(chat_id=chat.id,
                text="Anh chọn hồ sơ nào để đối chiếu?")
            await _present_case_picker(context.bot, chat.id, user_id, my_cases)
            return
        dm_info = next((c for _, c in my_cases if c.get("folder_id") == folder_id), None)
        if not dm_info:
            await context.bot.send_message(chat_id=chat.id,
                text="❌ Không tìm thấy hồ sơ đang chọn. Dùng /case để chọn lại.")
            return
        applicant_dm = dm_info.get("applicant", "?")
        try:
            await context.bot.send_message(chat_id=chat.id,
                text=f"⏳ Đang đối chiếu hồ sơ {applicant_dm}…")
        except Exception:
            pass
        try:
            manifest = await run_scan_pipeline(
                None, chat_id, case_key=folder_id,
                checklist_only=True, sweep_meta=True,
                case_folder_id=folder_id, applicant=applicant_dm,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"/check DM user={user_id}: {e}", exc_info=True)
            await context.bot.send_message(chat_id=chat.id, text=f"❌ Lỗi khi đối chiếu: {e}")
            return
        try:
            chatmod.invalidate_case_cache(folder_id)
        except Exception:
            pass
        ck = (manifest or {}).get("checklist") or {}
        if not ck.get("ran"):
            await context.bot.send_message(chat_id=chat.id,
                text=f"⚠️ Chưa đối chiếu được: {ck.get('error') or 'không rõ lý do (xem log)'}")
            return
        l_main, detail = _checklist_telegram_lines(manifest)
        if l_main:
            await context.bot.send_message(chat_id=chat.id, text=l_main, disable_web_page_preview=True)
        if detail:
            await send_html(context.bot, chat_id, detail, disable_web_page_preview=True)
        return

    # ── Pro group mode ────────────────────────────────────────────────────
    info = (load_registry() or {}).get(chat_id) or {}
    if info.get("kind") != "pro":
        return  # ignore /check anywhere that isn't a Pro group
    if not info.get("case_setup") or not info.get("folder_id"):
        try:
            await context.bot.send_message(chat_id=int(chat_id), text="⚠️ Group này chưa setup case.")
        except Exception:
            pass
        return
    try:
        await context.bot.send_message(chat_id=int(chat_id), text="⏳ Đang đối chiếu hồ sơ…")
    except Exception:
        pass
    try:
        # P1.4 — sweep_meta=True ở /check: dọn sidecar lạc trong folder khách trước khi thẩm định.
        manifest = await run_scan_pipeline(None, chat_id, case_key=info.get("folder_id", ""),
                                            checklist_only=True, sweep_meta=True)
    except Exception as e:  # noqa: BLE001
        logger.error(f"/check failed for {chat_id}: {e}", exc_info=True)
        await context.bot.send_message(chat_id=int(chat_id), text=f"❌ Lỗi khi đối chiếu: {e}")
        return
    try:
        chatmod.invalidate_case_cache(info.get("folder_id", ""))
    except Exception:
        pass
    ck = (manifest or {}).get("checklist") or {}
    if not ck.get("ran"):
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"⚠️ Chưa đối chiếu được: {ck.get('error') or 'không rõ lý do (xem log)'}",
        )
        return
    l_main, detail = _checklist_telegram_lines(manifest)
    if l_main:
        await context.bot.send_message(chat_id=int(chat_id), text=l_main, disable_web_page_preview=True)
    if detail:
        await send_html(context.bot, chat_id, detail, disable_web_page_preview=True)


# ── /oldfile: scan Drive folder `<case>/Old File` và đẩy qua cùng pipeline như Telegram batch ──
async def on_oldfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat = msg.chat
    user_id = msg.from_user.id if msg.from_user else None

    # ── Resolve case: DM vs Pro group ─────────────────────────────────────────
    _case_folder_id_arg = ""   # set in DM branch → --case-folder-id; empty → --from-registry
    if chat.type == "private":
        if not user_id or not (staff_by_telegram_id(str(user_id)) or str(user_id) in STAFF_TELE_IDS):
            return
        reg = load_registry()
        my_cases = chatmod.cases_for_staff(reg, user_id)
        sess = chatmod.dm_session(user_id)
        _folder = sess.get("folder")
        if not _folder:
            await context.bot.send_message(chat_id=chat.id,
                text="Anh chọn hồ sơ nào để chạy Old File?")
            await _present_case_picker(context.bot, chat.id, user_id, my_cases)
            return
        _dm_info = next((c for _, c in my_cases if c.get("folder_id") == _folder), None)
        if not _dm_info:
            await context.bot.send_message(chat_id=chat.id,
                text="❌ Không tìm thấy hồ sơ đang chọn. Dùng /case để chọn lại.")
            return
        chat_id = str(chat.id)
        folder_id = _dm_info["folder_id"]
        applicant = _dm_info.get("applicant", "?")
        drive_link = _dm_info.get("drive_link", "")
        _case_folder_id_arg = folder_id
    else:
        chat_id = str(chat.id)
        _info = (load_registry() or {}).get(chat_id) or {}
        if _info.get("kind") != "pro":
            return
        if not _info.get("case_setup") or not _info.get("folder_id"):
            try:
                await context.bot.send_message(chat_id=int(chat_id), text="⚠️ Group này chưa setup case.")
            except Exception:
                pass
            return
        folder_id = _info["folder_id"]
        applicant = _info.get("applicant", "?")
        drive_link = _info.get("drive_link", "")

    # ── Shared body: lock + download + pipeline + move ─────────────────────────

    # Per-case lock: ngăn 2 lần /oldfile chạy đồng thời cùng case.
    lock = _OLDFILE_LOCKS.setdefault(folder_id, asyncio.Lock())
    if lock.locked():
        try:
            await context.bot.send_message(chat_id=int(chat_id),
                text="⏳ Đang xử lý Old File, chờ chút.", reply_to_message_id=msg.message_id)
        except Exception:
            pass
        return

    async with lock:
        # Lazy-create folder Old File (case cũ chưa có → tạo ngay).
        try:
            old_file_id = get_or_create_folder(OLD_FILE_FOLDER, folder_id, drive_id=SHARED_DRIVE_ID)
        except Exception as e:  # noqa: BLE001
            logger.error(f"/oldfile resolve Old File folder failed for {chat_id}: {e}", exc_info=True)
            await context.bot.send_message(chat_id=int(chat_id), text=f"❌ Không truy cập được thư mục Old File: {e}")
            return

        try:
            # Long-lived bot process: _LIST_CACHE có thể stale (lần 1 thấy rỗng → cache rỗng;
            # staff dump file → cache cũ vẫn rỗng → tin "đang trống" sai). Luôn refresh.
            invalidate_list_cache(old_file_id)
            files = list_folder(old_file_id, drive_id=SHARED_DRIVE_ID)
        except Exception as e:  # noqa: BLE001
            logger.error(f"/oldfile list Old File failed for {chat_id}: {e}", exc_info=True)
            await context.bot.send_message(chat_id=int(chat_id), text=f"❌ Không liệt kê được Old File: {e}")
            return

        # Bỏ qua subfolder _processed (list_folder vốn chỉ trả non-folder; vẫn defensive ở đây).
        items = [(name, fid) for name, fid in files.items() if name and fid and name != OLD_FILE_PROCESSED]
        if not items:
            old_file_link = f"https://drive.google.com/drive/folders/{old_file_id}"
            await send_html(context.bot, chat_id,
                f'📂 Thư mục <a href="{html.escape(old_file_link, quote=True)}">Old File</a> '
                f"của hồ sơ {html.escape(applicant)} đang trống. "
                f"Anh kéo file (hoặc .zip) hồ sơ cũ vào đó rồi gõ lại /oldfile.",
                disable_web_page_preview=True)
            return

        try:
            await context.bot.send_message(chat_id=int(chat_id),
                text=f"📥 Đang xử lý {len(items)} file từ Old File của {applicant}…",
                reply_to_message_id=msg.message_id)
        except Exception:
            pass

        # Download → workdir tạm. Giữ tên gốc trên Drive; dedup nếu trùng; bỏ ký tự `/`/`\` cho an toàn FS.
        workdir = Path(tempfile.mkdtemp(prefix="donghanh_oldfile_"))
        downloaded: list[tuple[str, str, str]] = []   # (drive_name, drive_file_id, local_name)
        try:
            for drive_name, drive_fid in items:
                safe = re.sub(r"[\\/]+", "_", drive_name).strip() or f"file-{drive_fid}"
                local = _unique_in_dir(workdir, [], safe)
                try:
                    data = download_file_bytes(drive_fid, drive_id=SHARED_DRIVE_ID)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"/oldfile download skip {drive_name!r} ({drive_fid}): {e}")
                    continue
                (workdir / local).write_bytes(data)
                downloaded.append((drive_name, drive_fid, local))

            if not downloaded:
                await context.bot.send_message(chat_id=int(chat_id),
                    text="⚠️ Không tải được file nào từ Old File. Xem log để chi tiết.")
                return

            _expand_zips_in_dir(workdir)

            # P1.3+P1.4 — /oldfile: force_rescan + sweep_meta để bot reclassify file cũ
            # với rule mới + dọn sidecar lạc trong folder khách trước khi xử lý.
            manifest = await run_scan_pipeline(workdir, chat_id, case_key=folder_id,
                                                force_rescan=True, sweep_meta=True,
                                                case_folder_id=_case_folder_id_arg,
                                                applicant=applicant if _case_folder_id_arg else "")
            try:
                chatmod.invalidate_case_cache(folder_id)
            except Exception:
                pass

            if manifest is None:
                logger.error(f"/oldfile scan_pipeline could not run for {chat_id} ({len(downloaded)} file)")
                await context.bot.send_message(chat_id=int(chat_id),
                    text="❌ scan_pipeline không chạy được; xem log để chi tiết.")
                return

            if not manifest.get("items"):
                await context.bot.send_message(chat_id=int(chat_id),
                    text="⚠️ Lô Old File không có gì để xử lý.")
                return

            await _send_summary(context.bot, chat_id, summarize_manifest(manifest, drive_link),
                               disable_web_page_preview=True)
            _, ck_detail = _checklist_telegram_lines(manifest)
            if ck_detail:
                try:
                    await send_html(context.bot, chat_id, ck_detail, disable_web_page_preview=True)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"/oldfile checklist detail send failed: {e}")

            # P1.3 — chỉ move file ĐÃ XỬ LÝ THÀNH CÔNG sang _processed. File `failed`
            # và file silent-drop để nguyên trong Old File để staff thấy + retry.
            processed_src_names: set[str] = set()
            for it in manifest.get("items", []) or []:
                st = it.get("status", "")
                if st in ("uploaded", "uploaded-no-ocr", "uploaded-split", "duplicate", "duplicate-by-hash"):
                    if it.get("src_name"):
                        processed_src_names.add(it["src_name"])
                    if it.get("split_from"):
                        processed_src_names.add(it["split_from"])
            try:
                processed_id = get_or_create_folder(OLD_FILE_PROCESSED, old_file_id, drive_id=SHARED_DRIVE_ID)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"/oldfile create _processed failed: {e}")
                processed_id = None
            if processed_id:
                moved = 0
                kept = 0
                for drive_name, drive_fid, local in downloaded:
                    # Local có thể là tên zip → các file giải nén; manifest dùng tên file gốc trong zip.
                    # Match an toàn: nếu drive_name HOẶC local có trong processed_src_names → move.
                    src_match = drive_name in processed_src_names or local in processed_src_names
                    if not src_match:
                        # Kiểm tra thêm fuzzy: zip giải nén có thể có prefix "001_..."
                        for nm in processed_src_names:
                            if nm and (local.endswith(nm) or drive_name.endswith(nm)):
                                src_match = True
                                break
                    if not src_match:
                        kept += 1
                        logger.info(f"/oldfile giữ {drive_name!r} trong Old File (chưa xử lý xong)")
                        continue
                    try:
                        move_file(drive_fid, processed_id, drive_id=SHARED_DRIVE_ID)
                        moved += 1
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"/oldfile move {drive_name!r} ({drive_fid}) → _processed failed: {e}")
                logger.info(f"/oldfile moved {moved}/{len(downloaded)} file vào _processed (giữ lại {kept} file)")

            c = manifest.get("counts", {}) or {}
            logger.info(f"OLDFILE chat={chat_id} case={applicant!r} "
                        f"N_in={len(downloaded)} uploaded={c.get('uploaded',0)} "
                        f"split={c.get('uploaded-split',0)} no_ocr={c.get('uploaded-no-ocr',0)} "
                        f"dup={c.get('duplicate',0)} dup_hash={c.get('duplicate-by-hash',0)} "
                        f"failed={c.get('failed',0)}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"/oldfile {chat_id}: {e}", exc_info=True)
            try:
                await context.bot.send_message(chat_id=int(chat_id), text=f"❌ Lỗi khi xử lý Old File: {e}")
            except Exception:
                pass
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# ── Chat: nhân viên hỏi-đáp về hồ sơ KH (nhóm Pro khi @mention/reply bot · DM riêng) ──────────
def _agent_from_title(raw_title: str) -> str:
    rt = raw_title or ""
    return rt.split(" - ", 1)[-1].strip() if " - " in rt else "?"


async def _setup_streaming(bot, chat_id, reply_to_id: int | None = None):
    """Gửi ack tin "🤖 ⏳" + trả (ack_message_id, on_chunk_callback) cho stream chat.

    on_chunk(delta) async → buffer + throttled edit_message_text mỗi ~1.2s.
    Caller dùng để pass cho `chatmod.answer_question(..., stream_callback=on_chunk)`.
    Cuối: caller phải gọi edit_message_text 1 lần nữa với HTML final (linkify wrap).
    """
    ack = await bot.send_message(chat_id=int(chat_id), text="🤖 ⏳",
                                  reply_to_message_id=reply_to_id)
    msg_id = ack.message_id
    state = {"buf": "", "last_edit": 0.0, "thinking_shown": False}
    EDIT_INTERVAL = 1.2
    THINKING_INTERVAL = 5.0   # Fix A — đỡ rate limit khi reasoning stream nhanh
    MAX_PREVIEW = 3800   # Telegram message limit 4096; chừa cho " ⏳"

    async def on_chunk(delta: str):
        # Fix A — delta="" là heartbeat từ phase reasoning (DeepSeek V4). Hiển thị spinner
        # để user biết bot chưa treo, KHÔNG ghi vào buf.
        if not delta:
            now = time.monotonic()
            if not state["thinking_shown"] and now - state["last_edit"] >= THINKING_INTERVAL:
                try:
                    await bot.edit_message_text(chat_id=int(chat_id), message_id=msg_id,
                                                 text="🤖 đang suy nghĩ… ⏳", parse_mode=None,
                                                 disable_web_page_preview=True)
                    state["last_edit"] = now
                    state["thinking_shown"] = True
                except Exception:  # noqa: BLE001
                    pass
            return
        state["buf"] += delta
        now = time.monotonic()
        if now - state["last_edit"] >= EDIT_INTERVAL:
            preview = state["buf"][:MAX_PREVIEW] + " ⏳"
            try:
                await bot.edit_message_text(chat_id=int(chat_id), message_id=msg_id,
                                             text=preview, parse_mode=None,
                                             disable_web_page_preview=True)
                state["last_edit"] = now
                state["thinking_shown"] = False   # reset — đã sang content phase
            except Exception:  # noqa: BLE001 — rate limit / no-change OK
                pass

    return msg_id, on_chunk


async def _finalize_streaming(bot, chat_id, msg_id: int, final_html: str,
                               plain_fallback: str) -> None:
    """Edit ack tin thành final HTML (linkify wrapped). Fallback plain nếu HTML fail.

    Fix B: User đã thấy nội dung qua stream. Nếu finalize timeout/network → KHÔNG bubble lên
    `on_chat_message` (sẽ post "❌ Lỗi khi trả lời" thừa thãi). Chỉ log warning + giữ text streamed.
    """
    try:
        await bot.edit_message_text(chat_id=int(chat_id), message_id=msg_id,
                                     text=final_html, parse_mode=ParseMode.HTML,
                                     disable_web_page_preview=True)
        return
    except (TimedOut, NetworkError, RetryAfter) as e:
        logger.warning(f"streaming HTML finalize timeout/network (giữ stream text): {e}")
        return
    except BadRequest as e:
        logger.warning(f"streaming HTML edit fail ({e}); fallback plain")
    try:
        await bot.edit_message_text(chat_id=int(chat_id), message_id=msg_id,
                                     text=plain_fallback or "(empty)", parse_mode=None,
                                     disable_web_page_preview=True)
    except (TimedOut, NetworkError, RetryAfter) as e:
        logger.warning(f"streaming plain finalize timeout (giữ stream text): {e}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"streaming plain edit fail (ignored): {e}")


# ── /case picker (DM only) ──────────────────────────────────────────────────
# Khi staff phụ trách hàng trăm case, sticky session lo 95% câu hỏi liền nhau cùng 1 KH;
# khi cần đổi case nhanh thì /case → inline button cho 8 case gần đây + nút search.
async def _staff_my_cases(user_id) -> list:
    reg = load_registry()
    return chatmod.cases_for_staff(reg, user_id)

def _build_case_keyboard(cases: list, *, include_search: bool = True,
                          fallback_for_search: str = "") -> InlineKeyboardMarkup:
    """`cases` = list[info dict]. Mỗi case 1 nút riêng (text = applicant).
    callback_data = `pickcase:<folder_id>` (Telegram giới hạn 64 bytes — folder_id Drive < 50 ký tự OK)."""
    rows = []
    for info in cases:
        fid = info.get("folder_id", "")
        if not fid:
            continue
        rows.append([InlineKeyboardButton(
            text=(info.get("applicant", "?") or "?")[:60],
            callback_data=f"pickcase:{fid}",
        )])
    if include_search:
        rows.append([InlineKeyboardButton(text="🔍 Tìm KH khác", callback_data="pickcase:search")])
    return InlineKeyboardMarkup(rows)

def _recent_then_alpha(my_cases: list, user_id) -> list:
    """Lấy tối đa 8 case: ưu tiên recent của staff, lấp đầy bằng case còn lại theo alphabet applicant."""
    by_folder = {info.get("folder_id", ""): info for _, info in my_cases if info.get("folder_id")}
    out: list = []
    seen: set = set()
    for entry in (_RECENT_BY_STAFF.get(str(user_id)) or []):
        fid = entry.get("folder_id", "")
        if fid in by_folder and fid not in seen:
            out.append(by_folder[fid])
            seen.add(fid)
        if len(out) >= RECENT_MAX:
            break
    if len(out) < RECENT_MAX:
        rest = sorted([info for fid, info in by_folder.items() if fid not in seen],
                      key=lambda i: (i.get("applicant", "") or "").lower())
        for info in rest:
            out.append(info)
            if len(out) >= RECENT_MAX:
                break
    return out

async def _present_case_picker(bot, chat_id: int, user_id, my_cases: list, query: str = "") -> None:
    """Trả về DM tin nhắn picker. `query` rỗng → recent+alpha; có query → fuzzy match.

    Logic match khi có query:
      - chatmod._match_case duy nhất 1 hit → pick instant (đã edit message từ caller).
      - 2-10 conflict → buttons cho mấy case đó.
      - Không match scoring → substring `_norm(query)` trong `_norm(applicant)` ≤ 10 case → buttons.
      - >10 → "Quá nhiều kết quả, anh gõ chi tiết hơn".
    """
    if not my_cases:
        await bot.send_message(chat_id=chat_id,
            text="Bạn chưa được giao hồ sơ nào trong hệ thống. Hãy hỏi trực tiếp trong nhóm Pro.")
        return
    if not query.strip():
        cases = _recent_then_alpha(my_cases, user_id)
        n = len(my_cases)
        header = (f"🤖 Anh đang phụ trách {n} hồ sơ. Chạm để chọn, hoặc gõ /case <tên> để tìm:"
                  if n > RECENT_MAX else f"🤖 Anh đang phụ trách {n} hồ sơ. Chạm để chọn:")
        await bot.send_message(chat_id=chat_id, text=header, reply_markup=_build_case_keyboard(cases))
        return
    info, conflicts = chatmod._match_case(query, my_cases)
    if info is not None:
        await _commit_pick(bot, chat_id, user_id, info, ack_text=f"✅ Đã chọn: {info.get('applicant', '?')}")
        return
    if conflicts:
        await bot.send_message(chat_id=chat_id,
            text=f"Khớp {len(conflicts)} hồ sơ — chạm để chọn:",
            reply_markup=_build_case_keyboard(conflicts, include_search=False))
        return
    # Fuzzy fallback: substring normalize query trên applicant
    qn = chatmod._norm(query)
    sub = [info for _, info in my_cases if qn in chatmod._norm(info.get("applicant", "") or "")]
    if not sub:
        await bot.send_message(chat_id=chat_id,
            text=f"Không tìm thấy hồ sơ khớp \"{query}\". Gõ tên KH hoặc /case xem 8 hồ sơ gần đây.")
        return
    if len(sub) > 10:
        await bot.send_message(chat_id=chat_id,
            text=f"Tìm thấy {len(sub)} hồ sơ khớp — quá nhiều. Anh gõ chi tiết hơn (vd kèm năm sinh).")
        return
    await bot.send_message(chat_id=chat_id,
        text=f"Tìm thấy {len(sub)} hồ sơ — chạm để chọn:",
        reply_markup=_build_case_keyboard(sub, include_search=False))

async def _commit_pick(bot, chat_id: int, user_id, info: dict, *, ack_text: str | None = None,
                        edit_message_id: int | None = None) -> None:
    """Lock case `info` cho phiên DM của user_id, push recent, ack."""
    sess = chatmod.dm_session(user_id)
    folder_id = info.get("folder_id", "")
    if sess.get("folder") != folder_id:
        sess["folder"] = folder_id
        sess["history"].clear()
    sess.pop("awaiting_search", None)
    _push_recent(user_id, info)
    text = ack_text or f"✅ Đã chọn: {info.get('applicant', '?')}"
    if edit_message_id is not None:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=edit_message_id, text=text)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"_commit_pick edit fail ({e}); fallback send")
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"_commit_pick send fail (ignored): {e}")

async def on_case_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    chat = msg.chat
    user_id = msg.from_user.id
    if chat.type != "private":
        try:
            await context.bot.send_message(chat_id=chat.id, reply_to_message_id=msg.message_id,
                text="/case dùng trong DM riêng với bot (không phải trong nhóm).")
        except Exception:
            pass
        return
    if not (staff_by_telegram_id(str(user_id)) or str(user_id) in STAFF_TELE_IDS):
        return
    # Args sau "/case"
    raw = (msg.text or "").strip()
    parts = raw.split(maxsplit=1)
    query = parts[1].strip() if len(parts) >= 2 else ""
    my_cases = await _staff_my_cases(user_id)
    await _present_case_picker(context.bot, chat.id, user_id, my_cases, query=query)

async def on_syncstaff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM only, director only: sync staff[] cho tất cả Pro cases từ Telegram live."""
    msg = update.message
    if not msg or not msg.from_user:
        return
    if msg.chat.type != "private":
        return
    user_id = str(msg.from_user.id)
    all_staff = load_staff()
    caller = next((s for s in all_staff if s.get("tele_id") == user_id), None)
    if not caller or caller.get("role", "").strip().lower() != "director":
        await msg.reply_text("⛔ Chỉ director mới dùng được lệnh này.")
        return

    await msg.reply_text("⏳ Đang sync staff cho tất cả cases...")
    async with REGISTRY_LOCK:
        reg = load_registry() or {}
        added_total = 0
        cases_updated = 0
        for chat_id, info in reg.items():
            if not isinstance(info, dict):
                continue
            if info.get("kind") != "pro" or not info.get("case_setup"):
                continue
            pro_cid = chat_id
            kh_cid  = str(info.get("kh_chat_id", ""))
            current_ids = {str(x) for x in (info.get("staff") or [])}
            new_ids: list[str] = []
            for cid in filter(None, [pro_cid, kh_cid]):
                try:
                    detected = await get_group_staff(context.bot, int(cid), all_staff)
                except Exception as _e:
                    logger.warning(f"syncstaff: get_group_staff({cid}) error: {_e}")
                    continue
                for s in detected:
                    tid = str(s.get("tele_id", ""))
                    if tid and tid not in current_ids:
                        new_ids.append(tid)
                        current_ids.add(tid)
            if new_ids:
                merged = list(info.get("staff") or []) + new_ids
                reg[pro_cid]["staff"] = merged
                if kh_cid and kh_cid in reg:
                    reg[kh_cid]["staff"] = merged
                added_total += len(new_ids)
                cases_updated += 1
                logger.info(f"syncstaff: case {info.get('applicant','?')!r} +{len(new_ids)} staff: {new_ids}")
        save_registry(reg)

    await msg.reply_text(
        f"✅ Sync xong — {cases_updated} case(s) cập nhật, +{added_total} staff entries mới."
    )

async def on_pickcase_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data or not q.from_user:
        return
    try:
        await q.answer()
    except Exception:
        pass
    user_id = q.from_user.id
    chat_id = q.message.chat.id if q.message else user_id
    edit_msg_id = q.message.message_id if q.message else None
    if not (staff_by_telegram_id(str(user_id)) or str(user_id) in STAFF_TELE_IDS):
        return
    data = q.data
    if data == "pickcase:search":
        sess = chatmod.dm_session(user_id)
        sess["awaiting_search"] = True
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=edit_msg_id,
                text="🔍 Gõ tên/keyword KH muốn tìm (vd 'anh99', 'nguyễn thị', '1999')")
        except Exception:
            pass
        return
    if not data.startswith("pickcase:"):
        return
    folder_id = data.split(":", 1)[1]
    reg = load_registry()
    my_cases = chatmod.cases_for_staff(reg, user_id)
    info = next((c for _, c in my_cases if c.get("folder_id") == folder_id), None)
    if not info:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=edit_msg_id,
                text="❌ Hồ sơ không còn trong danh sách phụ trách của anh.")
        except Exception:
            pass
        return
    await _commit_pick(context.bot, chat_id, user_id, info,
                        ack_text=f"✅ Đã chọn: {info.get('applicant', '?')}. Hỏi đi.",
                        edit_message_id=edit_msg_id)


async def on_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text or not msg.from_user:
        return
    chat = msg.chat
    user_id = msg.from_user.id
    text = (msg.text or "").strip()
    if not text:
        return
    try:
        # ── DM mode ──────────────────────────────────────────────────────────
        if chat.type == "private":
            if not (staff_by_telegram_id(str(user_id)) or str(user_id) in STAFF_TELE_IDS):
                await context.bot.send_message(chat_id=user_id,
                    text="Xin lỗi, bạn không nằm trong danh sách nhân viên nên tôi không thể trả lời.")
                return
            if not chatmod.check_cooldown(user_id):
                return
            reg = load_registry()
            my_cases = chatmod.cases_for_staff(reg, user_id)
            sess = chatmod.dm_session(user_id)
            # 1. Đang chờ search query từ nút 🔍 → coi text này là /case <text>.
            if sess.pop("awaiting_search", False):
                await _present_case_picker(context.bot, chat.id, user_id, my_cases, query=text)
                return
            # 2. Reply tin bot cũ → ưu tiên case của tin gốc (zero-friction switch).
            replied_folder = None
            if msg.reply_to_message and msg.reply_to_message.message_id:
                replied_folder = _BOT_MSG_TO_FOLDER.get(f"{chat.id}:{msg.reply_to_message.message_id}")
            info: dict | None = None
            if replied_folder:
                info = next((c for _, c in my_cases if c.get("folder_id") == replied_folder), None)
            if info is None:
                info, ask = chatmod.pick_case_for_dm(text, my_cases, sess.get("folder"))
                if ask:
                    await context.bot.send_message(chat_id=user_id, text=ask)
                    return
                if not info:
                    # No case match → general Q&A (small talk / câu hỏi visa chung không cần hồ sơ).
                    ack_msg_id, on_chunk = await _setup_streaming(context.bot, user_id, reply_to_id=None)
                    async with chatmod.CHAT_SEMAPHORE:
                        ans = await chatmod.answer_general(sess["history"], text, stream_callback=on_chunk)
                    sess["history"].append((text, ans))
                    final_html = chatmod.linkify_answer(ans)
                    await _finalize_streaming(context.bot, user_id, ack_msg_id, final_html, ans)
                    return
            if sess.get("folder") != info.get("folder_id"):
                sess["folder"] = info.get("folder_id")
                sess["history"].clear()
            _push_recent(user_id, info)
            applicant = info.get("applicant", "?")
            case_meta = {"applicant": applicant, "visa": info.get("visa", "?"),
                         "agent": _agent_from_title(info.get("raw_title", "")), "folder_id": info.get("folder_id", ""),
                         "drive_link": info.get("drive_link", "")}
            await context.bot.send_chat_action(chat_id=user_id, action="typing")
            # Streaming: ack tin "🤖 ⏳" + edit_message mỗi 1.2s
            ack_msg_id, on_chunk = await _setup_streaming(context.bot, user_id, reply_to_id=None)
            async with chatmod.CHAT_SEMAPHORE:
                # Drive crawl chạy trực tiếp trên loop (Drive client httplib2 KHÔNG thread-safe);
                # answer_question stream qua OpenRouter (gemini-flash mặc định, pro nếu hard).
                ctx = chatmod.get_case_context(info["folder_id"], applicant, SHARED_DRIVE_ID)
                ans = await chatmod.answer_question(case_meta, ctx, sess["history"], text, SHARED_DRIVE_ID,
                                                    session_key=f"dm:{user_id}",
                                                    stream_callback=on_chunk)
            sess["history"].append((text, ans))
            final_html = chatmod.linkify_answer(ans, ctx.get("name_to_link") or {},
                                                 case_meta.get("drive_link", ""))
            await _finalize_streaming(context.bot, user_id, ack_msg_id, final_html, ans)
            # Đánh dấu tin trả lời này về case nào → staff reply lại tin này = ngầm chọn case.
            _BOT_MSG_TO_FOLDER[f"{chat.id}:{ack_msg_id}"] = info.get("folder_id", "")
            logger.info(f"CHAT DM user={user_id} case={applicant!r}")
            return

        # ── Pro group mode ───────────────────────────────────────────────────
        _full_reg = load_registry() or {}
        info = _full_reg.get(str(chat.id)) or {}
        if info.get("kind") != "pro" or not info.get("case_setup") or not info.get("folder_id"):
            return
        try:
            me = await context.bot.get_me()
            bot_username = (me.username or "")
        except Exception:
            bot_username = (context.bot.username or "")
        bot_id = context.bot.id
        text_low = text.lower()
        mentioned = bool(bot_username) and (f"@{bot_username.lower()}" in text_low)
        if not mentioned and msg.entities:
            for ent in msg.entities:
                if ent.type == "text_mention" and ent.user and ent.user.id == bot_id:
                    mentioned = True
                    break
        is_reply_to_bot = bool(msg.reply_to_message and msg.reply_to_message.from_user
                               and msg.reply_to_message.from_user.id == bot_id)
        if not (mentioned or is_reply_to_bot):
            return  # chitchat của nhân viên — im lặng
        # Auto-add staff vào staff[] nếu họ chat trong Pro group nhưng chưa có trong registry
        # (case setup trước khi họ được thêm vào group, hoặc get_group_staff không detect được)
        _uid_str = str(user_id)
        if _uid_str not in [str(x) for x in (info.get("staff") or [])]:
            try:
                _full_reg[str(chat.id)].setdefault("staff", []).append(user_id)
                _kh_cid = str(info.get("kh_chat_id", ""))
                if _kh_cid and _kh_cid in _full_reg:
                    _full_reg[_kh_cid].setdefault("staff", []).append(user_id)
                save_registry(_full_reg)
                logger.info(f"Auto-added staff {user_id} to case {info.get('applicant','?')!r} (was missing from staff[])")
            except Exception as _ae:
                logger.warning(f"Auto-add staff failed (ignored): {_ae}")
        question = text
        if bot_username:
            question = re.sub(rf"@{re.escape(bot_username)}", "", question, flags=re.IGNORECASE).strip()
        applicant = info.get("applicant", "?")
        if not question:
            await context.bot.send_message(chat_id=chat.id, reply_to_message_id=msg.message_id,
                text=(f"Hỏi gì về hồ sơ {applicant}? Ví dụ: hồ sơ còn thiếu giấy gì · có giấy nào sắp/đã hết hạn · "
                      "có mâu thuẫn thông tin nào · trên giấy X ghi gì."))
            return
        if not chatmod.check_cooldown(user_id):
            return
        case_meta = {"applicant": applicant, "visa": info.get("visa", "?"),
                     "agent": _agent_from_title(info.get("raw_title", "")), "folder_id": info.get("folder_id", ""),
                     "drive_link": info.get("drive_link", "")}
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        # Streaming: ack tin "🤖 ⏳" reply tới câu hỏi + edit_message mỗi 1.2s
        ack_msg_id, on_chunk = await _setup_streaming(context.bot, chat.id,
                                                       reply_to_id=msg.message_id)
        hist = chatmod.group_history(str(chat.id))
        async with chatmod.CHAT_SEMAPHORE:
            ctx = chatmod.get_case_context(info["folder_id"], applicant, SHARED_DRIVE_ID)  # Drive trên loop (không thread)
            ans = await chatmod.answer_question(case_meta, ctx, hist, question, SHARED_DRIVE_ID,
                                                session_key=f"grp:{chat.id}",
                                                stream_callback=on_chunk)
        hist.append((question, ans))
        final_html = chatmod.linkify_answer(ans, ctx.get("name_to_link") or {},
                                             case_meta.get("drive_link", ""))
        await _finalize_streaming(context.bot, chat.id, ack_msg_id, final_html, ans)
        logger.info(f"CHAT group={chat.id} user={user_id} case={applicant!r}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"on_chat_message: {e}", exc_info=True)
        try:
            await context.bot.send_message(chat_id=chat.id, text="❌ Lỗi khi trả lời, thử lại sau.")
        except Exception:
            pass


def _self_test_parse_titles() -> None:
    """Khoá behaviour của parse_group_title qua 6 ví dụ thực tế + 2 regression + edge cases."""
    # 6 ví dụ thực tế anh đưa
    r1 = parse_group_title("DH Pro WP2Y - Trần Đăng Sự 2006")
    assert r1 and r1["kind"] == "pro" and r1["applicant"] == "Trần Đăng Sự 2006" and r1["visa"] == "WP2Y", r1
    r2 = parse_group_title("DongHanh WP2Y - KH Trần Đăng Sự 2006")
    assert r2 and r2["kind"] == "kh" and r2["applicant"] == "Trần Đăng Sự 2006" and r2["visa"] == "WP2Y", r2
    r3 = parse_group_title("DH Pro HighSkilled - Lê Văn Hậu 1991")
    assert r3 and r3["kind"] == "pro" and r3["applicant"] == "Lê Văn Hậu 1991" and r3["visa"] == "HighSkilled", r3
    r4 = parse_group_title("DongHanh HighSkilled - KH Lê Văn Hậu 1991")
    assert r4 and r4["kind"] == "kh" and r4["applicant"] == "Lê Văn Hậu 1991" and r4["visa"] == "HighSkilled", r4
    r5 = parse_group_title("Nguyễn Trường An 2006 WP10m - A Hồng")
    assert r5 and r5["kind"] == "kh" and r5["applicant"] == "Nguyễn Trường An 2006" and r5["visa"] == "WP10M", r5
    r6 = parse_group_title("DH Pro WP10m – Nguyễn Trường An 2006")  # em-dash
    assert r6 and r6["kind"] == "pro" and r6["applicant"] == "Nguyễn Trường An 2006" and r6["visa"] == "WP10M", r6
    # 2 format gốc — regression
    rA = parse_group_title("Hoàng Thị Mơ TEST7 1991 WP10m - C Liên")
    assert rA and rA["kind"] == "kh" and rA["applicant"] == "Hoàng Thị Mơ TEST7 1991", rA
    rB = parse_group_title("DH Pro WP10m - Hoàng Thị Mơ TEST7 1991")
    assert rB and rB["kind"] == "pro" and rB["applicant"] == "Hoàng Thị Mơ TEST7 1991", rB
    # Edge cases
    assert parse_group_title("") is None
    assert parse_group_title("   ") is None
    assert parse_group_title("Random text no visa") is None
    # Tên thiếu năm sinh — vẫn detect, applicant = segment dài nhất
    rC = parse_group_title("DH Pro WP10m - Trần Đăng Sự")
    assert rC and rC["kind"] == "pro" and rC["applicant"] == "Trần Đăng Sự" and rC["visa"] == "WP10M", rC
    # Tên thiếu hẳn — vẫn detect, applicant=""
    rD = parse_group_title("DH Pro WP10m -")
    assert rD and rD["kind"] == "pro" and rD["applicant"] == "" and rD["visa"] == "WP10M", rD
    # pair_key 2 chiều cho ví dụ 2 vs 1, 4 vs 3
    assert make_pair_key(r2["applicant"], r2["visa"]) == make_pair_key(r1["applicant"], r1["visa"])
    assert make_pair_key(r4["applicant"], r4["visa"]) == make_pair_key(r3["applicant"], r3["visa"])
    # _canon_visa các nhánh chính
    assert _canon_visa("wp10m") == "WP10M"
    assert _canon_visa("High Skilled") == "HighSkilled"
    assert _canon_visa("highskilled") == "HighSkilled"
    assert _canon_visa("farm") == "FARM"


def _self_test_summary() -> None:
    """Fix 4 — surface needs_review trong summarize_manifest()."""
    manifest_ok = {"total_input_files": 2, "counts": {"uploaded": 2},
                   "items": [
                       {"src_name": "a.pdf", "new_name": "CCCD-Foo.pdf", "tag": "CCCD",
                        "status": "uploaded", "needs_review": False, "drive_link": "https://x"},
                       {"src_name": "b.pdf", "new_name": "Khac-Foo.pdf", "tag": "Khac",
                        "status": "uploaded", "needs_review": True, "drive_link": "https://y"},
                   ]}
    out = summarize_manifest(manifest_ok, drive_link="https://drive/x")
    assert len(out) == 1, f"expected 1 part, got {len(out)}"
    out = out[0]
    assert "1 file cần kiểm tra thủ công" in out, out
    assert "⚠️ " in out, out
    assert "không nhận diện được" in out, out  # Khac suffix
    # Không có needs_review → không có header cảnh báo
    out2 = summarize_manifest({"total_input_files": 1, "counts": {"uploaded": 1},
                               "items": [{"src_name": "c.pdf", "new_name": "CCCD-Foo.pdf",
                                          "tag": "CCCD", "status": "uploaded",
                                          "needs_review": False, "drive_link": "https://z"}]},
                              drive_link="")
    assert len(out2) == 1, f"expected 1 part, got {len(out2)}"
    out2 = out2[0]
    assert "cần kiểm tra thủ công" not in out2, out2
    # Fix B — count uploaded-split + duplicate-by-hash trong summary
    out3 = summarize_manifest({"total_input_files": 1,
                               "counts": {"uploaded-split": 3, "duplicate-by-hash": 2},
                               "items": [{"src_name": "big.pdf", "new_name": "CCCD-Foo.pdf",
                                          "tag": "CCCD", "status": "uploaded-split",
                                          "split_from": "big.pdf", "split_pages": "1-2",
                                          "needs_review": False, "drive_link": "https://a"}]},
                              drive_link="")
    assert len(out3) == 1, f"expected 1 part, got {len(out3)}"
    out3 = out3[0]
    assert "tách từ PDF" in out3, out3
    assert "trùng nội dung" in out3, out3
    # Fix A — invalidate_list_cache phải xoá entry, không raise nếu key không có
    from lib.drive_helpers import invalidate_list_cache, _LIST_CACHE
    _LIST_CACHE["__test_x__"] = {"foo": "bar"}
    invalidate_list_cache("__test_x__")
    assert "__test_x__" not in _LIST_CACHE
    invalidate_list_cache("nonexistent")  # không raise


async def _do_retry_checkpoints(app):
    """Background: chờ orphan subprocess xong rồi retry các batch bị interrupt khi restart."""
    await asyncio.sleep(300)   # 5 phút — đủ cho pipeline 32 trang FORCE-PRO hoàn thành
    if not _CHECKPOINT_DIR.exists():
        return
    ckpts = list(_CHECKPOINT_DIR.glob("*.json"))
    if not ckpts:
        return
    logger.info(f"Startup retry: {len(ckpts)} checkpoint(s) pending từ lần restart trước")
    for ckpt in ckpts:
        try:
            meta = json.loads(ckpt.read_text(encoding="utf-8"))
            if time.time() - meta.get("created_at", 0) < 60:
                continue          # Quá mới — skip, có thể batch đang chạy bình thường
            chat_id     = meta["chat_id"]
            pro_chat_id = meta["pro_chat_id"]
            drive_link  = meta.get("drive_link", "")
            logger.info(f"Startup retry: --checklist-only cho case {chat_id}")
            manifest = await run_scan_pipeline(None, chat_id, checklist_only=True)
            ckpt.unlink(missing_ok=True)
            if not manifest or not manifest.get("items"):
                continue
            await _send_summary(app.bot, pro_chat_id,
                               summarize_manifest(manifest, drive_link),
                               disable_web_page_preview=True)
            _, ck_detail = _checklist_telegram_lines(manifest)
            if ck_detail:
                await send_html(app.bot, pro_chat_id, ck_detail, disable_web_page_preview=True)
        except Exception as _e:
            logger.error(f"Startup retry {ckpt.name}: {_e}", exc_info=True)

async def _post_init_retry(application):
    asyncio.create_task(_do_retry_checkpoints(application))


def main():
    from telegram.request import HTTPXRequest
    app = (Application.builder()
           .token(BOT_TOKEN)
           .request(HTTPXRequest(read_timeout=120, write_timeout=120, connect_timeout=30, pool_timeout=30))
           .concurrent_updates(16)
           .post_init(_post_init_retry)
           .build())
    app.add_handler(ChatMemberHandler(on_bot_join, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("check", on_check_command))
    app.add_handler(CommandHandler("oldfile", on_oldfile_command))
    app.add_handler(CommandHandler("case", on_case_command))
    app.add_handler(CommandHandler("syncstaff", on_syncstaff_command))
    app.add_handler(CallbackQueryHandler(on_pickcase_callback, pattern=r"^pickcase:"))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.TEXT, remember_staff_activity), group=1)
    app.add_handler(MessageHandler(filters.ALL, debug_all), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_chat_message), group=2)
    logger.info("Bot @donghanhprocessingbot v2 started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test_parse_titles()
        _self_test_summary()
        print("OK")
        sys.exit(0)
    main()
