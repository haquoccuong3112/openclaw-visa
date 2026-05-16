# OpenClaw Setup Reference

_Last updated: 2026-05-16_

---

## Gateway

| Item | Value |
|------|-------|
| Version | 2026.5.12 (f066dd2) — updated from 2026.5.7 |
| Mode | local, `127.0.0.1:18789` |
| Auth | token-based |
| Service | systemd user unit (`openclaw-gateway`), auto-start on login |
| Tailscale | off |

**Common commands:**
```bash
openclaw status
openclaw gateway restart
journalctl --user -u openclaw-gateway -n 50
```

---

## Agent: "Pro Bot"

| Item | Value |
|------|-------|
| Name | Pro Bot |
| Vibe | Gọn, nhanh, thực dụng |
| User | Cường Hà Quốc (`Asia/Saigon`) |
| Main model | Claude Sonnet 4.6 |
| Telegram DM model | DeepSeek v4-pro |
| Heartbeat | 30-minute cycle |
| Sessions | 2 active (default + Telegram direct) |

---

## Workspace

**Path:** `/home/cuong/.openclaw/workspace/`  
Loaded by the gateway at session start. Edit → `openclaw gateway restart` to take effect.

| File | Purpose |
|------|---------|
| `IDENTITY.md` | Agent name, vibe |
| `SOUL.md` | Personality, tone, boundaries |
| `AGENTS.md` | Red lines, group-chat rules, memory discipline, heartbeat |
| `USER.md` | Cường's profile (timezone, context) |
| `TOOLS.md` | Drive/Sheets infra — service account, folder IDs, Document AI processor |
| `HEARTBEAT.md` | Placeholder (empty — periodic check tasks go here) |
| `MEMORY.md` | Ongoing project notes, curated long-term memory |
| `README.md` | Repo map + architecture notes |

**Google infra (from `TOOLS.md`):**
- Service account: `scan-ho-so-bot@ally-visa-bot.iam.gserviceaccount.com`
- Shared Drive: `0AIYOQpLqtMPvUk9PVA`
- OpenClaw folder: `1VUpoBV3fAudONv5mMFXYguRThKfOLyz7`
- Master sheet: `1Qv4gdxNKgS7EsDPvInFsR1rnob_Qgv9HCaays_al6io`
- Document AI processor: `3183188b763e1843` (project `ally-visa-bot`)

---

## Channels & Plugins

- **Telegram**: enabled, @mention required in groups, separate token from the bot
- **Plugins**: OpenAI, Anthropic, Google, OpenRouter (all enabled); Google webSearch enabled
- **Skills enabled**: `openai-whisper-api` only (60+ ClawHub skills disabled)

---

## The Bot: `@donghanhprocessingbot`

**Separate** from the OpenClaw gateway — standalone Python process managed by systemd.

| Item | Value |
|------|-------|
| Service | `donghanhbot.service` (system, not user) |
| User | `cuong` |
| Working dir | `/home/cuong/.openclaw/workspace/scan-ho-so/` |
| Bot token | `scan-ocr.env` → `TELEGRAM_BOT_TOKEN` |
| Logs | `journalctl -u donghanhbot -f` |

```bash
sudo systemctl restart donghanhbot
sudo systemctl status donghanhbot
journalctl -u donghanhbot -f
```

### Pipeline flow

```
KH group upload (.zip / loose files)
  → telegram_listener.py debounces into one batch
  → subprocess: scan_pipeline.py
      → enumerate all files
      → OCR parallel (5 workers, gemini-2.5-flash)
      → 2-pass multi-page PDF (rasterize → classify → segment → OCR per segment)
      → classify doc type (sop_naming.py)
      → SHA-1 dedup (skip if already uploaded)
      → Drive upload + .json/.md sidecars (_Bot OCR & Metadata/)
      → vision compare (lib/vision_check.py — Anh thẻ × Passport/GPLX/CCCD)
      → checklist/thẩm định (lib/checklist.py — 2-stage LLM)
          → Stage 1: cheap extract → condensed JSON
          → Stage 2: deterministic rule_engine.py pre-check + LLM reasoning
          → writes Google Doc "Bao cao tham dinh - <KH>"
  → post summary to Pro group (clickable Drive links)
```

