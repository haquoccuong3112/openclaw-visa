# Product Requirements Document
# @donghanhprocessingbot — Đồng Hành ALLY FARM Visa Document Processing System

**Version:** 1.1  
**Last updated:** 2026-05-16  
**Owner:** Cường Hà Quốc  
**Status:** Production

---

## 1. Overview

### 1.1 Product Summary

`@donghanhprocessingbot` is a Telegram-native AI document processing system built for **Đồng Hành / ALLY**, a Vietnamese visa consulting company specialising in the **Canada FARM Agricultural Worker visa program**.

The bot automates the end-to-end intake, classification, validation, and cross-checking of visa application documents — transforming a process that previously took staff hours of manual work into a 5–10 minute unattended pipeline.

### 1.2 Problem Statement

The Canada FARM visa application requires each customer to provide 18–26 legal documents across categories: personal identity, assets, employment, insurance, and family records. Đồng Hành processes many cases simultaneously.

Before automation, staff had to:

| Manual step | Time cost | Error risk |
|-------------|-----------|-----------|
| Receive and organise files from Telegram/WhatsApp/email | 20–40 min/case | Files missed, duplicates |
| Read each document to identify its type | 30–60 min/case | Misclassification |
| Rename files to standard convention | 15–20 min/case | Inconsistent naming |
| Upload to correct Google Drive folder | 10–15 min/case | Wrong folder, overwrite |
| Check 26-item FARM checklist for completeness | 20–30 min/case | Items overlooked |
| Identify legal issues (expired docs, mortgaged land, banned cards) | 30–60 min/case | High-stakes misses |
| Write thẩm định (cross-check) report | 45–90 min/case | Incomplete, inconsistent |

**Total: 2.5–5 hours per case, fully manual, blocking staff capacity.**

### 1.3 Solution

A single Telegram bot that:
1. Accepts raw file uploads from customers (no formatting required)
2. OCRs and classifies every document automatically
3. Renames and uploads to Google Drive with the standard naming convention
4. Runs 17 deterministic rule checks and a 2-stage AI cross-check
5. Produces a formal thẩm định report as a Google Doc
6. Posts a structured summary to staff with clickable Drive links

Staff interaction required: **zero** for a clean case. Review only when the bot flags issues.

---

## 2. Users

### 2.1 Primary Users

| User | Role | Interaction |
|------|------|-------------|
| **Đồng Hành staff** | Case managers reviewing applications | Read bot output in Pro group; ask Q&A via @mention; run /oldfile and /check |
| **Customer (KH)** | Vietnamese farmer applying for FARM visa | Drops files into KH group Telegram chat — no other interaction required |

### 2.2 Secondary Users

| User | Role | Interaction |
|------|------|-------------|
| **Cường (owner/operator)** | System owner, deploys code changes | Operates via OpenClaw Pro Bot or SSH; monitors via journalctl |

### 2.3 User Personas

**The Customer (Khách Hàng)**  
A farmer in rural Vietnam, 35–55 years old. Minimal tech literacy. Has a smartphone. Documents are physical papers photographed on the kitchen table, or scanned at a local print shop. Sends files however is convenient — zip files, individual photos, mixed formats. Cannot be expected to name, sort, or organise files.

**The Staff Member**  
Works at the Đồng Hành office. Manages 10–30 active cases simultaneously. Needs to know at a glance: what arrived, what's missing, what's flagged. Asks follow-up questions about specific documents in Vietnamese. May handle multiple cases in a single DM session with the bot.

---

## 3. Telegram Channel Structure

One bot (`@donghanhprocessingbot`) operates across three channel types:

