# scan_pipeline.py — Reference

`scan_pipeline.py` is the document pipeline orchestrator for `@donghanhprocessingbot`. It enumerates files from a `.zip` or directory, OCRs + classifies each document in parallel, renames to the SOP convention, uploads to Google Drive, runs face-comparison, and generates an AI thẩm định report. Designed to be **idempotent** (reruns skip already-uploaded content) and **exit non-zero on failure** so callers can retry.

---

## Environment Variables

Loaded from `scan-ocr.env` in the workspace parent directory.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SHARED_DRIVE_ID` | `0AIYOQpLqtMPvUk9PVA` | Google Shared Drive root |
| `OPENAI_API_KEY` | — | OpenAI API key (GPT vision classify + checklist) |
| `OPENROUTER_API_KEY` | — | OpenRouter fallback (checklist fallback) |
| `AWS_ACCESS_KEY_ID` | — | AWS credentials for Rekognition face compare |
| `AWS_SECRET_ACCESS_KEY` | — | AWS credentials for Rekognition face compare |
| `AWS_REGION` | `us-east-1` | AWS region for Rekognition |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Path to service account JSON |
| `GOOGLE_DOCUMENTAI_PROCESSOR_ID` | — | Document AI processor ID |
| `GOOGLE_DOCUMENTAI_PROJECT_ID` | — | Document AI GCP project |
| `GOOGLE_DOCUMENTAI_LOCATION` | `us` | Document AI region |
| `OCR_CLASSIFY_MODEL` | `gpt-5-mini` | Model for per-doc vision classify (Phase 2) |
| `CHECKLIST_MODEL` | `gpt-5-mini` | Model for thẩm định stage-2 reasoning |
| `CHECKLIST_EXTRACT_MODEL` | `gpt-5-mini` | Model for thẩm định stage-1 extract (`/check` re-runs only) |
| `CHECKLIST_FALLBACK_MODEL` | — | Fallback model if CHECKLIST_MODEL fails |
| `SCAN_OCR_WORKERS` | `5` | Thread count for Phase 1 (DocAI) and Phase 2 (GPT vision) |
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
| `--applicant "Name"` | Applicant name (fallback subject when GPT can't extract) |
| `--case-id STR` | Case ID string for metadata (auto-generated if omitted) |
| `--from-registry CHAT_ID` | Resolve case from `group_registry.json` by Telegram chat ID |
| `--manifest PATH` | Where to write `manifest.json` |
| `--retries N` | Per-file retry count (default 3, exponential backoff) |
| `--dry-run` | Enumerate & classify by filename only — no API calls, no Drive writes |
| `--self-test` | Run SOP naming validation suite; exit 0/1 |
| `--no-checklist` | Skip AI checklist after upload |
| `--checklist-only` | Skip OCR/upload; only run checklist on existing Drive sidecars |
| `--force-rescan` | Bypass SHA-1 dedup; re-OCR all files (used by `/oldfile`) |
| `--sweep-meta` | Move stray `.md`/`.json` from the 4 main folders → `_Bot OCR & Metadata` |

**Exit codes:** `0` = success · `1` = some files failed · `2` = silent drop or checklist failure

---

## Pipeline Steps

### Step 1 — Enumerate

**Functions:** `collect_from_zip()` · `collect_from_dir()`

- If `.zip` → extract to temp dir, preserve original filenames
- If directory → recursively list all real files
- Skip macOS junk: `__MACOSX/`, `._*`, `.DS_Store`
- Output: `list[(Path, basename)]` covering every real input file

---

### Step 2 — Sweep Stray Sidecars *(optional, `--sweep-meta`)*

**Function:** `sweep_stray_sidecars(case_folder_id)`

Scans the 4 top-level case folders (`Personal Docs`, `Education`, `Asset`, `Employment`) for stray `.md` / `.json` files and moves them into `_Bot OCR & Metadata`. Idempotent.

---

### Step 3 — Phase 1: DocAI OCR (parallel)

**Function:** `docai_prefetch(files, dry_run, workers)`  
**Library:** `lib.docai_client`, `concurrent.futures.ThreadPoolExecutor`

- Sends every OCR-able file (`DOCAI_OCR_EXTS`: PDF, JPEG, PNG, TIFF, BMP, WebP, GIF, DOC, DOCX) to Google Document AI in parallel (`SCAN_OCR_WORKERS` threads, default 5)
- DOC/DOCX: converted to PDF via LibreOffice before sending
- PDF > 15 pages: chunked into ≤15-page slices, results merged
- Returns: `ocr_cache = {src_name: [{page: int, text: str}]}`
- Non-OCR files and `--dry-run`: skipped (not added to cache)

---

### Step 4 — Phase 2: GPT Vision Classify (parallel)

**Function:** `vision_prefetch(files, ocr_cache, applicant, dry_run, workers)`  
**Library:** `httpx`, `pypdfium2`, `concurrent.futures.ThreadPoolExecutor`

Runs concurrently with the same `SCAN_OCR_WORKERS` thread count, using DocAI output from Step 3:

For each file in parallel:
1. `_rasterize_first_page(path)` → JPEG bytes
   - PDF: pypdfium2 at 150 DPI, page 0
   - Image (JPEG/PNG/etc.): read bytes directly
   - DOCX (pre-converted to PDF): same as PDF
   - Returns `None` on any error — pipeline continues without image
2. `docai_classify_vision(pages_text, filename, applicant, image_b64)` → one `gpt-5-mini` call
   - Multimodal message: `[{type: image_url, ...}, {type: text, "OCR text + filename"}]` if image available; else text-only
   - `response_format`: strict JSON schema `DOC_RESULT_SCHEMA`
   - Returns fallback `{tag: "Khac", md_content: ""}` on API error

**`DOC_RESULT_SCHEMA` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `tag` | string | Doc type tag (matches `data/doc_types.yaml`) |
| `folder` | enum | `Personal Docs` / `Education` / `Asset` / `Employment` |
| `filename` | string | Suggested filename |
| `subject` | string | Person name on the document |
| `relation` | enum | `applicant`/`cha`/`me`/`vo`/`chong`/`con`/`anh_chi_em`/`khac`/`""` |
| `confidence` | enum | `high` / `medium` / `low` |
| `needs_vision` | boolean | Whether face-compare is needed |
| `person[]` | array | `{full_name, date_of_birth, relation}` per person found |
| `summary_vi` | string | Short Vietnamese summary (≤400 chars) |
| `md_content` | string | **Full markdown of all document data** — dates, IDs, names, amounts, addresses. Used as `.md` sidecar body AND checklist input. |

Returns: `vision_cache = {src_name: gem_dict}`

---

### Step 5 — Phase 3: Process Each File (sequential)

Drive client is not thread-safe — this phase runs sequentially.

**Function:** `process_one(path, src_name, *, ..., gem_cache, pages_text, ...)`

```
1. SHA-1 dedup
   hashlib.sha1(file bytes)
   Check _HASH_CACHE (populated lazily from Drive sidecars on first call per case)
   If hash match and no naming drift and tag ≠ "Khac" → status: duplicate-by-hash, return