### Key modules

| Module | Purpose |
|--------|---------|
| `telegram_listener.py` | Main bot: KH/Pro group handling, debounce, /oldfile, Q&A dispatch, /check |
| `scan_pipeline.py` | Document pipeline (OCR → classify → rename → upload → checklist) |
| `lib/checklist.py` | AI thẩm định — 2-stage LLM + Google Doc report |
| `lib/chat.py` | Q&A visa officer; mechanisms: NEED_FILE / NEED_ADDR / NEED_WEB / NEED_RENAME |
| `lib/rule_engine.py` | Deterministic pre-checks (17 rules) — runs before LLM, no hallucination |
| `lib/vision_check.py` | Gemini multi-image portrait compare (phẫu thuật, trùng ảnh) |
| `lib/diadia.py` | Offline old↔new admin-boundary lookup (10,358 rows, no HTTP) |
| `lib/sop_naming.py` | Doc-type classifier + SOP filename builder (`LOAI-Họ Tên.ext`) |
| `lib/rule_loader.py` | Load/validate YAML: 26 checklist + 63 rules v1.1 + 32 doc-types + 8 relations |
| `lib/drive_helpers.py` | Drive API wrappers with in-process cache |
| `lib/google_clients.py` | Drive/Sheets client init |

### Data config (`data/`)

| File | Content |
|------|---------|
| `rules.yaml` | 63 validation rules v1.1 (17 with deterministic conditions) |
| `doc_types.yaml` | 32 document types |
| `relations.yaml` | 8 nhân thân relations |
| `provinces_34.json` | 34 provincial units + old_to_new map |
| `admin/` | Ward-level admin-boundary tables (VietMap source, ~10K rows) |

### Models (from `scan-ocr.env`)

| Role | Default model |
|------|--------------|
| OCR | `gemini-2.5-flash` (`GEMINI_MODEL`) |
| Page classify pass 1 | `gemini-2.5-flash` (`PAGE_CLASSIFY_MODEL`) |
| Checklist extract (stage 1) | `CHECKLIST_EXTRACT_MODEL` |
| Checklist reasoning (stage 2) | `CHECKLIST_MODEL` |
| Checklist fallback | `CHECKLIST_FALLBACK_MODEL` |
| Chat Q&A | via OpenRouter |

---

## Skills

**Custom (git-tracked):**
- `workspace/skills/scan-ho-so-pipeline/SKILL.md` — procedure doc for the pipeline (no code; points agent at `scan_pipeline.py`)

**ClawHub stock (gitignored):**
`algorithmic-art`, `brand-guidelines`, `canvas-design`, `doc-coauthoring`, `docx`, `frontend-design`, `internal-comms`, `mcp-builder`, `pdf`, `pptx`, `slack-gif-creator`, `theme-factory`, `web-artifacts-builder`, `webapp-testing`, `xlsx`

---

## Secrets & Config

| File | Location | Git-tracked |
|------|----------|-------------|
| `openclaw.json` | `~/.openclaw/` | No |
| `scan-ocr.env` | `workspace/` | No |
| `google-service-account.json` | `workspace/` | No |
| `group_registry.json` | `workspace/scan-ho-so/` | No |
| `donghanhbot.service` (repo copy) | `workspace/scan-ho-so/` | Yes |

Active service file: `/etc/systemd/system/donghanhbot.service` — keep in sync with repo copy; run `sudo systemctl daemon-reload` after editing.

---

## Repo

- **Remote**: `github.com/haquoccuong3112/openclaw-visa` (private)
- **Root**: `~/.openclaw/` — `.gitignore` is deny-all + allowlist; only `workspace/` is tracked
- **Before every commit**: `git status` carefully — secrets live at the repo root

```bash
cd ~/.openclaw && git add -A && git status   # always eyeball this
git commit -m "..." && git push
```
