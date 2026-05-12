# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is `~/.openclaw/` — the OpenClaw config/state directory. **Only `workspace/` is git-tracked**; everything else under `~/.openclaw/` (`credentials/`, `identity/`, `telegram/`, `agents/`, `openclaw.json` + its `.bak*`, `exec-approvals.json`, `logs/`, `tasks/`, `flows/`, `plugins/`, `tools/`, `tui/`, `audit/`, `media/`, `memory/` (SQLite), `completions/`, `canvas/`, …) is OpenClaw runtime state / credentials and is deliberately ignored. The `.gitignore` is **deny-all + allowlist** (`/*` then `!/.gitignore !/CLAUDE.md !/workspace/`), so a new top-level file is ignored by default — never add an `!` rule for anything sensitive. Repo: `github.com/haquoccuong3112/openclaw-visa` (private). When committing: `cd ~/.openclaw && git add -A && git status` (⚠️ always eyeball `git status` here — the repo root is full of secrets) `&& git commit && git push`.

`workspace/` holds two distinct things:

1. **The OpenClaw agent workspace** — `workspace/{AGENTS,SOUL,IDENTITY,USER,TOOLS,HEARTBEAT,MEMORY}.md` + `workspace/memory/YYYY-MM-DD.md`. The OpenClaw gateway loads these at session start (it's set as `agents.defaults.workspace` in `~/.openclaw/openclaw.json` → `/home/cuong/.openclaw/workspace`). Editing them only takes effect after `openclaw gateway restart`. These configure the *OpenClaw agent's* persona/instructions — not Claude Code.
2. **The `scan-ho-so` app** (`workspace/scan-ho-so/`) — the actual code project: the `@donghanhprocessingbot` Telegram bot + the document pipeline + shared `lib/`. The OpenClaw skill `workspace/skills/scan-ho-so-pipeline/` is now **just `SKILL.md`** (the procedure docs; ships no code — it points the agent at `scan-ho-so/scan_pipeline.py`). (The stock ClawHub skills like `pdf/`, `docx/`, etc. live on disk under `workspace/skills/` but are gitignored — only `scan-ho-so-pipeline/` is tracked.)

## scan-ho-so bot — architecture (the big picture)

`@donghanhprocessingbot` sorts Đồng Hành / ALLY visa-application documents into per-customer Google Drive folders and runs an AI cross-check ("thẩm định"). It is **not** the OpenClaw agent — it's a standalone `python-telegram-bot` process (token from `scan-ocr.env`, different from the OpenClaw gateway's Telegram token in `openclaw.json`).

