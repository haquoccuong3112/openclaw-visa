# SOP VẬN HÀNH BOT XỬ LÝ HỒ SƠ KHÁCH HÀNG

Nguồn: Cường gửi qua Telegram, 2026-05-09.
Trạng thái: Đã nhận phần đầu, nội dung dừng ở mục 11.4 “nhận diện tên chủ thể”. Cần Cường gửi tiếp phần còn lại nếu còn.

---

## 1. Mục tiêu của bot

Bot dùng để hỗ trợ vận hành hồ sơ khách hàng theo mô hình nhóm Telegram + Google Drive + Google Sheets, và mở rộng sau này sang Bitrix.

Bot có nhiệm vụ:

- theo dõi case qua nhóm Telegram
- nhận file khách gửi
- tạo cấu trúc lưu trữ hồ sơ
- OCR / nhận diện / đổi tên file
- sắp xếp file vào đúng folder
- cập nhật dữ liệu case vào sheet tổng và sheet cá nhân nhân viên
- check checklist mức 1 (đủ / thiếu)
- check checklist mức 2 (phân tích sâu) theo bộ câu hỏi sẽ bổ sung sau
- trả lời nhân viên qua DM

Bot không phải là bot chat công khai với khách trong nhóm. Bot chủ yếu xử lý nền và làm việc riêng với nhân viên nội bộ qua DM.

---

## 2. Cấu trúc nhóm cho mỗi case

Mỗi case có thể có 2 loại nhóm:

### 2.1. Nhóm có khách hàng

Là nhóm có:

- khách hàng
- bot
- có thể có nhân viên liên quan

Ví dụ:

- Nguyễn Ngọc Linh Chi 2007 WP10m - A Hồng

### 2.2. Nhóm làm việc nội bộ

Là nhóm gồm:

- nhân viên
- bot

Ví dụ:

- DH Pro WP10m - Nguyễn Ngọc Linh Chi 2007

---

## 3. Cách bot nhận diện case

Khi được thêm vào nhóm, bot phải:

1. nhận diện đây là một case mới hoặc case đang vận hành
2. đọc tên nhóm
3. đọc danh sách thành viên nhóm
4. đọc nội dung chat liên quan
5. đọc file được gửi trong nhóm
6. suy ra thông tin nền của case:
   - tên khách
   - năm sinh
   - chương trình
   - nhân viên liên quan
   - chat id của case nếu cần lưu

Bot được phép dùng:

- tên nhóm
- file khách gửi
- thông tin trong tài liệu
- sheet hiện có

để đối chiếu và nhận diện case.

---

## 4. Nguyên tắc xác định quyền truy cập case

### 4.1. Logic quyền

Bot xác định nhân viên nào được quyền truy cập case dựa trên thành viên nhóm làm việc của case đó.

Rule:

- nhân viên có mặt trong nhóm làm việc của case => được quyền access case đó
- nhân viên không có trong nhóm => không được quyền access case đó

### 4.2. Ý nghĩa

Nhóm chính là nguồn xác định quyền:

- ai được hỏi bot về case
- ai được bot báo cáo
- ai được xem hồ sơ case
- ai được ghi nhận vào sheet cá nhân
- ai sẽ được cấp quyền Drive trong phase sau

---

## 5. Cách nhân viên làm việc với bot

### 5.1. Trong nhóm

Bot có mặt trong nhóm để:

- đọc ngữ cảnh
- theo dõi case
- nhận file
- biết ai liên quan đến case

Bot không chủ động chat công khai trong nhóm để tránh spam.

### 5.2. Qua DM

Nhân viên sẽ làm việc trực tiếp với bot qua DM.

Bot sẽ:

- trả lời câu hỏi về case qua DM
- báo tình trạng hồ sơ qua DM
- báo kết quả xử lý file qua DM
- báo thiếu hồ sơ qua DM
- nhận lệnh xử lý bổ sung qua DM

### 5.3. Rule phản hồi

Bot không báo công khai trong nhóm.

Bot sẽ nhắn riêng cho:

- nhân viên phụ trách
- hoặc người nào hỏi bot và có quyền với case đó

---

## 6. Dữ liệu nhân viên cần có

Bot cần có bảng nhân viên để map quyền và cập nhật dữ liệu.

