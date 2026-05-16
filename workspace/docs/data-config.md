# Data Config Reference — scan-ho-so

All files live under `workspace/scan-ho-so/data/`. Loaded and validated at startup by `lib/rule_loader.py`; changes take effect after `sudo systemctl restart donghanhbot`.

> See also: [`docs/pipeline-diagram.md`](pipeline-diagram.md) — how these configs flow through the pipeline at runtime.

---

## rules.yaml — Validation Rules v1.1

Schema version: 1. Two top-level sections: `checklist` and `validations`.

### Checklist (26 mục FARM)

Coverage check: "KH có những giấy gì?" Mỗi mục có `severity` xác định khi nào áp dụng:

| Severity | Ý nghĩa | Đếm vào |
|----------|---------|---------|
| `bat_buoc` | Luôn bắt buộc | X/18 mục bắt buộc |
| `ket_hon` | Chỉ áp dụng nếu KH đã kết hôn (suy từ GKH) | — |
| `co_con` | Chỉ áp dụng nếu KH có con (≥2 GKS hoặc có XN học) | — |
| `tuy_chon` | Tăng hồ sơ, không bắt buộc | — |
| `lam_sau` | Làm/bổ sung sau | — |

#### 18 mục bắt buộc (`bat_buoc`)

| Code | Tên | Tags |
|------|-----|------|
| FARM-1 | Hộ chiếu đương đơn (gồm HC cũ nếu có) | Passport |
| FARM-2 | Giấy khai sinh đương đơn + vợ/chồng | GKS |
| FARM-5 | CCCD đương đơn + vợ/chồng/con | CCCD |
| FARM-6 | Giấy xác nhận cư trú CT07 (mới nhất) | XNCT |
| FARM-7 | Lý lịch tư pháp số 2 (≤6 tháng) | LLTP |
| FARM-9 | Ảnh thẻ 5x7 (phông trắng, có bản digital) | Anh the |
| FARM-10 | Bằng cấp & chứng chỉ | Bang cap |
| FARM-11-12 | Giấy tờ tài sản (sổ đỏ HOẶC HĐ cho/tặng-thừa kế) | So dat, HD cho-tang-thua ke |
| FARM-13-14 | Chứng minh nghề nông (sổ đỏ đất NN HOẶC ĐKKD HTX) | So dat NN, DKKD |
| FARM-15 | Sổ tiết kiệm (≥300-400tr, kỳ hạn ≥6 tháng) | STK |
| FARM-17 | Sao kê ngân hàng (3-6 tháng gần nhất) | Sao ke |
| FARM-19a | Biên lai BHXH tự nguyện (3 tháng gần nhất) | BHXH |
| FARM-19b | Biên lai BHYT (3 tháng gần nhất) | BHYT |
| FARM-21 | Thông tin cá nhân & gia đình (sơ yếu lý lịch) | CV |
| FARM-22 | Thẻ Visa/Mastercard quốc tế (ảnh 2 mặt) | The Visa-MC |
| FARM-23 | Thông tin 2 đại lý nông sản/phân bón | Dai ly NS |
| FARM-25 | Ảnh chụp gia đình | Anh gia dinh |
| FARM-26 | Ảnh & video làm nông | Anh-video lam nong |

#### Mục điều kiện

| Code | Tên | Severity |
|------|-----|---------|
| FARM-3 | Giấy đăng ký kết hôn / giấy ly hôn | `ket_hon` |
| FARM-2b | Giấy khai sinh của con | `co_con` |
| FARM-4 | Giấy xác nhận con đang học | `co_con` |

#### Mục tuỳ chọn / làm sau

| Code | Tên | Severity |
|------|-----|---------|
| FARM-8 | Bằng lái xe ô tô | `tuy_chon` |
| FARM-18 | Cà vẹt xe / hoá đơn mua vàng | `tuy_chon` |
| FARM-24 | Giấy công ích / bằng khen / thư cảm ơn | `tuy_chon` |
| FARM-16 | Giấy xác nhận số dư STK (EN/song ngữ) | `lam_sau` |
| FARM-20 | Khám sức khỏe IOM | `lam_sau` |

