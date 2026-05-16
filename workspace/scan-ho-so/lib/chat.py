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
# Streaming chat (Phase mới): flash mặc định, pro escalate câu khó.
CHAT_MODEL_FAST = os.environ.get("CHAT_MODEL_FAST", "google/gemini-2.5-flash")
CHAT_MODEL_HARD = os.environ.get("CHAT_MODEL_HARD", CHAT_MODEL)   # default pro
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
CHAT_TEMPERATURE = float(os.environ.get("CHAT_TEMPERATURE", "0.3"))

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


_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Từ chức năng / xưng hô tiếng Việt — bỏ khi so token tên KH (đứng một mình thì vô nghĩa để phân biệt case).
_STOPWORDS = {
    "la", "va", "thi", "cua", "cho", "voi", "ve", "nay", "do", "day", "kia", "sao", "roi", "con", "chua", "da",
    "duoc", "dang", "se", "khong", "ko", "kiem", "tra", "xem", "check", "hoi", "can", "muon", "biet", "gi", "nao",
    "giup", "dum", "oi", "the", "hay", "hoac", "lai", "nhe", "nha", "di", "de", "ban", "minh", "toi", "ad", "bot",
    "ai", "ne", "luon", "mot", "cai", "co", "khi", "trong", "truoc", "sau", "moi", "nguoi", "ho", "so", "hoso",
    "anh", "chi", "em", "a", "o", "ma",
}


def _toks(s: str) -> set:
    """Tập token tên (không dấu, lowercase, đã bỏ stopword)."""
    return set(_TOKEN_RE.findall(_norm(s))) - _STOPWORDS


def _name_vocab(my_cases: list) -> set:
    """Tập union mọi token trong applicant của my_cases — KHÔNG bỏ stopword.
    Dùng để biết token nào trong message là TÊN người (giữ lại) vs xưng hô (bỏ)."""
    vocab: set = set()
    for _, info in my_cases:
        vocab |= set(_TOKEN_RE.findall(_norm(info.get("applicant", "") or "")))
    return vocab


def _toks_with_namevocab(s: str, name_vocab: set) -> set:
    """Giống _toks nhưng GIỮ token là tên người (vd 'Anh', 'Chi', 'Em', 'A') nếu có trong vocab.
    Cứu trường hợp tên KH chứa token vốn là xưng hô tiếng Việt."""
    raw = set(_TOKEN_RE.findall(_norm(s)))
    return {t for t in raw if t in name_vocab or t not in _STOPWORDS}


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


def _match_case(user_message: str, my_cases: list):
    """Khớp `user_message` với danh sách case. Hai cơ chế:
      (a) **Substring instant-match**: nếu `_norm(applicant_full)` (≥6 ký tự) là substring
          của `_norm(user_message)` cho DUY NHẤT 1 case → trả case đó ngay (gõ/dán đủ tên ăn liền).
      (b) **Scoring overlap token** — stopword-aware: tokens là TÊN người vẫn giữ lại
          (vd 'Anh', 'Chi', 'Em' khi xuất hiện trong applicant của ≥1 case trong my_cases).

    Trả (info|None, conflicting:list). info != None ⇔ DUY NHẤT 1 case thắng.
    conflicting = các case cùng đỉnh khi >1; [] nếu không case nào match."""
    if not my_cases:
        return None, []
    name_vocab = _name_vocab(my_cases)
    msg_norm = _norm(user_message)
    # (a) Substring instant-match — bắt được "Nguyễn Thị Anh 1999", "DH Pro WP10m - Nguyễn Thị Anh 1999 …"
    substring_hits = [info for _, info in my_cases
                      if (an := _norm(info.get("applicant", "") or "")) and len(an) >= 6 and an in msg_norm]
    if len(substring_hits) == 1:
        return substring_hits[0], []
    # (b) Scoring overlap, stopword-aware
    msg = _toks_with_namevocab(user_message, name_vocab)
    if not msg:
        return None, []
    scored = [(len(msg & _toks_with_namevocab(info.get("applicant", ""), name_vocab)), info)
              for _, info in my_cases]
    scored = [(n, info) for n, info in scored if n > 0]
    if not scored:
        return None, []
    top = max(n for n, _ in scored)
    best = [info for n, info in scored if n == top]
    return (best[0], []) if len(best) == 1 else (None, best)