Mỗi nhân viên nên có:

- Telegram User ID
- Họ tên
- Role / chức danh
- Google Email
- Personal File ID

### 6.1. Mục đích

- Telegram ID: nhận diện đúng người trong nhóm
- Role: biết người đó là sales / processing / manager / director...
- Google Email: dùng cho phân quyền Drive sau này
- Personal File ID: dùng để cập nhật sheet riêng của nhân viên

---

## 7. Google Sheets – cấu trúc dữ liệu

### 7.1. Có 1 file tổng

Có 1 spreadsheet tổng lưu toàn bộ case.

Ví dụ:

- ALLY - Quản Lý Hồ Sơ

### 7.2. Có file riêng của từng nhân viên

Mỗi nhân viên có 1 file spreadsheet riêng, được lưu trong cột Personal File ID trong sheet Staff.

### 7.3. Khi có case mới

Khi bot nhận diện một case mới:

1. cập nhật case vào file tổng
2. xác định các nhân viên nội bộ có trong nhóm
3. với mỗi nhân viên đó:
   - nếu đã có file riêng → thêm / cập nhật case vào file đó
   - nếu chưa có file riêng → tạo file riêng, rồi thêm case vào

### 7.4. Nguyên tắc cập nhật sheet

Nếu trong nhóm có nhân viên A, B, C:

- bot sẽ ghi nhận case vào:
  - file tổng
  - file riêng của A
  - file riêng của B
  - file riêng của C

### 7.5. Dữ liệu sheet bot cần đọc/ghi

Sheet Cases — Bot cần đọc/ghi các trường như:

- TÊN KHÁCH HÀNG
- NĂM SINH KH
- NGÀY NHẬN HS / CF RESUBMIT
- TÊN AGENT / SALES
- LOẠI HS
- JOB
- CHƯƠNG TRÌNH
- GIAI ĐOẠN CHÍNH
- Case ID
- Visa
- Drive
- Chat ID KH
- Chat ID Pro

Sheet Staff — Bot cần đọc:

- Telegram User ID
- Họ tên
- Role
- Google Email
- Personal File ID

Sheet Documents — Bot cần ghi log:

- Case ID
- Tên file gốc
- Tên file chuẩn
- Loại giấy tờ
- Folder Drive
- Ngày nhận
- Người gửi
- Tóm tắt

---

## 8. Google Drive – nguyên tắc hiện tại

### 8.1. Năng lực hiện tại

Hiện tại bot:

- tạo folder được
- upload file được vào đúng folder có quyền
- sắp xếp file được

### 8.2. Giới hạn hiện tại

Hiện tại nên coi bot đang làm việc với:

- folder public / shared tạm
- hoặc root/folder đã cấp đúng quyền cho service account

Bot chưa coi là hoàn chỉnh phần phân quyền theo từng nhân viên ở Drive.

Phần đó sẽ bổ sung sau khi có cấu hình Google Cloud / OAuth / phân quyền phù hợp.

### 8.3. Nguyên tắc phase hiện tại

- bot tạo folder
- bot upload file
- bot sắp xếp hồ sơ
- bot chưa cần tự động share riêng tinh vi theo từng nhân viên trong phase đầu

---

## 9. Cấu trúc folder chuẩn của mỗi case

Mỗi case sẽ có cấu trúc folder như sau:

### 9.1. Personal Docs

- Hộ chiếu
- Khai sinh
- Kết hôn
- Xác nhận học của con
- CCCD
- Cư trú
- LLTP
- Bằng lái
- Ảnh thẻ
- BHXH/BHYT
- IOM
- CV
- Thẻ Visa/MC
- Bằng khen
- Ảnh gia đình

### 9.2. Education

- 10. Bằng cấp & chứng chỉ

### 9.3. Asset

- 11. Sổ đỏ tài sản
- 12. HĐ cho/tặng/thừa kế
- 15. Sổ tiết kiệm
- 16. XN số dư
- 18. Cà vẹt xe
- 19. Vàng

### 9.4. Employment

- 14. ĐKKD
- 23. Đại lý nông sản
- 26. Ảnh/video làm nông
- 13. Sổ đỏ nông nghiệp
- 17. Sao kê

