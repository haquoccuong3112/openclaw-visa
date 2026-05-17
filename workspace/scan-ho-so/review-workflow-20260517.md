# Đánh Giá Hiệu Suất Workflow — Đồng Hành Bot
**Case thử nghiệm:** Hoàng Thị Mơ Test11 2007 — WP10M  
**Ngày chạy:** 17/05/2026 11:34 → 11:53  
**Input:** 40 files gốc từ `runs/hoàng-thị-mơ-test11--wp10m/20260517-1134/input/`  
**Local archive:** `runs/hoàng-thị-mơ-test11--wp10m/20260517-1134/`

---

## 1. Tóm Tắt Kết Quả Pipeline

| Chỉ số | Giá trị |
|--------|---------|
| Files đầu vào | 40 |
| Uploaded mới | 4 |
| Duplicate (tên) | 8 |
| Duplicate (SHA-1 hash) | 28 |
| Failed | 0 |
| Vision compare pairs | 3 |
| Tài liệu bắt buộc có | **15 / 18** |
| Thẩm định model | gpt-5-mini |
| Thời gian chạy | ~19 phút |

Không có file nào thất bại. SHA-1 dedup hoạt động đúng — phần lớn file đã upload từ lần trước được bỏ qua chính xác.

---

## 2. Phân Loại & Đặt Tên (Classification & Naming)

### ✅ Đúng

| File gốc | Tên SOP | Nhận xét |
|----------|---------|----------|
| `CCCD.pdf` | `CCCD-Hoang Thi Mo.pdf` | Chính xác |
| `ho chieu.pdf` | `Passport-Hoang Thi Mo.pdf` | Chính xác |
| `BANG TN.pdf` | `Bang cap-Hoang Thi Mo.pdf` | Chính xác |
| `BIA DAT.pdf` | `So dat-Hoang Thi Mo.pdf` | Chính xác — phân biệt với `bia đất.pdf` |
| `GIAY KS.pdf` | `GKS-Hoang Thi Mo.pdf` | Chính xác (Trích lục khai sinh) |
| `20260415141134708.pdf` | `LLTP-Hoang Thi Mo.pdf` | Chính xác |
| `DK XE.pdf` | `Ca vet xe khac-Nguyen Ba Thang.pdf` | Chính xác (cà vẹt xe) |
| `photo_1778991799936.jpg` | `Anh the-Hoang Thi Mo Test11 2007.jpg` | Chính xác |
| `MO-09222018230831.pdf` | `XNCT-Hoang Thi Mo.pdf` | Chính xác (XNCT) |

### ⚠️ Cần Xem Lại

| File gốc | Tên SOP | Vấn đề |
|----------|---------|--------|
| `bia đất.pdf` | `XN hoc-Hoang Thi Mo.pdf` | **Tên file sai** — nội dung thực sự là giấy XN học, không phải sổ đỏ (đã debug trước). Bot phân loại đúng theo nội dung, nhưng tên file gốc misleading. Không phải lỗi bot. |
| `giấy chứng nhận kinh doanh HTX.pdf` | `DKKD khac-Tran Thi Nguyet.pdf` | Đặt tên với tên người khác (Trần Thị Nguyệt thay vì HTX Vĩnh Tân). Cần xem xét logic extract tên cho giấy HTX. |
| `DKI XE.pdf` | `Ca vet xe khac-Nguyen Ba Thang 1991.pdf` | Thêm năm sinh vào tên để tránh trùng với `DK XE.pdf` → OK nhưng hơi dư. |
| `hồ sơ scan.pdf` / `hồ sơ scan (2).pdf` | `XNCT 2-Hoang Thi Mo.pdf` / `XNCT-Hoang Thi Mo.pdf` | Tên file gốc quá chung chung → bot đặt XNCT, cần verify nội dung thực. |

### 🐛 Vấn đề Name Drift trên ảnh

Nhiều file `photo_*.jpg` tương tự nhau (Anh gia dinh, Anh-video lam nong) bị phân loại thành cùng 1 tag và cùng 1 tên → pipeline phát hiện "name-drift" và re-upload. Ví dụ:

```
photo_1778991805105.jpg → re-process (name-drift): old='Anh gia dinh 3-...' rebuilt='Anh gia dinh-...'
photo_1778991805299.jpg → re-process (name-drift): old='Anh-video lam nong 5-...' rebuilt='Anh-video lam nong-...'
```

