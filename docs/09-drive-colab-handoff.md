# Bàn giao truy cập Drive và chạy Colab

> **Kiểm chứng ngày 20/10/2026.** Tài liệu này ghi lại khả năng thao tác đã thử trong phiên Codex tại workspace hiện tại. Đây là cách làm việc với dữ liệu và môi trường tạo mẫu, không phải trạng thái triển khai của kiến trúc C1–C5. Xem [Level 0](00-overview.md) và [hướng dẫn notebook](../colab/README.md) khi làm module.

## Đọc nhanh khi mở phiên mới

1. Mở workspace `D:\vietnamese_financial_multiAgent\financial_multiagent_handoff` và đọc `AGENTS.md`, tài liệu này, cùng tài liệu của module được giao. Giữ nguyên các thay đổi chưa commit của người dùng.
2. Kiểm tra lại plugin Google Drive và quyền với các thư mục bên dưới. Liệt kê metadata hoặc một thư mục nhỏ trước; không tải toàn bộ ảnh OCR chỉ để thử kết nối.
3. Nếu cần chạy Colab, mở notebook trong VS Code, chọn **Select Kernel → Select Another Kernel → Colab → Auto Connect → Python 3**. Chạy một ô ngắn và xác nhận đầu ra thuộc runtime Colab trước khi chạy dữ liệu thật.
4. Ghi lại mã nguồn, đầu vào/đầu ra, kết quả kiểm thử và việc còn dở ở một tệp bàn giao của module. Phiên mới phải kiểm tra lại trạng thái thực tế, không suy ra từ kết quả thử ngày 20/10/2026.

## Level 0 — phạm vi đã xác nhận

| Khả năng | Bằng chứng đã kiểm tra | Giới hạn |
|---|---|---|
| Google Drive | Liệt kê ba thư mục; tạo, đọc lại và cập nhật **cùng một ID** của tệp thử | Chưa kiểm tra thao tác với mọi loại tệp hay toàn bộ dataset |
| Colab qua VS Code | Tạo notebook mới, tự chọn Colab Auto Connect, chạy ô Python và lưu output | Cần VS Code và runtime còn kết nối; đăng nhập lại có thể cần khi phiên hết hạn |
| Git cục bộ | Đọc trạng thái repository bằng Git | Chưa xác nhận quyền `push` lên GitHub trong chuỗi thử này |

Các kiểm chứng trên **không** chứng minh module OCR hoặc C1–C5 đã chạy với dataset đầy đủ. Kiến trúc mục tiêu và trạng thái triển khai được tách trong [Level 0](00-overview.md) và [cấu trúc repository](07-repository-structure.md).

## Level 1 — Google Drive

### Luồng và mục đích

Plugin Google Drive đọc metadata, thư mục và tệp từ tài khoản đã kết nối; thao tác ghi phải nhắm đúng ID tệp hoặc thư mục. Ba thư mục đầu vào/đầu ra đã mở được:

**Quy ước sử dụng do người dùng xác nhận:** `Financial_Report_OCR` chứa báo cáo đã OCR và các ảnh liên quan, gồm ảnh chữ ký; `Financial_Report` chứa ảnh trang PNG được cắt từ báo cáo. Hai thư mục này là **nguồn chỉ đọc**: được xem và dùng làm đầu vào, không tạo, sửa, di chuyển hoặc xóa tệp/thư mục trong đó. `multi_agent_financial` là nơi lưu kết quả và artifact của dự án trên Drive; được phép tạo và chỉnh sửa nội dung tại đây. Khi xử lý dữ liệu, ghi đầu ra vào `multi_agent_financial` hoặc workspace cục bộ, không ghi ngược vào hai thư mục nguồn.

