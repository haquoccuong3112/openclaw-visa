# Pipeline Architecture — @donghanhprocessingbot

> **1 bot** (`@donghanhprocessingbot`) · **1 KH group + 1 Pro group per case** · **DM open to all staff**
> Group identity detected via `parse_group_title()` on the Telegram group name.

```
  ONE BOT — @donghanhprocessingbot
  ┌─────────────────────────────────────────────────────────────────────────────────────────────┐
  │  Per-case pair (one pair created per customer)            │  Any staff member               │
  │  ┌──────────────────────────────────────────────────┐     │  ┌───────────────────────────┐  │
  │  │  "DH KH - FARM 24m - Nguyen Van A"               │     │  │  Direct Message (DM)      │  │
  │  │   KH Group  (customer + staff)                   │     │  │  to @donghanhprocessingbot│  │
  │  │   → bot reads uploads, never replies here         │     │  │  → bot matches case by    │  │
  │  └──────────────────────────────────────────────────┘     │  │    applicant name token   │  │
  │  ┌──────────────────────────────────────────────────┐     │  └───────────────────────────┘  │
  │  │  "DH Pro - FARM 24m - Nguyen Van A"              │     │                                 │
  │  │   Pro Group  (internal staff only)               │     │                                 │
  │  │   → bot posts summaries + checklist here         │     │                                 │
  │  │   → staff @mention/reply → Q&A                   │     │                                 │
  │  │   → /oldfile · /check commands                   │     │                                 │
  │  └──────────────────────────────────────────────────┘     │                                 │
  └─────────────────────────────────────────────────────────────────────────────────────────────┘

╔═══════════════════════════════════════════════════════════════════════════════════════════════╗
║                                          INPUT                                                ║
║  ┌─────────────────────────┐   ┌──────────────────────────────┐   ┌─────────────────────┐   ║
║  │   KH Group (per case)   │   │   Pro Group (per case)        │   │   Staff DM           │   ║
║  │  "DH KH - FARM - Name"  │   │  "DH Pro - FARM - Name"       │   │  any staff member    │   ║
║  │                         │   │                               │   │  matched by case     │   ║
║  │  upload .zip            │   │  @mention / reply → Q&A       │   │  applicant name      │   ║
║  │  or loose files         │   │  /oldfile · /check            │   │                      │   ║
║  │  (bot reads only)       │   │  ← bot posts output here      │   │                      │   ║
║  └────────────┬────────────┘   └──────────────┬───────────────┘   └──────────┬───────────┘   ║
╚═══════════════╪════════════════════════════════╪══════════════════════════════╪═══════════════╝
                └────────────────────────────────┼──────────────────────────────┘
                                                 ▼
╔═══════════════════════════════════════════════════════════════════════════════════════════════╗
║                     TELEGRAM LISTENER  ·  telegram_listener.py                                ║
║                                                                                               ║
║  ┌──────────────────────────────┐     ┌──────────────────────────────────────────────────┐   ║
║  │  Debounce 20s                │────▶│  Dispatcher                                      │   ║
║  │  batch accumulation          │     │                                                  │   ║
║  │  (KH uploads only)           │     └────────┬──────────────────────┬──────────────────┘   ║
║  └──────────────────────────────┘              │                      │                       ║
╚═══════════════════════════════════════════════╪══════════════════════╪═══════════════════════╝
                                                │                      │
                           zip/files · /oldfile │           /check     │   mention / reply / DM
                                                │                      │        │
         ┌──────────────────────────────────────┘                      │        ▼
         │                                                             │  ┌─────────────────────────────────────┐
         │                                                             │  │  Q&A BRANCH  ·  chat.py             │
         │                                                             │  │                                     │
         │                                                             │  │  NEED_FILE                          │
         │                                                             │  │    re-OCR one Drive file in full    │
         │                                                             │  │                                     │
         │                                                             │  │  NEED_ADDR                          │
         │                                                             │  │    diadia.py → admin boundary lookup│
         │                                                             │  │                                     │
         │                                                             │  │  NEED_WEB                           │
         │                                                             │  │    web search for external info     │
         │                                                             │  │                                     │
         │                                                             │  │  NEED_RENAME                        │
         │                                                             │  │    rename Drive file + sidecars     │
         │                                                             │  │    (bot asks staff to confirm ok)   │
         │                                                             │  │                                     │
         │                                                             │  │  linkify_answer()                   │
         │                                                             │  │  → Telegram HTML reply              │
         ▼                                                             │  └─────────────────────────────────────┘
╔═══════════════════════════════════════════════════════════╗         │
║       SCAN PIPELINE  ·  scan_pipeline.py  (subprocess)    ║         │
║                                                           ║         │
║  ┌─────────────────────────────────────────────────────┐  ║         │
║  │  ① ENUMERATE                                         │  ║         │
║  │     all files from .zip / directory                  │  ║         │
║  └──────────────────────────┬──────────────────────────┘  ║         │
║                             ▼                              ║         │
║  ┌─────────────────────────────────────────────────────┐  ║         │
║  │  ② OCR LAYER  ·  gemini-2.5-flash  ·  5 workers     │  ║         │
║  │                                                     │  ║         │
║  │  ┌─────────────────────┐  ┌────────────────────────┐│  ║         │
║  │  │  SINGLE-PAGE        │  │  MULTI-PAGE PDF        ││  ║         │
║  │  │                     │  │  ─── 2-pass ───        ││  ║         │
║  │  │  OCR directly       │  │  Pass 1:               ││  ║         │
║  │  │  → JSON strict      │  │    rasterize each page ││  ║         │
║  │  │    schema           │  │    classify doc_type   ││  ║         │
║  │  │  → structured field │  │    group consecutive   ││  ║         │
║  │  │    extraction       │  │    → define segments   ││  ║         │
║  │  │                     │  │  Pass 2:               ││  ║         │
║  │  │                     │  │    split PDF/segment   ││  ║         │
║  │  │                     │  │    full OCR each       ││  ║         │
║  │  │                     │  │    status: uploaded-   ││  ║         │
║  │  │                     │  │           split        ││  ║         │
║  │  └─────────────────────┘  └────────────────────────┘│  ║         │
║  └──────────────────────────┬──────────────────────────┘  ║         │
║                             ▼                              ║         │
║  ┌─────────────────────────────────────────────────────┐  ║         │
║  │  ③ CLASSIFY & RENAME  ·  sop_naming.py              │  ║         │
║  │     match doc_types.yaml (32 types, 4 folders)       │  ║         │
║  │     SOP filename : LOAI-Ho Ten.ext                    │  ║         │
║  │     SHA-1 dedup  → skip if already uploaded           │  ║         │
║  └──────────────────────────┬──────────────────────────┘  ║         │
║                             ▼                              ║         │
║  ┌─────────────────────────────────────────────────────┐  ║         │
║  │  ④ DRIVE UPLOAD  ·  drive_helpers.py                │  ║         │
║  │     upload file → case folder on Shared Drive        │  ║         │
║  │     .json sidecar → _Bot OCR & Metadata/             │  ║         │
║  │     .md sidecar  → human-readable summary            │  ║         │
║  └──────────────────────────┬──────────────────────────┘  ║         │
║                             ▼                              ║         │
║  ┌─────────────────────────────────────────────────────┐  ║         │
║  │  ⑤ VISION COMPARE  ·  vision_check.py  (Mức 3)     │  ║         │
║  │     pairs: Anh thẻ × Passport / GPLX / CCCD (max 3) │  ║         │
║  │     SHA-1 cache → skip pairs already compared        │  ║         │
║  │     Gemini multi-image:                              │  ║         │
║  │       same_person · confidence                       │  ║         │
║  │       age_diff_months · phau_thuat_signs             │  ║         │
║  │     → stored as _vision_compare in profile           │  ║         │
║  └──────────────────────────┬──────────────────────────┘  ║         │
║                             │ ◀───────────────────────────────────────┘ (/check enters here)
║                             ▼                              ║
║  ┌─────────────────────────────────────────────────────┐  ║
║  │  ⑥ CHECKLIST / THẨM ĐỊNH  ·  checklist.py          │  ║
║  │                                                     │  ║
║  │  ┌───────────────────────────────────────────────┐  │  ║
║  │  │  rule_engine.py — 17 deterministic pre-checks │  │  ║
║  │  │  RUNS BEFORE LLM  (cannot hallucinate)        │  │  ║
║  │  │  · thế chấp          · hết hạn LLTP/IOM       │  │  ║
║  │  │  · NH cấm            · missing ward members   │  │  ║
║  │  │  · vision flags      · parent DOB mismatch    │  │  ║
║  │  └────────────────────────┬──────────────────────┘  │  ║
║  │                           ▼                          │  ║
║  │  ┌───────────────────────────────────────────────┐  │  ║
║  │  │  Stage 1 — cheap extract                      │  │  ║
║  │  │  all OCR sidecars → LLM summarise             │  │  ║
║  │  │  → condensed JSON per document                │  │  ║
║  │  └────────────────────────┬──────────────────────┘  │  ║
║  │                           ▼                          │  ║
║  │  ┌───────────────────────────────────────────────┐  │  ║
║  │  │  Stage 2 — LLM reasoning                      │  │  ║
║  │  │  condensed JSON + rule violations             │  │  ║
║  │  │  + _vision_compare + _dia_gioi ground-truth   │  │  ║
║  │  │  → 4-part report: reject / warn / info / ok   │  │  ║
║  │  └──────────────┬────────────────────┬────────────┘  │  ║
║  │                 ▼                    ▼                │  ║
║  │  ┌──────────────────────┐  ┌────────────────────────┐│  ║
║  │  │  FARM Coverage Tally │  │  Google Doc Report      ││  ║
║  │  │  X / 18 bắt buộc     │  │  Bao cao tham dinh - KH││  ║
║  │  │  missing by FARM code│  │  → written to Drive     ││  ║
║  │  └──────────────────────┘  └────────────────────────┘│  ║
║  └─────────────────────────────────────────────────────┘  ║
╚═══════════════════════════════════════════════════════════╝
                             ▼
╔═══════════════════════════════════════════════════════════╗
║  OUTPUT  →  Pro Group                                     ║
║  · summary with clickable Drive links per uploaded file   ║
║  · ✓ Da tham dinh ho so — [Google Doc link]              ║
╚═══════════════════════════════════════════════════════════╝
```