**Nguyên nhân:** Nhiều ảnh gia đình/nông nghiệp giống nhau → bot đặt cùng tên, dedup index trong một batch khác → tên cũ trên Drive có số đuôi (2,3,4...), lần re-run rebuild lại không có số → phát hiện drift → xóa file cũ + re-upload. Hành vi này đúng về mặt logic nhưng gây lãng phí API call.

---

## 3. Chất Lượng OCR

### CCCD — ★★★★★ Xuất sắc
```
Số: 040191042322 ✓
DOB: 02/09/1991 ✓
Thường trú: Khối Phúc Lộc, Vinh Tân, TP Vinh, Nghệ An ✓
MRZ trích xuất đúng ✓
```

### Hộ Chiếu — ★★★★★ Xuất sắc
```
Số: E05002793 ✓
DOB: 02/09/1991 ✓
Cấp: 08/12/2025, Hết: 08/12/2035 ✓
MRZ: P<VNMHOANG<<THI<MO... ✓
Số CCCD trong hộ chiếu: 040191042322 ✓
```

### LLTP — ★★★★★ Xuất sắc
```
Số: 19783/LLTP-HSNV ✓
Ngày cấp: 08/04/2026 ✓
Tên cha mẹ: HOÀNG VĂN THÀNH, PHAN THỊ BÍNH ✓
Vợ/chồng: NGUYỄN BÁ THẮNG ✓
Án tích: Không có ✓
```

### Sổ Đỏ — ★★★★☆ Tốt (1 hạn chế)
```
Chủ sở hữu: Nguyễn Bá Thắng (1991) + Hoàng Thị Mơ (1991) ✓
Thửa đất 1708, tờ 22, Xóm Khoa Đà, Hưng Tây, Hưng Nguyên, Nghệ An ✓
Diện tích: 181.2 m² ✓
Nguồn gốc: "Được tặng cho" — trích xuất đúng ✓
Ngày ký: "ngày 16. tháng .... năm 2023" ← THIẾU (ngày bị che trên scan)
```

### BHYT — ★★★☆☆ Trung bình
```
Mã BHYT: GD4404025156679 ✓
Hiệu lực: 26/12/2022 → 25/12/2023 ✓ (bot phát hiện đúng đã hết hạn)
Tên chủ thẻ: KHÔNG đọc được (ảnh scan không hiện tên) ← hạn chế của scan
```

### CV / Tờ khai viết tay — ★★☆☆☆ Kém (expected)
Chữ viết tay + scan độ phân giải trung bình → OCR đọc sai tên (Nguyễn Bá Tháng/Thẳng thay vì Thắng). Đây là giới hạn OCR với chữ viết tay — không phải lỗi bot. Bot ghi chú đúng "không dùng làm chuẩn để bắt lỗi".

---

## 4. Vision Compare — AWS Rekognition

| Cặp | Trang tìm mặt | Similarity | Kết quả | Đúng? |
|-----|--------------|-----------|---------|-------|
| Ảnh thẻ × Passport | Trang 2 (không phải trang 0) | **99.97%** | same_person=True, confidence=high | ✅ |
| Ảnh thẻ × CCCD (Âu Thị Huyền) | Trang 0 | 0% | same_person=False, confidence=high | ✅ (đúng — người khác) |

**Điểm nổi bật:**
- Tính năng "tìm trang có mặt" hoạt động đúng: Passport thực tế có ảnh ở trang 2, bot không dùng trang 0 (trang bìa) → kết quả 99.97% thay vì có thể thất bại.
- Rekognition độ chính xác cao, latency hợp lý (~15 giây cho 3 cặp).
- `phau_thuat_signs: []` — luôn rỗng như thiết kế (Rekognition không phát hiện phẫu thuật thẩm mỹ).

**Bug nhỏ:** Cặp Ảnh thẻ × Passport xuất hiện 2 lần trong `vision_compare` của manifest (1 lần `cached=false`, 1 lần `cached=true`). Không ảnh hưởng kết quả nhưng dư dữ liệu.

---

## 5. Báo Cáo Thẩm Định

### Độ chính xác tổng thể: ★★★★☆

**Phần 1 — Giấy tờ chuẩn xác (13 mục):** Liệt kê đúng và đủ các giấy tờ hợp lệ. Ghi chú về cross-check CCCD/HC/KS/LLTP nhất quán, đúng.