### 9.5. Nguyên tắc phân loại folder

Sau khi file được nhận diện và đổi tên, bot phải đưa file vào đúng folder con tương ứng.

Bot cần phân biệt kỹ các trường hợp dễ nhầm:

- Sổ đỏ tài sản vs Sổ đỏ nông nghiệp
- Ảnh gia đình vs Ảnh/video làm nông
- CV vs giấy xác nhận kinh nghiệm
- Thẻ Visa/MC vs ảnh ngân hàng khác

---

## 10. Quy tắc đặt tên file

### 10.1. Format chuẩn

`[Tên giấy tờ]-[Tên chủ giấy tờ].[đuôi file]`

Ví dụ:

- CCCD-Bui Van Huan.pdf
- LLTP-Bui Van Huan.pdf
- GKS-Bui Van Huan.pdf
- Sao ke-Bui Van Huan.pdf

### 10.2. Nếu nhiều file cùng loại

Thêm số thứ tự:

- Sao ke 1-Bui Van Huan.pdf
- Sao ke 2-Bui Van Huan.pdf
- So dat 1-Bui Van Huan.pdf
- So dat 2-Bui Van Huan.pdf

### 10.3. Quy tắc chuẩn hóa

Bắt buộc:

- không dùng dấu tiếng Việt
- không ký tự đặc biệt
- không khoảng trắng thừa
- viết hoa chữ cái đầu mỗi từ
- giữ format đồng nhất

Không dùng tên kiểu:

- scan001
- image123
- zalo file
- final final
- document new
- file moi

Tên file phải thể hiện:

1. loại giấy tờ
2. chủ giấy tờ
3. quan hệ nếu là người thân
4. `_ENG` nếu là bản tiếng Anh / song ngữ / bản dịch

### 10.4. Quy tắc file người thân

Nếu giấy tờ thuộc:

- cha
- mẹ
- vợ/chồng
- con
- ông/bà
- cô/dì/chú/bác

=> thêm quan hệ vào tên file

Ví dụ:

- CCCD ba-Bui Van Huan.pdf
- CCCD me-Bui Van Huan.pdf
- GKS vo-Bui Van Huan.pdf
- So dat ong ba-Bui Van Huan.pdf

### 10.5. Không suy đoán khi không đủ dữ liệu

Nếu không đủ dữ liệu để xác định:

- chủ giấy tờ
- quan hệ
- bản ENG

=> bot không được tự suy đoán quá mức; phải đánh dấu cần kiểm tra.

### 10.6. Bảng quy đổi viết tắt

Bot cần hiểu:

- STK = Sổ tiết kiệm
- LLTP = Lý lịch tư pháp
- GKS = Giấy khai sinh
- HDLD = Hợp đồng lao động
- BHXHTN = Bảo hiểm xã hội tự nguyện
- BHYT = Bảo hiểm y tế
- GPLX = Giấy phép lái xe
- CCCD = Căn cước công dân
- XNCT = Xác nhận cư trú
- SYLL = Sơ yếu lý lịch
- KN HTX = Kinh nghiệm hợp tác xã

---

## 11. Quy trình xử lý file tổng quát

Khi khách gửi file hoặc zip trong nhóm, bot sẽ làm theo các bước sau:

### 11.1. Tiếp nhận

- nhận file
- nhận zip / ảnh / PDF / video

### 11.2. Tiền xử lý

- giải nén zip nếu có
- bỏ file rác:
  - .DS_Store
  - __MACOSX
  - ._...

### 11.3. OCR / extract

- OCR text nếu là scan / ảnh / PDF ảnh
- extract text nếu file có text sẵn
- render PDF ra ảnh nếu cần vision

### 11.4. Nhận diện

- nhận diện loại giấy tờ
- nhận diện tên chủ thể

### 11.4. Nhận diện (tiếp)

- nhận diện có phải bản ENG không
- nhận diện sơ bộ folder checklist tương ứng

### 11.5. Đổi tên

- đổi sang tên chuẩn theo rule

### 11.6. Sắp xếp

- upload lên Drive
- đưa vào đúng folder con
- log vào sheet Documents

### 11.7. Tổng hợp

- cập nhật trạng thái hồ sơ/case
- báo riêng cho nhân viên qua DM nếu cần

