"""Chat hỏi-đáp về hồ sơ KH cho @donghanhprocessingbot.

Nhân viên hỏi (trong nhóm Pro khi @mention/reply bot, hoặc DM riêng với bot) → bot trả lời như một
VIÊN CHỨC THẨM ĐỊNH VISA CANADA chuyên nghiệp — kỹ càng, chính xác, KHÔNG nịnh — dựa trên: dữ liệu giấy
tờ đã OCR (sidecar .json) + báo cáo thẩm định mới nhất (Google Doc) + điểm danh CHECKLIST FARM.

Phân quyền: nhóm Pro = ai gửi tin trong nhóm = đã được add = được phép (bot chỉ nạp context của case của
chính nhóm đó). DM = chỉ trả lời nếu user là nhân viên (Master Staff / STAFF_TELE_IDS) và chỉ về case mà
`reg[pro_chat_id]["staff"]` có chứa user_id đó.

Model: chat/suy luận = CHAT_MODEL (mặc định google/gemini-2.5-pro). Khi câu hỏi cần đọc NGUYÊN VĂN/chi tiết
sâu của 1 file → bot OCR lại đúng file đó bằng CHAT_SCAN_MODEL (mặc định google/gemini-2.5-flash-lite) qua
cơ chế `NEED_FILE:` rồi đưa nguyên văn cho CHAT_MODEL trả lời (tối đa 1 vòng).

Logic tập trung ở file này (single source of truth) — telegram_listener.py chỉ gọi vào đây.
"""
from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import os
import re
import time
import unicodedata
from collections import deque

# Các import vào package `lib` (checklist / sop_naming / drive_helpers / google_clients) được làm
# LAZY bên trong từng hàm — giống pattern ở lib/checklist.py — để `python3 -m lib.chat` self-test
# chạy được mà không kéo theo google api client.

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
CHAT_MODEL = os.environ.get("CHAT_MODEL", "google/gemini-2.5-pro")
CHAT_SCAN_MODEL = os.environ.get("CHAT_SCAN_MODEL", "google/gemini-2.5-flash-lite")  # OCR lại 1 file (NEED_FILE)
CHAT_WEB_MODEL = os.environ.get("CHAT_WEB_MODEL", "google/gemini-2.5-flash")        # tra cứu web (NEED_WEB) — đi kèm OpenRouter web plugin
CHAT_WEB_MAX_RESULTS = int(os.environ.get("CHAT_WEB_MAX_RESULTS", "4"))
CHAT_HISTORY_TURNS = int(os.environ.get("CHAT_HISTORY_TURNS", "6"))      # số cặp Q/A giữ lại trong context
CHAT_CTX_TTL = float(os.environ.get("CHAT_CTX_TTL", "600"))             # TTL cache context theo case (giây)
CHAT_FULLTEXT_TTL = float(os.environ.get("CHAT_FULLTEXT_TTL", "1800"))  # TTL cache nguyên văn theo file
CHAT_WEB_TTL = float(os.environ.get("CHAT_WEB_TTL", "3600"))           # TTL cache kết quả tra cứu web theo truy vấn
CHAT_SESSION_IDLE = float(os.environ.get("CHAT_SESSION_IDLE", "1800"))  # quên session DM/nhóm sau ngần này (giây) idle
CHAT_CONCURRENCY = int(os.environ.get("CHAT_CONCURRENCY", "4"))
CHAT_USER_COOLDOWN = float(os.environ.get("CHAT_USER_COOLDOWN", "3"))   # 1 user 1 câu / ngần này giây
CHAT_MAX_FILEBYTES = int(os.environ.get("CHAT_MAX_FILEBYTES", "9000000"))  # file lớn hơn → không OCR lại
CHAT_ANSWER_MAXLEN = 4000

CHAT_SEMAPHORE = asyncio.Semaphore(CHAT_CONCURRENCY)

_OCR_MIME = {"application/pdf", "image/jpeg", "image/jpg", "image/png"}

# ---------------------------------------------------------------------------
# state (module-level, lazy-evicted) — mỗi entry là dict có khoá "ts" (monotonic)
# ---------------------------------------------------------------------------
_CTX_CACHE: dict = {}       # case_folder_id -> {"ctx": dict, "ts": float}
_FULLTEXT_CACHE: dict = {}  # file_id        -> {"text": str,  "ts": float}
_WEB_CACHE: dict = {}       # query (norm)   -> {"text": str,  "ts": float}
_DM_SESSION: dict = {}      # user_id (int)  -> {"folder": str|None, "history": deque, "ts": float}
_GROUP_SESSION: dict = {}   # pro_chat_id    -> {"history": deque, "ts": float}
_LAST_ASK: dict = {}        # user_id (int)  -> float (monotonic)


def _now() -> float:
    return time.monotonic()


def _sweep(d: dict, ttl: float) -> None:
    now = _now()
    for k in [k for k, v in d.items() if isinstance(v, dict) and now - v.get("ts", 0.0) > ttl]:
        d.pop(k, None)


def _norm(s: str) -> str:
    """Bỏ dấu tiếng Việt + lowercase + strip (đủ cho so khớp tên KH; tự chứa, không phụ thuộc lib khác)."""
    s = (s or "").replace("đ", "d").replace("Đ", "D")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return s.lower().strip()


# ===========================================================================
# Cooldown chống spam
# ===========================================================================
def check_cooldown(user_id) -> bool:
    """True nếu được phép hỏi (đã qua cooldown) — đồng thời cập nhật mốc. False nếu còn cooldown."""
    now = _now()
    if now - _LAST_ASK.get(user_id, 0.0) < CHAT_USER_COOLDOWN:
        return False
    _LAST_ASK[user_id] = now
    return True


# ===========================================================================
# Phân quyền: case nào nhân viên phụ trách (cho DM mode)
# ===========================================================================
def cases_for_staff(reg: dict, user_id) -> list:
    """Trả [(pro_chat_id, info)] các case mà user_id là staff (theo reg['staff'] của nhóm Pro). Sắp theo tên KH."""
    uid = str(user_id)
    out = []
    for chat_id, info in (reg or {}).items():
        if not isinstance(info, dict):
            continue
        if info.get("kind") != "pro" or not info.get("case_setup"):
            continue
        if uid in [str(x) for x in (info.get("staff") or [])]:
            out.append((str(chat_id), info))
    out.sort(key=lambda t: _norm(t[1].get("applicant", "")))
    return out