---

## Module Reference

| Module | Role |
|--------|------|
| `telegram_listener.py` | Entry point & dispatcher — debounce, /oldfile, /check, Q&A routing, Pro group posting |
| `scan_pipeline.py` | Pipeline orchestrator — enumerate, OCR threads, classify, rename, dedup, upload, vision, checklist |
| `lib/checklist.py` | AI thẩm định — 2-stage LLM, FARM coverage tally, Google Doc writer |
| `lib/chat.py` | Q&A visa officer — NEED_FILE / NEED_ADDR / NEED_WEB / NEED_RENAME, linkify_answer() |
| `lib/rule_engine.py` | Deterministic eval — 17 conditions via simpleeval, runs before LLM |
| `lib/vision_check.py` | Gemini multi-image — portrait compare, phẫu thuật signs, same_person, age_diff |
| `lib/sop_naming.py` | Doc-type classifier + SOP filename builder (`LOAI-Ho Ten.ext`) |
| `lib/rule_loader.py` | YAML loader + schema validator — 63 rules, 26 checklist, 32 doc-types, 8 relations |
| `lib/drive_helpers.py` | Drive API wrappers with in-process cache — asyncio only, not thread-safe |
| `lib/diadia.py` | Offline old↔new admin-boundary lookup — 10,358 rows, no HTTP |
| `lib/google_clients.py` | Drive + Sheets API client init |

---

## Key Design Decisions

- **Parallelism boundary**: OCR runs in a thread pool (5 workers); Drive ops are asyncio-only (httplib2 not thread-safe)
- **LLM ladder**: `gemini-2.5-flash` for OCR + page classify → `gemini-2.5-pro` for low-confidence escalation + vision compare
- **Determinism first**: `rule_engine.py` catches 17 classes of errors before any LLM call — thế chấp, hết hạn, NH cấm, vision flags
- **Idempotency**: SHA-1 dedup + destination-name check → reruns safe, won't double-upload
- **No data loss**: manifest covers all inputs; non-PDF/image files uploaded without OCR; exit non-zero on failure → caller retries