---

### Validations (63 rules)

Mỗi rule có:
- `code` — mã v1.1 (vd `"1.1"`) hoặc `"FARM-X"`
- `severity` — `reject` | `warn` | `info`
- `applies_to` — list tags giấy tờ (hoặc `[_profile]` cho cross-doc rule)
- `condition` — Python expression eval bởi `lib/rule_engine.py` (null = LLM kiểm)
- `needs_llm` — `true` nếu cần LLM reasoning / visual / cross-validation

**17 rules có `condition`** (deterministic — chạy ngoài LLM, không thể bỏ sót):

| Code | Applies to | Condition | Severity |
|------|-----------|-----------|---------|
| 1.1 | Passport | `years_until(ngay_het_han) < 2` | warn |
| 1.6 | Passport | `noi_cap` không phải Việt Nam | reject |
| 2.4 | _profile | `parent_dob_mismatch(profile)` | reject |
| 4.3 | CCCD | `co_2_o_van_tay == false` | warn |
| 5.3 | _profile | `children_missing_from_xnct(profile)` | warn |
| 6.2 | LLTP | `months_since(ngay_cap) > 6` | reject |
| 8.2a | Anh the | `la_mat_moc == false` | warn |
| 8.2b | Anh the | `co_trang_suc == true` | warn |
| 8.2c | Anh the | `co_xam_lo == true` | warn |
| 8.2d | Anh the | `toc_toi_mau == false` | warn |
| 8.2e | Anh the | `phong_nen_trang == false` | warn |
| 10.2 | IOM | `months_since(ngay_cap) > 12` | warn |
| 12.1 | The Visa-MC | `any_in_text(summary, ['ever-link','agribank','mb hybrid','vietcombank'])` | reject |
| 13.3 | So dat | `tinh_trang_the_chap == true` | reject |
| 13.4 | So dat | `co_to_bo_sung == true` | warn |
| 14.3 | HD cho-tang-thua ke | `co_cong_chung == false` | reject |
| 19.4 | So dat NN | `years_since(ngay_cap) < 1` | reject |
| 19.6 | So dat NN | `tinh_trang_the_chap == true` | reject |
| 19.7 | So dat NN | `co_to_bo_sung == true` | warn |

**46 rules cần LLM** (`needs_llm: true`) — cross-validation, visual compare, chữ ký, mốc thời gian.

**Rules đáng chú ý:**
- `1.2` — Phẫu thuật thẩm mỹ (Mức 3 vision: `_vision_compare.phau_thuat_signs`)
- `1.3` — Chữ ký chuẩn từ HC → đối chiếu toàn hồ sơ
- `8.3` — Ảnh thẻ không trùng HC/GPLX > 6 tháng (Mức 3 vision)
- `12.1` — Thẻ NH CẤM: Ever-link VCB, Agribank, MB Hybrid

**Cách add rule mới:** Thêm entry vào `rules.yaml` → `sudo systemctl restart donghanhbot`. Schema validation tự động fail-fast nếu YAML sai.

---

## doc_types.yaml — 32 Document Types

Schema version: 1. Mỗi entry:
- `tag` — SOP tag dùng trong filename (vd `CCCD`, `So dat`)
- `folder` — `Personal Docs` | `Education` | `Asset` | `Employment`
- `description` — 1 câu giải thích (LLM dùng để classify)
- `patterns.doc_type` / `patterns.filename` — regex list
- `in_checklist` — cross-ref sang `rules.yaml`

> **Order matters:** Entry chi tiết hơn phải đứng trước (vd `So dat NN` trước `So dat`, `SYLL` trước `CV`).

### Personal Docs (22 types)

