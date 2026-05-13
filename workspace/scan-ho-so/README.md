# scan-ho-so — the `@donghanhprocessingbot` visa-document app

This folder is the whole app. It sorts Đồng Hành / ALLY visa-application documents into per-customer
Google Drive folders and runs an AI cross-check ("thẩm định"). It runs as the systemd unit
`donghanhbot.service` (which executes `telegram_listener.py`).

## Layout
- **`telegram_listener.py`** — the Telegram bot (`@donghanhprocessingbot`). Receives `.zip`s / files from
  customer (KH) groups → debounces a batch → spawns `scan_pipeline.py` as a subprocess → posts a summary
  + a short AI-checklist confirmation to the Pro group. Also handles staff Q&A (@mention / reply / DM) via
  `lib/chat.py`, and on-demand `/check` re-runs the thẩm định; **`/oldfile`** (Pro group) — scan
  `<case>/Old File/` trên Drive và đẩy qua cùng pipeline như khi gửi file qua Telegram; file gốc chuyển
  sang `Old File/_processed/`. Telegram messages use `parse_mode=HTML`
  (`send_html()` helper); the HTML is built by our code (`html.escape`), never by the LLM. **Group-title
  parser** (`parse_group_title()`): phân biệt KH vs Pro bằng chữ `Pro` (case-insensitive); hỗ trợ nhiều
  prefix (`DH Pro` / `DongHanh` / `Đồng Hành Pro` / `Đồng Hành`), em-dash / en-dash, token `KH` trên nhánh
  khách; chương trình gồm `WP\d+[mMyY]?` + `HighSkilled` + `FARM` + các code visa truyền thống. Self-test:
  `python3 telegram_listener.py --self-test`.
- **`scan_pipeline.py`** — the unzip → Gemini-OCR → classify → SOP-rename → upload-to-Drive → AI-thẩm-định
  pipeline. Gemini-OCR runs **in parallel** across files (`SCAN_OCR_WORKERS` threads, default 5); classify /
  rename / Drive-upload / thẩm-định stay sequential. Default OCR model `gemini-2.5-flash` (env `GEMINI_MODEL`)
  với `response_format: json_schema` (strict); 3-tier fallback `json_schema → json_object → off` cho model
  chưa hỗ trợ. **Multi-page PDF nhiều loại giấy tờ** đi qua flow 2-pass: Pass 1 — rasterize từng trang
  (`pypdfium2`) → `gemini-2.5-flash` quick-classify per page (env `PAGE_CLASSIFY_MODEL`); group trang
  liên tiếp cùng loại → segment. Pass 2 — split PDF (`pypdf`) + OCR đầy đủ per segment, mỗi segment thành
  1 file riêng (status `uploaded-split`). File `confidence=low + tag=Khac` → escalate `gemini-2.5-pro` 1 call
  để cứu. File đã có hash SHA-1 trong sidecar → status `duplicate-by-hash` (skip upload, KH gửi lại không
  tạo trùng). Relation tag (bo/me/vo/chong/con…) tự đính vào filename: `CCCD bo-Nguyen Van A.pdf`.
  Manifest covers every input file; per-file retries; idempotent re-runs. Run by the bot
  (subprocess) and by the OpenClaw agent via the `../skills/scan-ho-so-pipeline/` skill (which is *just*
  `SKILL.md` — the procedure docs; the code is here). CLI: `python3 scan_pipeline.py <zip|dir>
  --from-registry <chat-id> --manifest <path>` (or `--case-folder-id … --applicant …`); `--dry-run`,
  `--checklist-only`, `--no-checklist`, `--self-test`, `--retries N`.