---

## 12. Kiến trúc AI – mô hình hybrid

### 12.1. Bước 1 dùng AI nhẹ / rẻ

Bước 1 dùng Gemini loại nhẹ/rẻ để làm:

- OCR
- extract text
- nhận diện file
- rename
- tóm tắt file
- mapping checklist sơ bộ
- check thiếu / đủ mức nhanh

Bước 1 phải tạo ra dữ liệu trung gian gồm:

1. OCR text
2. metadata nhận diện
   - loại giấy tờ
   - tên chủ thể
   - số trang
   - có ENG hay không
   - confidence
3. tóm tắt ngắn
4. mapping checklist sơ bộ
5. tên file chuẩn đề xuất / đã đổi

### 12.2. Mục đích của bước 1

- giảm chi phí
- tăng tốc
- để model bước 2 không phải quét lại file từ đầu
- tạo data layer cho phân tích sâu

### 12.3. Bước 2 dùng ChatGPT model cao nhất

Bước 2 dùng ChatGPT model cao nhất để làm:

- phân tích sâu
- đánh giá chất lượng hồ sơ
- kiểm tra logic checklist theo câu hỏi
- xử lý ngoại lệ
- xử lý file khó / mơ hồ / nhiều người / tên sai / scan xấu

---

## 13. Checklist – 2 tầng xử lý

### 13.1. Bước checklist 1 – đủ / thiếu

Dùng model nhẹ ở bước 1.

Mục tiêu:

- check nhanh đã có gì
- check thiếu gì
- check mục nào chưa rõ

Output:

- Đã có
- Thiếu
- Cần kiểm tra thêm

### 13.2. Bước checklist 2 – phân tích sâu

Dùng model mạnh ở bước 2.

Mục tiêu:

- đi sâu từng giấy tờ
- phân tích theo bộ câu hỏi nghiệp vụ
- trả lời chất lượng, logic, điều kiện đạt

Bộ câu hỏi chi tiết:

- sẽ được Cường cập nhật sau

---

## 14. Rule kiểm tra theo loại giấy tờ

Ở giai đoạn đầu, nên ghi rõ các rule kiểm tra quan trọng để bot làm ổn định.

Ví dụ:

- Passport:
  - còn hạn ít nhất 6 tháng không
  - có đúng tên đương đơn không
  - có đủ trang nhân thân / visa / dấu mộc không
- CCCD:
  - đủ 2 mặt không
  - rõ nét không
- LLTP:
  - có đúng LLTP số 2 không
  - ngày cấp có quá 6 tháng không

Bot có thể tự hiểu một phần, nhưng để scale ổn định thì nên định nghĩa rõ:

- loại giấy tờ
- cần check gì
- điều kiện đạt là gì
- nếu không đạt thì báo thế nào

---

## 15. Tạo folder và ghi nhận case mới

Khi bot nhận diện case mới, bot cần:

1. xác định tên khách
2. xác định năm sinh
3. xác định chương trình
4. xác định nhân viên trong nhóm
5. tạo folder case trên Drive
6. cập nhật link folder vào sheet tổng
7. cập nhật case vào file riêng của từng nhân viên liên quan

---

## 16. Nguyên tắc xác định nhân viên liên quan từ nhóm

Bot đọc thành viên nhóm và đối chiếu với bảng Staff.

Nhân viên nào:

- có Telegram ID khớp bảng Staff
- xuất hiện trong nhóm case

=> bot xem là người liên quan đến case và đưa case vào file cá nhân của người đó.

---

## 17. Logic báo cáo của bot

Bot không chat công khai trong nhóm để tránh spam.

Bot sẽ:

- báo riêng qua DM cho nhân viên
- hoặc trả lời riêng cho người hỏi bot nếu người đó có quyền với case

Bot có thể báo:

- đã nhận file nào
- đã đổi tên gì
- đã upload gì
- còn thiếu gì
- checklist mức 1
- checklist mức 2
- điểm bất thường của hồ sơ

---

## 18. Tích hợp tương lai

Sau khi workflow ổn định, bot có thể tích hợp thêm:

- Google Sheets nâng cao
- Google Drive phân quyền chuẩn
- Bitrix24
- cập nhật deal
- cập nhật trạng thái case
- cập nhật thanh toán
- đẩy link Drive vào deal