- **`workspace/scan-ho-so/telegram_listener.py`** — the bot process (systemd unit `donghanhbot.service`, `WorkingDirectory`/`ExecStart` = `~/.openclaw/workspace/scan-ho-so/`). Flow: a KH (customer) Telegram group forwards a `.zip` (or loose files) → the bot debounces them into one batch → delegates the heavy work to a **subprocess**: the sibling `scan_pipeline.py` (env override `SCAN_PIPELINE_SCRIPT`) → on completion posts to the Pro group: a summary (`summarize_manifest()`, filenames are clickable Drive links) + a short AI-checklist confirmation. It also handles Q&A: when staff @mention/reply the bot in the Pro group, or DM it, it answers via `lib/chat.py`; and `/check` re-runs the thẩm định. Telegram messages are sent as `parse_mode=HTML` via the `send_html()` helper — **all HTML is built by our code with `html.escape`, never emitted by the LLM**.
- **`workspace/scan-ho-so/scan_pipeline.py`** — the document pipeline (run by the bot as a subprocess, and by the OpenClaw agent via the `scan-ho-so-pipeline` skill): enumerates *every* real file in the zip/dir, OCRs + summarizes each with Gemini (via OpenRouter / Google Document AI), classifies the doc type, renames to the SOP convention, uploads to the case's Drive folder with `.json`/`.md` metadata sidecars in a `_Bot OCR & Metadata` subfolder, then runs the checklist. It writes a **manifest** covering all inputs, retries each file, keeps non-`pdf/jpg/png` files (uploads without OCR), and exits non-zero if anything still failed so the caller re-runs (re-runs are idempotent — uploads skip by destination name). It uses `lib.*` directly (it's in the same dir as `lib/`) and resolves the target Drive folder from `scan-ho-so/group_registry.json` (keyed by Telegram chat id: `folder_id`, `applicant`, `visa`, `drive_link`) via `--from-registry <chat-id>`, or takes `--case-folder-id` + `--applicant`. Other flags: `--dry-run`, `--checklist-only`, `--no-checklist`, `--self-test`, `--retries N`; see `workspace/skills/scan-ho-so-pipeline/SKILL.md` for the procedure. **The bot/agent must use this pipeline, not do unzip/OCR/upload by hand — doing it manually silently drops files.**
- **`workspace/scan-ho-so/lib/checklist.py`** — the AI "thẩm định" step. Two-stage LLM pipeline over all of a case's OCR sidecars: tầng 1 (cheap extract → one condensed JSON), tầng 2 (reasoning → a 4-part Markdown report). Writes the report as a Google Doc (`Bao cao tham dinh - <KH>`) at the case folder, plus a deterministic "điểm danh" coverage tally against `REQUIRED_DOCS` (the 26-item ALLY FARM checklist, 18 required). `summarize_for_telegram()` returns the short `✅ Đã thẩm định hồ sơ — <link>` confirmation. Models via OpenRouter (env `CHECKLIST_MODEL` / `CHECKLIST_EXTRACT_MODEL` / `CHECKLIST_FALLBACK_MODEL`).
- **`workspace/scan-ho-so/lib/chat.py`** — the Q&A "visa officer". `answer_question()` answers from a case's OCR sidecars + the thẩm-định Google Doc + the FARM coverage + a **`_dia_gioi` block** (every address in the case pre-resolved old↔new via `lib/diadia.py` — fed to the LLM as ground-truth so it won't call an old-name vs new-name of the *same* place a "contradiction" even if the báo-cáo Doc is stale); it has four one-shot LLM mechanisms — `NEED_FILE:` (re-OCR one file in full), `NEED_ADDR:` (look one unit/address up in `lib/diadia.py` — used instead of `NEED_WEB` for administrative-boundary questions), `NEED_WEB:` (web search for other genuinely-external info), `NEED_RENAME: <old> => <new>` (rename a file → `do_rename()` renames the Drive file *and* its `.json`/`.md` sidecars; the bot asks the user to confirm `ok`/`huỷ` first — pending state in `_PENDING_RENAME`). `linkify_answer()` post-processes the plain-text answer into Telegram-HTML, turning known doc-name mentions and bare Drive URLs into `<a>` links (and stripping any stray markdown). Replies are plain text (no bold/italic) by design.
- **`workspace/scan-ho-so/lib/drive_helpers.py`** — Google Drive API wrappers with an in-process folder/list cache: `get_or_create_folder`, `list_folder`, `upload_file`/`replace_file`, `delete_file`, `rename_file`, `find_file_by_name`, `copy_file`, `download_file_text`/`download_file_bytes`. All run **on the asyncio event loop, never in a thread** (the Drive client / httplib2 is not thread-safe — only OpenRouter calls are offloaded with `asyncio.to_thread`).
- **`workspace/scan-ho-so/lib/sop_naming.py`** — doc-type classification + the SOP filename builder (the `LOAI-Họ Tên.ext` convention).
- **`workspace/scan-ho-so/lib/google_clients.py`** — Drive/Sheets API client init.
- **`workspace/scan-ho-so/lib/diadia.py`** — deterministic old↔new Vietnamese administrative-unit lookup (2025 reform), reading `data/admin/`: `resolve_address(text)`, `same_place(a,b)`, `commune_merge_info(name)`. Used by `checklist.py` (attaches `profile["_dia_gioi"]` as ground-truth for tầng 2 — so a doc saying "Vĩnh Phúc" and one saying "Phú Thọ" aren't flagged as a contradiction) and by `chat.py` (the `NEED_ADDR` mechanism). **Not an HTTP service** — flat JSON loaded into memory, lazily, cached.
- **`workspace/scan-ho-so/data/`** — config data: `provinces_34.json` (the 34 provincial units + a province `old_to_new` map + effective dates — read by `lib/checklist.py`), `customer-folder-structure.json` (reference: the 4 top folders + subfolders), and **`data/admin/`** — the admin-boundary tables for `lib/diadia.py` (`province_new.json`, `ward_new.json`, `old_to_new_wards.json` [10,358 rows, derived from `admin_mapping_old_to_new.xlsx`], `_convert_xlsx.py`, `SOURCES.md`; data sourced from the VietMap repo — see `SOURCES.md`, which also has the license note: use offline freely, don't modify-and-republish).
- **`workspace/scan-ho-so/archive/run_sop_v2.py`** — an old one-off SOP dev script, superseded; kept for reference, not run.
- **`workspace/scan-ho-so/README.md`** — the in-folder map of all of the above.

## Config & secrets

- Bot config: `workspace/scan-ocr.env` (loaded by both `telegram_listener.py` and `scan_pipeline.py` as `<that file's parent dir's parent>/scan-ocr.env`, i.e. the workspace root). Holds `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS` (→ `workspace/google-service-account.json`), the Document AI / Gemini / checklist / chat model ids. **Gitignored.**
- `workspace/scan-ho-so/group_registry.json` — KH↔Pro group ↔ Drive case folder map; **written by the bot at runtime** and **gitignored** — never commit it.
- OpenClaw's own config `~/.openclaw/openclaw.json` is full of API keys (OpenRouter, OpenAI, Google) and the OpenClaw gateway's Telegram bot token — it's outside `workspace/` and gitignored. Don't touch it except to point `agents.defaults.workspace` if the workspace ever moves (a `.pre-ws-move` backup convention exists).
- `donghanhbot.service` exists in two places: the repo copy `workspace/scan-ho-so/donghanhbot.service` and the active `/etc/systemd/system/donghanhbot.service`. Keep them in sync; `sudo systemctl daemon-reload` after editing the active one.

## Common commands

```bash
# the "tests" — each prints "OK"; run from the bot dir
cd ~/.openclaw/workspace/scan-ho-so && python3 scan_pipeline.py --self-test && python3 lib/diadia.py && python3 lib/checklist.py && python3 lib/chat.py
# syntax check everything
python3 -m py_compile ~/.openclaw/workspace/scan-ho-so/{telegram_listener,scan_pipeline}.py ~/.openclaw/workspace/scan-ho-so/lib/*.py

# bot: restart / logs / status
sudo systemctl restart donghanhbot
journalctl -u donghanhbot -f
systemctl is-active donghanhbot

# run the pipeline by hand (resolves the case folder from group_registry.json)
python3 ~/.openclaw/workspace/scan-ho-so/scan_pipeline.py <zip-or-dir> --from-registry <telegram-chat-id>
# (or: --case-folder-id <id> --applicant "<name>"; --dry-run; --checklist-only; --manifest <path>; see SKILL.md)

# OpenClaw gateway (needed after editing workspace *.md files or openclaw.json's workspace path)
openclaw gateway restart
openclaw status
journalctl --user -u openclaw-gateway -n 50

# git (repo root = ~/.openclaw — review status carefully, secrets live here)
cd ~/.openclaw && git add -A && git status && git commit -m "..." && git push
```

There is no test framework — the only automated checks are `scan_pipeline.py --self-test` (SOP-naming) and the `if __name__ == "__main__"` self-check blocks at the bottom of `lib/diadia.py`, `lib/checklist.py`, `lib/chat.py`. Keep those passing and extend them when you change those modules.

## Notes

- **When unsure about anything OpenClaw-related** — directory layout, the workspace, skills, `openclaw.json` config keys, the gateway, CLI commands, plugins, channels — check the official docs first: **https://docs.openclaw.ai** (the machine-readable index is at https://docs.openclaw.ai/llms.txt; the workspace concept specifically is at https://docs.openclaw.ai/concepts/agent-workspace). Don't guess about OpenClaw internals.
- New files: `rm -f file && cat > file <<'EOF' … EOF` (recreate from scratch) is the preferred edit style for config/scripts here, not `sed`/`awk`.
- Vietnamese is the working language for bot output and most code comments; keep it that way.
- See `workspace/scan-ho-so/docs/VISA_CANADA_BOT.md` and `docs/visa_canada_sop_raw.md` for the domain (the ALLY FARM document checklist, naming SOP, Vietnamese administrative-boundary rules post-2025-06-12).
