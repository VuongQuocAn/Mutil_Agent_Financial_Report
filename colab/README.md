# Chạy Module 1 trên Colab

Mở [01_ocr_quality_report.ipynb](01_ocr_quality_report.ipynb) trong VS Code và chọn kernel Colab; hoặc mở bản trên [web Colab](https://colab.research.google.com/github/VuongQuocAn/Mutil_Agent_Financial_Report/blob/main/colab/01_ocr_quality_report.ipynb).

Chạy các ô theo thứ tự: mount Drive → clone/pull repository → chọn báo cáo → quality report → xem issue. Với BTS/2025 đã import, giữ `RUN_IMPORT = False`; notebook dùng `MyDrive/Financial_MultiAgent_Results/BTS/2025/ocr-bundle-v1/`. Nếu bundle nằm ở nơi khác, sửa `bundle_dir` trong ô cấu hình.

Mỗi lần chạy lưu ba file trong `quality/<run-id>/`: `quality_report.json`, `quality_issues.jsonl`, `quality_report.md`. Quality chỉ đọc artifact JSON, không cần GPU và không đọc lại toàn bộ ảnh. `SHOW_IMAGE = False` là mặc định; bật khi muốn mở một crop để review. Crop được hiển thị truyền qua kết nối mạng của máy bạn.

`needs_review` cho biết có dấu hiệu cần xem ảnh, không chứng minh OCR sai. `checks_passed` áp dụng cho các phép kiểm đã chạy; visual accuracy chưa được đo. Run không nâng document lên `ready`. `access_scope` chỉ được đối chiếu trong prototype; phân quyền thật ở database/retrieval vẫn thuộc kiến trúc đích.

Code chính: [ocr_quality.py](../financial_multiagent/ingestion/ocr_quality.py). Contract: [schema v1](../financial_multiagent/ingestion/ocr_quality.schema.json). Thiết kế và giới hạn: [kế hoạch quality](../docs/01a-ocr-quality-report-plan.md).

Khi có code mới trên GitHub, chạy lại ô clone/pull: ô này reload module trong kernel. Thư mục `/content` là nơi tạm của Colab; output trong Drive được giữ lại. Notebook được commit không chứa output hoặc thông tin xác thực.
