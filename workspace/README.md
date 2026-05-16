# OpenClaw workspace + scan-ho-so bot

This directory is the OpenClaw **agent workspace** — set as `agents.defaults.workspace` in
`~/.openclaw/openclaw.json` → `/home/cuong/.openclaw/workspace`. OpenClaw loads the `*.md` files
below at session start. (Everything *else* under `~/.openclaw/` — config, credentials, sessions,
auth, Codex runtime — is OpenClaw runtime state and is **not** part of this repo.)

## Layout
- `AGENTS.md` — operating instructions / behavioural rules
- `SOUL.md` — persona, tone, boundaries
- `IDENTITY.md` — agent name / vibe / emoji
- `USER.md` — user identity & addressing preferences
- `TOOLS.md` — local tool conventions (guidance only)
- `HEARTBEAT.md` — checklist for heartbeat runs
- `MEMORY.md` — curated long-term memory
- `memory/YYYY-MM-DD.md` — daily memory logs
- `docs/` — reference documentation (edit along the way):
  - `docs/PRD.md` — Product Requirements Document (what the system is, users, features, constraints)
  - `docs/openclaw-setup.md` — gateway, agent config, bot architecture, commands
  - `docs/data-config.md` — rules.yaml (63 rules), doc_types.yaml (32 types), relations.yaml (8 relations), provinces_34.json (34 units)
  - `docs/pipeline-diagram.md` — full pipeline Mermaid diagram + module reference + design decisions
- `skills/` — OpenClaw skills: stock ClawHub skills + the custom `scan-ho-so-pipeline`
- `scan-ho-so/` — the `@donghanhprocessingbot` Telegram bot (systemd unit `donghanhbot.service`);
  `scan-ho-so/docs/` holds the visa-bot project notes

## Not in git (live only on this box — see `.gitignore`)
- `scan-ocr.env`, `google-service-account.json` — secrets
- `scan-ho-so/runs/`, `scan-ho-so/test-runs/`, `scan-ho-so/group_registry.json`, `__pycache__/`, `*.bak*`

## Repo
Private: `github.com/haquoccuong3112/openclaw-visa`