---

## 19. Giai đoạn phát triển

### Giai đoạn hiện tại

- đang build workflow
- rule còn có thể đổi
- đang tối ưu naming / Drive / sheet / checklist

### Giai đoạn sau

Khi workflow ổn định:

- đóng gói toàn bộ logic thành skill
- skill có thể dùng lại cho nhiều bot / nhiều nhóm
- từ đó scale ra nhiều case và nhiều nhân viên

---

## 20. Kết luận ngắn gọn

Bot hoạt động theo mô hình:

- vào nhóm để nhận diện case và nhận file
- xác định quyền theo thành viên nhóm
- nhân viên làm việc với bot qua DM
- bot dùng Gemini nhẹ cho OCR + rename + check nhanh
- bot dùng ChatGPT model cao nhất cho phân tích sâu

---

## 21. Bổ sung kết luận vận hành

- bot lưu hồ sơ vào Drive theo cấu trúc chuẩn
- bot cập nhật case vào sheet tổng và sheet riêng của từng nhân viên có trong nhóm
- bot báo cáo riêng, không spam công khai trong nhóm

## 22. Các hướng xử lý tiếp theo được đề xuất

1. rút SOP này thành bản ngắn gọn 1 trang
2. chuyển SOP này thành draft skill cho OpenClaw
3. chuyển SOP này thành checklist triển khai kỹ thuật theo phase

---

## 23. Phân quyền Google Drive và Google Sheets

Cường xác nhận: bot cần xử lý phân quyền cả Google Drive và Google Sheets.

### 23.1. Nguyên tắc phân quyền Drive

Bot phân quyền Drive theo quyền truy cập case:

- Nhân viên có trong nhóm làm việc nội bộ của case => được cấp quyền vào folder/file Drive của case.
- Nhân viên không có trong nhóm => không được cấp quyền.
- Google Email của nhân viên lấy từ sheet Staff.
- Quyền mặc định nên là Viewer hoặc Commenter; Editor chỉ cấp cho vai trò được phép như processing/manager/director nếu Cường cấu hình.

Bot cần ghi log mỗi lần phân quyền:

- Case ID
- Folder/File ID
- Google Email được cấp quyền
- Role quyền: viewer/commenter/editor
- Thời điểm cấp quyền
- Người/nguồn kích hoạt
- Trạng thái thành công/thất bại

### 23.2. Nguyên tắc phân quyền Google Sheets

Bot phân quyền Google Sheets theo cùng logic case/staff:

- File tổng: chỉ cấp cho quản lý/admin hoặc nhân sự được chỉ định.
- File riêng nhân viên: nhân viên đó được quyền xem/chỉnh theo cấu hình.
- Case được ghi vào sheet riêng của những nhân viên có mặt trong nhóm làm việc nội bộ.
- Không tự cấp quyền sheet cho người không có trong Staff hoặc không khớp Telegram ID/email.

### 23.3. Rule an toàn

- Không share public/open link nếu không được Cường cho phép rõ.
- Không cấp quyền editor mặc định cho tất cả nhân viên.
- Nếu thiếu Google Email hoặc email không khớp Staff, bot đánh dấu [Cần kiểm tra] và báo DM cho người phụ trách/admin.
- Nếu nhân viên bị remove khỏi nhóm nội bộ, bot cần có phase xử lý thu hồi quyền sau khi Cường xác nhận rule.

### 23.4. Phase triển khai đề xuất

Phase 1:
- Tạo folder/file và ghi log quyền cần cấp, chưa auto share rộng.

Phase 2:
- Auto cấp quyền Drive theo Staff.Google Email và role.

Phase 3:
- Auto cấp quyền Google Sheets theo role và cập nhật file cá nhân.

Phase 4:
- Đồng bộ thu hồi quyền khi nhân viên không còn trong nhóm/case.

---

## 24. Tự cân đối tải và tối ưu xử lý

Cường xác nhận: bot được phép tự cân đối và tối ưu tốc độ xử lý hồ sơ theo tải thực tế.

### 24.1. Nguyên tắc

Bot ưu tiên nhận hồ sơ nhanh, không để mất file/message. Việc xử lý nặng sẽ được đưa vào hàng đợi và chạy theo mức ưu tiên.

