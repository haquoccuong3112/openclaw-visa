# scan_pipeline.py

Document pipeline for `@donghanhprocessingbot`. Receives a `.zip` or directory, runs OCR + AI classification in parallel, renames files to the SOP convention, uploads to Google Drive, compares portrait photos, and generates an AI thẩm định report. **Idempotent** — reruns skip already-uploaded content via SHA-1 dedup.

---

## Workflow

```
Input (.zip / dir)
  │
  ├─ Phase 1: DocAI OCR ──────────────────────── parallel (5 threads)
  │    └─ Google Document AI → [{page, text}] per file
  │
  ├─ Phase 2: GPT Vision Classify ────────────── parallel (5 threads)
  │    └─ page-0 JPEG + OCR text → gpt-5-mini (strict JSON)
  │         tag / folder / subject / relation / md_content / …
  │
  └─ Phase 3: Upload loop ─────────────────────── sequential (Drive not thread-safe)
       │
       ├─ SHA-1 dedup → skip if already in Drive
       ├─ classify_doc_type() + build_filename()  [sop_naming]
       ├─ upload_file() → correct top folder
       └─ write .json + .md sidecars → _Bot OCR & Metadata/
            (md_content is the sidecar body AND the checklist input)

  └─ Vision Compare (lib.vision_check)
       └─ Ảnh thẻ × Passport/GPLX/CCCD (max 3 pairs) → AWS Rekognition

  └─ AI Thẩm Định (lib.checklist)
       ├─ rule_engine: 17 deterministic pre-checks (no LLM)
       └─ LLM reasoning → 4-part report → Google Doc
```

---

## Key Algorithms

### SHA-1 Dedup
`hashlib.sha1(file_bytes)` — checked against `_HASH_CACHE` (built lazily from Drive sidecars on first run). Match + stable name + tag ≠ "Khac" → skip. Naming drift or was-Khac → re-classify and re-upload.

### GPT Vision Classify
Each file gets one `gpt-5-mini` call with:
- First-page JPEG (pypdfium2, 300 DPI) encoded as base64
- Full DocAI OCR text
- Strict JSON schema (`DOC_RESULT_SCHEMA`) — no free-form output

Returns: `tag`, `folder`, `subject`, `relation`, `confidence`, `md_content` (full markdown of all document data), and more. `md_content` is written as the `.md` sidecar and passed directly to the checklist — no Drive round-trip.

Falls back to `{tag: "Khac", md_content: ""}` on API error; filename-based heuristics in `classify_doc_type()` then apply.

### SOP Filename
```
<Tag>[ relation][ idx]-<Subject>[_ENG].<ext>
```
e.g. `CCCD bo-Nguyen Van A.pdf`, `Passport-Hoang Thi Mo.pdf`, `So dat 2-Hoang Thi Mo.pdf`

Collision-safe within a batch via `name_registry` dict. `dedup_name()` appends an index when the same (tag, subject) appears more than once.

### Face Compare (AWS Rekognition)
`find_compare_pairs()` pairs the Ảnh thẻ against Passport > GPLX > CCCD (priority order, max 3 pairs). For each pair:
1. If PDF: rasterize each page at 300 DPI, call `detect_faces` per page — use first page where a face is found (fallback: page 0)
2. `compare_faces(source, target, SimilarityThreshold=50)` → similarity score
3. Thresholds: ≥95 → high, ≥80 → medium, ≥60 → low, <60 → not same person

Result injected into checklist prompt as ground-truth. `phau_thuat_signs` always `[]` — Rekognition does not detect surgery; rule 1.2 needs manual review.

### Deterministic Rule Engine
`rule_engine.detect_deterministic_errors()` runs 17 rules from `data/rules.yaml` (via `simpleeval`) **before** any LLM call. Catches clear-cut failures (expired LLTP, thế chấp sổ đỏ, banned NH, CCCD photo flags) and injects them into the checklist prompt as `⚠️ LỖI BOT ĐÃ PHÁT HIỆN`.

### Checklist (Fresh Run)
`run_from_md_contents(md_contents)` — uses in-memory `md_content` strings from Phase 2, skips stage-1 extraction, goes straight to the stage-2 reasoning LLM. One call → 4-part Markdown report → written as Google Doc.

**Re-run (`--checklist-only`):** `run_and_write()` reads `.json`/`.md` sidecars from Drive and runs the full 2-stage pipeline (stage 1: cheap extract → JSON; stage 2: reasoning).

---

## CLI

```bash
python3 scan_pipeline.py <zip|dir> --from-registry <chat-id>
python3 scan_pipeline.py <zip|dir> --case-folder-id <id> --applicant "Name"

# Useful flags
--dry-run          # no API calls, no Drive writes
--no-checklist     # skip AI thẩm định
--checklist-only   # skip OCR/upload, re-run thẩm định from Drive sidecars
--force-rescan     # bypass SHA-1 dedup (used by /oldfile)
--retries N        # per-file retries (default 3, exponential backoff)
--self-test        # SOP naming regression suite, no live API calls
```

**Exit codes:** `0` success · `1` some files failed · `2` silent drop or checklist failure

---

## Config

Loaded from `../scan-ocr.env` (workspace root).

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_API_KEY` | — | GPT vision classify + checklist |
| `OPENROUTER_API_KEY` | — | Checklist fallback |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | — | Rekognition face compare |
| `AWS_REGION` | `us-east-1` | Rekognition region |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Service account JSON path |
| `GOOGLE_DOCUMENTAI_PROCESSOR_ID` | — | Document AI processor |
| `OCR_CLASSIFY_MODEL` | `gpt-5-mini` | Vision classify model |
| `CHECKLIST_MODEL` | `gpt-5-mini` | Thẩm định reasoning model |
| `CHECKLIST_EXTRACT_MODEL` | `gpt-5-mini` | Stage-1 extract (re-runs only) |
| `SCAN_OCR_WORKERS` | `5` | Thread count for Phase 1 + 2 |