- **`lib/`** — shared building blocks (used by both `telegram_listener.py` and `scan_pipeline.py`):
  - `rule_loader.py` ⭐ — load + validate `data/rules.yaml` / `data/doc_types.yaml` / `data/relations.yaml`
    (data-driven sprint). `load_checklist()` → 26 mục FARM; `load_validations()` → 63 rule v1.1;
    `load_doc_types()` → 32 loại giấy tờ; `load_relations()` → 8 quan hệ. Plus `generate_rules_block()`
    (sinh section RULES REFERENCE cho prompt thẩm định) + `generate_doc_type_catalog()` (cho prompt OCR).
  - `rule_engine.py` ⭐ — deterministic eval (simpleeval) cho 17 rule có `condition` trong rules.yaml.
    `detect_deterministic_errors(rules, dataset)` chạy NGOÀI LLM phát hiện ngay các lỗi rõ ràng (sổ đỏ
    thế chấp, LLTP hết hạn, NH cấm…, Mức 1 vision: mặt mộc / trang sức / xăm / tóc tối / phông trắng / vân tay CCCD).
    Helpers: `years_until/months_since/days_since/contains/any_in_text`.
  - `vision_check.py` ⭐ — Mức 3 cross-photo comparison qua `gemini-2.5-pro` multi-image.
    `compare_portraits(anh_the, doc, doc_type)` so 2 ảnh chân dung → trả `{same_person, confidence,
    age_diff_months, phau_thuat_signs, anomalies}`. `find_compare_pairs(dataset)` tìm tối đa 3 cặp
    (Anh thẻ × Passport > GPLX > CCCD). Cache SHA-1. Result inject vào `eval_input._vision_compare`
    làm ground-truth cho LLM tầng 2 (pattern giống `_dia_gioi`).
  - `sop_naming.py` — doc-type classification + the SOP filename builder (`<Tag>[ relation][ idx]-<Subject>[_ENG].ext`).
    `DOC_TYPE_PATTERNS` + `FILENAME_HINTS` + `RELATION_MAP` giờ derive từ `data/*.yaml` lúc module-import.
  - `checklist.py` — the AI thẩm định: 2-stage LLM pipeline (cheap extract → reasoning) → a 4-part Markdown
    report written as a Google Doc, + the deterministic "điểm danh" FARM coverage (26 items / 18 required).
    Tầng 2: chạy `rule_engine` pre-check trước → đưa "LỖI BOT ĐÃ PHÁT HIỆN" vào prompt → LLM chỉ làm
    cross-validation + viết báo cáo (giảm false-negative 30-50%).
  - `chat.py` — the Q&A "visa officer": `answer_question()` with one-shot mechanisms `NEED_FILE` / `NEED_ADDR`
    (tra `diadia.py`) / `NEED_WEB` / `NEED_RENAME`; the case context also carries a `_dia_gioi` block (đã tra
    sẵn địa giới mọi địa chỉ trong hồ sơ → LLM coi là ground-truth, không gọi tên cũ↔mới của cùng nơi là "mâu
    thuẫn"); `linkify_answer()` (doc-name → clickable Telegram link); `do_rename()` (renames a Drive file + its
    `.json`/`.md` sidecars). Yêu cầu LINK/URL/đường dẫn cho file cụ thể đi qua helper deterministic
    `_try_link_intent()` (bypass LLM, trả thẳng filenames để `linkify_answer` wrap `<a>`); `_OFFICER_SYSTEM`
    cũng được dạy rằng "dẫn link / gửi link / URL" → lặp tên file Y NGUYÊN, mỗi tên 1 dòng.
  - `drive_helpers.py` — Google Drive API wrappers (folder cache; upload/list/find/delete/rename/replace).
    **All Drive calls run on the asyncio event loop, never in a thread** (the httplib2 client isn't thread-safe).
  - `google_clients.py` — Drive/Sheets API client init.
  - `diadia.py` — tra cứu địa giới hành chính VN cũ↔mới (cải cách 2025) — deterministic, đọc từ `data/admin/`:
    `resolve_address(text)` · `same_place(a,b)` · `commune_merge_info(name)`. Dùng bởi `checklist.py` (gắn
    `profile["_dia_gioi"]` làm ground-truth cho tầng 2) và `chat.py` (cơ chế `NEED_ADDR`). Không phải HTTP service.
- **`data/`** — config data (DATA-DRIVEN sprint hoàn tất):
  - ⭐ **`rules.yaml`** — 26 checklist FARM + 63 validation rule v1.1 (HƯỚNG DẪN CHECK HỒ SƠ).
    Add/sửa rule mới: edit YAML, restart bot (~2-5 phút, không cần edit Python).
  - ⭐ **`doc_types.yaml`** — 32 loại giấy tờ với description + regex patterns.
  - ⭐ **`relations.yaml`** — 8 quan hệ nhân thân (bo/me/vo/chong/con/ông bà/anh chị em/cô dì chú bác).
  - `provinces_34.json` (34 đơn vị cấp tỉnh + map tỉnh cũ→mới + ngày hiệu lực; dùng bởi `checklist.py`),
    `customer-folder-structure.json` (tham khảo: 4 thư mục top + thư mục con),
  - **`data/admin/`** — bảng địa giới hành chính cho `diadia.py`: `province_new.json` (34 tỉnh), `ward_new.json`
    (~3.321 xã/phường), `old_to_new_wards.json` (10.358 dòng map xã cũ→mới, từ `admin_mapping_old_to_new.xlsx`),
    `_convert_xlsx.py` + `SOURCES.md` (nguồn: VietMap — xem `SOURCES.md`).
- **`tests/`** — golden test cho rule_engine: `tests/golden/sample_case.yaml` (mock profile + expected rule
  codes) + `tests/run_golden.py` (runner — chạy deterministic eval, 0 LLM call, <1s).
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
python3 telegram_listener.py --self-test                                 # group-title parser self-test
python3 scan_pipeline.py --self-test                                     # SOP-naming self-test
python3 lib/checklist.py && python3 lib/chat.py                           # the "tests" — each prints OK
python3 lib/rule_loader.py && python3 lib/rule_engine.py                 # data-driven loader + engine
python3 tests/run_golden.py                                              # golden test (rule_engine)
python3 scan_pipeline.py <some.zip> --dry-run --applicant Test --manifest /tmp/m.json   # no Drive writes
sudo systemctl restart donghanhbot && journalctl -u donghanhbot -f       # run / tail the bot
```

## Add rule mới (data-driven)
```bash
# 1. Edit YAML
vi data/rules.yaml          # thêm rule kiểm tra giấy tờ
vi data/doc_types.yaml      # thêm loại giấy tờ mới
vi data/relations.yaml      # thêm quan hệ nhân thân

# 2. Validate
python3 lib/rule_loader.py  # schema check + load count
python3 tests/run_golden.py # rule_engine vẫn detect đúng

# 3. Apply
sudo systemctl restart donghanhbot
```