def pick_case_for_dm(user_message: str, my_cases: list, active_folder):
    """Trả (info|None, ask_text|None). info = entry registry của case được chọn cho phiên DM này."""
    if not my_cases:
        return None, ("Bạn chưa được giao hồ sơ nào trong hệ thống (hoặc bot chưa ghi nhận hoạt động của bạn "
                      "trong nhóm Pro). Hãy hỏi trực tiếp trong nhóm Pro của khách hàng.")
    if len(my_cases) == 1:
        return my_cases[0][1], None
    msg_norm = _norm(user_message)
    matched = []
    for _, info in my_cases:
        nn = _norm(info.get("applicant", ""))
        if len(nn) >= 6 and nn in msg_norm:
            matched.append(info)
    if len(matched) == 1:
        return matched[0], None
    if len(matched) > 1:
        return None, "Khớp với nhiều hồ sơ: " + " · ".join(i.get("applicant", "?") for i in matched) + \
                     ". Bạn muốn hỏi về hồ sơ nào? (ghi rõ tên KH)"
    if active_folder:
        for _, info in my_cases:
            if info.get("folder_id") == active_folder:
                return info, None
    names = " · ".join(info.get("applicant", "?") for _, info in my_cases)
    return None, f"Bạn đang phụ trách {len(my_cases)} hồ sơ: {names}. Bạn muốn hỏi về hồ sơ nào? (ghi rõ tên KH)"


# ===========================================================================
# Session state helpers (gọi từ telegram_listener)
# ===========================================================================
def dm_session(user_id) -> dict:
    _sweep(_DM_SESSION, CHAT_SESSION_IDLE)
    e = _DM_SESSION.get(user_id)
    if not e:
        e = _DM_SESSION[user_id] = {"folder": None, "history": deque(maxlen=2 * CHAT_HISTORY_TURNS), "ts": _now()}
    e["ts"] = _now()
    return e


def group_history(pro_chat_id: str) -> deque:
    _sweep(_GROUP_SESSION, CHAT_SESSION_IDLE)
    e = _GROUP_SESSION.get(pro_chat_id)
    if not e:
        e = _GROUP_SESSION[pro_chat_id] = {"history": deque(maxlen=2 * CHAT_HISTORY_TURNS), "ts": _now()}
    e["ts"] = _now()
    return e["history"]


# ===========================================================================
# Build case context (sidecar đã OCR + báo cáo thẩm định + điểm danh FARM)
# ===========================================================================
def _trim_for_chat(dataset: list):
    docs, name_to_link = [], {}
    for d in dataset:
        ten = d.get("ten", "")
        link = d.get("drive_link", "") or ""
        docs.append({
            "ten": ten,
            "loai": d.get("loai", ""),
            "nguoi": d.get("nguoi", ""),
            "tom_tat": (d.get("tom_tat") or "")[:1200],
            "du_lieu": d.get("du_lieu") or {},
            "key_fields": d.get("key_fields") or {},
            "needs_review": bool(d.get("needs_review")),
            "drive_link": link,                       # để bot gửi link khi nhân viên cần xem giấy tờ
        })
        if ten:
            name_to_link[ten] = link
    return docs, name_to_link


def _fetch_tham_dinh_doc(case_folder_id: str, applicant: str, drive_id) -> str:
    """Xuất Google Doc 'Bao cao tham dinh - <KH>' ở case folder ra text/plain. '' nếu chưa có / lỗi."""
    try:
        from .drive_helpers import find_file_by_name
        from .google_clients import drive
        from .sop_naming import title_case_ascii
        from googleapiclient.http import MediaIoBaseDownload
        DOC_MIME = "application/vnd.google-apps.document"
        name = f"Bao cao tham dinh - {title_case_ascii(applicant) or 'Unknown'}"
        doc_id = find_file_by_name(name, case_folder_id, drive_id, mime_type=DOC_MIME)
        if not doc_id:
            return ""
        req = drive().files().export_media(fileId=doc_id, mimeType="text/plain")
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        print(f"chat: _fetch_tham_dinh_doc lỗi: {type(e).__name__}: {e}", flush=True)
        return ""


def build_case_context(case_folder_id: str, applicant: str, drive_id) -> dict:
    from .checklist import build_dataset, compute_coverage
    dataset = build_dataset(case_folder_id, drive_id)
    cov = compute_coverage(dataset)
    report_text = _fetch_tham_dinh_doc(case_folder_id, applicant, drive_id)
    docs, name_to_link = _trim_for_chat(dataset)
    return {"docs": docs, "name_to_link": name_to_link, "coverage": cov,
            "report_text": report_text, "doc_names": [d["ten"] for d in docs if d["ten"]], "n_docs": len(dataset)}


def get_case_context(case_folder_id: str, applicant: str, drive_id) -> dict:
    _sweep(_CTX_CACHE, CHAT_CTX_TTL)
    e = _CTX_CACHE.get(case_folder_id)
    if e and _now() - e["ts"] <= CHAT_CTX_TTL:
        return e["ctx"]
    ctx = build_case_context(case_folder_id, applicant, drive_id)
    _CTX_CACHE[case_folder_id] = {"ctx": ctx, "ts": _now()}
    return ctx


def invalidate_case_cache(case_folder_id: str) -> None:
    _CTX_CACHE.pop(case_folder_id, None)


# ===========================================================================
# OCR lại 1 file (đọc nguyên văn) bằng CHAT_SCAN_MODEL
# ===========================================================================
_FILE_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{20,})")


def _file_id_from_link(drive_link: str) -> str:
    if not drive_link:
        return ""
    m = _FILE_ID_RE.search(drive_link)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", drive_link)
    return m.group(1) if m else ""