### 24.2. Mức ưu tiên xử lý

1. Nhận message/file, ghi nhận case, lưu metadata: ưu tiên cao nhất.
2. Download/lưu file gốc: ưu tiên cao.
3. Tạo folder Drive, upload file gốc: ưu tiên cao.
4. OCR/extract/rename/classify: ưu tiên trung bình.
5. Update Sheets/Documents log: ưu tiên trung bình/cao.
6. Checklist mức 1: ưu tiên trung bình.
7. Checklist mức 2/phân tích sâu bằng model mạnh: ưu tiên thấp hơn, chạy sau hoặc khi được yêu cầu.

### 24.3. Giới hạn song song đề xuất ban đầu

- File/OCR jobs: 2–4 job song song.
- Google Drive/Sheets write jobs: giới hạn theo rate limit, có retry/backoff.
- GPT phân tích sâu: 1–2 job song song.
- Nếu queue tăng cao, bot chỉ nhận + lưu + báo đang xử lý, không phân tích sâu ngay.

### 24.4. Báo cáo tiến độ

Bot báo riêng cho nhân viên khi:

- đã nhận hồ sơ
- đã đưa vào queue
- đã upload xong file gốc
- đã xử lý xong rename/classify
- có lỗi cần kiểm tra
- checklist mức 1/mức 2 hoàn tất

### 24.5. Chống quá tải

Khi nhiều case gửi cùng lúc, bot được phép:

- xử lý tuần tự theo queue
- trì hoãn checklist mức 2
- gom nhiều update Sheets thành batch nếu phù hợp
- retry khi Google/AI/API lỗi tạm thời
- đánh dấu job lỗi vào danh sách cần xử lý thủ công

---

## 25. Tạm hoãn phân tích sâu

Cường xác nhận: hiện tại chưa cần checklist mức 2 / phân tích sâu.

### 25.1. Phạm vi hiện tại

Bot chỉ cần tập trung:

- nhận file/message
- tạo/nhận diện case
- lưu file gốc
- OCR/extract cơ bản nếu cần cho đổi tên
- nhận diện loại giấy tờ
- đổi tên file theo rule
- upload/sắp xếp vào Drive
- cập nhật Google Sheets
- phân quyền Drive/Sheets
- checklist mức 1: Đã có / Thiếu / Cần kiểm tra
- báo cáo riêng cho nhân viên qua DM

### 25.2. Chưa làm ở phase này

- Không tự phân tích sâu chất lượng hồ sơ.
- Không đánh giá mạnh/yếu theo logic visa phức tạp.
- Không gọi model cao nhất cho mọi hồ sơ.
- Không kết luận khả năng đậu/rớt.

### 25.3. Bổ sung sau

Rule phân tích sâu/checklist mức 2 sẽ được Cường gửi sau. Khi có rule đó, bot mới kích hoạt tầng phân tích sâu.

---

## 26. OCR, extract text và tóm tắt trung gian cho phân tích sau

Cường xác nhận: dù hiện tại chưa cần phân tích sâu, bot vẫn cần OCR/extract text và tạo tóm tắt trung gian để model hoặc nhân sự khác đọc sâu về sau.

### 26.1. Mục tiêu

- Biến ảnh/scan/PDF ảnh thành text có thể đọc/search được.
- Tạo data layer trung gian để bước phân tích sâu sau này không phải đọc lại file gốc từ đầu.
- Giúp AI khác hoặc nhân viên đọc nhanh nội dung hồ sơ.
- Hỗ trợ rename/classify/checklist mức 1 chính xác hơn.

### 26.2. Output bắt buộc cho mỗi file nếu có thể

Bot cần tạo và lưu metadata/text summary cho từng file:

- Case ID
- File ID / Drive link
- Tên file gốc
- Tên file chuẩn
- Loại giấy tờ nhận diện
- Chủ giấy tờ
- Quan hệ với đương đơn nếu có
- Có phải bản ENG/bản dịch/song ngữ không
- Số trang
- OCR text / extracted text
- Tóm tắt ngắn nội dung file
- Checklist mapping sơ bộ
- Confidence
- Trạng thái: OK / Cần kiểm tra / Lỗi OCR

