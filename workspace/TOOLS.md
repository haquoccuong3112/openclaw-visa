# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.

## Google Drive / Sheets

- Scan hồ sơ service account credentials: `/home/cuong/google-service-account.json` (mode 600; do not print contents)
- Env file: `/home/cuong/scan-ocr.env`
- OpenRouter API key is stored in `/home/cuong/scan-ocr.env` (mode 600; do not print contents). `/models` and chat completion smoke tests passed on 2026-05-10.
- Project: `ally-visa-bot`
- Service account email: `scan-ho-so-bot@ally-visa-bot.iam.gserviceaccount.com`
- Drive API OAuth smoke test passed on 2026-05-10.
- Shared/team Drive writable folder found: `Bot folder` (`1DtlLR80z9ptPN0Ub804aBtVsSnXWJ4vv`, driveId `0AIYOQpLqtMPvUk9PVA`).
- OpenClaw working folder inside `Bot folder`: `OpenClaw` (`1VUpoBV3fAudONv5mMFXYguRThKfOLyz7`, driveId `0AIYOQpLqtMPvUk9PVA`).
- Sheets write/read smoke test passed on 2026-05-10. Test sheet: `1EBjXQkieyAdEFRAdksJO42SeC17BmaG0sEpRFSzN_HM`.
- Production/sample management Sheet: `ALLY - Quản Lý Hồ Sơ` (`1Qv4gdxNKgS7EsDPvInFsR1rnob_Qgv9HCaays_al6io`); bot can edit. Main tabs: `Cases`, `Documents`, `Activity Log`.
- Document AI OCR processor: project `ally-visa-bot` / number `245613344727`, location `us`, processor `3183188b763e1843`, endpoint stored in `/home/cuong/scan-ocr.env`. OAuth and `processOnline` OCR smoke test passed on 2026-05-10.
- Creating new Sheets outside a shared/user folder under the service account fails because the service account has no Drive storage quota; create files inside the shared `Bot folder` or another folder shared with the service account as Editor.

## Related

- [Agent workspace](/concepts/agent-workspace)
