# Chạy Module 1 trên Colab

## Pilot theo ba mã BTS, BTW, BPC

Mở [00_module1_drive_pilot.ipynb](00_module1_drive_pilot.ipynb) trên Colab để chạy theo ID thư mục Drive, không phụ thuộc vào đường dẫn MyDrive. Notebook xác thực Drive API, clone/pull code từ Git, xử lý từng báo cáo trong runtime Colab và lưu kết quả vào `multi_agent_financial/module_01_pilot/`. Chạy lại dùng completion marker để bỏ qua báo cáo đã hoàn tất với cùng nguồn, code và config. Mặc định chạy mọi năm của ba mã; đặt `YEARS = ('2025',)` để giới hạn khi thử. Xem [hợp đồng pilot](../docs/10-module-1-colab-pilot.md). Hai thư mục nguồn tuyệt đối chỉ đọc.

Các notebook 01–03 bên dưới là luồng thủ công lịch sử dùng đường dẫn MyDrive và bundle có sẵn; không phải bộ điều khiển batch pilot mới.

Mở [01_ocr_quality_report.ipynb](01_ocr_quality_report.ipynb) trong VS Code và chọn kernel Colab; hoặc mở bản trên [web Colab](https://colab.research.google.com/github/VuongQuocAn/Mutil_Agent_Financial_Report/blob/main/colab/01_ocr_quality_report.ipynb).

Chạy các ô theo thứ tự: mount Drive → clone/pull repository → chọn báo cáo → quality report → xem issue. Với BTS/2025 đã import, giữ `RUN_IMPORT = False`; notebook dùng `MyDrive/Financial_MultiAgent_Results/BTS/2025/ocr-bundle-v1/`. Nếu bundle nằm ở nơi khác, sửa `bundle_dir` trong ô cấu hình.

Mỗi lần chạy lưu ba file trong `quality/<run-id>/`: `quality_report.json`, `quality_issues.jsonl`, `quality_report.md`. Quality chỉ đọc artifact JSON, không cần GPU và không đọc lại toàn bộ ảnh. `SHOW_IMAGE = False` là mặc định; bật khi muốn mở một crop để review. Crop được hiển thị truyền qua kết nối mạng của máy bạn.

`needs_review` cho biết có dấu hiệu cần xem ảnh, không chứng minh OCR sai. `checks_passed` áp dụng cho các phép kiểm đã chạy; visual accuracy chưa được đo. Run không nâng document lên `ready`. `access_scope` chỉ được đối chiếu trong prototype; phân quyền thật ở database/retrieval vẫn thuộc kiến trúc đích.

Code chính: [ocr_quality.py](../financial_multiagent/ingestion/ocr_quality.py). Contract: [schema v1](../financial_multiagent/ingestion/ocr_quality.schema.json). Thiết kế và giới hạn: [kế hoạch quality](../docs/01a-ocr-quality-report-plan.md).

Sau quality, mở [02_financial_normalization.ipynb](02_financial_normalization.ipynb) để chuẩn hóa một bảng kết quả kinh doanh (mặc định BTS/2025 trang 12). Notebook dùng bundle hiện có, ghi `normalization/<run-id>/`. Để scope và period overrides trống cho đến khi xác nhận từ tài liệu; kết quả có thể có `needs_review` dù giá trị số đã được chuyển đúng. Xem [thiết kế normalization](../docs/01b-financial-normalization.md).

Tiếp theo mở [03_c1_validation.ipynb](03_c1_validation.ipynb), liệt kê và chọn rõ `NORMALIZATION_RUN_ID`. Nếu chỉ có một run có thể để None; nhiều run yêu cầu chọn, không tự lấy latest. C1 ghi `c1/<run-id>/`, kiểm lại hash và replay normalization. Registry/context mặc định None; pháp lý trả unknown khi chưa có rule được duyệt. Dữ kiện thiếu metadata vẫn unknown, chẩn đoán phép tính không nâng thành pass. Xem [C1](../docs/01c-c1-validation.md).

Khi có code mới trên GitHub, chạy lại ô clone/pull: ô này reload module trong kernel. Thư mục `/content` là nơi tạm của Colab; output trong Drive được giữ lại. Notebook được commit không chứa output hoặc thông tin xác thực.
