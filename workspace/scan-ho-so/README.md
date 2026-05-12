# scan-ho-so — the `@donghanhprocessingbot` visa-document app

This folder is the whole app. It sorts Đồng Hành / ALLY visa-application documents into per-customer
Google Drive folders and runs an AI cross-check ("thẩm định"). It runs as the systemd unit
`donghanhbot.service` (which executes `telegram_listener.py`).

## Layout
- **`telegram_listener.py`** — the Telegram bot (`@donghanhprocessingbot`). Receives `.zip`s / files from
  customer (KH) groups → debounces a batch → spawns `scan_pipeline.py` as a subprocess → posts a summary
  + a short AI-checklist confirmation to the Pro group. Also handles staff Q&A (@mention / reply / DM) via
  `lib/chat.py`, and on-demand `/check` re-runs the thẩm định. Telegram messages use `parse_mode=HTML`
  (`send_html()` helper); the HTML is built by our code (`html.escape`), never by the LLM.
- **`scan_pipeline.py`** — the unzip → Gemini-OCR → classify → SOP-rename → upload-to-Drive → AI-thẩm-định
  pipeline, with a manifest covering every input file, per-file retries, idempotent re-runs. Run by the bot
  (subprocess) and by the OpenClaw agent via the `../skills/scan-ho-so-pipeline/` skill (which is *just*
  `SKILL.md` — the procedure docs; the code is here). CLI: `python3 scan_pipeline.py <zip|dir>
  --from-registry <chat-id> --manifest <path>` (or `--case-folder-id … --applicant …`); `--dry-run`,
  `--checklist-only`, `--no-checklist`, `--self-test`, `--retries N`.
- **`lib/`** — shared building blocks (used by both `telegram_listener.py` and `scan_pipeline.py`):
  - `sop_naming.py` — doc-type classification + the SOP filename builder (`<Tag>[ relation][ idx]-<Subject>[_ENG].ext`).
  - `checklist.py` — the AI thẩm định: 2-stage LLM pipeline (cheap extract → reasoning) → a 4-part Markdown
    report written as a Google Doc, + the deterministic "điểm danh" FARM coverage (26 items / 18 required).
  - `chat.py` — the Q&A "visa officer": `answer_question()` with `NEED_FILE` / `NEED_WEB` / `NEED_RENAME`
    one-shot mechanisms; `linkify_answer()` (doc-name → clickable Telegram link); `do_rename()` (renames a
    Drive file + its `.json`/`.md` sidecars).
  - `drive_helpers.py` — Google Drive API wrappers (folder cache; upload/list/find/delete/rename/replace).
    **All Drive calls run on the asyncio event loop, never in a thread** (the httplib2 client isn't thread-safe).
  - `google_clients.py` — Drive/Sheets API client init.
- **`data/`** — config data: `provinces_34.json` (34 administrative units, effective 2025-06-12; used by
  `checklist.py`), `customer-folder-structure.json` (reference: the 4 top folders + their subfolders).
- **`docs/`** — domain notes: `VISA_CANADA_BOT.md`, `visa_canada_sop_raw.md` (the ALLY FARM checklist + naming SOP).
- **`archive/`** — `run_sop_v2.py`, an old one-off dev script (superseded by `scan_pipeline.py` / `telegram_listener.py`); kept for reference, not run.
- **`donghanhbot.service`** — copy of the systemd unit (active copy is `/etc/systemd/system/donghanhbot.service`; keep both in sync, `daemon-reload` after editing the active one).
- **`group_registry.json`** — KH↔Pro group ↔ Drive case folder map (`folder_id`, `applicant`, `visa`, `drive_link` per Telegram chat id). **Written by the bot at runtime; gitignored.**

## Config & secrets (not in git)
`../scan-ocr.env` (= `<workspace>/scan-ocr.env`) holds `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`GOOGLE_APPLICATION_CREDENTIALS` (→ `../google-service-account.json`), Document AI / Gemini / checklist /
chat model ids. Both `telegram_listener.py` and `scan_pipeline.py` load it from `<parent dir>/scan-ocr.env`.

## Develop
```bash
python3 -m py_compile telegram_listener.py scan_pipeline.py lib/*.py     # syntax check
python3 scan_pipeline.py --self-test                                     # SOP-naming self-test
python3 lib/checklist.py && python3 lib/chat.py                           # the "tests" — each prints OK
python3 scan_pipeline.py <some.zip> --dry-run --applicant Test --manifest /tmp/m.json   # no Drive writes
sudo systemctl restart donghanhbot && journalctl -u donghanhbot -f       # run / tail the bot
```
