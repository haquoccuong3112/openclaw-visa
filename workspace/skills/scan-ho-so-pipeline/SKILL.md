---
name: scan-ho-so-pipeline
description: Process a batch of Đồng Hành / ALLY visa documents end-to-end — unzip the customer's .zip (or a folder of loose files), OCR + summarize each file with Gemini, rename it to the SOP convention, and upload it to the case's Google Drive folder (with .json/.md metadata sidecars). Use this whenever a customer (KH) sends a zip/files of hồ sơ that need sorting into Drive, and ALWAYS use it instead of doing the unzip/OCR/upload steps yourself with individual tool calls, because doing it by hand drops files. Triggers include "xử lý hồ sơ", "scan hồ sơ", a forwarded .zip of visa docs, "OCR và up lên Drive".
---

# Scan-hồ-sơ pipeline (consistent unzip → OCR → rename → upload to Drive)

> **This skill ships no code.** The pipeline is `~/.openclaw/workspace/scan-ho-so/scan_pipeline.py` (part of the scan-ho-so app); this file is just the procedure the agent follows. (The bot `telegram_listener.py` runs the same `scan_pipeline.py` as a subprocess.)

## Why this skill exists

Doing this task by hand (unzip with one tool call, OCR each file with another,
upload each with another) is **not reliable** — files get silently dropped:

- a transient Gemini/Drive/Telegram error on one file → that file is skipped and
  never mentioned;
- non-`pdf/jpg/png` files in the zip (`.MOV`, `.HEIC`, `.docx`, …) get filtered
  out and never uploaded — e.g. the `hoang_thi_mo3.zip` test set has
  `IMG_2483.MOV` and the old path uploaded 3 of 4 files;
- nothing ever reconciles "files in the zip" vs "files in Drive".

The script below fixes all of that: it enumerates **every** real file, retries
each one, keeps unsupported files (uploads them without OCR), writes a
**manifest** covering every input file, and exits non-zero if anything is still
failed so you know to re-run. Re-runs are safe (uploads skip by destination
name), so a partial run can always be finished by running the same command again.

## The procedure — follow exactly

1. **Get the input locally.** Download the customer's `.zip` (or the loose
   files into one directory). Note the absolute path.

2. **Resolve the case Drive folder.** Each KH↔Pro group pair has a case folder
   recorded in `~/.openclaw/workspace/scan-ho-so/group_registry.json` keyed by Telegram chat id
   (field `folder_id`, plus `applicant`, `visa`, `drive_link`). Either pass the
   chat id with `--from-registry`, or pass `--case-folder-id` + `--applicant`
   directly. If you can't determine the case folder, ask — do **not** guess and
   do **not** create a new top-level folder.

   > **Cách bot nhận diện nhóm KH/Pro** (`parse_group_title()` trong `telegram_listener.py`):
   > phân biệt KH vs Pro bằng chữ `Pro` (case-insensitive) trong tên nhóm; hỗ trợ các prefix
   > `DH Pro` / `DongHanh` / `Đồng Hành Pro` / `Đồng Hành`, em-dash `–` và en-dash `-`, token
   > `KH` (vd `DongHanh WP2Y - KH Trần Đăng Sự 2006`). Chương trình nhận diện: `WP\d+[mMyY]?`
   > (WP10m, WP2Y…), `HighSkilled`, `FARM`, cùng các code visa truyền thống (SP/VP/PR/SUV/TRV/…).
   > Pair KH↔Pro qua `(applicant.lower(), visa.upper())` — 2 nhóm phải đặt cùng tên KH + visa.
   > Nếu tên thiếu năm sinh hoặc thiếu hẳn → vẫn đăng ký nhóm, ô tên/năm sinh trong sheet để
   > trống (visa vẫn bắt buộc). Self-test: `python3 telegram_listener.py --self-test`.