```
Per customer case:
  ┌─────────────────────────────────────────────┐
  │  KH Group  "DH KH - FARM 24m - Nguyen Van A" │  ← customer + staff
  │  Bot reads uploads silently. Never replies.   │
  └─────────────────────────────────────────────┘
  ┌─────────────────────────────────────────────┐
  │  Pro Group "DH Pro - FARM 24m - Nguyen Van A"│  ← internal staff only
  │  Bot posts output here.                      │
  │  Staff @mention bot for Q&A.                 │
  │  /oldfile · /check commands available.       │
  └─────────────────────────────────────────────┘

Across all cases:
  ┌─────────────────────────────────────────────┐
  │  Staff DM (any staff member)                 │
  │  Bot matches case by applicant name tokens.  │
  │  Multi-case sessions supported.              │
  └─────────────────────────────────────────────┘
```

Group identity is detected via `parse_group_title()` reading the Telegram group name. The bot distinguishes KH vs Pro by the presence of the word `Pro` (case-insensitive) in the group title.

---

## 4. Core Features

### 4.1 Document Intake & Debounce

- Accept `.zip` archives or loose files (PDF, JPG, PNG) in the KH group
- Debounce uploads: accumulate all files for 20 seconds after the last upload before triggering the pipeline (handles customers who send files one by one)
- Non-image/PDF files are uploaded to Drive without OCR (no silent drops)
- Runs pipeline as a subprocess — listener continues polling during processing

### 4.2 OCR & Field Extraction

- OCR engine: `gemini-2.5-flash` via OpenRouter, strict JSON schema response
- 5 parallel OCR workers (thread pool) for throughput
- Per-document output: `doc_type`, `extracted fields`, `summary`, `sha1`
- 3-tier fallback on OCR failure

**Multi-page PDF handling (2-pass):**
- Pass 1: Rasterize each page → classify page doc_type → group consecutive same-type pages into segments
- Pass 2: Split PDF at segment boundaries → full OCR each segment → separate output file per segment
- Status: `uploaded-split` on segmented outputs

### 4.3 Document Classification & SOP Renaming

- 32 document types across 4 folders: Personal Docs / Education / Asset / Employment
- Classification via pattern matching against `data/doc_types.yaml` + LLM confirmation
- SOP filename convention: `LOAI-Ho Ten.ext` (e.g. `CCCD-Nguyen Van A.pdf`)
- Relation tagging: detects whose document it is (bố/mẹ/vợ/con…) and appends to filename (e.g. `CCCD bo-Nguyen Van A.pdf`)
- Priority ordering: detailed entries matched before generic ones (e.g. `So dat NN` before `So dat`)

### 4.4 SHA-1 Deduplication

- Content hash computed for every file before upload
- Hash compared against existing `.json` sidecars on Drive
- Duplicate content → skip upload, status: `duplicate-by-hash`
- Idempotent: pipeline reruns are safe, no double-uploads

### 4.5 Google Drive Upload

- Upload to the case's folder on Google Shared Drive (resolved from `group_registry.json`)
- Write `.json` sidecar: extracted fields, doc_type, sha1, status, timestamps
- Write `.md` sidecar: human-readable summary
- Sidecars stored in `_Bot OCR & Metadata/` subfolder
- Drive client is asyncio-only (httplib2 not thread-safe); OCR offloaded via `asyncio.to_thread`
- In-process folder/list cache to avoid redundant API calls

### 4.6 Portrait Vision Compare (Mức 3)

- Runs after upload, before checklist
- Finds portrait pairs: Anh thẻ × Passport / GPLX / CCCD (up to 3 pairs, priority order)
- SHA-1 cache per pair — skips pairs already compared
- Gemini multi-image call: returns `same_person`, `confidence`, `age_diff_months`, `phau_thuat_signs`, `anomalies`
- Results stored as `_vision_compare` in profile → used as ground-truth by checklist Stage 2
- Cost: ~$0.015–0.03/case with Anh thẻ; $0 without
- Covers rules 1.2 (cosmetic surgery) and 8.3 (photo reuse)

### 4.7 Deterministic Rule Engine

`lib/rule_engine.py` runs **17 hard rules before any LLM call** using `simpleeval` on extracted fields. These cannot hallucinate.

