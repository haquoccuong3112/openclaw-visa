# MEMORY.md - Long-term Memory

## Dong Hanh Bot Project (2026-05-11)
- Bot: @donghanhprocessingbot | Token trong `scan-ocr.env`
- Service: `donghanhbot.service` (systemd) | Code: `/home/cuong/.openclaw/workspace/scan-ho-so/telegram_listener.py`
- Pipeline: Nhóm KH gửi file → OCR (Document AI) → Gemini classify → rename SOP → Drive → forward nhóm Pro
- Tên nhóm KH: `{Tên KH} {Visa} - {Agent}` | Tên nhóm Pro: `DH Pro {Visa} - {Tên KH}`
- Master Sheet: `1Qv4gdxNKgS7EsDPvInFsR1rnob_Qgv9HCaays_al6io`
- Shared Drive: `0AIYOQpLqtMPvUk9PVA` | OpenClaw folder: `1VUpoBV3fAudONv5mMFXYguRThKfOLyz7`
- Service account: `scan-ho-so-bot@ally-visa-bot.iam.gserviceaccount.com` (organizer trên Shared Drive)
- Bot cần là admin trong nhóm Pro để detect nhân viên
- Chi tiết đầy đủ: `memory/2026-05-11.md`
- **Còn lại:** Test end-to-end gửi file → OCR → Drive → forward Pro (bot đang STOPPED)