def get_file_fulltext(drive_link: str, drive_id, model: str | None = None) -> str | None:
    """Tải file từ Drive (theo link), OCR lại bằng CHAT_SCAN_MODEL → nguyên văn. None nếu không đọc được/quá lớn.
    Có cache theo file_id (TTL CHAT_FULLTEXT_TTL)."""
    model = model or CHAT_SCAN_MODEL
    fid = _file_id_from_link(drive_link)
    if not fid:
        return None
    _sweep(_FULLTEXT_CACHE, CHAT_FULLTEXT_TTL)
    e = _FULLTEXT_CACHE.get(fid)
    if e and _now() - e["ts"] <= CHAT_FULLTEXT_TTL:
        return e["text"]
    try:
        from .google_clients import drive
        from .drive_helpers import download_file_bytes
        import httpx
        kw = dict(fileId=fid, fields="name,mimeType,size")
        if drive_id:
            kw["supportsAllDrives"] = True
        meta = drive().files().get(**kw).execute()
        mime = (meta.get("mimeType") or "").lower()
        size = int(meta.get("size") or 0)
        if mime not in _OCR_MIME or (size and size > CHAT_MAX_FILEBYTES):
            print(f"chat: get_file_fulltext bỏ qua {meta.get('name')!r} (mime={mime}, size={size})", flush=True)
            return None
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            return None
        raw = download_file_bytes(fid, drive_id)
        b64 = base64.b64encode(raw).decode()
        fname = meta.get("name") or "file"
        prompt = ("Trích xuất TOÀN BỘ văn bản nhìn thấy trong file đính kèm, GIỮ NGUYÊN VĂN — không tóm tắt, "
                  "không thêm thắt, không bình luận. Chỉ trả về phần văn bản.")
        if mime.startswith("image/"):
            content = [{"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
        else:  # pdf
            content = [{"type": "text", "text": prompt},
                       {"type": "file", "file": {"filename": fname, "file_data": f"data:{mime};base64,{b64}"}}]
        with httpx.Client(timeout=120) as client:
            resp = client.post("https://openrouter.ai/api/v1/chat/completions",
                               headers={"Authorization": f"Bearer {api_key}"},
                               json={"model": model, "messages": [{"role": "user", "content": content}],
                                     "temperature": 0.0})
        resp.raise_for_status()
        txt = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt).strip()[:12000]
        _FULLTEXT_CACHE[fid] = {"text": txt, "ts": _now()}
        return txt
    except Exception as e:  # noqa: BLE001
        print(f"chat: get_file_fulltext lỗi: {type(e).__name__}: {e}", flush=True)
        return None


# ===========================================================================
# Tra cứu web (NEED_WEB) — OpenRouter web plugin trên CHAT_WEB_MODEL
# ===========================================================================
def web_search(query: str, model: str | None = None, max_results: int | None = None) -> str | None:
    """Tra cứu web cho `query` qua OpenRouter web plugin → tóm tắt dữ kiện + nguồn (text). None nếu lỗi.
    Có cache theo truy vấn (TTL CHAT_WEB_TTL). An toàn chạy trong thread (httpx.Client riêng)."""
    q = (query or "").strip()
    if not q:
        return None
    model = model or CHAT_WEB_MODEL
    max_results = max_results or CHAT_WEB_MAX_RESULTS
    key = _norm(q)[:200]
    _sweep(_WEB_CACHE, CHAT_WEB_TTL)
    e = _WEB_CACHE.get(key)
    if e and _now() - e["ts"] <= CHAT_WEB_TTL:
        return e["text"]
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return None
    try:
        import httpx
        sys_p = ("Bạn là trợ lý tra cứu. Dùng kết quả web tìm được để trả lời truy vấn bằng tiếng Việt: nêu "
                 "các DỮ KIỆN CHÍNH (ngắn gọn, có ngày/số nếu có) kèm NGUỒN (tên trang/URL). Nếu không tìm được "
                 "thông tin đáng tin cậy → nói rõ 'không tìm thấy thông tin xác đáng'. KHÔNG bịa.")
        with httpx.Client(timeout=90) as client:
            resp = client.post("https://openrouter.ai/api/v1/chat/completions",
                               headers={"Authorization": f"Bearer {api_key}"},
                               json={"model": model,
                                     "messages": [{"role": "system", "content": sys_p},
                                                  {"role": "user", "content": f"Tra cứu: {q}"}],
                                     "plugins": [{"id": "web", "max_results": max_results}],
                                     "temperature": 0.1})
        resp.raise_for_status()
        txt = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt).strip()[:6000]
        if not txt:
            return None
        _WEB_CACHE[key] = {"text": txt, "ts": _now()}
        return txt
    except Exception as e:  # noqa: BLE001
        print(f"chat: web_search lỗi: {type(e).__name__}: {e}", flush=True)
        return None


# ===========================================================================
# Prompt + trả lời (CHAT_MODEL; NEED_FILE = đọc sâu 1 file · NEED_ADDR = tra địa giới hành chính · NEED_WEB = tra ngoài · NEED_RENAME = đổi tên file)
# ===========================================================================
_OFFICER_SYSTEM = """Bạn là một viên chức thẩm định hồ sơ visa Canada (visa officer) chuyên nghiệp, đang HỖ
TRỢ NHÂN VIÊN xử lý hồ sơ về MỘT khách hàng cụ thể (thông tin dưới đây). Trả lời bằng tiếng Việt.

PHONG CÁCH BẮT BUỘC:
- Chuyên nghiệp, kỹ càng, đi thẳng vào vấn đề. KHÔNG nịnh, KHÔNG khách sáo thừa thãi, KHÔNG "dạ vâng" lan
  man, KHÔNG cảm thán, KHÔNG mở đầu kiểu "Chào bạn, rất vui được hỗ trợ...". Vào thẳng nội dung.
- Ngắn gọn; nêu con số / ngày cụ thể.
- TIN NHẮN PHẲNG: KHÔNG dùng định dạng markdown — KHÔNG in đậm (**...**), KHÔNG in nghiêng (*...* / _..._),
  KHÔNG gạch chân, KHÔNG code (`...`), KHÔNG tiêu đề (#). Chỉ văn bản thường; khi liệt kê thì đánh số
  "1." "2." hoặc gạch đầu dòng "-". Tên file viết bình thường (KHÔNG backtick, KHÔNG bọc ngoặc, KHÔNG kèm URL).
- Khi nêu một dữ kiện về hồ sơ → DẪN NGUỒN: ghi đúng TÊN FILE của giấy tờ đó (trường `ten` trong DỮ LIỆU
  bên dưới), vd: theo CCCD-Hoang Thi Mo …, trên LLTP-Hoang Thi Mo ghi ….
- CHỈ dựa trên DỮ LIỆU HỒ SƠ + BÁO CÁO THẨM ĐỊNH + (nếu có) NGUYÊN VĂN GIẤY TỜ / KẾT QUẢ TRA CỨU được cung
  cấp dưới đây. KHÔNG bịa, KHÔNG suy đoán. Thiếu dữ liệu → nói rõ "không có trong hồ sơ / chưa đọc được".
- TUYỆT ĐỐI không tiết lộ thông tin của bất kỳ khách hàng nào khác ngoài hồ sơ này.
- Mặc định: gần như MỌI câu hỏi của nhân viên trong khung này đều LIÊN QUAN đến hồ sơ này — hãy cố trả lời
  theo hướng đó (kể cả câu nói tắt / mơ hồ / kèm yêu cầu phụ như "gửi link", "in ra", "tôi check lại"…).
  CHỈ từ chối khi câu hỏi RÕ RÀNG ngoài lề (thời tiết, tin tức chung, chuyện cá nhân không dính hồ sơ);
  lúc đó mới trả lời 1 câu rằng bạn chỉ hỗ trợ về hồ sơ này.
- LINK: KHÔNG tự dán URL Drive. Khi nhân viên muốn mở / xem / "check lại" một (hoặc vài) giấy tờ cụ thể →
  chỉ cần NHẮC ĐÚNG TÊN FILE của giấy tờ đó (vd: "Xem lại LLTP-Hoang Thi Mo") — hệ thống tự gắn link
  clickable vào đúng tên file đó. Khi nhân viên muốn mở CẢ hồ sơ → nhắc tới "thư mục hồ sơ"
  (link thư mục Drive của hồ sơ này: {{DRIVE_LINK}}).
- Mọi mốc thời gian / hạn → tính theo HÔM NAY = {{TODAY}} (dd/mm/yyyy).

CÓ BỐN CƠ CHẾ ĐẶC BIỆT (mỗi cái = câu trả lời CHỈ gồm DUY NHẤT một dòng đúng cú pháp; chỉ dùng khi THẬT sự
cần — dữ liệu hiện có đã đủ thì TRẢ LỜI LUÔN, đừng yêu cầu tra thêm):
1) ĐỌC SÂU MỘT FILE — danh sách giấy tờ trong hồ sơ (tên file): {{DOC_LIST}}. Nếu cần đọc NGUYÊN VĂN /
   chi tiết sâu hơn của ĐÚNG MỘT giấy tờ (mà phần tóm tắt + trích xuất bên dưới KHÔNG đủ):
   NEED_FILE: <tên file y hệt trong danh sách trên>
2) ĐỊA GIỚI HÀNH CHÍNH (cải cách 2025) — câu hỏi xã/phường/huyện/tỉnh CŨ↔MỚI (vd "phường X có bị sáp nhập
   không / địa chỉ … giờ thuộc đâu / xã A huyện B tỉnh C nay là gì"): tra BẢNG CHÍNH THỨC, KHÔNG đoán:
   NEED_ADDR: <tên đơn vị hoặc địa chỉ cần tra (kèm tên tỉnh nếu biết)>
3) TRA CỨU NGOÀI — chỉ khi cần thông tin KHÔNG có trong hồ sơ và KHÔNG phải địa giới hành chính (vd quy
   định / biểu mẫu mới…):
   NEED_WEB: <truy vấn tìm kiếm ngắn gọn, tiếng Việt>
4) ĐỔI TÊN FILE — nếu nhân viên YÊU CẦU đổi tên một giấy tờ (vd "đổi tên file X thành Y", "sửa tên thành …"):
   NEED_RENAME: <tên file hiện tại y hệt trong danh sách giấy tờ> => <tên file mới như nhân viên muốn>
   KHÔNG tự đổi khi nhân viên không yêu cầu rõ. Bot sẽ hỏi nhân viên xác nhận trước khi đổi.
Trong mọi trường hợp: KHÔNG thêm lời dẫn/giải thích nào khác — bot sẽ xử lý rồi phản hồi/hỏi bạn lại.
KHÔNG dùng các cơ chế này cho câu hỏi ngoài lề (thời tiết, tin tức chung… — những câu đó vẫn từ chối như quy định ở trên)."""