| Category | Rules |
|----------|-------|
| Document expiry | LLTP > 6 months old, IOM > 12 months old, Passport < 2 years remaining |
| Asset flags | Land certificate mortgaged (`tinh_trang_the_chap`), supplementary page exists (`co_to_bo_sung`) |
| Employment | Agricultural land certificate issued < 1 year ago |
| Banking | Banned card issuers: Ever-link VCB, Agribank, MB Hybrid |
| Legal docs | HĐ cho/tặng/thừa kế missing notarisation (`co_cong_chung == false`) |
| Photo standards | Mặt mộc, no jewellery, no visible tattoos, dark hair, white background |
| Cross-document | Children missing from XNCT (CT07), parent DOB mismatch across documents |
| Passport | Issued outside Vietnam |
| CCCD | Missing fingerprint boxes |

### 4.8 AI Thẩm Định (2-Stage LLM Cross-Check)

**Stage 1 — cheap extract:**
- Input: all OCR `.json` sidecars for the case
- LLM: `CHECKLIST_EXTRACT_MODEL` (low-cost)
- Output: condensed JSON per document (key fields only)

**Stage 2 — reasoning:**
- Input: condensed JSON + deterministic rule violations + `_vision_compare` results + `_dia_gioi` address ground-truth
- LLM: `CHECKLIST_MODEL` (higher capability)
- Output: 4-part Markdown report — `reject` / `warn` / `info` / `ok`
- Fallback: `CHECKLIST_FALLBACK_MODEL` on error

**FARM Coverage Tally:**
- 26-item checklist evaluated against uploaded docs
- Counts X/18 bắt buộc (mandatory) items present
- Conditional items evaluated based on case profile (married → GKH required; has children → GKS con required)
- Missing items listed by FARM code

**Output:** Google Doc `"Bao cao tham dinh - <KH>"` written to case Drive folder.

### 4.9 Staff Q&A (chat.py)

Staff @mention or reply to the bot in Pro group, or DM directly. The bot answers from:
- All OCR sidecars for the case
- The thẩm định Google Doc
- FARM coverage state
- `_dia_gioi` block (pre-resolved addresses for the case)

Four one-shot mechanisms:
| Mechanism | Trigger | Action |
|-----------|---------|--------|
| `NEED_FILE` | Bot needs full text of a specific file | Re-OCR that Drive file in full |
| `NEED_ADDR` | Question about an address/location | `diadia.py` lookup (offline, no HTTP) |
| `NEED_WEB` | External information needed | Web search |
| `NEED_RENAME` | Bot proposes renaming a file | Renames Drive file + both sidecars; asks staff to confirm `ok`/`huỷ` first |

**Link intent detection:** Requests like "gửi link CCCD" or "dẫn URL hộ chiếu" are handled deterministically (no LLM) by `_try_link_intent()` — faster, cheaper, no compliance risk.

**DM case matching:** `pick_case_for_dm()` matches message tokens against all known applicant names (Vietnamese stopwords dropped). Single distinguishing token (e.g. `test8`) is enough. Bot never lists all cases — only lists colliding names when ambiguous.

### 4.10 /oldfile Command

For cases where documents already exist on Drive before the bot was registered:
1. Staff uploads files to `<case>/Old File/` on Drive manually
2. Staff types `/oldfile` in the Pro group
3. Bot scans that Drive subfolder → runs same pipeline
4. Original files moved to `Old File/_processed/` (original preserved, prevents reprocessing)
5. Per-case lock (`_OLDFILE_LOCKS`) prevents double-fire

### 4.11 /check Command

Re-runs thẩm định only — no file processing:
- Reads existing OCR sidecars from Drive
- Skips enumerate/OCR/upload/vision steps
- Useful after staff manually adds or renames a file

### 4.12 Vietnamese Administrative Boundary Resolution

Vietnam merged 63 provinces into 34 in 2025 (effective 2025-06-12 for provinces, 2025-07-01 for communes). A document from before the reform may name a province/commune that no longer exists.

