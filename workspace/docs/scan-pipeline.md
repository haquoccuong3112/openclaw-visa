# scan_pipeline.py — Reference

`scan_pipeline.py` is the document pipeline orchestrator for `@donghanhprocessingbot`. It enumerates files from a `.zip` or directory, OCRs and classifies each document, renames to the SOP convention, uploads to Google Drive, runs face-comparison, and generates an AI thẩm định report. It is designed to be **idempotent** (reruns skip already-uploaded content) and **exit non-zero on any failure** so callers can retry.

---

## Environment Variables

Loaded from `scan-ocr.env` in the workspace parent directory (path searched via `SCAN_OCR_ENV` → sibling `scan-ocr.env` → `~/scan-ocr.env`).

| Variable | Default | Purpose |
|----------|---------|---------|
| `SHARED_DRIVE_ID` | `0AIYOQpLqtMPvUk9PVA` | Google Shared Drive root |
| `OPENROUTER_API_KEY` | — | OpenRouter API key (Gemini / DeepSeek calls) |
| `DEEPSEEK_API_KEY` | — | DeepSeek direct API key |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Path to service account JSON |
| `GOOGLE_DOCUMENTAI_PROCESSOR_ID` | — | Document AI processor ID (enables DocAI flow) |
| `GEMINI_MODEL` | `google/gemini-2.5-flash` | Model for full-file OCR (single-doc) |
| `PAGE_CLASSIFY_MODEL` | `google/gemini-2.5-flash` | Model for Pass 1 page-by-page classification |
| `PAGE_CLASSIFY_PRO_MODEL` | `google/gemini-2.5-pro` | Escalation model for uncertain Pass 1 pages |
| `PAGE_CLASSIFY_PRO_RATIO` | `0.20` | If ≥20% pages are low-conf/Khac → escalate all to Pro |
| `PAGE_CLASSIFY_FORCE_PRO_MIN_PAGES` | `10` | PDFs with ≥N pages use Pro directly in Pass 1 |
| `CLASSIFY_MODEL` | — | Text-only re-classify after OCR (e.g. DeepSeek flash) |
| `DOCAI_PLAN_MODEL` | `deepseek/deepseek-v4-pro` | Model for planning PDF splits via DocAI |
| `DOCAI_BATCH_PLAN` | `1` | Enable batch-planning multiple PDFs in 1 call |
| `DOCAI_BATCH_PLAN_MAX_FILES` | `12` | Max PDFs per batch plan call |
| `DOCAI_BATCH_PLAN_MAX_CHARS` | `24000` | Max total OCR text per batch plan |
| `SCAN_OCR_WORKERS` | `5` | Parallel Gemini OCR thread count |
| `SCAN_DPI` | `150` | DPI when rasterizing PDF pages to JPEG (Pass 1) |
| `SCAN_HO_SO_DIR` | script's directory | Root of the scan-ho-so app |

---

## CLI Flags

```
python3 scan_pipeline.py [input] [options]
```