| Thư mục | Link | Nội dung trực tiếp đã thấy ngày 20/10/2026 |
|---|---|---|
| `Financial_Report_OCR` | [Mở thư mục](https://drive.google.com/drive/folders/1BH5yCw32xQuCfLnPPigEPAf5R6mYBSID) | 15 thư mục con theo mã doanh nghiệp; ví dụ `CAG` có các năm 2022–2025 |
| `Financial_Report` | [Mở thư mục](https://drive.google.com/drive/folders/1bNLJJwk3IhsPa7g2-2m0Zhq6HTGV7qvQ) | 15 thư mục con theo mã doanh nghiệp và `ocr_pipeline.py`; ví dụ `CAG` có các năm 2021–2025 |
| `multi_agent_financial` | [Mở thư mục](https://drive.google.com/drive/folders/1WSI4FwmAWtK_Dan1TGjglUxhxo-WPKc_) | Trống trước phép thử; sau đó có tệp thử bên dưới |

### Level 2 — phép thử ghi và hợp đồng dữ liệu

- Tệp [codex_drive_write_test_2026-10-20.txt](https://drive.google.com/file/d/11Vkd7QAd9a3mi18qe469F-T2hPfJ2lsu/view) được tạo trong `multi_agent_financial`, ID `11Vkd7QAd9a3mi18qe469F-T2hPfJ2lsu`.
- Sau đó nội dung tệp được **cập nhật tại cùng ID** và đọc lại thành công. Nội dung có câu `Đã cập nhật nội dung tệp hiện có ngày 2026-10-20`.
- Link chuẩn cũng được lưu trong [link_drive.txt](link_drive.txt). Một link chia sẻ chỉ cho biết địa chỉ; phiên mới vẫn phải dùng kết nối hợp lệ và kiểm tra quyền đọc/ghi thực tế.
- Với ảnh OCR lớn, đọc/ghi theo lô và lưu tiến độ. Không mặc định rằng ba thư mục này đã bao gồm mọi PDF gốc, metadata phiên bản, ACL hoặc quy tắc pháp lý của kiến trúc đích.

**Đầu vào:** folder/file ID và tài khoản Drive đã kết nối. **Đầu ra:** metadata, nội dung tệp hoặc tệp mới/cập nhật trong thư mục đích. **Phụ thuộc:** quyền thực tế của tài khoản và plugin. **Lỗi có thể gặp:** mất kết nối, không đủ quyền, tệp bị di chuyển/xóa, giới hạn tải hoặc định dạng không được hỗ trợ. Kiểm tra metadata và đọc lại sau thao tác ghi; không suy ra thành công chỉ từ link.

## Level 1 — điều khiển Google Colab từ VS Code

### Luồng và mục đích

Mã notebook `.ipynb` nằm trong workspace; VS Code dùng extension `google.colab` và Jupyter để chọn kernel Colab. Khi chọn runtime Colab, ô code chạy trong máy ảo của Colab thay vì Python cục bộ. Chọn kernel trong VS Code không đồng nghĩa với việc code package đã có sẵn trên Colab; module thực tế còn cần bước đồng bộ mã và truy cập dữ liệu riêng.

### Level 2 — bằng chứng và cách kiểm lại

| Notebook | Thao tác đã thử | Output đã lưu |
|---|---|---|
| [test_colab_codex.ipynb](../colab/test_colab_codex.ipynb) | Chạy ô kiểm tra trên kernel Colab đang nối | `CODEX_COLAB_TEST_OK`, `/content`, `Colab runtime: True`, `execution_count: 1` |
| [test_colab_codex_autoconnect.ipynb](../colab/test_colab_codex_autoconnect.ipynb) | Tạo tệp mới, mở trong VS Code, tự chọn **Colab → Auto Connect → Python 3**, chạy và lưu | `CODEX_AUTOCONNECT_TEST_OK`, `/content`, `Colab runtime: True`, `execution_count: 1` |

Lần thử này không cần người dùng đăng nhập lại vì kết nối còn hiệu lực. Việc điều khiển được thực hiện qua giao diện VS Code đang mở; chưa xác nhận một API điều khiển Colab độc lập hoặc khả năng chạy khi VS Code đóng. Dữ liệu tạm trong `/content` có thể mất khi runtime kết thúc; đầu ra cần giữ phải ghi vào nơi lưu bền vững theo contract của module.

**Đầu vào:** notebook, kernel Colab và dữ liệu có quyền truy cập. **Đầu ra:** output cell và artifact của module. **Phụ thuộc:** VS Code, extension Colab/Jupyter, runtime và xác thực Google còn hiệu lực. **Lỗi có thể gặp:** runtime ngắt, xác thực hết hạn, thiếu package/code trên VM, giới hạn RAM/đĩa hoặc I/O Drive. Cách chạy theo lô, checkpoint và lựa chọn CPU/GPU cho module cụ thể: `TBD` cho đến khi kiểm tra yêu cầu thực tế.

## Tiếp tục công việc qua phiên chat

Phiên mới có thể đọc repo và tài liệu này để khôi phục bối cảnh. Nếu tiếp tục **chat cũ** từ Recent chats thì còn transcript; một **chat mới** không tự mang toàn bộ quyết định và kết quả của chat cũ. Khi làm module dài, lưu checkpoint vào file trong repo: phần đã làm, lệnh/notebook đã chạy, nguồn dữ liệu và phiên bản, kết quả, lỗi, bước kế tiếp. Không để credential hoặc token trong notebook, Git hay tài liệu bàn giao.

Không có bằng chứng rằng Codex có thể tự mở phiên mới khi quota hồi phục hoặc biết chính xác thời điểm quota sắp hết. Người dùng cần quay lại và yêu cầu tiếp tục; phiên mới kiểm tra lại Drive, Colab, Git và các artifact trước khi chạy tiếp. Nếu cần thay đổi phạm vi kiến trúc C1–C5, tuân theo `AGENTS.md` và hỏi trước.