| Tag | Mô tả tóm tắt |
|-----|---------------|
| CCCD | Căn cước công dân 2 mặt + chip/QR |
| Passport | Hộ chiếu: trang bio-data + visa + mộc |
| GKS | Giấy khai sinh / trích lục khai sinh |
| GKH | Giấy đăng ký kết hôn / hôn thư |
| Ly hon | Quyết định / bản án / đơn ly hôn |
| XN hoc | Giấy XN học sinh — có logo trường + dấu đỏ + chữ ký HT |
| XNCT | CT07 xác nhận cư trú — đủ tất cả thành viên |
| LLTP | Phiếu LLTP số 2 — còn hạn ≤6 tháng |
| Hien mau | Giấy CN hiến máu — hoạt động cộng đồng |
| GPLX | Giấy phép lái xe A1/A/B1/B2/C |
| Anh the | Ảnh thẻ 5x7 phông trắng — 1 người đầu+vai |
| Medical | Giấy khám sức khoẻ (E-Medical / Medical Information) |
| IOM | IOM e-Medical — hạn ≤12 tháng |
| SYLL | Sơ yếu lý lịch CÓ DẤU MỘC UBND xã/phường |
| XN doc than | Xác nhận tình trạng hôn nhân (độc thân) |
| Trich luc HT | Trích lục cải chính / thay đổi hộ tịch |
| CV | Sơ yếu lý lịch / CV tự khai (không dấu) |
| The Visa-MC | Thẻ Visa/MC — DÙNG: Techcombank/MB Bank; CẤM: Ever-link VCB/Agribank/MB Hybrid |
| Bang khen | Giấy khen / bằng khen / thư cảm ơn / huy chương |
| Anh gia dinh | Ảnh gia đình (cưới, đi chơi, sinh nhật, lễ tết…) |
| BHXH | Biên lai BHXH tự nguyện — 3 tháng gần nhất, có dấu đỏ |
| BHYT | Biên lai / thẻ BHYT — còn hiệu lực |

### Education (3 types)

| Tag | Mô tả tóm tắt |
|-----|---------------|
| Hoc ba | Học bạ THCS/THPT — danh sách điểm, có dấu trường |
| CC tin hoc | Chứng chỉ tin học / ứng dụng CNTT (MOS, IC3…) |
| Bang cap | Bằng cấp cấp 2/3/trung cấp/CĐ/ĐH + chứng chỉ nghề |

### Asset (7 types)

| Tag | Mô tả tóm tắt |
|-----|---------------|
| So dat | Sổ đỏ/sổ hồng/GCNQSDĐ đất ở — KHÔNG thế chấp |
| HD cho-tang-thua ke | HĐ cho/tặng/thừa kế — phải công chứng, người thân hợp lệ |
| STK | Sổ tiết kiệm — ≥300tr, kỳ hạn ≥6 tháng, số dư KHÔNG tròn |
| XN so du | Xác nhận số dư STK (EN/song ngữ, quy đổi USD) |
| Ca vet xe | Cà vẹt xe ô tô/mô tô |
| Vang | Hoá đơn mua vàng — có tên đương đơn, 3-4 lần, 2-3 chỉ/lần |

### Employment (7 types)

| Tag | Mô tả tóm tắt |
|-----|---------------|
| Xac nhan DNN | Đơn XN đất nông nghiệp (NĐ 64/CP) — KHÁC sổ đỏ NN |
| So dat NN | Sổ đỏ đất nông nghiệp — chứng minh nghề nông |
| DKKD | Đăng ký kinh doanh / GCN HTX |
| Dai ly NS | Thông tin 2 đại lý nông sản/phân bón |
| Anh-video lam nong | Ảnh/video đương đơn đang làm nông, khớp sổ đỏ NN |
| Sao ke | Sao kê ngân hàng — danh sách giao dịch + số dư |
| HDLD | Hợp đồng lao động |
| HD thue MB | Hợp đồng thuê mặt bằng / nhà / đất |
| Bien lai | Biên lai thu tiền / hoá đơn bán nông sản |

---

## relations.yaml — 8 Quan Hệ Nhân Thân