### 26.3. Nguyên tắc OCR/extract

- Nếu PDF có text sẵn: extract text trực tiếp trước.
- Nếu là ảnh/scan/PDF ảnh: OCR hoặc dùng vision để đọc text.
- Nếu file mờ, thiếu trang, bị cắt, nhiều người trong cùng file: đánh dấu [Cần kiểm tra].
- Không dùng OCR để kết luận sâu về khả năng visa ở phase này.

### 26.4. Lưu trữ text trung gian

Bot nên lưu text/tóm tắt vào một hoặc nhiều nơi:

- Sheet Documents: tóm tắt ngắn + metadata chính.
- File sidecar dạng `.txt` hoặc `.json` trong Drive nếu cần lưu OCR text đầy đủ.
- Database/local queue storage nếu triển khai backend riêng.

### 26.5. Phạm vi hiện tại

Phase hiện tại có OCR + tóm tắt, nhưng chỉ phục vụ:

- đọc được nội dung file
- rename
- classify
- checklist mức 1
- chuẩn bị dữ liệu cho phân tích sâu sau này

Không kích hoạt checklist mức 2/phân tích sâu cho đến khi Cường gửi rule riêng.

---

## 27. Lưu dữ liệu OCR/tóm tắt trực tiếp lên Google Drive case

Cường điều chỉnh: không lưu data layer nặng trong workspace/local nếu không cần. Với mỗi case, bot ưu tiên ghi OCR text, metadata và summary trực tiếp vào folder Google Drive của nhóm khách hàng/case.

### 27.1. Nguyên tắc

- Folder Drive của case là nơi lưu trữ chính.
- Workspace/local chỉ dùng làm bộ nhớ tạm khi xử lý.
- Sau khi upload/lưu thành công lên Drive, file tạm có thể được dọn để giảm tải máy.
- Không nhồi OCR text dài vào Google Sheets nếu quá nặng; Sheets chỉ giữ metadata, trạng thái, link Drive và summary ngắn.

### 27.2. Cấu trúc đề xuất trong folder case

Trong mỗi folder case nên có thêm thư mục:

- `_Bot OCR & Metadata`

Bot lưu vào đó:

- OCR text đầy đủ: `.txt`
- Metadata từng file: `.json`
- Summary ngắn/tổng hợp: `.md` hoặc `.txt`
- Log xử lý nếu cần: `.jsonl` hoặc `.csv`

### 27.3. Google Sheets chỉ lưu nhẹ

Sheet Documents chỉ nên lưu:

- Case ID
- Tên file gốc
- Tên file chuẩn
- Loại giấy tờ
- Chủ giấy tờ
- Folder/Drive link
- Link OCR/metadata/summary trên Drive
- Trạng thái OCR/classify/rename
- Confidence
- Ghi chú ngắn

### 27.4. Lợi ích

- Giảm tải local workspace.
- Dễ bàn giao cho nhân viên/AI khác đọc tiếp.
- Drive folder của case chứa đủ file gốc + file đã chuẩn hóa + OCR/summary.
- Sheet tổng vẫn nhẹ, ít bị chậm.

---

## 28. Giới hạn ghi Google Sheets

Cường điều chỉnh: Google Sheets không dùng để ghi log chi tiết hoặc metadata nặng.

### 28.1. Nguyên tắc

Bot chỉ ghi/cập nhật Google Sheets trong các trường hợp cần thiết:

1. Khi tạo/nhận diện nhóm case lần đầu.
2. Khi có cập nhật quan trọng từ Telegram nhóm khách hàng cần phản ánh vào dữ liệu case.

### 28.2. Không ghi vào Sheet cho các việc sau

- Không ghi OCR text đầy đủ vào Sheet.
- Không ghi summary dài vào Sheet.
- Không ghi log từng bước xử lý file vào Sheet nếu không cần.
- Không dùng Sheet như database chính cho metadata nặng.

### 28.3. Nơi lưu dữ liệu chi tiết

- File gốc, file đã chuẩn hóa, OCR text, metadata, summary và log xử lý ưu tiên lưu trong folder Google Drive của case.
- Sheet chỉ giữ thông tin case cần quản lý/tìm kiếm/cập nhật trạng thái ở mức nhẹ.