| Flag | Description |
|------|-------------|
| `input` | `.zip` file or directory to process (optional with `--checklist-only`) |
| `--case-folder-id ID` | Google Drive folder ID for the case |
| `--applicant "Name"` | Applicant name (fallback subject when Gemini can't extract) |
| `--case-id STR` | Case ID string for metadata (auto-generated if omitted) |
| `--from-registry CHAT_ID` | Resolve case from `group_registry.json` by Telegram chat ID |
| `--manifest PATH` | Where to write `manifest.json` |
| `--retries N` | Per-file retry count (default 3, exponential backoff) |
| `--dry-run` | Enumerate & classify by filename only — no API calls, no Drive writes |
| `--self-test` | Run SOP naming validation suite; exit 0/1 |
| `--no-checklist` | Skip AI checklist after upload |
| `--checklist-only` | Skip OCR/upload; only run checklist on existing case |
| `--force-rescan` | Bypass SHA-1 dedup; re-OCR all files (used by `/oldfile`) |
| `--sweep-meta` | Move stray `.md`/`.json` from the 4 main folders → `_Bot OCR & Metadata` |
| `--no-docai` | Skip Document AI flow; fall back to Gemini page-classify |

**Exit codes:** `0` = success · `1` = some files failed · `2` = silent drop or checklist failure

---

## Pipeline Steps

### Step 1 — Enumerate

**Functions:** `collect_from_zip()` · `collect_from_dir()`  
**Libraries:** `zipfile`, `pathlib`, `shutil`

- If `.zip` → extract to temp dir, number files sequentially (`001_name`, `002_name`, …)
- If directory → recursively list all real files
- Skip macOS junk: `__MACOSX/`, `._*`, `.DS_Store`
- Output: `list[(Path, basename)]` covering every real input file

---

### Step 2 — Sweep Stray Sidecars *(optional, `--sweep-meta`)*

**Function:** `sweep_stray_sidecars(case_folder_id)`  
**Library:** `lib.drive_helpers` (Google Drive API)

Scans the 4 top-level case folders (`Personal Docs`, `Education`, `Asset`, `Employment`) for stray `.md` / `.json` files and moves them into `_Bot OCR & Metadata`. Adds suffix `_dup<ts>` on collision. Idempotent.

---

### Step 3 — Parallel OCR Prefetch

**Libraries:** `httpx`, `base64`, `concurrent.futures`, `lib.docai_client`, `lib.drive_helpers`

Two parallel streams run concurrently via `ThreadPoolExecutor(2)`:

#### 3A — Gemini OCR (non-PDF / images)
**Function:** `ocr_prefetch()` → `gemini_classify_file()`

- Sends each file as base64 to Gemini via OpenRouter  
- Requests structured JSON: `{doc_type, person[], summary_vi, key_fields, extracted}`  
- 3-tier response_format fallback: `json_schema` → `json_object` → no format  
- Internal retry: 2 attempts with exponential backoff (max 15s sleep)  
- Output: `ocr_cache = {src_name: gem_dict}`

#### 3B — Document AI OCR (PDFs, if `GOOGLE_DOCUMENTAI_PROCESSOR_ID` set)
**Function:** `_docai_ocr_pdfs()`  → `lib.docai_client.ocr_pdf_with_docai()`

- Sends each PDF to Google Document AI  
- Returns per-page text: `[{page: int, text: str}]`  
- Output: `_docai_ocr_items = [{file_id, src_name, pages_text}]`

---

### Step 3B — Batch Classify & Plan *(after prefetch)*

#### DeepSeek Batch Classify *(if `CLASSIFY_MODEL` set)*
**Function:** `_deepseek_batch_classify()`  
**Library:** `httpx`, `lib.rule_loader`

- Sends all OCR results (text only) to DeepSeek in one call  
- Returns `{src_name: {tag, subject, folder, relation}}`  
- Used as a hint in `process_one()` to override or confirm Gemini classification  
- Falls through to regex `classify_doc_type()` on error

#### DeepSeek Batch Plan PDFs *(if `DOCAI_BATCH_PLAN` + ≥2 PDFs)*
**Functions:** `_make_docai_batch_chunks()` · `_docai_batch_plan_pdfs()` · `_build_docai_batch_plan_prompt()`  
**Library:** `httpx`, `lib.rule_loader`

- Groups PDFs into chunks (max `DOCAI_BATCH_PLAN_MAX_FILES` files, `DOCAI_BATCH_PLAN_MAX_CHARS` chars)  
- Sends each chunk to DeepSeek with existing-docs context + doc-type catalog  
- Returns `{src_name: {documents: [...], _pages_text: [...]}}`  
- Output cached in `docai_batch_plans` — consumed in Step 4

---

### Step 4 — Process Each File

Each file goes through one of two flows:

#### Flow A — DocAI PDF Flow *(if `GOOGLE_DOCUMENTAI_PROCESSOR_ID` set and file is PDF)*

```
4A-1  Retrieve plan from docai_batch_plans cache
      or call _docai_plan_pdf() → OCR + DeepSeek single-PDF plan
      Libraries: lib.docai_client, httpx

4A-2  Validate plan → _validate_plan()
      Sanitizes tags, page ranges, relations against doc_types.yaml + relations.yaml
      Libraries: lib.sop_naming, lib.rule_loader

4A-3  Execute plan → _execute_plan()
      For each document in plan:
        _split_pdf_pages()          → temp PDF   [pypdf]
        gemini_classify_file()      → OCR result (if needs_vision=true) [httpx]
        dedup_name()                → collision-safe filename
        _find_sidecar_by_hash()     → skip if already uploaded [hashlib, lib.drive_helpers]
        upload_file()               → Drive [lib.drive_helpers]
        write .json + .md sidecars  → _Bot OCR & Metadata/ [lib.drive_helpers]
```

#### Flow B — Gemini Flow *(fallback / images / non-PDF)*

**Multi-page PDF detection** (PDF only, not `--dry-run`):

```
detect_pdf_segments(path)
  1. _count_pdf_pages()                 [pypdf]
  2. _rasterize_page_to_jpg_b64()       [pypdfium2, PIL] × all pages at SCAN_DPI
  3. _gemini_quick_classify_page()      [httpx] × each page
     → {doc_type, ten_chu_the, confidence}
  4. P1.1 escalation: if ≥20% low-conf/Khac → re-classify those pages via Pro model
  5. _group_consecutive()               → group pages by (doc_type, person)
     → _names_clearly_differ()          → split on person-name boundary
  Returns: list[{tu_trang, den_trang, doc_type, ten_chu_the}]
  If 1 segment → [] (use single-doc path)
```

If segments ≥2: for each segment:
- `_split_pdf_pages(path, tu_trang, den_trang)` → temp PDF `[pypdf]`
- `process_one(seg_path, …)` (see below)
- Item tagged with `split_from`, `split_pages`, `status: uploaded-split`

**`process_one()` — single file processing:**

```
1. Hash dedup
   SHA-1 of file bytes [hashlib]
   _find_sidecar_by_hash() checks _HASH_CACHE [lib.drive_helpers]
   → If match and no naming drift and not was-Khac → status: duplicate-by-hash, return

2. OCR
   Use prefetched_gem if available
   Else: gemini_classify_file() [httpx, base64]
   
3. Classify
   Use ds_hint (DeepSeek) if tag is valid
   Else: classify_doc_type() regex [lib.sop_naming, lib.rule_loader]
   Escalate: if tag == "Khac" (attempt 1 only) → re-call with Pro model [httpx]

4. Extract
   subject_from_gemini() → person name
   extract_relation() → relationship to applicant [lib.sop_naming]
   detect_english() → is English-language doc [lib.sop_naming]

5. Build filename
   build_filename(tag, subject_title, ext, is_english, relation) [lib.sop_naming]
   dedup_name() → append " 2" / " 3" on collision

6. Check destination name dedup [lib.drive_helpers]
   If same filename already in Drive folder → status: duplicate, return

7. Upload [lib.drive_helpers]
   upload_file() → case Drive folder (or _Bot OCR & Metadata for unclassified)
   status: uploaded / uploaded-no-ocr

8. Write sidecars [lib.drive_helpers]
   .json → _Bot OCR & Metadata/<new_name>.json
     Fields: content_hash, new_name, tag, folder, subject, relation,
             confidence, needs_review, is_english, ocr, summary,
             extracted, drive_link, case_id, source
   .md  → _Bot OCR & Metadata/<new_name>.md
     Human-readable summary of OCR results

9. Retry on error: exponential backoff 2^attempt (max 30s), up to --retries times
```

---

### Step 5 — Reconciliation & Manifest

- Count outcomes: `uploaded` / `uploaded-no-ocr` / `uploaded-split` / `duplicate` / `duplicate-by-hash` / `failed` / `dry-run`
- Detect **silent drops**: input files not found in any item → exit code 2
- Build `manifest.json` (see [Manifest Structure](#manifest-structure) below)

---

### Step 6 — Vision Compare *(skipped if `--no-checklist`)*

**Functions:** `lib.vision_check.find_compare_pairs()` · `evaluate_pairs()`  
**Library:** Gemini multi-image via OpenRouter (`httpx`)

- Finds pairs: `Ảnh thẻ` × `Passport` / `GPLX` / `CCCD` (max 3 pairs)
- SHA-1 cache: skips pairs already compared
- Gemini multi-image: `{same_person, confidence, age_diff_months, phau_thuat_signs, anomalies}`
- Results stored as `_vision_compare` in manifest — used as ground-truth in checklist Stage 2

---

### Step 7 — Scan Đã Duyệt Folder *(skipped if `--no-checklist`)*

**Function:** `scan_da_duyet_folder(case_folder_id, applicant, case_id)`  
**Libraries:** `lib.drive_helpers`, `httpx` (Gemini OCR)

- OCRs files that staff placed in the "Đã duyệt" (reviewed) folder
- Writes sidecars prefixed `da-duyet - ` to `_Bot OCR & Metadata`
- Idempotent: skips files already sidecar'd
- Bot never writes files into "Đã duyệt" itself

---

### Step 8 — AI Checklist / Thẩm Định *(skipped if `--no-checklist`)*

**Function:** `lib.checklist.run_and_write()`  
**Libraries:** `lib.rule_loader`, `lib.rule_engine`, `lib.drive_helpers`, `httpx` (LLM via OpenRouter)

Two-stage LLM pipeline over all OCR sidecars:

| Stage | Model (`env var`) | Input | Output |
|-------|-------------------|-------|--------|
| Stage 1 — cheap extract | `CHECKLIST_EXTRACT_MODEL` (flash) | All `.json` sidecars | Condensed JSON profile |
| Stage 2 — LLM reasoning | `CHECKLIST_MODEL` (pro) | Profile + rule violations + `_vision_compare` + `_dia_gioi` | 4-part Markdown report |

Before Stage 2: `lib.rule_engine.detect_deterministic_errors()` runs 17 deterministic checks (thế chấp, hết hạn LLTP, NH cấm, vision flags, …) — results injected into Stage 2 prompt as `⚠️ LỖI BOT ĐÃ PHÁT HIỆN`.

Outputs:
- FARM coverage tally: X/18 bắt buộc, list of missing items
- Google Doc `"Bao cao tham dinh - <KH>"` written to case Drive folder
- `summarize_for_telegram()` → short `✅ Đã thẩm định hồ sơ — <link>` line for Pro group

---

### Step 9 — Write Manifest & Output

- Write `manifest.json` (JSON, UTF-8)
- Print human-readable summary to stdout (counts, failed files, dropped files)
- Clean up temp extraction directory
- Return exit code: `0` / `1` / `2`

---

## Manifest Structure

```json
{
  "input": "/tmp/abc/files",
  "case_folder_id": "1xABC...",
  "case_id": "nguyen-van-a-2026",
  "applicant": "Nguyen Van A",
  "generated_at": "2026-05-16T10:00:00",
  "total_input_files": 12,
  "total_output_items": 14,
  "unique_sources_covered": 12,
  "counts": {
    "uploaded": 10,
    "uploaded-no-ocr": 1,
    "uploaded-split": 2,
    "duplicate": 0,
    "duplicate-by-hash": 1,
    "failed": 0,
    "dry-run": 0
  },
  "dropped_files": [],
  "ok": true,
  "items": [
    {
      "src_name": "001_CCCD.jpg",
      "new_name": "CCCD-Nguyen Van A.jpg",
      "ext": ".jpg",
      "tag": "CCCD",
      "folder": "Personal Docs",
      "subject": "Nguyen Van A",
      "relation": "applicant",
      "confidence": "high",
      "needs_review": false,
      "is_english": false,
      "ocr": true,
      "summary": "CCCD số 012345678901, ngày cấp 01/01/2020...",
      "extracted": { "so_cccd": "012345678901", "ngay_cap": "01/01/2020" },
      "content_hash": "a1b2c3...",
      "drive_link": "https://drive.google.com/file/d/...",
      "case_id": "nguyen-van-a-2026",
      "status": "uploaded"
    }
  ],
  "vision_compare": [...],
  "checklist": {
    "ran": true,
    "coverage": { "CCCD": true, "Ho_chieu": false, ... },
    "report_link": "https://docs.google.com/document/d/..."
  }
}
```

---

## Multi-Page PDF 2-Pass Detail

### Pass 1 — Page-by-Page Classification

```
detect_pdf_segments(path: Path) → list[segment] | []

1. _count_pdf_pages(path)                    [pypdf]
   → total_pages: int

2. Choose model:
   - If total_pages ≥ PAGE_CLASSIFY_FORCE_PRO_MIN_PAGES (10) → use Pro directly
   - Else → use Flash

3. For each page (parallel):
   _rasterize_page_to_jpg_b64(path, page_idx, dpi=SCAN_DPI)  [pypdfium2, PIL]
   → base64 JPEG string

4. For each page:
   _gemini_quick_classify_page(img_b64, page_no, model)        [httpx]
   → {doc_type, ten_chu_the, confidence: high|medium|low}
   Fallback on error → {doc_type: "Khac", confidence: "low"}

5. P1.1 Escalation:
   Count low-conf or "Khac" pages
   If count / total ≥ PAGE_CLASSIFY_PRO_RATIO (0.20):
     Re-classify those pages with Pro model
     Accept Pro result only if confidence ≥ medium AND doc_type ≠ "Khac"

6. _group_consecutive(pages_class)
   Split on: doc_type change OR _names_clearly_differ(a, b)
     _names_clearly_differ: True if different first name OR different last name ≥3 chars
   → list[{tu_trang, den_trang, doc_type, ten_chu_the}]

7. If only 1 segment → return [] (single-doc path, no split needed)
```

### Pass 2 — Split & OCR Per Segment

```
For each segment {tu_trang, den_trang, doc_type, ten_chu_the}:
  _split_pdf_pages(path, tu_trang, den_trang)   [pypdf]
  → temp PDF with pages tu_trang..den_trang (1-based, inclusive)
  
  process_one(seg_path, seg_src, ...)
  → Full Gemini OCR + classify + upload + sidecars
  → item with status: uploaded-split
  
  Delete temp PDF
```

---

## SHA-1 Dedup

**Computed:** in `process_one()` — `hashlib.sha1(path.read_bytes()).hexdigest()`  
**Skipped if:** `--dry-run` or `--force-rescan`

**Compared against:** `_find_sidecar_by_hash(case_folder_id, content_hash)`
- First call per case: lists all `.json` sidecars from `_Bot OCR & Metadata` via Drive API; caches in `_HASH_CACHE`
- Returns matching sidecar dict if `content_hash` field matches

**Decision logic:**
1. If sidecar found → check for naming drift (old tag → rebuilt filename ≠ stored name)
2. If drift or old tag was "Khac" → re-process (reclassify + re-upload with new name)
3. Else → `status: duplicate-by-hash`, skip immediately

This makes reruns idempotent: same file content → same Drive file, no double-upload.

---

## Error Handling & Retries

| Layer | Mechanism | Detail |
|-------|-----------|--------|
| `process_one()` main loop | `--retries` (default 3) | Exponential backoff: `sleep(min(2^attempt, 30))` |
| `_ocr_one_with_retry()` | 2 internal retries | Backoff max 15s; `None` on exhaustion |
| `gemini_classify_file()` | 3-tier format fallback | `json_schema` → `json_object` → no format |
| `process_one()` Khac escalation | 1 Pro escalation per file | Only on attempt 1; skipped on retry |
| `_deepseek_batch_classify()` | Returns `{}` on error | Falls through to regex `classify_doc_type()` |
| Sidecar write | `item["sidecar_error"]` | File still uploaded; sidecar failure non-fatal |
| Vision / checklist | `try/except` logged | Non-fatal; manifest still written |

---

## Module & Library Reference

| Module / Library | Source | Used For |
|------------------|--------|----------|
| `lib.drive_helpers` | local | All Google Drive API calls (upload, list, download, rename, move, delete, sidecar write) |
| `lib.sop_naming` | local | `classify_doc_type()`, `build_filename()`, `extract_relation()`, `detect_english()`, `_names_clearly_differ()` |
| `lib.rule_loader` | local | Load `doc_types.yaml`, `rules.yaml`, `relations.yaml`; generate doc-type catalog for prompts |
| `lib.rule_engine` | local | Deterministic pre-checks before checklist LLM |
| `lib.checklist` | local | 2-stage AI thẩm định + Google Doc writer |
| `lib.vision_check` | local | Gemini multi-image portrait comparison |
| `lib.docai_client` | local | Google Document AI OCR wrapper |
| `lib.diadia` | local | Offline Vietnamese admin-boundary lookup (used in checklist) |
| `httpx` | pip | All HTTP API calls (OpenRouter/Gemini, DeepSeek) |
| `pypdfium2` | pip | Rasterize PDF pages → JPEG (Pass 1) |
| `pypdf` | pip | Count PDF pages, split pages into temp files (Pass 2) |
| `PIL` (Pillow) | pip | JPEG encode after pypdfium2 rasterize |
| `concurrent.futures` | stdlib | `ThreadPoolExecutor` for parallel OCR prefetch (5 workers) |
| `hashlib` | stdlib | SHA-1 content hash for dedup |
| `zipfile` | stdlib | Extract `.zip` input |
| `base64` | stdlib | Encode files for Gemini multimodal API |
| `tempfile` | stdlib | Temp dir for zip extraction and PDF segments |
| `argparse` | stdlib | CLI flags |

---

## Self-Test (`--self-test`)

Runs without Drive or API calls (except network-free library checks):

1. **SOP naming** — `classify_doc_type()` regression cases (CV vs CCCD, Ảnh thẻ vs Ảnh gia đình, …)
2. **OCR prefetch** — `ocr_prefetch()` with empty / non-OCR input returns `{}` without API calls
3. **Model sanity** — `GEMINI_MODEL` must be flash or pro; strict schema flag present
4. **Hash dedup** — `_find_sidecar_by_hash()` returns `None` for unknown case/hash
5. **Multi-person split** — `_group_consecutive()` and `_names_clearly_differ()` unit tests
6. **PDF operations** — create 4-page PDF in memory; split pages 2–3; rasterize page 0 to JPEG base64
7. **Checklist module** — import + verify constants + `should_run_checklist()` + coverage computation

Exit `0` if all pass, `1` if any fail.