Dùng bởi `extract_relation()` trong `lib/sop_naming.py` để gắn vào tên file (vd `CCCD bo-Nguyen Van A.pdf`). Trigger khớp summary OCR (đã strip dấu + lower).

| Relation slug | Nhãn | Trigger words (mẫu) |
|---------------|------|---------------------|
| `ba` | bố / cha | "cha", "bo ", "ong bo", "ba ruot" |
| `me` | mẹ | "me ", "ba me", "me ruot" |
| `vo` | vợ | "vo ", "ba xa" |
| `chong` | chồng | "chong" |
| `con` | con (trai/gái) | "con trai", "con gai", "con ruot", " con " |
| `ong ba` | ông/bà (nội/ngoại) | "ong ba", "ong noi", "ba noi", "ong ngoai", "ba ngoai" |
| `anh chi em` | anh/chị/em ruột | "anh ruot", "chi ruot", "em ruot", "anh trai", "chi gai" |
| `co di chu bac` | cô/dì/chú/bác | "co ruot", "di ruot", "chu ruot", "bac ruot" |

---

## provinces_34.json — 34 Đơn Vị Cấp Tỉnh (Sau Cải Cách 2025)

Dùng bởi `lib/checklist.py` và `lib/diadia.py`. Tra cứu cấp xã chi tiết hơn → dùng `lib/diadia.py` (đọc `data/admin/old_to_new_wards.json`, 10,358 rows).

**Ngày hiệu lực:**
- Sáp nhập cấp tỉnh: **2025-06-12**
- Sáp nhập cấp xã/phường: **2025-07-01**

### 6 Thành Phố Trực Thuộc TW

Hà Nội, Hồ Chí Minh, Đà Nẵng, Hải Phòng, Cần Thơ, Huế

### 28 Tỉnh

An Giang, Bắc Ninh, Cà Mau, Cao Bằng, Đắk Lắk, Điện Biên, Đồng Nai, Đồng Tháp, Gia Lai, Hà Tĩnh, Hưng Yên, Khánh Hòa, Lai Châu, Lâm Đồng, Lạng Sơn, Lào Cai, Nghệ An, Ninh Bình, Phú Thọ, Quảng Ngãi, Quảng Ninh, Quảng Trị, Sơn La, Tây Ninh, Thái Nguyên, Thanh Hóa, Tuyên Quang, Vĩnh Long

### old_to_new — 33 Tỉnh Cũ → Tỉnh Mới

| Tỉnh cũ | Sáp nhập vào |
|---------|-------------|
| Bà Rịa - Vũng Tàu | Hồ Chí Minh |
| Bình Dương | Hồ Chí Minh |
| Bình Phước | Đồng Nai |
| Bình Thuận | Lâm Đồng |
| Bình Định | Gia Lai |
| Bạc Liêu | Cà Mau |
| Bắc Giang | Bắc Ninh |
| Bắc Kạn | Thái Nguyên |
| Bến Tre | Vĩnh Long |
| Hà Giang | Tuyên Quang |
| Hà Nam | Ninh Bình |
| Hòa Bình | Phú Thọ |
| Hải Dương | Hải Phòng |
| Hậu Giang | Cần Thơ |
| Kiên Giang | An Giang |
| Kon Tum | Quảng Ngãi |
| Long An | Tây Ninh |
| Nam Định | Ninh Bình |
| Ninh Thuận | Khánh Hòa |
| Phú Yên | Đắk Lắk |
| Quảng Bình | Quảng Trị |
| Quảng Nam | Đà Nẵng |
| Sóc Trăng | Cần Thơ |
| TP. Hồ Chí Minh | Hồ Chí Minh |
| Thái Bình | Hưng Yên |
| Thừa Thiên Huế / Thừa Thiên - Huế | Huế |
| Tiền Giang | Đồng Tháp |
| Trà Vinh | Vĩnh Long |
| Vĩnh Phúc | Phú Thọ |
| Yên Bái | Lào Cai |
| Đắk Nông | Lâm Đồng |