**Phần 2 — Hạn dùng:** Tính toán ngày đúng:
- BHYT hết hạn 25/12/2023 → đúng (+2 năm 4 tháng)
- LLTP cấp 08/04/2026 → còn 4,5 tháng → đúng
- Passport hết 08/12/2035 → còn 9.5 năm → đúng
- CT07: phân biệt 2 CT07 khác nhau (001333 và 000889) → đúng

**Phần 3 — Lỗi phát hiện:**

| Lỗi | Mức độ | Đúng? | Ghi chú |
|-----|--------|-------|---------|
| Thiếu STK (sổ tiết kiệm) | 🔴 | ✅ | Bắt buộc FARM, chưa có trong hồ sơ |
| Thiếu sao kê ngân hàng | 🔴 | ✅ | Chỉ có ảnh thẻ, không phải sao kê |
| Thiếu thông tin 2 đại lý | 🔴 | ✅ | Mục 23 FARM checklist, không có trong hồ sơ |
| Sổ đỏ "Được tặng cho" thiếu hợp đồng công chứng | 🔴 | ✅ | Đúng theo rule [14.3] |
| BHYT hết hạn | 🟡 | ✅ | Hết từ 25/12/2023 |
| Ngày ký GCN mờ | 🟡 | ✅ | OCR không đọc được ngày |
| Tên chồng biến thể (OCR viết tay) | 🟡 | ✅ | Lỗi OCR chữ viết tay, giải thích đúng |
| 2 CT07 — cần xác định bản chính | 🟡 | ✅ | Hợp lý |
| Vision compare Âu Thị Huyền | 🟢 | ✅ | Ghi nhận đúng đây là CCCD người khác |

**Phần 4 — Tóm tắt:** Kết luận đúng — "Không thể nộp vì thiếu tài chính bắt buộc (STK + sao kê)". Thứ tự ưu tiên hành động hợp lý.

### Điểm cần cải thiện trong thẩm định:
1. Không mention rõ `STK-Hoang Thi Mo.pdf` đã có trong hồ sơ (file `XN so du-Hoang Thi Mo.pdf` xuất hiện trong Drive) — cần kiểm tra xem đây có phải sổ tiết kiệm thực sự không, hay chỉ là xác nhận số dư.
2. Report không breakdown rõ "15/18 = có gì, thiếu gì" dạng bảng — chỉ đề cập rải rác trong phần 3.

---

## 6. Local Archive

Cấu trúc lưu trữ cục bộ hoạt động đúng:

```
runs/hoàng-thị-mơ-test11--wp10m/20260517-1134/
├── input/          (40 files gốc — tên gốc, unmodified)
├── docs/           (40 file .md — OCR + AI extract per doc)
├── manifest.json   (kết quả pipeline)
└── report.md       (báo cáo thẩm định đầy đủ)
```

Có thể review toàn bộ hồ sơ offline mà không cần mở Drive.

---

## 7. Kết Luận & Đề Xuất

### Bot hoạt động tốt:
- ✅ OCR chính xác cho tất cả giấy tờ chính thức (CCCD, HC, LLTP, GKS, Sổ đỏ)
- ✅ SHA-1 dedup đúng — không upload trùng
- ✅ Vision compare với Rekognition: chính xác, tìm đúng trang có mặt
- ✅ Thẩm định phát hiện đủ 4 lỗi nghiêm trọng
- ✅ Date calculation chính xác trong report
- ✅ Local archive lưu đủ input + output

### Cần cải thiện:

| # | Vấn đề | Ưu tiên |
|---|--------|---------|
| 1 | Ảnh gia đình/nông nghiệp nhiều file → cùng tên → name-drift → re-upload lãng phí | Trung bình |
| 2 | Giấy HTX được đặt tên theo người ký thay vì tên HTX | Thấp |
| 3 | Duplicate entry trong `vision_compare` manifest (cùng cặp, cached=true lẫn false) | Thấp (bug nhỏ) |
| 4 | Report không in bảng "15/18 — có/thiếu cái nào" rõ ràng | Trung bình |
| 5 | BHYT: OCR không thấy tên — cần nhắc staff scan cả 2 mặt thẻ | Thấp |

---

*Tạo ngày: 17/05/2026 — Claude Code review tự động sau pipeline re-run*