_NEED_FILE_RE = re.compile(r"^\s*NEED_FILE\s*:\s*(.+?)\s*$", re.IGNORECASE)
_NEED_WEB_RE = re.compile(r"^\s*NEED_WEB\s*:\s*(.+?)\s*$", re.IGNORECASE)
_NEED_ADDR_RE = re.compile(r"^\s*NEED_ADDR\s*:\s*(.+?)\s*$", re.IGNORECASE)
_NEED_RENAME_RE = re.compile(r"^\s*NEED_RENAME\s*:\s*(.+?)\s*=>\s*(.+?)\s*$", re.IGNORECASE)


def _diadia():
    """Import lib.diadia robustly (works as a package and when chat.py is run standalone)."""
    try:
        from . import diadia as _dd  # type: ignore
    except (ImportError, ValueError):
        import diadia as _dd  # lib/ on sys.path (standalone self-check)
    return _dd
_AFFIRM_RE = re.compile(r"^\s*(ok(ie|ay)?|đồng\s*ý|đúng(\s*r[ồô]i)?|ph[ảa]i\s*r[ồô]i|có|ừm?|ờ+|uh+|yes|y|"
                        r"đư[ợơ]c|đư[ợơ]c\s*r[ồô]i|ch[ốô]t|chu[ẩa]n|x[áa]c\s*nh[ậâ]n)\s*[.!]*\s*$", re.IGNORECASE)
_NEG_RE = re.compile(r"^\s*(hu[ỷy](\s*b[ỏo])?|kh[ôo]ng|ko|th[ôo]i|b[ỏo]|kh[ỏo]i|cancel|no)\s*[.!]*\s*$", re.IGNORECASE)


def _is_need_web(text: str):
    t = (text or "").strip()
    if "\n" in t:
        return None
    m = _NEED_WEB_RE.match(t)
    return m.group(1).strip().strip("'\"") if m else None


def _is_need_file(text: str):
    t = (text or "").strip()
    if "\n" in t:
        return None
    m = _NEED_FILE_RE.match(t)
    return m.group(1).strip().strip("'\"") if m else None


def parse_need_rename(text: str):
    """'NEED_RENAME: <cũ> => <mới>' (1 dòng) → (cũ, mới), else None."""
    t = (text or "").strip()
    if "\n" in t:
        return None
    m = _NEED_RENAME_RE.match(t)
    if not m:
        return None
    old = m.group(1).strip().strip("'\"`").strip()
    new = m.group(2).strip().strip("'\"`").strip()
    return (old, new) if old and new else None


def parse_need_addr(text: str):
    """'NEED_ADDR: <đơn vị / địa chỉ>' (1 dòng) → query string, else None."""
    t = (text or "").strip()
    if "\n" in t:
        return None
    m = _NEED_ADDR_RE.match(t)
    return m.group(1).strip().strip("'\"`").strip() if m else None


