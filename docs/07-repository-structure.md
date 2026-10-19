# Repository structure — ranh giới code cho toàn pipeline

**Đã chốt:** D01–D18 giữ nguyên; cách tổ chức package là lựa chọn triển khai. [Level 0](00-overview.md) mô tả kiến trúc; folder tồn tại không phải bằng chứng module đã triển khai.

```text
financial_multiagent/
  common/                    # Artifact/hash/scoped read và publish run; C1 đang dùng
  ingestion/                 # Adapter, OCR quality, financial normalization hiện có
  validation/
    c1/
      contracts.py           # Profile, source checks, result invariants
      accounting.py          # Decimal identities và cross-document
      regulatory.py          # Applicability, exception, disclosure và dual provenance
      engine.py              # Pure orchestration của C1
      runner.py              # Scoped IO, replay normalization, stage output
      __main__.py            # CLI
      profiles/              # Accounting checks có phiên bản, không phải luật
      schemas/               # Result, curated registry và validation context
  knowledge/                 # C2 + storage; reserved
  retrieval/                 # Query/conversation/retrieval/tools; reserved
  orchestration/             # C3 supervisor và agents; reserved
  verification/              # C4 + verified ledger/firewall; reserved
  evaluation/                # C5 benchmarks/ablation/monitoring; reserved
  interfaces/                # API/chat/report UI boundary; reserved
configs/c1/                  # Template rỗng cho registry/context, không chứa luật giả
colab/                       # Notebook mỏng: cấu hình, gọi package, xem kết quả
tests/
  module_01/                 # Test ingestion đã có; giữ đường dẫn để tương thích
  unit/validation/c1/        # Engine và fixture luật tổng hợp chỉ cho test
  integration/validation/c1/ # Runner, replay, IO failure và notebook API
docs/                        # Technical design specs và tiến độ thật
context/                     # Kiến trúc đã thống nhất và sơ đồ nguồn
ocr_samples/                 # Code OCR cũ; raw mẫu bị ignore
artifacts/                   # Kết quả local bị ignore
pyproject.toml               # Package metadata, resources, CLI và cấu hình pytest
```

## Quy tắc tổ chức

1. Code nghiệp vụ nằm trong package sở hữu stage; notebook gọi API, không sao chép logic validator vào cell. C1 khác OCR quality và C4.
2. Schemas nằm cùng package sở hữu contract; ingestion ít file nên giữ schema cạnh module, C1 có thư mục `schemas/` riêng. Profile thuật toán có thể đóng gói trong package; registry/context theo môi trường nằm ngoài code và truyền đường dẫn rõ ràng.
3. Package `common` chỉ chứa khả năng dùng chung; không đặt policy pháp lý hay tính toán theo chỉ tiêu ở đó. C1 dùng utility này; các runner ingestion cũ vẫn giữ API/writer để không làm hỏng notebook đang chạy.
4. Namespace reserved có docstring chỉ rõ chưa triển khai, không có fake service. Module tiếp theo có code, schema và test trong chính ranh giới đó, không gom vào C1.
5. Test mới theo unit/integration và domain; test ingestion cũ giữ nguyên đường dẫn để lệnh/notebook hiện tại tiếp tục dùng được. Unit test luật dùng synthetic text, không được mặc định nạp làm registry production.
6. Input scope/version/hashes đi cùng stage output. Đường dẫn Drive là địa chỉ lưu trữ; source_ref là danh tính logic. Không đưa `.shortcut-targets-by-id` hoặc credential vào artifact chia sẻ.
7. Mỗi run ghi staging rồi công bố thư mục hoàn chỉnh; run cũ bất biến. Không chọn latest một cách im lặng khi nhiều run đầu vào có thể khác context.

## Layout dữ liệu trên Drive

```text
MyDrive/
  Financial_OCR/                              # Ảnh trang nguồn
  Financial_Report_OCR/                       # OCR JSON/crops nguồn
  Financial_MultiAgent_Results/
    <company>/<year>/<bundle>/
      manifest.json
      elements.jsonl
      quality/<run-id>/...
      normalization/<run-id>/...
      c1/<run-id>/...
    rules/                                   # Registry được rà soát, do nhóm cấp
```

Layout trên là quy ước cho prototype, không thay thế raw PDF storage/metadata registry của kiến trúc đích. Phiên bản nguồn mới tạo bundle/version mới. Nguồn/metadata thay đổi làm các run phụ thuộc cần xét lại; C2 invalidation và current-run registry còn TBD.

## Trạng thái triển khai và dependencies

C1 dùng ingestion snapshot, normalization candidates, rule registry tùy chọn và entity/disclosure context; xuất finding offline cho Module 2/C2 về sau. C3 sẽ dùng retrieval/tools, C4 xác minh claim runtime rồi đưa vào verified ledger, C5 đánh giá toàn chuỗi. Các ranh giới này đã chuẩn bị namespace, chưa có backend/index/agents/web/evaluation benchmark.

ACL thực, rule extraction tự động, DB migrations, queue/checkpoint toàn kho, API deployment và C2–C5 runtime còn cần thiết kế/triển khai. Không xem `pip install` thành công hoặc có folder reserved là đã hoàn thành pipeline.
