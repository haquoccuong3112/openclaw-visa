# Plan: Option B — Intelligence Layer Migration to OpenClaw

## Context

`@donghanhprocessingbot` has two distinct layers:

1. **Pipeline layer** — file detection, 20s debounce (`_PENDING_BATCHES` per chat_id), `asyncio.Semaphore(SCAN_RUN_CONCURRENCY)` for concurrency, per-case locks, `scan_pipeline.py` subprocess, Pro group posting. Well-designed, handles hundreds of groups safely. **Stays unchanged.**

2. **Intelligence layer** — Q&A in Pro groups (`on_chat_message()` → `lib/chat.py`), staff DMs, `/check` command (`on_check_command()`). This is pure AI reasoning and is exactly what OpenClaw is built for.

Migrating layer 2 to OpenClaw gives: per-group persistent memory across sessions, native web_search tool, better model context management, and cleaner separation of concerns. The pipeline bot becomes infrastructure-only.

Cross-group posting confirmed: `openclaw message send --channel telegram --target <chat_id>` with negative supergroup IDs.

## Approach: Two bots in Pro groups — no changes to standalone bot

Add the existing OpenClaw agent (`@dhproclawbot`) to all Pro groups alongside `@donghanhprocessingbot`. Staff @mention `@dhproclawbot` for Q&A and `/check`. The pipeline bot keeps doing what it does: file processing and posting results.

No changes to `telegram_listener.py` or `scan_pipeline.py`.

## Architecture after migration

```
KH group                    Pro group                    Staff DM
─────────                   ─────────                    ────────
Upload .zip         →       @donghanhprocessingbot       @dhproclawbot
@donghanhprocessingbot      posts summary + checklist    (OpenClaw)
(pipeline, silent)
                            @dhproclawbot                answers Q&A
                            (OpenClaw)                   picks case by name
                            answers @mentions / replies  per-user memory
                            /check → pipeline --checklist-only
```

## Files to create

### 1. `workspace/skills/pro-group-qa/SKILL.md`

New skill that teaches the OpenClaw agent how to answer visa case questions. The skill:
- Reads `~/.openclaw/workspace/scan-ho-so/group_registry.json` via exec to map the current Pro group chat_id → `folder_id` + `applicant`
- Calls `exec python3 ~/.openclaw/workspace/scan-ho-so/lib/qa_cli.py --group-chat-id <id> --question "<q>"` to get the answer from case Drive sidecars
- Posts the result back to the Pro group

The `qa_cli.py` wrapper (see below) handles all Drive access, NEED_FILE, NEED_ADDR, NEED_WEB, NEED_RENAME internally using the existing `lib/chat.py` logic.

### 2. `workspace/scan-ho-so/lib/qa_cli.py` ← key new file

Thin CLI wrapper around the existing `answer_question()` in `lib/chat.py`. Accepts:
```
python3 qa_cli.py --group-chat-id <telegram_chat_id> --question "<text>" [--user <name>]
```
- Reads `group_registry.json` to resolve folder_id + applicant from chat_id
- Calls `asyncio.run(answer_question(case_meta, ctx, history=[], question=text, drive_id=SHARED_DRIVE_ID))`
- Prints answer to stdout (plain text, linkified)
- History is session-managed separately (OpenClaw handles turn context natively via its own memory)

Note: `lib/chat.py` currently has async functions and uses `asyncio`; `qa_cli.py` wraps with `asyncio.run()`.

### 3. `workspace/skills/check-command/SKILL.md`

Skill triggered by `/check` in Pro groups or DMs. Calls:
```bash
python3 ~/.openclaw/workspace/scan-ho-so/scan_pipeline.py \
  --from-registry <pro_group_chat_id> --checklist-only
```
Posts the checklist output to the Pro group. Maps Pro group chat_id → KH chat_id via `group_registry.json` (the registry has `pro_chat_id` field for cross-lookup).

## Files to modify

### 4. `~/.openclaw/openclaw.json`

Add Pro group IDs to the agent's Telegram channel config:
```json5
channels: {
  telegram: {
    groups: {
      "-100XXXXXXXXX": {           // each Pro group ID
        requireMention: true,      // only respond when @mentioned or replied-to
        skills: ["pro-group-qa", "check-command"],
        systemPrompt: "You are the Đồng Hành visa processing assistant..."
      }
    }
  }
}
```

⚠️ This file has API keys — always eyeball `git status` before any commit; `openclaw.json` is gitignored.

After editing: `openclaw gateway restart`

## What OpenClaw gives that lib/chat.py doesn't

| Capability | Current `lib/chat.py` | OpenClaw |
|------------|----------------------|----------|
| Per-group memory | None (stateless) | Persistent per group session |
| Multi-turn context | In-memory history (lost on restart) | File-backed, survives restarts |
| Web search | Manual NEED_WEB → OpenRouter | Native `web_search` tool |
| Model ladder | Hardcoded env vars | Configurable per agent/group |
| NEED_RENAME confirmation | `_PENDING_RENAME` dict hack | Clean tool flow (Lobster Phase 2) |

## What stays in lib/chat.py (called via qa_cli.py)

- `answer_question()` — Drive sidecar reading, NEED_FILE/NEED_ADDR/NEED_RENAME logic
- `linkify_answer()` — Telegram HTML post-processing
- `do_rename()` — Drive file + sidecar rename
- `lib/diadia.py` — offline admin boundary lookup (NEED_ADDR)

## Out of scope (Phase 1)

- Lobster plugin for NEED_RENAME approvals (nice-to-have Phase 2)
- Webhook bridge to remove second bot from Pro groups (Phase 2)
- DM staff case-picker via OpenClaw (Phase 2 — needs per-user session bridging)

## Verification

```bash
# 1. Test qa_cli.py directly
cd ~/.openclaw/workspace/scan-ho-so
python3 lib/qa_cli.py --group-chat-id <pro_group_chat_id> --question "CCCD của người vay hết hạn chưa?"

# 2. Syntax check new file
python3 -m py_compile lib/qa_cli.py

# 3. Run existing self-tests (must still pass)
python3 scan_pipeline.py --self-test && python3 lib/diadia.py && python3 lib/checklist.py && python3 lib/chat.py

# 4. Restart OpenClaw gateway after openclaw.json edit
openclaw gateway restart && openclaw status

# 5. Add @dhproclawbot to one test Pro group, @mention it with a question
# 6. Test /check in the same group
```
