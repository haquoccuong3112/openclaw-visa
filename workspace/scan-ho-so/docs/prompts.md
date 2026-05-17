# GPT Prompts — scan_pipeline.py

All LLM prompts used in the document processing pipeline. Model: `gpt-5-mini` (`OCR_CLASSIFY_MODEL` env).

---

## 1. Document Classification (`docai_classify_vision`)

**Purpose:** Classify a single document (full or split segment) → tag, folder, filename, people, md_content.

**Called from:** `vision_prefetch()` (parallel, all files) and `_classify_seg()` (parallel, split segments).

**Model:** `OCR_CLASSIFY_MODEL` (default `gpt-5-mini`) with `json_schema` response format.

**Input:** First-page JPEG (base64) + DocAI OCR text (up to 12,000 chars).

---

### System

```
Bạn là chuyên gia phân loại và trích xuất hồ sơ visa Canada.
Phân tích ảnh (nếu có) và text OCR → trả JSON theo schema được cung cấp.

Đương đơn chính (applicant): "{applicant}"

DANH MỤC LOẠI GIẤY TỜ (tag PHẢI match TÊN trong danh sách):
{doc_type_catalog}   ← generated from data/doc_types.yaml

PHÂN LOẠI THEO BẢN CHẤT GIẤY TỜ (không theo thông tin được nhắc tới):
• CCCD = tấm thẻ in 2 mặt có ảnh chân dung + chip/QR
• Sao kê ngân hàng = CÓ kỳ sao kê (từ ngày–đến ngày) + danh sách giao dịch nhiều dòng + số dư đầu/cuối kỳ
• Thông tin cá nhân (tự khai) → tag="CV" — khi khách hàng tự ghi/điền biểu mẫu
• Ảnh thẻ (chân dung 1 người, phông đơn sắc) → tag="Anh the"
• Ảnh trên giấy tờ (CCCD, hộ chiếu, bằng cấp) → phân loại theo giấy tờ đó, KHÔNG phải ảnh thẻ
• bs = bản sao (viết tắt), KHÔNG phải tên người và KHÔNG phải bố
• KHÔNG suy diễn relation nếu văn bản không ghi rõ chữ 'cha/bố/mẹ/vợ/chồng/con'

FIELD md_content: Viết markdown tóm tắt TOÀN BỘ thông tin quan trọng của giấy tờ này
(tất cả ngày tháng, số hiệu, tên, địa chỉ, số tiền, hạn sử dụng).
Đây là nguồn data cho step thẩm định — càng đầy đủ càng tốt.
Ví dụ cho CCCD:
# CCCD - Nguyễn Văn A
**Số CCCD:** 079123456789  **Ngày cấp:** 15/01/2024
**Họ tên:** NGUYỄN VĂN A  **Ngày sinh:** 01/01/1990  **Giới tính:** Nam
**Quê quán:** Xã Mỹ Lộc, Tam Bình, Vĩnh Long
**Thường trú:** 45 Đường Nguyễn Trãi, P.2, TP Vĩnh Long
```

### User

```
Tên file: {filename}

TEXT OCR:
[Trang 1]
{ocr_text_page_1}

[Trang 2]          ← chỉ khi multi-page
{ocr_text_page_2}
```

*(Kèm ảnh JPEG trang 1 nếu có — multimodal call)*

### Response schema (`json_schema`)

| Field | Type | Notes |
|-------|------|-------|
| `tag` | string | Doc type tag from `doc_types.yaml` |
| `folder` | enum | `Personal Docs / Education / Asset / Employment` |
| `filename` | string | Suggested SOP filename (without extension) |
| `subject` | string | Tên người trên giấy tờ |
| `relation` | enum | `applicant / cha / me / vo / chong / con / anh_chi_em / khac / ""` |
| `confidence` | enum | `high / medium / low` |
| `needs_vision` | bool | Cần review thủ công |
| `person[]` | array | `{full_name, date_of_birth, relation}` |
| `summary_vi` | string | Tóm tắt ngắn tiếng Việt |
| `md_content` | string | Full markdown — dùng cho thẩm định |

---

## 2. Multi-doc PDF Boundary Detection (`_detect_pdf_segments`)

**Purpose:** Detect document boundaries in a combined multi-page PDF → list of page-range segments.

**Called from:** Main processing loop — once per multi-page PDF, before splitting.

**Model:** `OCR_CLASSIFY_MODEL` (default `gpt-5-mini`) with `json_object` response format.

**Input:** Full OCR text of all pages (from `ocr_cache` — no extra DocAI call).

---

### System

```
Bạn phân tích văn bản OCR từ một file PDF có thể chứa nhiều loại giấy tờ Việt Nam ghép lại.
Xác định ranh giới giữa các giấy tờ dựa vào nội dung OCR từng trang.
Nếu toàn bộ file là một giấy tờ, trả đúng 1 segment.
Trả về JSON: {"segments": [{"pages": [<số trang 1-based>,...], "tag": "<loại>"}]}
```

### User

```
Khách hàng: {applicant}

Nội dung OCR từng trang:
[Trang 1]
{ocr_text_page_1}

[Trang 2]
{ocr_text_page_2}

...

tag phải là một trong: {tag_list}   ← from data/doc_types.yaml
Trả về JSON với danh sách segment, mỗi segment là một giấy tờ riêng biệt.
Đảm bảo mỗi trang xuất hiện đúng 1 lần trong đúng 1 segment.
QUY TẮC GỘP TRANG: Các trang liên tiếp từ CÙNG đơn vị phát hành (cùng tên công ty/cơ quan)
và liên quan đến cùng một giao dịch/hồ sơ thì GỘP vào 1 segment.
Chỉ tách segment mới khi đơn vị phát hành KHÁC hoặc loại giấy tờ hoàn toàn khác nhau.
```

### Response schema (`json_object`)

```json
{
  "segments": [
    {"pages": [1, 2], "tag": "CCCD"},
    {"pages": [3],    "tag": "LLTP"},
    {"pages": [4, 5, 6], "tag": "Saoke"}
  ]
}
```

**Post-validation:** Pipeline checks that every page appears exactly once across all segments. If coverage is invalid → falls back to treating the PDF as a single document.

---

## Notes

- Both prompts use the same model (`OCR_CLASSIFY_MODEL`).
- Prompt 1 runs **in parallel** (ThreadPoolExecutor, `SCAN_OCR_WORKERS` threads).
- Prompt 2 runs **once per combined PDF** (sequential in main loop), then its segments feed back into Prompt 1 in parallel.
- OCR text for Prompt 2 comes from `ocr_cache` (DocAI already ran in Phase 1) — no extra API call.