2. Use prefetched GPT result
   gem = vision_cache[src_name]     (from Step 4)
   Fallback: call docai_classify_vision() inline on retry attempts

3. Classify
   classify_doc_type(tag, summary_vi, src_name)  [lib.sop_naming]
   Regex / YAML patterns refine the GPT tag into a canonical doc type

4. Extract metadata
   subject_from_gemini(gem, applicant) → person name (from person[0].full_name)
   relation = gem["relation"] (empty string if "applicant")
   detect_english(summary_vi, "") → bool
   title_case_ascii(subject_raw)

5. Build filename
   dedup_name(name_registry, tag, subject_title, ext, is_english, relation)
   → build_filename() → "Tag[ relation][ idx]-Subject[_ENG].ext"
   Collision-safe within a batch via name_registry dict

6. Check destination name dedup [lib.drive_helpers]
   If same filename already in Drive folder → status: duplicate, return

7. Upload [lib.drive_helpers]
   upload_file() → correct top folder (Personal Docs / Education / Asset / Employment)
   Non-OCR files → uploaded-no-ocr

8. Write sidecars [lib.drive_helpers]
   .json → _Bot OCR & Metadata/<new_name>.json
     Includes: content_hash, tag, folder, subject, relation, confidence,
               needs_review, is_english, ocr, summary (400 chars), md_content,
               extracted: {} (kept for backward compat), drive_link, case_id
   .md → _Bot OCR & Metadata/<new_name>.md
     Header: # filename / Loại / Người / Confidence
     Body: md_content (full markdown from GPT)