> Tra cứu cấp xã/phường: `lib/diadia.py` — `resolve_address(text)`, `same_place(a, b)`, `commune_merge_info(name)`. Không dùng HTTP, load offline từ `data/admin/`.

---

## Quy tắc đặt tên file (SOP Naming)

Nguồn: `visa_canada_sop_raw.md` mục 10. Thực thi bởi `lib/sop_naming.py`.

### Format chuẩn

```
[Loại giấy tờ]-[Tên chủ thể].[ext]
```

Ví dụ:
- `CCCD-Bui Van Huan.pdf`
- `LLTP-Bui Van Huan.pdf`
- `GKS-Bui Van Huan.pdf`
- `Sao ke-Bui Van Huan.pdf`

### Nhiều file cùng loại

Thêm số thứ tự vào sau tên loại, trước dấu `-`:

- `Sao ke 1-Bui Van Huan.pdf`
- `Sao ke 2-Bui Van Huan.pdf`
- `So dat 1-Bui Van Huan.pdf`
- `So dat 2-Bui Van Huan.pdf`

### Chuẩn hóa tên

Bắt buộc:
- Không dùng dấu tiếng Việt
- Không ký tự đặc biệt
- Không khoảng trắng thừa
- Viết hoa chữ cái đầu mỗi từ (title case)
- Không dùng tên chung chung: `scan001`, `image123`, `zalo file`, `final final`, `document new`, `file moi`

Tên file phải thể hiện: loại giấy tờ + chủ giấy tờ (+ quan hệ nếu người thân) (+ `_ENG` nếu bản tiếng Anh).

### Hậu tố `_ENG`

Thêm `_ENG` vào sau tên loại khi file là bản tiếng Anh, song ngữ, hoặc bản dịch:

- `Bang cap_ENG-Nguyen Van A.pdf`
- `LLTP_ENG-Nguyen Van A.pdf`

### File người thân

Format: `[Tag] [relation]-[Tên chủ thể].[ext]`

Relation slug chèn vào giữa tag và tên (xem bảng relations.yaml bên trên):

| Quan hệ | Slug | Ví dụ |
|---------|------|-------|
| Bố / cha | `ba` | `CCCD ba-Bui Van Huan.pdf` |
| Mẹ | `me` | `CCCD me-Bui Van Huan.pdf` |
| Vợ | `vo` | `GKS vo-Bui Van Huan.pdf` |
| Chồng | `chong` | `GKS chong-Nguyen Thi A.pdf` |
| Con | `con` | `GKS con-Bui Van Huan.pdf` |
| Ông/bà | `ong ba` | `So dat ong ba-Bui Van Huan.pdf` |
| Anh/chị/em ruột | `anh chi em` | `CCCD anh chi em-Bui Van Huan.pdf` |
| Cô/dì/chú/bác | `co di chu bac` | `CCCD co di chu bac-Bui Van Huan.pdf` |

### Không suy đoán

Nếu không đủ dữ liệu để xác định chủ giấy tờ, quan hệ, hoặc bản ENG → **không tự suy đoán**. Đặt `needs_review = true` và đánh dấu để nhân viên kiểm tra.

### Bảng viết tắt

| Viết tắt | Tên đầy đủ |
|----------|-----------|
| CCCD | Căn cước công dân |
| GPLX | Giấy phép lái xe |
| GKS | Giấy khai sinh |
| LLTP | Lý lịch tư pháp |
| XNCT | Xác nhận cư trú |
| STK | Sổ tiết kiệm |
| BHYT | Bảo hiểm y tế |
| BHXHTN | Bảo hiểm xã hội tự nguyện |
| HDLD | Hợp đồng lao động |
| SYLL | Sơ yếu lý lịch |
| KN HTX | Kinh nghiệm hợp tác xã |

---

## Liên quan

- Schema validation: `lib/rule_loader.py` — fail-fast khi YAML malformed
- Deterministic rule eval: `lib/rule_engine.py` — chạy 17 conditions trước LLM
- Thay đổi bất kỳ file nào → `sudo systemctl restart donghanhbot`