`lib/diadia.py`:
- Offline lookup — no HTTP, loaded lazily, cached in memory
- 10,358-row ward-level old→new mapping (`data/admin/old_to_new_wards.json`)
- `resolve_address(text)` — normalises any address to current name
- `same_place(a, b)` — returns true if two names refer to the same unit
- `commune_merge_info(name)` — returns merge details

The resolved address block (`_dia_gioi`) is injected into the thẩm định Stage 2 prompt as ground-truth, so the LLM never flags old-name vs new-name of the same place as a contradiction.

---

## 5. Data Model

### 5.1 group_registry.json

Written by the bot at runtime. Maps Telegram chat ID to case metadata.

```json
{
  "-1001234567890": {
    "folder_id": "1abc...xyz",
    "applicant": "Nguyen Van A",
    "visa": "FARM 24m",
    "drive_link": "https://drive.google.com/...",
    "pro_chat_id": "-1009876543210"
  }
}
```

### 5.2 Document Sidecar (.json)

Stored at `_Bot OCR & Metadata/<filename>.json` in the Drive case folder.

```json
{
  "doc_type": "CCCD",
  "tag": "CCCD",
  "filename": "CCCD-Nguyen Van A.pdf",
  "sha1": "a3f2...",
  "status": "uploaded",
  "extracted": {
    "ho_ten": "Nguyen Van A",
    "ngay_sinh": "1985-03-12",
    "so_cccd": "079085...",
    "co_2_o_van_tay": true
  },
  "summary": "CCCD của Nguyen Van A, sinh 1985-03-12...",
  "uploaded_at": "2026-05-16T08:32:11Z"
}
```

### 5.3 data/rules.yaml

Schema version 1. Two sections:
- `checklist`: 26 FARM items with `severity` (`bat_buoc` / `ket_hon` / `co_con` / `tuy_chon` / `lam_sau`)
- `validations`: 63 rules with `code`, `severity`, `applies_to`, `condition`, `needs_llm`

### 5.4 data/doc_types.yaml

32 document types. Each entry: `tag`, `folder`, `description`, `patterns.doc_type`, `patterns.filename`, `in_checklist`.

---

## 6. Technical Architecture

### 6.1 Process Model

```
systemd (system)
  └── donghanhbot.service  [enabled, restart-on-failure]
        └── python3 telegram_listener.py   [always running, ~56 MB idle]
              └── python3 scan_pipeline.py  [subprocess per batch, exits on completion]
```

### 6.2 Transport

- **Long polling** (not webhooks) — bot polls Telegram API every ~10 seconds
- No inbound port required — works behind CGNAT
- All outbound HTTPS

### 6.3 External Dependencies

| Service | Purpose |
|---------|---------|
| Telegram Bot API | Message transport |
| Google Drive API | File storage, case folder management |
| Google Sheets API | (master case registry) |
| Google Document AI | OCR processing |
| OpenRouter | LLM gateway (Gemini, DeepSeek) |
| Gemini (via OpenRouter) | OCR, classification, checklist, vision compare |

### 6.4 Config

| File | Content | Git-tracked |
|------|---------|-------------|
| `scan-ocr.env` | All API keys, model IDs, bot token | No |
| `google-service-account.json` | GCP service account credentials | No |
| `group_registry.json` | Runtime case registry | No |
| `data/rules.yaml` | 63 validation rules | Yes |
| `data/doc_types.yaml` | 32 document types | Yes |
| `data/relations.yaml` | 8 relation types | Yes |
| `data/admin/` | 10,358-row admin boundary tables | Yes |

### 6.5 Model Ladder

| Stage | Model env var | Default | Notes |
|-------|--------------|---------|-------|
| OCR | `GEMINI_MODEL` | `gemini-2.5-flash` | Strict JSON schema |
| Page classify | `PAGE_CLASSIFY_MODEL` | `gemini-2.5-flash` | Pass 1 only |
| Low-conf escalation | — | `gemini-2.5-pro` | 1 call for Khác class |
| Checklist extract | `CHECKLIST_EXTRACT_MODEL` | low-cost model | Stage 1 |
| Checklist reason | `CHECKLIST_MODEL` | capable model | Stage 2 |
| Checklist fallback | `CHECKLIST_FALLBACK_MODEL` | fallback | On error |
| Vision compare | — | `gemini-2.5-pro` | Multi-image |
| Chat Q&A | — | via OpenRouter | Configurable |