9. Retry on error: exponential backoff 2^attempt (max 30s), up to --retries times
   On retry: gem_cache is None → re-calls docai_classify_vision() inline
```

**Note:** Multi-doc PDF splitting is **not supported** — each input file is one document. If a PDF contains multiple document types, staff should split it before sending.

---

### Step 6 — Reconciliation & Manifest

- Count outcomes per status bucket
- Detect **silent drops**: input files with no corresponding item → exit code 2
- Write `manifest.json`

---

### Step 7 — Vision Compare *(skipped if `--no-checklist`)*

**Library:** `lib.vision_check` (AWS Rekognition `CompareFaces` + `DetectFaces`)

- `find_compare_pairs(dataset)`: find Ảnh thẻ × Passport / GPLX / CCCD pairs (max 3)
- SHA-1 cache: skip pairs already compared
- PDF inputs auto-converted to JPEG (pypdfium2 page 0) before sending to Rekognition
- `compare_portraits()` → `{same_person, confidence, age_diff_months, phau_thuat_signs, anomalies, rekognition_similarity}`
  - `similarity ≥ 95` → `same_person=True, confidence=high`
  - `similarity ≥ 80` → `same_person=True, confidence=medium`
  - `similarity ≥ 60` → `same_person=True, confidence=low`
  - `similarity < 60` → `same_person=False`
  - `phau_thuat_signs` always `[]` — Rekognition không phát hiện phẫu thuật (cần review thủ công)
- Results stored in manifest `vision_compare[]` — injected as ground-truth into checklist Stage 2

---

### Step 8 — AI Checklist / Thẩm Định *(skipped if `--no-checklist`)*

**Fresh run path:** `lib.checklist.run_from_md_contents(md_contents, ...)`

- `md_contents`: list of `md_content` strings collected from `vision_cache` (in-memory, no Drive read)
- Deterministic pre-checks: `lib.rule_engine.detect_deterministic_errors()` runs 17 rules BEFORE LLM
- ONE LLM call (`CHECKLIST_MODEL`): all md_contents + rules violations + vision_compare → 4-part Markdown report
- `compute_coverage(dataset)` → FARM tally (X/18 required)
- Write Google Doc `"Bao cao tham dinh - <KH>"` to case folder

**Re-run path (`--checklist-only`):** `lib.checklist.run_and_write()`

- Reads `.json` sidecars from Drive via `build_dataset()` (supports both `md_content` and old `summary` field)
- Stage 1: cheap LLM extract → condensed JSON profile
- Stage 2: reasoning LLM → 4-part Markdown report
- Same deterministic pre-checks and Google Doc write

**Checklist output shape:** `{ran, model, extract_model, n_docs, coverage, report_link, doc_link, md_link, report_text, error}`

**Report sections:**
1. ✅ Giấy tờ chuẩn xác — docs that passed all checks
2. ⏰ Giấy tờ sắp / đã hết hạn — expiry calculations
3. ⚠️ Điểm mâu thuẫn cần làm rõ — contradictions, missing info, rule violations
4. 📌 Tóm tắt & khuyến nghị — overall status + prioritized action list + FARM coverage table

---

### Step 9 — Write Manifest & Output

- Write `manifest.json` (JSON, UTF-8)
- Print human-readable summary to stdout
- Clean up temp extraction directory
- Exit: `0` / `1` / `2`

---

## Manifest Structure

```json
{
  "input": "/path/to/file.zip",
  "case_folder_id": "1xABC...",
  "case_id": "hoang-thi-mo-2026",
  "applicant": "Hoang Thi Mo",
  "generated_at": "2026-05-17T10:00:00",
  "total_input_files": 6,
  "total_output_items": 6,
  "unique_sources_covered": 6,
  "counts": {
    "uploaded": 6,
    "uploaded-no-ocr": 0,
    "duplicate": 0,
    "duplicate-by-hash": 0,
    "failed": 0,
    "dry-run": 0
  },
  "dropped_files": [],
  "ok": true,
  "items": [
    {
      "src_name": "CCCD.pdf",
      "new_name": "CCCD-Hoang Thi Mo.pdf",
      "ext": ".pdf",
      "tag": "CCCD",
      "folder": "Personal Docs",
      "subject": "Hoang Thi Mo",
      "relation": "",
      "confidence": "high",
      "needs_review": false,
      "is_english": false,
      "ocr": true,
      "summary": "CCCD số 040191042322, ngày sinh 02/09/1991...",
      "md_content": "# CCCD - Hoàng Thị Mơ\n**Số CCCD:** 040191042322...",
      "extracted": {},
      "content_hash": "a1b2c3...",
      "drive_link": "https://drive.google.com/file/d/...",
      "case_id": "hoang-thi-mo-2026",
      "status": "uploaded"
    }
  ],
  "vision_compare": [],
  "checklist": {
    "ran": true,
    "model": "gpt-5-mini",
    "extract_model": null,
    "coverage": "4/18",
    "report_link": "https://docs.google.com/document/d/..."
  }
}
```

**Note:** `extracted: {}` is kept in `.json` sidecars for backward compatibility with `/check` re-runs on older cases. The `md_content` field is the canonical rich content for new cases.

---

## SHA-1 Dedup

- Computed: `hashlib.sha1(path.read_bytes()).hexdigest()` — skipped on `--dry-run` / `--force-rescan`
- Cache: `_HASH_CACHE` populated lazily per case from Drive sidecars on first `process_one()` call
- Decision: if hash matches and filename unchanged and tag ≠ "Khac" → `duplicate-by-hash`, return immediately
- Naming drift or was-Khac → re-process (re-classify + re-upload with correct name)

Makes reruns idempotent: same file bytes → same Drive file, no double-upload.

---

## Error Handling & Retries

| Layer | Mechanism | Detail |
|-------|-----------|--------|
| `process_one()` main loop | `--retries` (default 3) | Exponential backoff: `sleep(min(2^attempt, 30))` |
| `vision_prefetch()` | Returns `{}` on exception | File still processed; classify falls back to filename-only |
| `docai_prefetch()` | Returns `None` on exception | `process_one` re-calls DocAI inline on retry |
| `docai_classify_vision()` | 3-tier format fallback | `json_schema` → `json_object` → no format |
| Sidecar write | `item["sidecar_error"]` | File uploaded; sidecar failure non-fatal |
| Vision / checklist | `try/except` logged | Non-fatal; manifest still written |

---

## Module & Library Reference

| Module / Library | Source | Used For |
|------------------|--------|----------|
| `lib.drive_helpers` | local | All Google Drive API calls (upload, list, download, rename, move, delete, sidecar write) |
| `lib.docai_client` | local | Google Document AI OCR wrapper — PDF chunking, DOCX→PDF, image passthrough |
| `lib.sop_naming` | local | `classify_doc_type()`, `build_filename()`, `extract_relation()`, `detect_english()` |
| `lib.rule_loader` | local | Load `doc_types.yaml`, `rules.yaml`, `relations.yaml`; generate prompts |
| `lib.rule_engine` | local | Deterministic pre-checks before checklist LLM |
| `lib.checklist` | local | `run_from_md_contents()` (fresh) + `run_and_write()` (/check re-run) |
| `lib.vision_check` | local | AWS Rekognition portrait comparison (`CompareFaces` + `DetectFaces`) |
| `boto3` | pip | AWS SDK — Rekognition client |
| `lib.diadia` | local | Offline Vietnamese admin-boundary lookup (used in checklist) |
| `httpx` | pip | HTTP calls to OpenAI API |
| `pypdfium2` | pip | Rasterize PDF page 0 → JPEG for GPT vision |
| `pypdf` | pip | Count PDF pages; PDF chunking in docai_client |
| `concurrent.futures` | stdlib | `ThreadPoolExecutor` — parallel DocAI OCR + GPT vision classify |
| `hashlib` | stdlib | SHA-1 content hash for dedup |
| `zipfile` | stdlib | Extract `.zip` input |
| `base64` | stdlib | Encode first-page JPEG for GPT multimodal API |
| `tempfile` | stdlib | Temp dir for zip extraction |
| `argparse` | stdlib | CLI flags |

---

## Self-Test (`--self-test`)

Runs without Drive or live API calls:

1. **SOP naming** — `classify_doc_type()` regression cases
2. **OCR prefetch** — `docai_prefetch()` with empty / non-OCR input returns `{}` without API calls
3. **Model sanity** — `OCR_CLASSIFY_MODEL` must be set; `DOC_RESULT_SCHEMA` is valid
4. **PDF page count** — `_count_pdf_pages()` with pypdf available
5. **Checklist module** — import + verify constants + coverage computation

Exit `0` if all pass, `1` if any fail.