def addr_lookup_text(query: str) -> str:
    """Tra `query` trong bảng địa giới hành chính cũ↔mới (lib.diadia) → đoạn văn bản cho LLM dùng."""
    try:
        dd = _diadia()
    except Exception as e:  # noqa: BLE001
        return f"(không tra được bảng địa giới: {type(e).__name__}: {e})"
    q = str(query or "").strip()
    if not q:
        return "(truy vấn rỗng)"
    lines = [f"TRA CỨU «{q}» trong bảng địa giới hành chính chính thức (cải cách 2025; sáp nhập tỉnh hiệu lực "
             f"12/06/2025, sáp nhập xã 01/07/2025) — KẾT QUẢ DETERMINISTIC, coi là chính xác:"]
    try:
        info = dd.commune_merge_info(q)
        if info.get("found"):
            if info.get("new_ward") and info.get("new_province"):
                lines.append(f"  • {info['ghi_chu']}")
            elif info.get("candidates"):
                lines.append(f"  • {info['ghi_chu']}")
            else:
                lines.append(f"  • {info['ghi_chu']}")
        else:
            lines.append(f"  • (xã/phường) {info.get('ghi_chu','không có trong bảng')}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"  • (lỗi tra cấp xã: {type(e).__name__})")
    try:
        r = dd.resolve_address(q)
        if r.get("tinh_moi"):
            moi = (f"{r['xa_moi']}, {r['tinh_moi']}" if r.get("xa_moi") else r["tinh_moi"])
            lines.append(f"  • Phân giải địa chỉ: nay thuộc «{moi}»"
                         f"{' (chuỗi dùng tên trước cải cách)' if (r.get('is_old_province') or r.get('is_old_ward')) else ''}"
                         f" — độ tin: {r.get('confidence')}"
                         + (f". Lưu ý: {r['ghi_chu']}" if r.get("ghi_chu") else ""))
            if r.get("candidates"):
                lines.append("    ứng viên cấp xã: " + "; ".join(f"{c['new_ward']}, {c['new_province']}" for c in r["candidates"][:8]))
        elif r.get("confidence") == "unknown":
            lines.append("  • Không nhận ra cấp tỉnh trong chuỗi — có thể sai tên / cần kiểm tra văn bản chính thức.")
    except Exception as e:  # noqa: BLE001
        lines.append(f"  • (lỗi phân giải địa chỉ: {type(e).__name__})")
    return "\n".join(lines)


def is_affirmative(text: str) -> bool:
    return bool(_AFFIRM_RE.match(text or ""))


def is_negative(text: str) -> bool:
    return bool(_NEG_RE.match(text or ""))


# Xác nhận đổi tên đang chờ. key = "dm:<uid>" | "grp:<chat_id>" → {file_id, old, new, case_folder_id, ts}.
_PENDING_RENAME: dict = {}
_PENDING_RENAME_TTL = 300.0


def set_pending_rename(key: str, **fields) -> None:
    fields["ts"] = time.monotonic()
    _PENDING_RENAME[key] = fields


def pop_pending_rename(key: str):
    p = _PENDING_RENAME.pop(key, None)
    if not p:
        return None
    if time.monotonic() - p.get("ts", 0) > _PENDING_RENAME_TTL:
        return None
    return p


_FORBIDDEN_FNAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _sanitize_new_name(new_name: str, old_name: str) -> str:
    n = (new_name or "").strip().strip("`'\"").strip()
    n = _FORBIDDEN_FNAME_CHARS.sub("-", n)
    n = re.sub(r"\s{2,}", " ", n).strip().strip(".")
    if not n:
        return ""
    if not os.path.splitext(n)[1]:
        n += os.path.splitext(old_name)[1]
    return n


def do_rename(case_folder_id: str, file_id: str, old_name: str, new_name: str, drive_id) -> str:
    """Đổi tên file thật trên Drive + sidecar .json/.md trong _Bot OCR & Metadata (cập nhật `new_name`).
    CHẠY TRỰC TIẾP TRÊN LOOP (Drive client httplib2 không thread-safe). Trả về tin nhắn kết quả."""
    import tempfile
    from googleapiclient.http import MediaFileUpload
    from .google_clients import drive
    from .drive_helpers import rename_file, get_or_create_folder, find_file_by_name, download_file_text, _LIST_CACHE
    saa = bool(drive_id)
    try:
        rename_file(file_id, new_name, drive_id)
    except Exception as e:  # noqa: BLE001
        print(f"chat.do_rename: đổi tên file thất bại: {type(e).__name__}: {e}", flush=True)
        return f'❌ Không đổi được tên file "{old_name}" ({type(e).__name__}). Thử lại sau.'
    missing_meta = False
    try:
        meta_id = get_or_create_folder("_Bot OCR & Metadata", case_folder_id, drive_id=drive_id)
        for ext, mime in ((".json", "application/json"), (".md", "text/markdown")):
            sid = find_file_by_name(f"{old_name}{ext}", meta_id, drive_id=drive_id)
            if not sid:
                missing_meta = True
                continue
            try:
                txt = download_file_text(sid, drive_id=drive_id)
                if ext == ".json":
                    try:
                        data = json.loads(txt)
                        if isinstance(data, dict):
                            data["new_name"] = new_name
                            data["renamed_from"] = old_name
                            txt = json.dumps(data, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                else:
                    txt = txt.replace(old_name, new_name)
                with tempfile.NamedTemporaryFile("w", suffix=ext, delete=False, encoding="utf-8") as fh:
                    fh.write(txt)
                    tmp = fh.name
                try:
                    kw = dict(fileId=sid, body={"name": f"{new_name}{ext}"},
                              media_body=MediaFileUpload(tmp, mimetype=mime, resumable=False))
                    if saa:
                        kw["supportsAllDrives"] = True
                    drive().files().update(**kw).execute()
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            except Exception as e:  # noqa: BLE001
                print(f"chat.do_rename: cập nhật sidecar {ext} lỗi: {type(e).__name__}: {e}", flush=True)
                missing_meta = True
        _LIST_CACHE.pop(meta_id, None)
    except Exception as e:  # noqa: BLE001
        print(f"chat.do_rename: xử lý metadata lỗi: {type(e).__name__}: {e}", flush=True)
        missing_meta = True
    try:
        invalidate_case_cache(case_folder_id)
    except Exception:
        pass
    msg = f'✅ Đã đổi tên: "{old_name}" → "{new_name}"'
    if missing_meta:
        msg += " (lưu ý: có metadata chưa cập nhật được — sẽ tự đồng bộ ở lần đối chiếu/xử lý sau)"
    return msg


def _match_doc_name(wanted: str, doc_names: list):
    wn = (wanted or "").strip().lower()
    if not wn:
        return None
    for dn in doc_names:
        if dn.lower() == wn:
            return dn
    for dn in doc_names:
        if wn in dn.lower() or dn.lower() in wn:
            return dn
    return None


def _history_text(history) -> str:
    items = list(history or [])[-2 * CHAT_HISTORY_TURNS:]
    if not items:
        return "(chưa có)"
    out = []
    for q, a in items:
        out.append(f"NHÂN VIÊN: {q}")
        out.append(f"BẠN: {a}")
    return "\n".join(out)


def _coverage_block(cov: dict) -> str:
    have, req = cov.get("have", 0), cov.get("required", 0)
    miss = cov.get("missing") or []
    s = f"Điểm danh CHECKLIST FARM: {have}/{req} mục bắt buộc đã có"
    if miss:
        s += "; còn thiếu (bắt buộc): " + ", ".join(miss)
    extra = [f"{i['loai']} → {i['status']}" for i in (cov.get("items") or []) if i.get("nhom") != "bat_buoc"]
    if extra:
        s += "\nMục điều kiện/tùy chọn/làm sau: " + " | ".join(extra)
    return s


async def answer_question(case_meta: dict, ctx: dict, history, question: str, drive_id,
                          model: str | None = None, session_key: str | None = None) -> str:
    from .checklist import _call_openrouter
    model = model or CHAT_MODEL
    today = time.strftime("%d/%m/%Y")
    doc_names = ctx.get("doc_names") or []
    applicant = case_meta.get("applicant", "?")
    visa = case_meta.get("visa", "?")
    agent = case_meta.get("agent", "?")
    folder_link = case_meta.get("drive_link", "") or "(chưa có)"
    sysprompt = (_OFFICER_SYSTEM
                 .replace("{{TODAY}}", today)
                 .replace("{{DRIVE_LINK}}", folder_link)
                 .replace("{{DOC_LIST}}", ", ".join(doc_names) if doc_names else "(chưa có giấy tờ nào)"))
    report_text = (ctx.get("report_text") or "")[:8000]
    # KHÔNG đưa `drive_link` cho model — nó hay copy URL dài vào câu trả lời; bot tự gắn link từ name_to_link.
    docs_llm = [{k: v for k, v in (d or {}).items() if k != "drive_link"} for d in (ctx.get("docs") or [])]
    docs_json = json.dumps(docs_llm, ensure_ascii=False)
    cov_block = _coverage_block(ctx.get("coverage") or {})
    hist = _history_text(history)

    def mk_user(extra: str = "") -> str:
        return (f"=== HỒ SƠ KHÁCH HÀNG: {applicant} | visa {visa} | agent {agent} ===\n"
                f"--- {cov_block} ---\n"
                f"--- DỮ LIỆU GIẤY TỜ ĐÃ OCR (JSON — mỗi phần tử 1 giấy tờ; ten/loai/nguoi/tom_tat/du_lieu/key_fields) ---\n{docs_json}\n"
                + (f"--- BÁO CÁO THẨM ĐỊNH (trích) ---\n{report_text}\n" if report_text else "")
                + extra
                + f"--- LỊCH SỬ HỘI THOẠI GẦN ĐÂY ---\n{hist}\n"
                + f"--- CÂU HỎI CỦA NHÂN VIÊN ---\n{question}")
    try:
        # ── Có xác nhận đổi tên đang chờ? → xử lý trước, khỏi gọi model ──
        if session_key:
            pend = pop_pending_rename(session_key)
            if pend:
                if is_affirmative(question):
                    return do_rename(pend.get("case_folder_id", ""), pend.get("file_id", ""),
                                     pend.get("old", ""), pend.get("new", ""), drive_id)
                if is_negative(question):
                    return "Đã huỷ đổi tên."
                # không phải ok/huỷ → bỏ pending, trả lời câu hỏi như bình thường
        text = (await asyncio.to_thread(_call_openrouter, model, sysprompt, mk_user()) or "").strip()
        # ── Yêu cầu đổi tên file? (xử lý trước NEED_FILE/NEED_WEB) ──
        wren = parse_need_rename(text)
        if wren is not None:
            old_in, new_in = wren
            n2l = ctx.get("name_to_link") or {}
            old = old_in if old_in in n2l else _match_doc_name(old_in, doc_names)
            if not old:
                return ('Không tìm thấy file tên "' + old_in + '" trong hồ sơ. Các file: '
                        + (", ".join(doc_names) if doc_names else "(chưa có)"))
            fid = _file_id_from_link(n2l.get(old, ""))
            if not fid:
                return f'Không lấy được ID file để đổi tên "{old}".'
            new = _sanitize_new_name(new_in, old)
            if not new:
                return "Tên mới không hợp lệ."
            if new == old:
                return "Tên mới trùng tên hiện tại — không cần đổi."
            if session_key:
                set_pending_rename(session_key, file_id=fid, old=old, new=new,
                                   case_folder_id=case_meta.get("folder_id", ""))
                return f'Xác nhận đổi tên: "{old}" → "{new}"? (trả lời "ok" để đổi, "huỷ" để bỏ)'
            return do_rename(case_meta.get("folder_id", ""), fid, old, new, drive_id)
        # ── Tối đa 1 vòng tra thêm: NEED_ADDR (bảng địa giới) / NEED_FILE (đọc sâu 1 file) / NEED_WEB (tra ngoài) ──
        wfile = _is_need_file(text)
        wweb = _is_need_web(text)
        waddr = parse_need_addr(text)
        if waddr is not None:
            ans_addr = await asyncio.to_thread(addr_lookup_text, waddr)
            extra = f"--- TRA CỨU ĐỊA GIỚI HÀNH CHÍNH (bảng chính thức, deterministic — coi là chính xác) cho «{waddr}» ---\n{ans_addr}\n"
            print(f"chat: NEED_ADDR -> tra «{waddr}»", flush=True)
            text = (await asyncio.to_thread(_call_openrouter, model, sysprompt, mk_user(extra)) or "").strip()
            if parse_need_addr(text) is not None or _is_need_file(text) is not None or _is_need_web(text) is not None:
                text = "Cần kiểm tra thêm — chưa đủ căn cứ để kết luận chắc chắn."
        elif wfile is not None:
            dn = _match_doc_name(wfile, doc_names)
            if dn:
                link = (ctx.get("name_to_link") or {}).get(dn, "")
                # Tải file từ Drive chạy TRỰC TIẾP trên loop (Drive client httplib2 KHÔNG thread-safe —
                # tuyệt đối không bỏ vào asyncio.to_thread); chỉ phần OpenRouter mới được tách thread.
                ft = get_file_fulltext(link, drive_id)
                if ft:
                    extra = f"--- NGUYÊN VĂN GIẤY TỜ '{dn}' (OCR lại bằng {CHAT_SCAN_MODEL}) ---\n{ft}\n"
                    print(f"chat: NEED_FILE -> đã OCR lại {dn!r} ({len(ft)} ký tự)", flush=True)
                else:
                    extra = (f"(LƯU Ý: không lấy được nguyên văn của '{wfile}' — file không đọc được hoặc quá lớn. "
                             f"Trả lời dựa trên dữ liệu hiện có và nêu rõ giới hạn này.)\n")
            else:
                extra = (f"(LƯU Ý: không tìm thấy giấy tờ tên '{wfile}' trong hồ sơ. Trả lời dựa trên dữ liệu "
                         f"hiện có; nếu cần, gợi ý nhân viên kiểm tra lại tên file.)\n")
            text = (await asyncio.to_thread(_call_openrouter, model, sysprompt, mk_user(extra)) or "").strip()
            if _is_need_file(text) is not None or _is_need_web(text) is not None:
                text = "Cần kiểm tra thêm — chưa đủ dữ liệu để trả lời chính xác."
        elif wweb is not None:
            web = await asyncio.to_thread(web_search, wweb)
            if web:
                extra = (f"--- KẾT QUẢ TRA CỨU NGOÀI cho truy vấn '{wweb}' (qua web; có thể chưa hoàn toàn "
                         f"chính xác — nếu dẫn dữ kiện, nêu rõ nguồn) ---\n{web}\n")
                print(f"chat: NEED_WEB -> đã tra cứu {wweb!r} ({len(web)} ký tự)", flush=True)
            else:
                extra = (f"(LƯU Ý: không tra cứu được thông tin ngoài cho '{wweb}'. Trả lời dựa trên dữ liệu "
                         f"hiện có và nêu rõ giới hạn này — vd: 'cần kiểm tra văn bản chính thức về địa giới hành chính'.)\n")
            text = (await asyncio.to_thread(_call_openrouter, model, sysprompt, mk_user(extra)) or "").strip()
            if _is_need_web(text) is not None or _is_need_file(text) is not None:
                text = "Cần kiểm tra thêm thông tin chính thức — chưa đủ căn cứ để kết luận chắc chắn."
        if not text:
            text = "Không có thông tin để trả lời câu hỏi này dựa trên hồ sơ hiện có."
    except Exception as e:  # noqa: BLE001
        print(f"chat: answer_question lỗi: {type(e).__name__}: {e}", flush=True)
        return "❌ Lỗi khi xử lý câu hỏi, thử lại sau ít phút."
    if len(text) > CHAT_ANSWER_MAXLEN:
        text = text[:CHAT_ANSWER_MAXLEN].rstrip() + "\n…(rút gọn — hỏi tiếp nếu cần chi tiết)"
    return text


# ===========================================================================
# Linkify câu trả lời → Telegram-HTML: tên giấy tờ / URL Drive → <a href> clickable
# ===========================================================================
_DRIVE_URL_RE = re.compile(r"https?://(?:drive|docs)\.google\.com/[^\s)>\]]+")
_URL_TRAIL = ".,;:!?)]}>\"'"


def _strip_markdown_plain(t: str) -> str:
    """Bỏ định dạng markdown thô khỏi câu trả lời (model không nên dùng nhưng phòng hờ):
    **đậm** / __gạch chân__ / `code` → bỏ dấu; tiêu đề '#' → bỏ; bullet '* '/'+ ' → '- ';
    bỏ '(<url Drive>)' thừa; gộp khoảng trắng kép giữa dòng."""
    t = t or ""
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", t)
    t = re.sub(r"__([^_\n]+)__", r"\1", t)
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    t = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+", "", t)
    t = re.sub(r"(?m)^([ \t]*)[*+][ \t]+", r"\1- ", t)
    t = re.sub(r"[ \t]*[(\[]\s*(?:xem[: ]+)?https?://(?:drive|docs)\.google\.com/[^\s)\]]+\s*[)\]]", "", t)
    t = re.sub(r"(?<=\S)[ \t]{2,}", " ", t)
    return t


def linkify_answer(text: str, name_to_link: dict | None = None, folder_link: str = "") -> str:
    """Biến câu trả lời (plain text) thành Telegram-HTML — CHỈ tạo thẻ <a> (không <b>/<i>/<u>):
      - tên file giấy tờ (khớp `name_to_link`, có/không đuôi) → <a href="drive_link">tên</a>
      - URL Drive/Docs trần (kể cả `folder_link`) → <a href="url">tên giấy tờ | 📁 thư mục hồ sơ | mở liên kết</a>
      - mọi ký tự còn lại → html.escape
    Bỏ markdown thô trước (xem _strip_markdown_plain). HTML sinh ra luôn hợp lệ (không phụ thuộc LLM)."""
    text = _strip_markdown_plain(text or "")
    name_to_link = name_to_link or {}

    fid_to_name: dict[str, str] = {}            # file-id → tên ngắn nhất (để làm anchor đẹp cho URL trần)
    for ten, link in name_to_link.items():
        fid = _file_id_from_link(link or "")
        if fid and ten and (fid not in fid_to_name or len(ten) < len(fid_to_name[fid])):
            fid_to_name[fid] = ten
    folder_norm = (folder_link or "").rstrip(_URL_TRAIL)

    by_surface: dict[str, str] = {}             # "bề mặt" (tên đầy đủ / tên không đuôi) → drive_link
    ambiguous: set[str] = set()
    def _add(form: str, link: str) -> None:
        form = (form or "").strip()
        if not form or not link:
            return
        if form in by_surface and by_surface[form] != link:
            ambiguous.add(form)
        else:
            by_surface[form] = link
    for ten, link in name_to_link.items():
        _add(ten, link)
        stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", ten or "")
        if stem and stem != ten:
            _add(stem, link)
    for f in ambiguous:
        by_surface.pop(f, None)
    surfaces = sorted((s for s in by_surface if s), key=len, reverse=True)

    def _anchor(url: str, txt: str) -> str:
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(txt)}</a>'

    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        m = _DRIVE_URL_RE.match(text, i)
        if m:
            url = m.group(0).rstrip(_URL_TRAIL)
            if url:
                fid = _file_id_from_link(url)
                if folder_norm and url == folder_norm:
                    anchor_txt = "📁 thư mục hồ sơ"
                elif fid and fid in fid_to_name:
                    anchor_txt = fid_to_name[fid]
                else:
                    anchor_txt = "mở liên kết"
                out.append(_anchor(url, anchor_txt))
                i += len(url)
                continue
        hit = next((s for s in surfaces if text.startswith(s, i)), None)
        if hit:
            out.append(_anchor(by_surface[hit], hit))
            i += len(hit)
            continue
        out.append(html.escape(text[i], quote=False))
        i += 1
    return "".join(out)


# ===========================================================================
# self-check
# ===========================================================================
if __name__ == "__main__":
    print("CHAT_MODEL:", CHAT_MODEL, "| CHAT_SCAN_MODEL:", CHAT_SCAN_MODEL, "| CHAT_WEB_MODEL:", CHAT_WEB_MODEL)
    print("history_turns:", CHAT_HISTORY_TURNS, "| ctx_ttl:", CHAT_CTX_TTL, "| fulltext_ttl:", CHAT_FULLTEXT_TTL,
          "| web_ttl:", CHAT_WEB_TTL, "| cooldown:", CHAT_USER_COOLDOWN, "| concurrency:", CHAT_CONCURRENCY)
    for fn in (build_case_context, get_case_context, invalidate_case_cache, answer_question, get_file_fulltext,
               web_search, cases_for_staff, pick_case_for_dm, group_history, dm_session, check_cooldown, linkify_answer,
               _strip_markdown_plain, parse_need_rename, parse_need_addr, addr_lookup_text, is_affirmative, is_negative,
               set_pending_rename, pop_pending_rename, _sanitize_new_name, do_rename):
        assert callable(fn), fn
    _sys_lc = _OFFICER_SYSTEM.lower()
    assert "visa officer" in _sys_lc and "không nịnh" in _sys_lc and "need_file" in _sys_lc and "need_web" in _sys_lc
    assert "need_rename" in _sys_lc and "need_addr" in _sys_lc and "tên file" in _sys_lc and "{{DRIVE_LINK}}" in _OFFICER_SYSTEM
    s = (_OFFICER_SYSTEM.replace("{{TODAY}}", "12/05/2026")
         .replace("{{DRIVE_LINK}}", "https://drive.google.com/x").replace("{{DOC_LIST}}", "a.pdf, b.pdf"))
    assert "{{" not in s and "12/05/2026" in s and "a.pdf, b.pdf" in s and "drive.google.com/x" in s
    assert _is_need_file("NEED_FILE: GIAY KS.pdf") == "GIAY KS.pdf"
    assert _is_need_file("NEED_FILE: 'x.pdf'") == "x.pdf"
    assert _is_need_file("Câu trả lời bình thường.") is None
    assert _is_need_file("NEED_FILE: a\nkèm thêm dòng") is None  # nhiều dòng → không coi là NEED_FILE
    assert _is_need_web("NEED_WEB: phường Vinh Tân sáp nhập") == "phường Vinh Tân sáp nhập"
    assert _is_need_web("Bình thường") is None and _is_need_web("NEED_WEB: a\nb") is None
    assert _is_need_file("NEED_WEB: x") is None and _is_need_web("NEED_FILE: x") is None
    assert _match_doc_name("giay ks.pdf", ["GIAY KS.pdf", "CCCD.pdf"]) == "GIAY KS.pdf"
    assert _match_doc_name("cccd", ["GIAY KS.pdf", "CCCD-Hoang Thi Mo.pdf"]) == "CCCD-Hoang Thi Mo.pdf"
    c1 = {"applicant": "Nguyen Van A", "folder_id": "F1", "kind": "pro", "case_setup": True}
    c2 = {"applicant": "Tran Thi Bich", "folder_id": "F2", "kind": "pro", "case_setup": True}
    assert pick_case_for_dm("hỏi gì đó", [], None)[0] is None and pick_case_for_dm("x", [], None)[1]
    assert pick_case_for_dm("hồ sơ này sao rồi", [("P1", c1)], None)[0] is c1
    info, ask = pick_case_for_dm("hỏi về hồ sơ Tran Thi Bich đi", [("P1", c1), ("P2", c2)], None)
    assert info is c2 and ask is None
    info, ask = pick_case_for_dm("hồ sơ chung chung", [("P1", c1), ("P2", c2)], None)
    assert info is None and ask and "Tran Thi Bich" in ask and "Nguyen Van A" in ask
    info, ask = pick_case_for_dm("vậy còn passport thì sao?", [("P1", c1), ("P2", c2)], "F2")
    assert info is c2 and ask is None
    reg = {"P1": {"kind": "pro", "case_setup": True, "staff": ["111", "222"], "applicant": "A", "folder_id": "F1"},
           "P2": {"kind": "pro", "case_setup": True, "staff": ["333"], "applicant": "B", "folder_id": "F2"},
           "K1": {"kind": "kh", "case_setup": True, "staff": ["111"], "applicant": "A"}}
    assert [i.get("applicant") for _, i in cases_for_staff(reg, 111)] == ["A"]
    assert cases_for_staff(reg, 999) == []
    assert _file_id_from_link("https://drive.google.com/file/d/1abcDEF_ghijklmnopqrstuv/view?usp=drivesdk") == "1abcDEF_ghijklmnopqrstuv"
    assert _file_id_from_link("https://drive.google.com/open?id=1abcDEF_ghijklmnopqrstuv") == "1abcDEF_ghijklmnopqrstuv"
    assert _file_id_from_link("") == ""
    assert _coverage_block({"have": 12, "required": 18, "missing": ["x"], "items": [{"loai": "y", "nhom": "lam_sau", "status": "— sẽ làm sau"}]})
    # linkify_answer
    _n2l = {"CCCD-Hoang Thi Mo.pdf": "https://drive.google.com/file/d/1abcDEF_ghijklmnopqrstuv/view?usp=drivesdk"}
    _la = linkify_answer("Theo CCCD-Hoang Thi Mo cho thấy 1 < 2 và A&B", _n2l, "https://drive.google.com/drive/folders/1FOLDERidxxxxxxxxxxxxxx")
    assert '<a href="https://drive.google.com/file/d/1abcDEF_ghijklmnopqrstuv/view?usp=drivesdk">CCCD-Hoang Thi Mo</a>' in _la
    assert "1 &lt; 2 và A&amp;B" in _la
    assert linkify_answer("không có gì đặc biệt", {}, "") == "không có gì đặc biệt"
    _la2 = linkify_answer("Mở: https://drive.google.com/drive/folders/1FOLDERidxxxxxxxxxxxxxx.", _n2l, "https://drive.google.com/drive/folders/1FOLDERidxxxxxxxxxxxxxx")
    assert ">📁 thư mục hồ sơ</a>." in _la2
    # Round 2 — bỏ markdown + URL thừa, không double-link
    _n2l2 = {"Sao ke-Mo.pdf": "https://drive.google.com/file/d/1abcDEF_ghijklmnopqrstuv/view?usp=drivesdk"}
    _la3 = linkify_answer("xem `Sao ke-Mo.pdf (https://drive.google.com/file/d/1abcDEF_ghijklmnopqrstuv/view?usp=drivesdk)` và **đậm**", _n2l2, "")
    assert '<a href="https://drive.google.com/file/d/1abcDEF_ghijklmnopqrstuv/view?usp=drivesdk">Sao ke-Mo.pdf</a>' in _la3
    assert "đậm" in _la3 and "**" not in _la3 and "`" not in _la3 and "(https" not in _la3 and _la3.count("<a href=") == 1
    assert _strip_markdown_plain("* mục một\n* mục hai") == "- mục một\n- mục hai"
    assert _strip_markdown_plain("## Tiêu đề\n**x** và __y__") == "Tiêu đề\nx và y"
    # Round 3 — rename helpers
    assert parse_need_rename("NEED_RENAME: A.pdf => B.pdf") == ("A.pdf", "B.pdf")
    assert parse_need_rename("NEED_RENAME: `Khac-Mo.jpg` => 'ID photo-Mo.jpg'") == ("Khac-Mo.jpg", "ID photo-Mo.jpg")
    assert parse_need_rename("trả lời bình thường") is None and parse_need_rename("NEED_RENAME: A\nB => C") is None
    assert parse_need_addr("NEED_ADDR: Phường Vĩnh Tân, TP Vinh, Nghệ An") == "Phường Vĩnh Tân, TP Vinh, Nghệ An"
    assert parse_need_addr("trả lời bình thường") is None and parse_need_addr("NEED_ADDR: a\nb") is None
    _alt = addr_lookup_text("Xã Hợp Thịnh, Huyện Tam Dương, Tỉnh Vĩnh Phúc")
    assert "Phú Thọ" in _alt and "Vĩnh Phúc" in _alt   # tỉnh cũ → mới được nhận diện
    assert is_affirmative("ok") and is_affirmative("Đồng ý.") and is_affirmative("xác nhận") and not is_affirmative("ok đổi đi nhé")
    assert is_negative("huỷ") and is_negative("không") and not is_negative("không sao")
    assert _sanitize_new_name("ID photo/Mo", "Khac-Mo.jpg") == "ID photo-Mo.jpg"
    assert _sanitize_new_name("Sao ke 2-Mo", "X.pdf") == "Sao ke 2-Mo.pdf"
    assert _sanitize_new_name("   ", "X.pdf") == ""
    set_pending_rename("dm:1", file_id="F", old="A.pdf", new="B.pdf", case_folder_id="C")
    _p = pop_pending_rename("dm:1")
    assert _p and _p["new"] == "B.pdf" and pop_pending_rename("dm:1") is None
    print("OK")