---

## 7. Non-Functional Requirements

### 7.1 Reliability
- Bot must restart automatically on crash (`Restart=on-failure`, `RestartSec=10`)
- Pipeline reruns must be idempotent (SHA-1 dedup, destination-name check)
- Non-zero exit on pipeline failure → caller must retry
- Manifest covers all inputs — no silent file drops

### 7.2 Performance
- OCR: 5 parallel workers; target < 2 min for a 30-file batch
- Drive ops: asyncio event loop only (httplib2 not thread-safe)
- Vision compare: SHA-1 cached — each pair compared once per run
- Checklist: 2-stage to minimise expensive LLM calls

### 7.3 Cost Control
- `gemini-2.5-flash` for all bulk OCR (cheap, fast)
- `gemini-2.5-pro` only for: low-confidence docs, vision compare
- Vision compare: ~$0.015–0.03/case; $0 for cases without Anh thẻ
- Link intent resolved deterministically — no LLM token spend

### 7.4 Correctness
- Deterministic rules (17) run before LLM — cannot hallucinate
- Address contradictions suppressed via `_dia_gioi` ground-truth
- Vision compare results are ground-truth for rules 1.2, 8.3 — LLM cannot override

### 7.5 Security
- Bot token and API keys in `scan-ocr.env` (gitignored, mode 600)
- Service account credentials gitignored, mode 600
- group_registry.json gitignored — never committed
- All HTML output built by code with `html.escape` — LLM never emits raw HTML

### 7.6 Observability
- All output to `journalctl -u donghanhbot`
- Structured logging with timestamps and log levels
- Manifest written per pipeline run covering all input files and their status

---

## 8. Constraints & Known Limitations

| Constraint | Detail |
|------------|--------|
| Telegram Bot API | No message history access — bot only sees messages as they arrive |
| Drive client thread safety | `drive_helpers.py` must run on asyncio event loop, never in a thread |
| Synology Docker (NAS) | Not applicable — bot runs on LXC claude-dev (172.16.1.186), not NAS |
| Vietnamese OCR accuracy | Gemini handles Vietnamese well; handwritten documents may have lower accuracy |
| Vision compare cost | Disabled for cases without Anh thẻ — no baseline photo, no comparison |
| Admin boundary data | Ward-level data sourced from VietMap; use offline, do not modify and republish |

---

## 9. Out of Scope

- Submission to the Canadian consulate — the bot processes and validates documents only
- Customer communication — the bot never replies in KH groups
- Document translation — all output is in Vietnamese
- Mobile app or web interface — Telegram is the only interface
- Multi-language support — Vietnamese only

---

## 10. Success Metrics

| Metric | Target |
|--------|--------|
| Pipeline completion time | < 10 min for a 30-file batch |
| Staff time per case (document intake) | < 5 min (review only) |
| False negative rate on deterministic rules | 0% (hard rules cannot hallucinate) |
| Duplicate upload rate | 0% (SHA-1 dedup) |
| Bot uptime | > 99% (systemd auto-restart) |
| Drive naming convention compliance | 100% of uploaded files |

---

## 11. Related Documents

- [`docs/pipeline-diagram.md`](pipeline-diagram.md) — ASCII architecture diagram + module reference
- [`docs/data-config.md`](data-config.md) — rules.yaml, doc_types.yaml, relations.yaml, provinces_34.json reference
- [`docs/openclaw-setup.md`](openclaw-setup.md) — system setup, gateway, commands
- `workspace/skills/scan-ho-so-pipeline/SKILL.md` — operator procedure for running the pipeline manually
- `scan-ho-so/README.md` — module map and code guide