3. **Run the pipeline:**

   ```bash
   python3 ~/.openclaw/workspace/scan-ho-so/scan_pipeline.py "<ZIP_OR_DIR>" \
       --from-registry <TELEGRAM_CHAT_ID> \
       --manifest /tmp/scan_manifest.json
   ```

   or, without the registry:

   ```bash
   python3 ~/.openclaw/workspace/scan-ho-so/scan_pipeline.py "<ZIP_OR_DIR>" \
       --case-folder-id <DRIVE_FOLDER_ID> --applicant "Hoang Thi Mo" \
       --manifest /tmp/scan_manifest.json
   ```

   Useful flags: `--dry-run` (enumerate + classify-by-filename only, no Gemini,
   no Drive writes — use it first if you just want to see what's in the zip),
   `--retries N` (default 3), `--case-id <str>` (metadata label),
   `--self-test`.

4. **Check the manifest — this is the consistency gate.** Read
   `/tmp/scan_manifest.json`. It has `total_input_files`, `counts`
   (`uploaded`, `uploaded-no-ocr`, `duplicate`, `failed`), `ok` (true ⇔ nothing
   failed), and an `items` array with one entry per input file
   (`src_name`, `new_name`, `folder`, `status`, `drive_link`, `needs_review`,
   `error`). The script also exits `0` only when `failed == 0`.

5. **If anything failed, re-run the exact same command** (up to ~2 more times).
   Already-uploaded files are detected and skipped; only the unfinished ones are
   retried. If files still fail after that, report them explicitly with their
   error — never report the batch as "done" while `failed > 0`.

6. **Report honestly.** Tell the user: how many files were in the zip, how many
   uploaded, how many were already there (`duplicate`), how many uploaded
   without OCR (`uploaded-no-ocr` — videos/docs that need a human to rename
   properly), how many still failed, and the list of `needs_review` files.
   Include the case Drive link from the registry. Do not claim every file was
   processed unless `items` length == `total_input_files` and `failed == 0`
   (the script asserts the first and you must check the second).

## What the script guarantees

- Walks `.zip` / directory **recursively**; skips only `__MACOSX/`, `._*`,
  `.DS_Store`. Everything else is processed.
- `pdf/jpg/jpeg/png` → Gemini OCR/understanding (run **in parallel** across files —
  `SCAN_OCR_WORKERS` threads, default 5; classify + Drive upload + thẩm định stay
  sequential) → classify (SOP tag + one of
  `Personal Docs` / `Education` / `Asset` / `Employment`) → SOP filename
  (`<Tag>[ <relation>][ <index>]-<Subject>[_ENG].<ext>`, e.g.
  `CCCD-Hoang Thi Mo.pdf`). Naming/classification logic is the maintained
  `~/.openclaw/workspace/scan-ho-so/lib/sop_naming.py` (single source of truth — don't reimplement).
  > ⚠️ **Phân loại theo bản chất giấy tờ, không theo trường nó nhắc tới.** Một tờ giấy do khách **tự khai /
  > viết tay / tự điền** thông tin cá nhân (họ tên, số CCCD, địa chỉ, người thân…) là tag **`CV`** ("Thông tin
  > cá nhân / sơ yếu lý lịch" — mục 21 checklist FARM), KHÔNG phải `CCCD` — `CCCD` chỉ là tấm thẻ Căn cước công
  > dân thật (2 mặt, có ảnh, chip/QR). Tương tự: `Hộ chiếu`/`Sổ tiết kiệm`/… chỉ cho giấy tờ thật, không phải
  > vì file nhắc tới số/tên đó. Nếu một file vẫn bị phân loại sai, đổi tên về `CV-<Họ Tên>.ext` (hoặc
  > `Khac-<Họ Tên>.ext` nếu không hợp `CV`) — giữ đúng quy tắc `<Loại>-<Họ Tên>.ext`, đừng đặt tên tự do
  > kiểu "Thông tin cá nhân KH gửi.jpg". (`scan_pipeline.py` đã được chỉnh để Gemini + `classify_doc_type`
  > tự nhận diện tờ tự khai → `CV` với cờ ⚠️ needs_review, nhưng vẫn nên rà lại.)
  > Phân biệt ảnh (chỉ khi **cả file LÀ một tấm ảnh** — ảnh chân dung in TRÊN CCCD/hộ chiếu/bằng cấp thì phân theo
  > giấy tờ đó, KHÔNG phải `Anh the`): **ảnh chân dung CHÍNH THỨC kiểu ảnh dán hồ sơ** (1 người, đầu+vai, phông đơn
  > sắc trắng/xanh, nhìn thẳng, không cảnh vật) → tag **`Anh the`** (mục 9 FARM "Ảnh thẻ 5x7"); **người đang làm
  > việc / làm nông / ở vườn-ruộng-nhà kính** (dù thấy mặt) → `Anh-video lam nong` (mục 26); **ảnh nhiều người /
  > gia đình / tiệc** → `Anh gia dinh` (mục 25). Gemini gắn cờ `extracted.la_anh_the` cho ảnh thẻ riêng lẻ;
  > `classify_doc_type` chỉ tin cờ đó sau khi đã loại trừ doc_type / tên file đã chỉ rõ loại giấy tờ.
- Other extensions (`.mov`, `.heic`, `.docx`, …) are still **uploaded**
  (classified from the filename, flagged `needs_review`) so nothing is lost.
- Each file: up to `--retries` attempts with exponential backoff on any error.
- `.json` + `.md` metadata sidecars go to `_Bot OCR & Metadata` inside the case
  folder. A sidecar failure is recorded but does **not** fail the file.
- A `manifest.json` is always written, with one entry per input file.

## AI thẩm định hồ sơ (sau OCR)

After the OCR/upload loop, if the batch contains at least one document whose tag is in the
**CHECKLIST HỒ SƠ FARM** (auto-computed `CHECKLIST_DOC_TAGS` ≈ all tags referenced by `REQUIRED_DOCS`
— `Passport`, `CCCD`, `GKS`, `GKH`, `XN hoc`, `XNCT`, `LLTP`, `GPLX`, `Anh the`, `Bang cap`, `BHXH`,
`BHYT`, `IOM`, `CV`, `The Visa-MC`, `Bang khen`, `Anh gia dinh`, `So dat`, `So dat NN`,
`HD cho-tang-thua ke`, `STK`, `XN so du`, `Sao ke`, `Ca vet xe`, `Vang`, `DKKD`, `Dai ly NS`,
`Anh-video lam nong` — auto-debounce), the script runs an **AI thẩm định** over the *whole case*
(it re-reads every `.json` sidecar in `_Bot OCR & Metadata`) — a **2-stage dual-model pipeline**
to keep cost down:

- **Stage 1 — extract & normalize** (`CHECKLIST_EXTRACT_MODEL`, default cheap `google/gemini-2.5-flash`):
  reads the per-file `summary`+`extracted` of every doc → one compact normalized **profile JSON**
  (`personal_info / passport / criminal_record / residence_ct07 / marriage / children / financial /
  insurance / documents[] / visual_flags / notes`), keeping every value **verbatim** (no summarizing)
  and pushing every suspected typo/variant into `notes`. (`extract_profile_data()` in checklist.py;
  uses OpenRouter `response_format: json_object`.)
- **Stage 2 — evaluate business logic** (`CHECKLIST_MODEL`, default `google/gemini-2.5-pro`; falls
  back to `CHECKLIST_FALLBACK_MODEL` = `google/gemini-2.5-flash` on a bad id / transient error):
  feeds the small profile JSON (not the bulky raw sidecars) + Cường's thẩm định prompt (vai trò
  chuyên viên thẩm định LMIA · checklist Phần 1/2/3/4 · 34 đơn vị hành chính hiệu lực 12/06/2025) →
  a **free-text Markdown report** (4 parts: BÁO CÁO THẨM ĐỊNH · ✅ PHẦN 1 chuẩn xác · ⏰ PHẦN 2
  sắp/đã hết hạn · ⚠️ PHẦN 3 điểm mâu thuẫn cần làm rõ · 📌 PHẦN 4 tóm tắt & khuyến nghị), written
  like a human reviewer. (`evaluate_profile_logic()`.) If Stage 1 fails, Stage 2 falls back to the
  raw trimmed dataset — the pipeline never breaks. **Data-driven (Phase 6 sprint)**: 26 mục FARM
  + 63 rule kiểm tra v1.1 (HC/CCCD/LLTP/sổ đỏ thế chấp/sổ đất NN cấp <1 năm/NH cấm…) ở
  `data/rules.yaml`; bot chạy `lib/rule_engine.py` **deterministic pre-check** 11 rule có condition
  TRƯỚC khi gọi LLM (thế chấp/hết hạn/NH cấm…) — kết quả đưa vào prompt section "⚠️ LỖI BOT ĐÃ
  PHÁT HIỆN" để LLM tin tưởng đưa vào báo cáo PHẦN 3 với mã code `[13.3]`/`[19.4]`. Add rule mới:
  edit YAML, restart bot — không cần code Python.
- Creates / overwrites a **Google Doc** `Bao cao tham dinh - <Applicant>` at the **root of the
  case folder** (the Markdown is converted to a real Google Doc; an appendix is appended: the
  **"Điểm danh hồ sơ theo CHECKLIST FARM"** table — all 26 FARM items with status `✅ đã có / ❌ THIẾU /
  — không áp dụng / — chưa có (tùy chọn) / — sẽ làm sau`, denominator = **18 mục bắt buộc** — plus the
  list of OCR'd files). The điểm danh is **deterministic** (`compute_coverage()` in checklist.py — matches
  doc tags against `REQUIRED_DOCS`), runs on every thẩm định (even when the eval LLM errors), and is also
  injected into the Stage-2 prompt as ground truth + shown in the Telegram summary.
- Adds a `checklist` block to the manifest: `{ran, model (=eval model), extract_model, n_docs,
  coverage:{have,required,missing,...}, report_link (= doc_link = md_link), report (the Markdown text),
  profile (the Stage-1 JSON, or null on fallback), error}`.
- This step is wrapped — a thẩm định failure **never** affects OCR/upload; it just records
  `"checklist": {"ran": false, "error": ...}`.

**Địa giới hành chính (cải cách 2025)**: trước Stage-2, `run_and_write()` chạy `lib/diadia.py` (tra cứu
DETERMINISTIC từ bảng cũ↔mới ở `data/admin/`, tới cấp xã/phường) trên mọi địa chỉ trong hồ sơ → gắn block
**`_dia_gioi`** vào JSON hồ sơ (mỗi địa chỉ: `don_vi_moi`, `la_ten_cu`, `do_tin`, `ghi_chu`; và `doi_chieu`
giữa các cặp địa chỉ: `same`/`different`/`unknown`). Stage-2 prompt coi `_dia_gioi` là **ground-truth** — KHÔNG
tự dò lại: hai địa chỉ text khác nhau nhưng cùng đơn vị mới / `doi_chieu`=`same` → KHÔNG báo mâu thuẫn; giấy
cấp sau mốc cải cách (tỉnh 12/06/2025, xã 01/07/2025) mà ghi đơn vị `la_ten_cu` → lỗi; `do_tin`=`unknown`/
`fuzzy` → tự đánh giá thêm. Dữ liệu địa giới = repo VietMap (xem `data/admin/SOURCES.md`: dùng offline tự do;
KHÔNG sửa-rồi-phát-hành-lại). `lib/diadia.py` cũng cấp `resolve_address` / `same_place` / `commune_merge_info`
cho cơ chế chat `NEED_ADDR` (xem mục "Bot chat" dưới).

**Quy tắc thẩm định — vài chỗ hay bị báo lỗi sai (đã làm rõ trong prompt)**: (a) **chủ hộ trên CT07 ≠ vợ/chồng đương
đơn** (chủ hộ có thể là bố/mẹ ruột, bố/mẹ chồng-vợ, anh/chị/em…) → đừng báo "mâu thuẫn tên chồng/vợ" chỉ vì tên chủ
hộ khác tên vợ/chồng ghi trên giấy khác; (b) đương đơn là **người yêu cầu** trên CT07 — KHÔNG có tên đương đơn trong
bảng "các thành viên khác trong hộ" là **bình thường**, không làm giấy "vô giá trị"; (c) **bỏ hẳn** quy tắc "thiếu
Giấy xác nhận số CMND 9 số ↔ CCCD 12 số" (không còn dùng CMND — có cả hai số là chuyện bình thường, không báo lỗi);
(d) mỗi giấy đầu vào kèm `confidence`/`needs_review` (scan mờ / viết tay / phân loại chưa chắc) — khác biệt nhỏ giữa
một giấy `needs_review`/tự khai/viết tay và một giấy CHÍNH THỨC → ghi 🟢/🟡 "cần đối chiếu bản gốc", KHÔNG phải lỗi 🔴;
một tờ tự khai có ghi số CCCD vẫn KHÔNG phải CCCD (xem `la_to_khai` ở bước OCR → `classify_doc_type` gắn tag `CV`).

Logic/prompts live in `~/.openclaw/workspace/scan-ho-so/lib/checklist.py` (single source of truth — don't reimplement;
Stage-2 prompt = `CHECKLIST_PROMPT_TEMPLATE`, Stage-1 prompt = `_PROFILE_EXTRACT_SYSTEM`; địa giới = `build_dia_gioi()`).
Orchestrator = `run_and_write()` (≈ `process_lmia_dossier`). Flags: `--no-checklist` (skip it
entirely), `--checklist-only` (skip enumerate/OCR/upload; just (re)run the 2-stage thẩm định for the
case — `INPUT` not required, use `--from-registry` or `--case-folder-id` + `--applicant`; used by the
bot's `/check` command).

## Bot chat — hỏi-đáp về hồ sơ KH (`~/.openclaw/workspace/scan-ho-so/lib/chat.py`)

The bot also answers staff questions about a case, **as a professional Canadian visa officer** — kỹ càng,
chính xác, **không nịnh** — using Gemini. **Where**: in a case's **Pro group** (only when the message
**@mentions the bot** or is a **reply to a bot message** — plain chitchat is ignored), or via **DM** with
the bot (staff asks about "the cases I'm assigned to"). Never answers chat in a **KH group** (the customer
is there). **Access control**: Pro group = whoever is in the group is authorized for that case (the bot only
ever loads that case's context); DM = the sender must be a known staff (Master Staff sheet / `STAFF_TELE_IDS`)
and only gets cases where `reg[pro_chat_id]["staff"]` contains them — other cases are refused. **DM — chọn case**:
`pick_case_for_dm()` khớp câu hỏi với **token tên KH** (đã bỏ stopword) — gõ phần phân biệt là đủ (`test8` → "Hoàng
Thị Mơ TEST8 1991"), không cần gõ đủ tên; bot **không liệt kê** danh sách KH, chỉ hỏi lại / liệt kê đúng mấy case khi
staff gõ thứ trùng nhiều case; câu hỏi tiếp theo bám case đang mở trong phiên DM. **Data**: the
case's OCR'd sidecar `.json` data (each with its `drive_link` so the bot can hand staff the link to a specific
file) + the case-folder Drive link + the latest thẩm định Google Doc (exported as text) + the FARM điểm danh
table + a **`_dia_gioi` block** (every address in the case already resolved old↔new via `lib/diadia.py` — the
LLM treats it as ground-truth, so it won't call an old-name vs new-name of the *same* place a "contradiction"
even when a stale báo cáo Doc still does) + an **external web search** when needed (see below) — no re-OCR of
the whole case, no extra sheets.
The bot treats almost every staff message in this scope as case-related (only refuses clearly off-topic chitchat).
**Models / tools**: chat/reasoning = `CHAT_MODEL` (default `google/gemini-2.5-pro`). Opt-in follow-up
mechanisms (the model emits exactly one line, the bot acts, then re-asks — max 1 round each):
(1) `NEED_FILE: <name>` → bot re-OCRs that one file with `CHAT_SCAN_MODEL` (`google/gemini-2.5-flash-lite`,
cached 30') for verbatim/deep detail; (2) `NEED_ADDR: <đơn vị / địa chỉ>` → bot tra `lib/diadia.py` (bảng địa
giới chính thức cũ↔mới, `data/admin/`) — dùng cho mọi câu hỏi "xã/phường/tỉnh X giờ là gì / có bị sáp nhập
không" (deterministic, tức thì, không tốn token, KHÔNG dùng `NEED_WEB` cho việc này); (3) `NEED_WEB: <query>`
→ external web search via the OpenRouter web plugin on `CHAT_WEB_MODEL` (`google/gemini-2.5-flash`,
`CHAT_WEB_MAX_RESULTS`=4, cached 60') — only for genuinely-external info that ISN'T administrative-boundary
(new regulations/forms…); (4) `NEED_RENAME: <cũ> => <mới>` → đổi tên file (hỏi xác nhận trước). (None of
these for off-topic chitchat.)
**Yêu cầu LINK** ("dẫn link / gửi link / URL / đường dẫn / send link" + nhắc tên file): được xử lý
**deterministic** trước khi gọi LLM bằng `_try_link_intent()` — match noun (`link|url|liên kết|đường dẫn`) +
verb (`dẫn|gửi|cho|send|đưa|lấy|mở|xem…`) trong câu hỏi, đối chiếu tên file vs `name_to_link`, trả thẳng
danh sách filenames (1/dòng, KHÔNG kèm chú thích) → `linkify_answer` wrap `<a href="drive_link">`. Không
tốn token, không lệ thuộc LLM compliance. Trường hợp mơ hồ (câu chỉ có "dẫn link giúp" không nêu tên file)
fall-through xuống LLM bình thường.
Case context cached per case (TTL 10', invalidated after each scan / `/check`); per-user cooldown
`CHAT_USER_COOLDOWN` (3s) + `CHAT_CONCURRENCY` (4) semaphore. **Threading**: only the OpenRouter calls run
in `asyncio.to_thread` (their own `httpx.Client`); all Google-Drive calls run on the event loop directly
(the shared `httplib2`-based Drive client is NOT thread-safe — running it in a worker thread alongside the
main thread's Drive use segfaults the process). Handler `on_chat_message` in `telegram_listener.py`
(registered `group=2`, `filters.TEXT & ~COMMAND`).

## Lệnh `/oldfile` — xử lý hồ sơ cũ đã có sẵn trên Drive

Với khách hàng đã có 1 đống hồ sơ trên Drive **trước khi bot join Telegram**, dùng lệnh `/oldfile` trong
nhóm **Pro** để chạy chính pipeline trên những file đó. Mỗi case có subfolder `<case>/Old File/` (bot
tự tạo lúc setup, cho case cũ thì lazy-create khi `/oldfile` chạy lần đầu). Flow: staff kéo file (hoặc
`.zip`) vào `Old File/` trên Drive → gõ `/oldfile` ở nhóm Pro → bot ack `📥 Đang xử lý N file…` → download
về tempdir → unzip → `run_scan_pipeline` (cùng subprocess như Telegram batch) → invalidate cache chat →
post summary + ✅ AI checklist lên Pro group → move file gốc từ `Old File/` sang `Old File/_processed/`
(giữ bản gốc, tránh reprocess). Per-case lock `_OLDFILE_LOCKS` chống double-fire; pipeline dedup nên
retry idempotent. Quota Drive: 0 call idle, chỉ chạm Drive khi staff bấm lệnh. Handler
`on_oldfile_command` đăng ký qua `CommandHandler("oldfile", …)`.

## Drive whitelist (must respect)

The case folder lives inside the bot's sandbox (`OpenClaw` folder, id
`1VUpoBV3fAudONv5mMFXYguRThKfOLyz7`, under `Bot folder` in the shared drive
`ALLY PROCESSING`). The script only ever creates folders/files **under the case
folder id you pass it** and never lists or modifies anything outside it. Don't
point it at any folder id outside that sandbox.

## Environment

Reads `~/.openclaw/workspace/scan-ocr.env` automatically: needs `OPENROUTER_API_KEY` (Gemini via
OpenRouter) and `GOOGLE_APPLICATION_CREDENTIALS` (service-account JSON with
Drive scope — the thẩm định step creates a Google Doc). Override
the OCR model with `GEMINI_MODEL` (default `google/gemini-2.5-flash-lite`),
the thẩm định **Stage-1 extract** model with `CHECKLIST_EXTRACT_MODEL` (default
`google/gemini-2.5-flash`), and the **Stage-2 reasoning** model with `CHECKLIST_MODEL`
(default `google/gemini-2.5-pro`; falls back to `CHECKLIST_FALLBACK_MODEL`, default
`google/gemini-2.5-flash`, on a bad model id or transient error). Override
library/env locations with `SCAN_HO_SO_DIR`, `SCAN_OCR_ENV`, `SHARED_DRIVE_ID`
if needed. The checklist requires `~/.openclaw/workspace/scan-ho-so/lib/checklist.py` and (optionally)
`~/.openclaw/workspace/scan-ho-so/data/provinces_34.json`. Python deps: `google-api-python-client`,
`google-auth`, `httpx` (already installed on this host).

## Quick check

```bash
python3 ~/.openclaw/workspace/scan-ho-so/scan_pipeline.py --self-test
python3 ~/.openclaw/workspace/scan-ho-so/scan_pipeline.py <some.zip> --dry-run --applicant "Test" --manifest /tmp/m.json
# re-run only the checklist for a case (no OCR/upload):
python3 ~/.openclaw/workspace/scan-ho-so/scan_pipeline.py --checklist-only --from-registry <CHAT_ID> --manifest /tmp/m.json
```
