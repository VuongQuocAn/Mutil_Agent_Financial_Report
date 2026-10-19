# Vietnamese Financial Multi-Agent Pipeline

Code Python và notebook Colab cho kiến trúc đầy đủ C1–C5. Thiết kế gốc ở [Level 0](docs/00-overview.md); trạng thái từng package và quy tắc folder ở [repository structure](docs/07-repository-structure.md).

| Chặng | Trạng thái |
|---|---|
| Import OCR JSON/PNG và source refs | Đã có; không thay thế OCR PDF mới toàn pipeline |
| OCR quality | Đã có kiểm tra xác định, giữ issue và provenance |
| Financial normalization | Đã có lát cắt một bảng kết quả kinh doanh B02 |
| C1 | Đã có structural/accounting/cross-document và engine cho curated regulatory/disclosure rules |
| C2, retrieval, C3, C4, C5 benchmarks/web | Namespace được chuẩn bị; chưa triển khai chức năng tương ứng |

## Chạy trên Colab

Mở các notebook theo thứ tự trong [colab/](colab/README.md): quality → normalization → [C1](colab/03_c1_validation.ipynb). Dùng kernel Colab; code được clone/pull từ GitHub, dữ liệu và kết quả giữ trên Drive. C1 phải chọn một normalization run cụ thể; không tự giả định thông tin thiếu hoặc tự tạo quy định.

## Chạy local

Python >= 3.11. Runtime hiện dùng standard library; pytest dùng cho test. Có thể chạy từ root repo hoặc cài package bằng `python -m pip install -e ".[dev]"`.

```powershell
python -m pytest
python -m financial_multiagent.validation.c1 --help
```

CLI C1 nhận `--bundle-dir`, `--normalization-dir`, `--access-scope`; tùy chọn `--registry`, `--context`, `--output-dir`, `--absolute-tolerance`. API nhiều tài liệu: `run_c1([C1Input(...), ...], access_scope=...)`.

## Giới hạn cần hiểu

C1 mặc định không có bộ luật được duyệt. Phần pháp lý trả `unknown` cho đến khi được cung cấp registry hợp lệ. Rule giả chỉ xuất hiện trong unit test và bị tắt trong profile mặc định. C1 giữ metadata/review còn thiếu; sai lệch kế toán là warning, không tự thành vi phạm pháp lý. Kiểm C1 không tự nâng document `ready`, không thay thế C4 hoặc bước publish Knowledge Storage.

Raw PDF/PNG/JSON, credentials, artifact và notebook cá nhân không cần nằm trong commit. `.gitignore` loại dữ liệu mẫu và kết quả lớn; code OCR cũ trong `ocr_samples/ocr_pipeline.py` vẫn được theo dõi. Không lưu token hay output nhạy cảm vào notebook được push lên Git.