def pick_case_for_dm(user_message: str, my_cases: list, active_folder):
    """Trả (info|None, ask_text|None). info = entry registry của case cho phiên DM này.
    Khi phải hỏi lại: KHÔNG liệt kê toàn bộ KH — chỉ hỏi tên; chỉ liệt kê khi staff gõ thứ trùng nhiều case."""
    if not my_cases:
        return None, ("Bạn chưa được giao hồ sơ nào trong hệ thống (hoặc bot chưa ghi nhận hoạt động của bạn "
                      "trong nhóm Pro). Hãy hỏi trực tiếp trong nhóm Pro của khách hàng.")
    if len(my_cases) == 1:
        return my_cases[0][1], None
    info, conflicts = _match_case(user_message, my_cases)
    if info is not None:                       # gõ đủ phân biệt → chốt luôn (kể cả khác case đang active)
        return info, None
    if active_folder:                          # đang hỏi tiếp về case đã chọn trong phiên DM này → giữ nguyên
        for _, c in my_cases:
            if c.get("folder_id") == active_folder:
                return c, None
    if conflicts:                              # gõ thứ trùng nhiều case → liệt kê đúng mấy case đó
        names = " · ".join(c.get("applicant", "?") for c in conflicts)
        return None, f"Khớp nhiều hồ sơ: {names}. Bạn hỏi hồ sơ nào? (gõ phần phân biệt, vd 'test8')"
    return None, "Bạn muốn hỏi về hồ sơ KH nào? (gõ tên KH hoặc phần phân biệt, vd 'test8')"


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
    try:  # tra địa giới hành chính cũ↔mới cho mọi địa chỉ trong hồ sơ → ground-truth cho LLM (đỡ phải NEED_ADDR)
        from .checklist import build_dia_gioi
        dia_gioi = build_dia_gioi(dataset, None)
    except Exception as e:  # noqa: BLE001
        print(f"chat: build_dia_gioi lỗi: {type(e).__name__}: {e}", flush=True)
        dia_gioi = None
    return {"docs": docs, "name_to_link": name_to_link, "coverage": cov, "dia_gioi": dia_gioi,
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

PHONG CÁCH:
- Tự nhiên và thân thiện như đồng nghiệp — ngắn gọn, không lan man. Câu chào/cảm ơn/tạm biệt → trả lời
  1 câu ngắn tự nhiên (không cần dẫn hồ sơ); câu hỏi về hồ sơ → vào thẳng nội dung.
- Nêu con số / ngày cụ thể. Không "dạ vâng" lan man, không mở đầu sáo rỗng dài dòng.
- TIN NHẮN PHẲNG: KHÔNG dùng định dạng markdown — KHÔNG in đậm (**...**), KHÔNG in nghiêng (*...* / _..._),
  KHÔNG gạch chân, KHÔNG code (`...`), KHÔNG tiêu đề (#). Chỉ văn bản thường. Khi câu trả lời có nhiều ý
  rời rạc → đánh số "1." "2." hoặc gạch đầu dòng "-", mỗi ý trên dòng riêng; dùng dòng trống phân tách
  khi trả lời dài. Tên file viết bình thường (KHÔNG backtick, KHÔNG bọc ngoặc, KHÔNG kèm URL).
- Khi nêu một dữ kiện về hồ sơ → DẪN NGUỒN: ghi đúng TÊN FILE của giấy tờ đó (trường `ten` trong DỮ LIỆU
  bên dưới), vd: theo CCCD-Hoang Thi Mo …, trên LLTP-Hoang Thi Mo ghi ….
- CHỈ dựa trên DỮ LIỆU HỒ SƠ + BÁO CÁO THẨM ĐỊNH + (nếu có) NGUYÊN VĂN GIẤY TỜ / KẾT QUẢ TRA CỨU được cung
  cấp dưới đây. KHÔNG bịa, KHÔNG suy đoán. Thiếu dữ liệu → nói rõ "không có trong hồ sơ / chưa đọc được".
- ĐỊA GIỚI HÀNH CHÍNH: phần "ĐỊA GIỚI HÀNH CHÍNH" bên dưới (nếu có) là kết quả tra cứu DETERMINISTIC từ bảng
  chính thức 2025 (cũ↔mới, tới cấp xã/phường) — COI LÀ GROUND-TRUTH. Hai địa chỉ khác nhau chỉ vì tên TRƯỚC
  vs SAU cải cách (`doi_chieu`=`same`, hoặc cùng `don_vi_moi`) thì KHÔNG phải mâu thuẫn — ĐỪNG gọi đó là "lỗi"
  / "không khớp" / "cần đồng bộ". Nếu BÁO CÁO THẨM ĐỊNH (có thể tạo từ lần thẩm định CŨ, trước khi có bảng này)
  coi cặp đó là mâu thuẫn nhưng phần ĐỊA GIỚI HÀNH CHÍNH cho thấy `same` → TIN phần ĐỊA GIỚI HÀNH CHÍNH; nói
  rõ với nhân viên đây chỉ là tên trước/sau cải cách 2025 (nêu tên đơn vị MỚI), và gợi ý chạy lại /check để báo
  cáo cập nhật. (Giấy cấp SAU mốc cải cách — tỉnh 12/06/2025, xã 01/07/2025 — mà còn ghi đơn vị `la_ten_cu=true`:
  vẫn nên nhắc cập nhật theo tên mới.) `do_tin`=`unknown`/`fuzzy` → bảng chưa chắc phủ hết, tự đánh giá thêm.
- TUYỆT ĐỐI không tiết lộ thông tin của bất kỳ khách hàng nào khác ngoài hồ sơ này.
- Mặc định: gần như MỌI câu hỏi của nhân viên trong khung này đều LIÊN QUAN đến hồ sơ này — hãy cố trả lời
  theo hướng đó (kể cả câu nói tắt / mơ hồ / kèm yêu cầu phụ như "gửi link", "in ra", "tôi check lại"…).
  Câu hỏi CHUNG về visa/thủ tục FARM (không cần đọc giấy tờ cụ thể, vd "FARM cần mấy tháng sao kê?") →
  dùng NEED_WEB để tra thay vì từ chối. CHỈ từ chối khi câu hỏi RÕ RÀNG ngoài lề không liên quan gì đến
  visa/hồ sơ (thời tiết, tin tức, chuyện riêng tư thuần túy); lúc đó mới trả lời ngắn tự nhiên rồi hỏi
  nhân viên cần giúp gì về hồ sơ.
- LINK: KHÔNG tự dán URL Drive. Khi nhân viên muốn mở / xem / "check lại" / DẪN LINK / GỬI LINK / URL /
  đường dẫn của một (hoặc vài) giấy tờ cụ thể → chỉ cần NHẮC ĐÚNG TÊN FILE của giấy tờ đó Y NGUYÊN
  như trong DỮ LIỆU (trường `ten`), MỖI tên file 1 dòng, KHÔNG kèm chú thích/giải thích/URL. Hệ thống tự
  gắn link clickable vào đúng tên file đó. Khi nhân viên muốn mở CẢ hồ sơ → nhắc tới "thư mục hồ sơ"
  (link thư mục Drive của hồ sơ này: {{DRIVE_LINK}}).
  ⚠️ TUYỆT ĐỐI: viết tên file EXACT từ {{DOC_LIST}} — KHÔNG bỏ suffix năm sinh (vd ` Test11 2007`),
  KHÔNG bỏ prefix quan hệ (` con`/` bo`/` me`/` vc`), KHÔNG rút gọn ` Hoang Thi Mo` → ` Hoang Thi Mo`.
  Nếu DOC_LIST có `CCCD-Hoang Thi Mo Test11 2007.pdf` thì viết đầy đủ vậy, KHÔNG được viết
  `CCCD-Hoang Thi Mo.pdf` (file đó không tồn tại trong hồ sơ).
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

_GENERAL_SYSTEM = """Bạn là trợ lý hỗ trợ nhân viên Đồng Hành / ALLY về visa Canada (FARM / LMIA / Work Permit).
Trả lời bằng tiếng Việt. Tự nhiên, thân thiện như đồng nghiệp.

- Câu chào / cảm ơn / tin nhắn thông thường → trả lời ngắn gọn tự nhiên, hỏi có cần giúp gì không.
- Câu hỏi về quy trình, danh sách giấy tờ, chính sách visa Canada → trả lời trực tiếp nếu biết chắc, hoặc dùng:
  NEED_WEB: <truy vấn ngắn gọn tiếng Việt>
- Câu hỏi cần hồ sơ cụ thể → hướng dẫn nhân viên nhắn trong nhóm Pro hoặc dùng /case trong DM để chọn ca.

KHÔNG in đậm markdown (**...**). Liệt kê nhiều ý thì dùng "1." "2." hoặc "-", mỗi ý dòng riêng."""

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


# ===========================================================================
# "Dẫn / gửi link" intent — bypass LLM khi staff xin link của 1+ file cụ thể.
# Trả thẳng danh sách tên file (1/dòng) → linkify_answer sẽ wrap thành <a>.
# ===========================================================================
_LINK_NOUN_RE = re.compile(r"\b(link|url|liên\s*kết|đường\s*dẫn)\b", re.IGNORECASE)
_LINK_VERB_RE = re.compile(
    r"\b(dẫn|gửi|gởi|cho|send|đưa|lấy|kiếm|tìm|tag|xem|mở|click|đâu|where)\b",
    re.IGNORECASE,
)
# "Khách" / "đương đơn" / "kh" → chỉ list file của ĐƯƠNG ĐƠN (không bố/mẹ/con/vc/anh chị em).
_APPLICANT_FILTER_RE = re.compile(
    r"\b(khách|đương\s*đơn|\bkh\b|chính\s*chủ|của\s*kh)\b",
    re.IGNORECASE,
)
# Doc-type aliases: tag SOP → list từ khoá (lower, đã strip diacritics nếu cần).
# Build từ data/doc_types.yaml `description` field (tag name + alias VN/EN).
DOC_TYPE_ALIASES: dict[str, list[str]] = {
    "CCCD":               ["cccd", "căn cước", "can cuoc", "cmnd"],
    "Passport":           ["passport", "hộ chiếu", "ho chieu", " hc "],
    "GKS":                ["khai sinh", "trích lục khai sinh", "gks"],
    "GKH":                ["đăng ký kết hôn", "kết hôn", "gkh", "hôn thú"],
    "Ly hon":             ["ly hôn", "ly hon"],
    "XN hoc":             ["xác nhận học", "xn học", "giấy xn học sinh"],
    "XNCT":               ["cư trú", "ct07", "xnct", "xác nhận cư trú"],
    "LLTP":               ["lý lịch", "ly lich", "lltp", "phiếu lý lịch"],
    "Hien mau":           ["hiến máu", "hien mau"],
    "GPLX":               ["bằng lái", "bang lai", "gplx", "giấy phép lái"],
    "Anh the":            ["ảnh thẻ", "anh the", "ảnh 5x7", "ảnh chân dung"],
    "BHXH":               ["bhxh", "bảo hiểm xã hội", "bhxh tự nguyện"],
    "BHYT":               ["bhyt", "bảo hiểm y tế", "thẻ bhyt"],
    "IOM":                ["iom", "khám iom"],
    "CV":                 ["sơ yếu", "syll", "cv", "thông tin cá nhân", "tự khai"],
    "The Visa-MC":        ["thẻ visa", "mastercard", "the visa", "thẻ tín dụng"],
    "Bang khen":          ["bằng khen", "bang khen", "giấy khen", "thư cảm ơn"],
    "Anh gia dinh":       ["ảnh gia đình", "anh gia dinh", "family photo"],
    "Bang cap":           ["bằng cấp", "bang cap", "bằng tốt nghiệp", "diploma"],
    "So dat":             ["sổ đỏ", "so do", "sổ đất", "gcnqsd", "sổ hồng"],
    "HD cho-tang-thua ke": ["hợp đồng cho", "tặng đất", "thừa kế", "hd cho", "hd tặng"],
    "STK":                ["sổ tiết kiệm", "so tiet kiem", "stk"],
    "XN so du":           ["xác nhận số dư", "xn số dư", "xnsd"],
    "Ca vet xe":          ["cà vẹt", "ca vet", "đăng ký xe"],
    "Vang":               ["mua vàng", "hoá đơn vàng", " sjc "],
    "So dat NN":          ["đất nông nghiệp", "sổ đất nn", "đất canh tác"],
    "DKKD":               ["đăng ký kinh doanh", "dkkd", "htx"],
    "Dai ly NS":          ["đại lý nông sản", "đại lý phân bón", "đại lý"],
    "Anh-video lam nong": ["ảnh nông", "video nông", "ảnh làm nông", "video làm nông"],
    "Sao ke":             ["sao kê", "sao ke", "sao kê ngân hàng"],
    "HDLD":               ["hợp đồng lao động", "hdld"],
}


def _detect_doc_type(q: str) -> str | None:
    """Tìm tag doc type được nhắc trong câu hỏi (lowercase substring match).
    Ưu tiên tag có alias DÀI hơn (vd "khai sinh" trước "cv" cho câu 'khai sinh cv')."""
    q_lower = " " + q.lower() + " "   # đệm khoảng trắng để match alias có space đầu/cuối
    matched_tags: list[tuple[int, str]] = []   # (alias_len, tag)
    for tag, aliases in DOC_TYPE_ALIASES.items():
        for alias in aliases:
            if alias.lower() in q_lower:
                matched_tags.append((len(alias), tag))
                break
    if not matched_tags:
        return None
    matched_tags.sort(reverse=True)   # alias dài nhất thắng
    return matched_tags[0][1]


def _try_link_intent(question: str, name_to_link: dict | None,
                      dataset: list[dict] | None = None) -> str | None:
    """Bypass LLM khi staff yêu cầu link file cụ thể HOẶC list file theo doc type.

    Mode 1 (cũ): câu có cả LINK noun + verb + tên file → list filename match.
    Mode 2 (mới): câu có verb + doc type → list mọi file thuộc tag đó trong case.
                  Nếu có "khách/đương đơn/kh" → filter chỉ file của đương đơn (no relation).

    Trả `\\n`-joined filenames, hoặc None nếu không khớp → caller fallback LLM."""
    q = (question or "").strip()
    if not q or not name_to_link:
        return None
    has_link_noun = bool(_LINK_NOUN_RE.search(q))
    has_verb = bool(_LINK_VERB_RE.search(q))

    # Mode 1 — noun + verb + filename trong câu (cũ)
    if has_link_noun and has_verb:
        matched: list[str] = []
        seen: set[str] = set()
        for ten in sorted((t for t in name_to_link if t), key=len, reverse=True):
            link = name_to_link.get(ten) or ""
            if not link or ten in seen:
                continue
            stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", ten)
            if ten in q or (stem and stem != ten and stem in q):
                matched.append(ten)
                seen.add(ten)
        if matched:
            return "\n".join(matched)

    # Mode 2 — verb + doc type → list mọi file thuộc tag đó
    if has_verb and dataset:
        doc_tag = _detect_doc_type(q)
        if doc_tag:
            applicant_only = bool(_APPLICANT_FILTER_RE.search(q))
            matches = []
            for d in dataset:
                if (d.get("tag") or d.get("loai")) != doc_tag:
                    continue
                if applicant_only and (d.get("relation") or ""):
                    continue   # bỏ file của người thân (CCCD bố/mẹ…)
                ten = d.get("ten") or d.get("new_name") or ""
                if ten and ten in name_to_link:
                    matches.append(ten)
            if matches:
                return "\n".join(matches)
    return None


# ===========================================================================
# Hard-question detection — chọn model flash (mặc định) vs pro (câu reasoning sâu).
# Goal: bot chat fast cho 80% câu hỏi (file X ghi gì / thiếu giấy gì / dẫn link),
# escalate pro chỉ khi câu cần reasoning đa-giấy (phân tích / so sánh / mâu thuẫn).
# ===========================================================================
_HARD_KEYWORDS = (
    "tại sao", "phân tích", "so sánh", "mâu thuẫn", "đối chiếu",
    "tất cả", "toàn bộ", "thẩm định", "kiểm tra hết", "viết báo cáo",
    "đánh giá", "tổng hợp", "rà soát", "lý do",
)
_HARD_LEN_THRESHOLD = 120


def is_hard_question(q: str) -> bool:
    """Heuristic chọn model: True → pro (reasoning), False → flash (Q&A nhanh)."""
    if not q:
        return False
    q_lower = q.lower()
    if len(q) > _HARD_LEN_THRESHOLD:
        return True
    return any(k in q_lower for k in _HARD_KEYWORDS)


async def answer_question(case_meta: dict, ctx: dict, history, question: str, drive_id,
                          model: str | None = None, session_key: str | None = None,
                          stream_callback=None) -> str:
    """Trả câu trả lời. Nếu `stream_callback` (async function) được pass, LLM call sẽ stream
    qua callback (mỗi delta) → Telegram_listener edit_message để user thấy text "rớt" dần.
    `model` không truyền → auto chọn fast/hard theo `is_hard_question(question)`.
    """
    from .checklist import _call_openrouter, _call_openrouter_stream
    if model is None:
        model = CHAT_MODEL_HARD if is_hard_question(question) else CHAT_MODEL_FAST
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
    dg = ctx.get("dia_gioi") or None
    dia_gioi_json = json.dumps(dg, ensure_ascii=False) if isinstance(dg, dict) and (dg.get("dia_chi_da_tra") or dg.get("doi_chieu")) else ""
    # KHÔNG đưa `drive_link` cho model — nó hay copy URL dài vào câu trả lời; bot tự gắn link từ name_to_link.
    docs_llm = [{k: v for k, v in (d or {}).items() if k != "drive_link"} for d in (ctx.get("docs") or [])]
    docs_json = json.dumps(docs_llm, ensure_ascii=False)
    cov_block = _coverage_block(ctx.get("coverage") or {})
    hist = _history_text(history)

    def mk_user(extra: str = "") -> str:
        return (f"=== HỒ SƠ KHÁCH HÀNG: {applicant} | visa {visa} | agent {agent} ===\n"
                f"--- {cov_block} ---\n"
                f"--- DỮ LIỆU GIẤY TỜ ĐÃ OCR (JSON — mỗi phần tử 1 giấy tờ; ten/loai/nguoi/tom_tat/du_lieu/key_fields) ---\n{docs_json}\n"
                + (f"--- ĐỊA GIỚI HÀNH CHÍNH (tra cứu DETERMINISTIC từ bảng chính thức 2025, cũ↔mới tới cấp xã/phường — GROUND-TRUTH; `doi_chieu`=`same` ⇒ hai địa chỉ chỉ khác do tên trước/sau cải cách, KHÔNG phải mâu thuẫn) ---\n{dia_gioi_json}\n" if dia_gioi_json else "")
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
        # ── Yêu cầu LINK của file cụ thể? → bypass LLM, linkify sẽ tự gắn <a> ──
        _li = _try_link_intent(question, ctx.get("name_to_link") or {}, ctx.get("docs") or [])
        if _li:
            return _li
        # ── Gọi LLM: stream nếu caller pass stream_callback (cho Telegram edit_message UX) ──
        if stream_callback:
            try:
                text = (await _call_openrouter_stream(model, sysprompt, mk_user(),
                                                       stream_callback, temperature=CHAT_TEMPERATURE) or "").strip()
            except Exception as e:  # noqa: BLE001 — fallback non-stream
                print(f"chat: stream lỗi ({type(e).__name__}: {e}) — fallback non-stream", flush=True)
                text = (await asyncio.to_thread(_call_openrouter, model, sysprompt, mk_user(),
                                                 temperature=CHAT_TEMPERATURE) or "").strip()
        else:
            text = (await asyncio.to_thread(_call_openrouter, model, sysprompt, mk_user(),
                                             temperature=CHAT_TEMPERATURE) or "").strip()
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
            text = (await asyncio.to_thread(_call_openrouter, model, sysprompt, mk_user(extra),
                                             temperature=CHAT_TEMPERATURE) or "").strip()
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
            text = (await asyncio.to_thread(_call_openrouter, model, sysprompt, mk_user(extra),
                                             temperature=CHAT_TEMPERATURE) or "").strip()
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
            text = (await asyncio.to_thread(_call_openrouter, model, sysprompt, mk_user(extra),
                                             temperature=CHAT_TEMPERATURE) or "").strip()
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


async def answer_general(history, question: str, stream_callback=None) -> str:
    """Trả lời câu hỏi chung (không cần case context): small talk + visa Canada chung."""
    from .checklist import _call_openrouter, _call_openrouter_stream
    model = CHAT_MODEL_HARD if is_hard_question(question) else CHAT_MODEL_FAST
    hist = _history_text(history)
    user_msg = f"--- LỊCH SỬ GẦN ĐÂY ---\n{hist}\n--- CÂU HỎI ---\n{question}"
    try:
        if stream_callback:
            text = (await _call_openrouter_stream(model, _GENERAL_SYSTEM, user_msg,
                                                   stream_callback, temperature=CHAT_TEMPERATURE) or "").strip()
        else:
            text = (await asyncio.to_thread(_call_openrouter, model, _GENERAL_SYSTEM, user_msg,
                                             temperature=CHAT_TEMPERATURE) or "").strip()
        wweb = _is_need_web(text)
        if wweb:
            web = await asyncio.to_thread(web_search, wweb)
            if web:
                extra = f"--- KẾT QUẢ TRA CỨU ---\n{web}\n"
                user_msg2 = extra + user_msg
                if stream_callback:
                    text = (await _call_openrouter_stream(model, _GENERAL_SYSTEM, user_msg2,
                                                           stream_callback, temperature=CHAT_TEMPERATURE) or "").strip()
                else:
                    text = (await asyncio.to_thread(_call_openrouter, model, _GENERAL_SYSTEM, user_msg2,
                                                     temperature=CHAT_TEMPERATURE) or "").strip()
    except Exception as e:  # noqa: BLE001
        print(f"chat: answer_general lỗi: {type(e).__name__}: {e}", flush=True)
        return "❌ Lỗi khi xử lý câu hỏi, thử lại sau ít phút."
    return (text or "")[:CHAT_ANSWER_MAXLEN]


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

    def _fuzzy_at(start: int) -> tuple[str, int] | None:
        """Tại vị trí `start`, tìm token có prefix khớp 1 file duy nhất trong `surfaces`
        → trả (full_surface, token_len). Wrap với SURFACE ĐẦY ĐỦ thay vì token rút gọn.

        Giải quyết hallucination LLM (vd "CCCD-Hoang Thi Mo" thiếu " Test11 2007"):
        bot trả tên file rút gọn → linkify wrap thành link đúng file thật.

        Tokenization: greedy đến next stop punct (newline / `,;:!?()[]{}"'`).
        Boundary check: yêu cầu token ≥8 chars + chứa `-` hoặc digit (signature filename).
        Disambiguation: nếu nhiều surface khớp prefix nhưng cùng drive_link (vd full + stem)
        → coi là 1 file duy nhất, dùng surface DÀI NHẤT làm anchor text."""
        # Tìm các vị trí boundary (stop char) từ start tới EOF
        boundaries: list[int] = []
        j = start
        while j < n:
            if text[j] in "\n\r,;:!?()[]{}\"'":
                boundaries.append(j)
            j += 1
        boundaries.append(n)   # EOF cũng là boundary
        # Thử token dài nhất trước
        for end in sorted(boundaries, reverse=True):
            if end <= start:
                continue
            token = text[start:end].rstrip()
            if len(token) < 8:
                continue
            if "-" not in token and not any(c.isdigit() for c in token):
                continue
            candidates = [s for s in surfaces if len(s) > len(token) and s.startswith(token)]
            if not candidates:
                continue
            # Dedupe theo drive_link: nếu nhiều surface khớp nhưng cùng file → OK
            unique_links = {by_surface.get(s) for s in candidates}
            if len(unique_links) == 1:
                matched_surface = max(candidates, key=len)
                return (matched_surface, end - start)
        return None

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
        # Fuzzy fallback: LLM viết tên file thiếu suffix/prefix → wrap với surface đầy đủ
        fuzzy = _fuzzy_at(i)
        if fuzzy:
            matched_surface, token_len = fuzzy
            out.append(_anchor(by_surface[matched_surface], matched_surface))
            i += token_len
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
    for fn in (build_case_context, get_case_context, invalidate_case_cache, answer_question, answer_general, get_file_fulltext,
               web_search, cases_for_staff, _match_case, pick_case_for_dm, group_history, dm_session, check_cooldown, linkify_answer,
               _strip_markdown_plain, parse_need_rename, parse_need_addr, addr_lookup_text, is_affirmative, is_negative,
               set_pending_rename, pop_pending_rename, _sanitize_new_name, do_rename, _try_link_intent):
        assert callable(fn), fn
    _sys_lc = _OFFICER_SYSTEM.lower()
    assert "visa officer" in _sys_lc and "tự nhiên" in _sys_lc and "need_file" in _sys_lc and "need_web" in _sys_lc
    assert "need_rename" in _sys_lc and "need_addr" in _sys_lc and "tên file" in _sys_lc and "{{DRIVE_LINK}}" in _OFFICER_SYSTEM
    assert "địa giới hành chính" in _sys_lc and "ground-truth" in _sys_lc and "doi_chieu" in _sys_lc  # mục địa-giới ground-truth
    assert "dẫn link" in _sys_lc and "y nguyên" in _sys_lc  # rule LINK mới: yêu cầu LLM lặp tên file y nguyên
    # _try_link_intent — bypass LLM khi staff xin link file cụ thể
    _n2l = {"So dat-Tran Thong Tin.pdf": "https://drive.google.com/file/d/A/view",
            "So dat-Tran Van Ly.pdf":    "https://drive.google.com/file/d/B/view",
            "CCCD-Tran Van Huy.pdf":     "https://drive.google.com/file/d/C/view"}
    _ans = _try_link_intent("hãy dẫn link\n- So dat-Tran Thong Tin.pdf\n- So dat-Tran Van Ly.pdf", _n2l)
    assert _ans == "So dat-Tran Thong Tin.pdf\nSo dat-Tran Van Ly.pdf", _ans
    assert _try_link_intent("gửi link So dat-Tran Van Ly", _n2l) == "So dat-Tran Van Ly.pdf"
    assert _try_link_intent("cho mình URL của CCCD-Tran Van Huy.pdf", _n2l) == "CCCD-Tran Van Huy.pdf"
    assert _try_link_intent("dẫn đường dẫn CCCD-Tran Van Huy.pdf", _n2l) == "CCCD-Tran Van Huy.pdf"
    assert _try_link_intent("file này có link không?", _n2l) is None           # có noun, không có verb chỉ định
    assert _try_link_intent("dẫn link giúp", _n2l) is None                     # có intent, không filename
    assert _try_link_intent("mở giùm CCCD-Tran Van Huy.pdf", _n2l) is None     # không có noun link/url
    assert _try_link_intent("", _n2l) is None
    assert _try_link_intent("dẫn link CCCD-Tran Van Huy.pdf", {}) is None     # name_to_link rỗng

    # === Mode 2 — doc-type query (mới) ===
    _n2l_case = {
        "CCCD-Hoang Thi Mo Test11 2007.pdf": "https://drive.google.com/file/d/CC1/view",
        "CCCD con-Hoang Thi Mo.pdf":          "https://drive.google.com/file/d/CC2/view",
        "Passport-Hoang Thi Mo.pdf":          "https://drive.google.com/file/d/P1/view",
        "LLTP-Hoang Thi Mo.pdf":              "https://drive.google.com/file/d/L1/view",
    }
    _dataset_case = [
        {"tag": "CCCD",     "ten": "CCCD-Hoang Thi Mo Test11 2007.pdf", "relation": None},
        {"tag": "CCCD",     "ten": "CCCD con-Hoang Thi Mo.pdf",          "relation": "con"},
        {"tag": "Passport", "ten": "Passport-Hoang Thi Mo.pdf",          "relation": None},
        {"tag": "LLTP",     "ten": "LLTP-Hoang Thi Mo.pdf",              "relation": None},
    ]
    # "cho tôi CCCD" → list tất cả CCCD (2 file)
    res = _try_link_intent("cho tôi cccd", _n2l_case, _dataset_case)
    assert res and "CCCD-Hoang Thi Mo Test11 2007.pdf" in res and "CCCD con-Hoang Thi Mo.pdf" in res, res
    # "cho tôi cccd khách" → chỉ đương đơn (no relation) → chỉ 1 file
    res2 = _try_link_intent("cho tôi cccd khách", _n2l_case, _dataset_case)
    assert res2 == "CCCD-Hoang Thi Mo Test11 2007.pdf", res2
    # "lấy passport" → 1 file Passport
    assert _try_link_intent("lấy passport", _n2l_case, _dataset_case) == "Passport-Hoang Thi Mo.pdf"
    # "đưa LLTP" → 1 file LLTP
    assert _try_link_intent("đưa lltp", _n2l_case, _dataset_case) == "LLTP-Hoang Thi Mo.pdf"
    # "passport đâu" → verb "đâu" + tag → match
    assert _try_link_intent("passport đâu", _n2l_case, _dataset_case) == "Passport-Hoang Thi Mo.pdf"
    # "link" alone (chỉ noun, no verb, no dataset clue) → None
    assert _try_link_intent("link", _n2l_case, _dataset_case) is None
    # "cccd" alone (no verb) → None
    assert _try_link_intent("cccd", _n2l_case, _dataset_case) is None
    # "đổi tên file LLTP" — có verb "đổi" không trong _LINK_VERB_RE, no link noun → None
    assert _try_link_intent("đổi tên file LLTP", _n2l_case, _dataset_case) is None

    # === Fuzzy match trong linkify_answer ===
    # LLM viết "CCCD-Hoang Thi Mo" thiếu " Test11 2007.pdf" → fuzzy wrap với surface đầy đủ
    _html = linkify_answer("CCCD-Hoang Thi Mo", _n2l_case, "")
    assert '<a href' in _html and "CCCD-Hoang Thi Mo Test11 2007.pdf" in _html, _html
    # Multiple matches (CCCD-… và CCCD con-…) — text "CCCD" 4 chars < 8 → KHÔNG wrap fuzzy
    _html2 = linkify_answer("CCCD", _n2l_case, "")
    assert '<a' not in _html2, _html2
    # Exact match vẫn hoạt động (chính xác hơn fuzzy)
    _html3 = linkify_answer("Passport-Hoang Thi Mo.pdf", _n2l_case, "")
    assert '<a href' in _html3 and "Passport-Hoang Thi Mo.pdf" in _html3, _html3
    print("link intent Mode 2 + fuzzy linkify OK")
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
    c6 = {"applicant": "Hoàng Thị Mơ TEST6 1991", "folder_id": "F6", "kind": "pro", "case_setup": True}
    c8 = {"applicant": "Hoàng Thị Mơ TEST8 1991", "folder_id": "F8", "kind": "pro", "case_setup": True}
    assert pick_case_for_dm("hỏi gì đó", [], None)[0] is None and pick_case_for_dm("x", [], None)[1]
    assert pick_case_for_dm("hồ sơ này sao rồi", [("P1", c1)], None)[0] is c1
    info, ask = pick_case_for_dm("hỏi về hồ sơ Tran Thi Bich đi", [("P1", c1), ("P2", c2)], None)
    assert info is c2 and ask is None
    # gõ "test8" → chốt được dù 2 hồ sơ trùng tên cơ sở; gõ tên trùng → liệt kê đúng 2 case; câu không có tên → hỏi suông, KHÔNG liệt kê
    info, ask = pick_case_for_dm("test8", [("P6", c6), ("P8", c8)], None)
    assert info is c8 and ask is None
    info, ask = pick_case_for_dm("kiểm tra lại địa chỉ hồ sơ Hoàng Thị Mơ TEST8 1991", [("P6", c6), ("P8", c8)], None)
    assert info is c8 and ask is None
    info, ask = pick_case_for_dm("hồ sơ Hoàng Thị Mơ sao rồi", [("P6", c6), ("P8", c8)], None)
    assert info is None and ask and "TEST6" in ask and "TEST8" in ask
    info, ask = pick_case_for_dm("kiểm tra lại địa chỉ", [("P6", c6), ("P8", c8)], None)
    assert info is None and ask and "TEST6" not in ask and "TEST8" not in ask
    info, ask = pick_case_for_dm("vậy còn passport thì sao?", [("P1", c1), ("P2", c2)], "F2")
    assert info is c2 and ask is None
    # === Regression bug: tên KH chứa token là xưng hô tiếng Việt (Anh / Chi / Em / A) ===
    cA = {"applicant": "Nguyễn Thị Anh 1999", "folder_id": "FA", "kind": "pro", "case_setup": True}
    cD = {"applicant": "Nguyễn Văn Duyệt 1999", "folder_id": "FD", "kind": "pro", "case_setup": True}
    cS = {"applicant": "Nguyễn Quốc Sơn 2004", "folder_id": "FS", "kind": "pro", "case_setup": True}
    triple = [("PA", cA), ("PD", cD), ("PS", cS)]
    # 1. Substring match — gõ ĐỦ tên đầy đủ → ăn ngay, dù token "Anh" trước đây bị bỏ
    info, ask = pick_case_for_dm("Nguyễn Thị Anh 1999 đến đâu rồi", triple, None)
    assert info is cA and ask is None
    # 2. Substring match — dán nguyên group title prefix vẫn ăn
    info, ask = pick_case_for_dm("DH Pro WP10m - Nguyễn Thị Anh 1999 => khách này đến đâu rồi", triple, None)
    assert info is cA and ask is None
    # 3. Scoring — gõ "Anh 1999" (không substring đủ) — vẫn match vì 'Anh' nay được giữ làm tên
    info, ask = pick_case_for_dm("báo cáo Anh 1999", [("PA", cA), ("PD", cD)], None)
    assert info is cA and ask is None
    # 4. Discriminator hiếm (Duyệt) — match cD
    info, ask = pick_case_for_dm("báo cáo Duyệt 1999", [("PA", cA), ("PD", cD)], None)
    assert info is cD and ask is None
    # 5. Câu chung chung chỉ có "1999" → conflict, liệt kê 2 case 1999 (KHÔNG kèm Sơn 2004)
    info, ask = pick_case_for_dm("hồ sơ 1999 thế nào", triple, None)
    assert info is None and ask and "Anh" in ask and "Duyệt" in ask and "Sơn" not in ask
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
