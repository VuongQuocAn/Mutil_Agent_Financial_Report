# Quy chuẩn viết Technical Design Specification

**Áp dụng cho:** tài liệu thiết kế module trong `docs/` từ Level 1 đến Level 2. Đây là quy chuẩn viết cho nhóm lập trình, không phải báo cáo học thuật hay bằng chứng rằng hệ thống đã được triển khai. [Level 0](00-overview.md) giữ vai trò bản đồ hệ thống; tài liệu module phải triển khai chi tiết nhưng không đổi kiến trúc C1–C5.

## 1. Nguồn và ranh giới thiết kế

Đối chiếu theo thứ tự: [quyết định kiến trúc](../context/ARCHITECTURE_DECISIONS.md), [ngữ cảnh dự án](../context/PROJECT_CONTEXT.md), [sơ đồ tham chiếu](../context/architecture-reference.png), rồi [Level 0](00-overview.md). Nếu sơ đồ mơ hồ hoặc các nguồn có vẻ không khớp, ghi mục **Điểm cần xác nhận** với hai cách hiểu và vị trí nguồn; ưu tiên quyết định kiến trúc, không tự đoán hoặc âm thầm sửa phạm vi.

Mỗi tài liệu phân biệt:

| Nhãn | Cách dùng |
|---|---|
| **Đã chốt** | Quyết định D01–D18 hoặc nội dung C1–C5 được xác nhận trong nguồn; dẫn ID/quy chiếu. |
| **Đề xuất** | Giao diện, schema, thuật toán, bảng dữ liệu hoặc công nghệ nhóm có thể chọn; ghi lý do và tác động, không viết như đã duyệt. |
| **TBD** | Thiếu dữ liệu để chốt; nêu câu hỏi, lựa chọn liên quan và nơi cần quyết định. |
| **Giả định** | Điều kiện tạm dùng để mô tả luồng; ghi cách kiểm chứng và hệ quả nếu sai. |

Mỗi tài liệu nêu rõ **kiến trúc mục tiêu**, **trạng thái triển khai được xác nhận** và **giai đoạn triển khai đề xuất** nếu có. Khi chưa có bằng chứng triển khai, viết `Chưa xác nhận`, không suy diễn từ sơ đồ. Không tự gán model, GPU, ngưỡng, số liệu benchmark, quy định pháp lý hay chính sách truy cập cụ thể. Google Colab là môi trường tạo mẫu chính; SQLite, FAISS/Chroma, NetworkX, LangGraph, Qwen và các lựa chọn khác chỉ là phương án khi chưa được chốt.

## 2. Phạm vi Level 1 và Level 2

- **Level 1 — subsystem:** mục đích, ranh giới, luồng end-to-end, trách nhiệm các module con, giao tiếp với subsystem khác và đường lỗi chính. Một sơ đồ Level 1 tóm tắt các khối; một hoặc nhiều sơ đồ Level 2 phóng to các thành phần quan trọng.
- **Level 2 — component/data contract:** trạng thái, bước xử lý, ràng buộc dữ liệu, schema, lưu trữ, API/tool, quyền và test có thể dùng để chia việc lập trình. Vẫn đặt một sơ đồ Level 1 nhỏ để thấy vị trí trong luồng cha, rồi sơ đồ Level 2 cho logic nội bộ.
- Mỗi file có đường dẫn về [Level 0](00-overview.md), về tài liệu cha nếu có và đến tài liệu con/liên quan. File Level 1/2 dùng tên có số thứ tự; file quy chuẩn này giữ tên người dùng yêu cầu.
- Bắt đầu mỗi subsystem bằng **luồng và mục đích**. Tách sơ đồ lớn thành nhiều sơ đồ nhỏ; không dồn C1–C5 vào một flowchart.

## 3. Cấu trúc bắt buộc của mỗi module

Mỗi module có đủ mười mục sau, theo thứ tự. Nếu một mục chưa thể cụ thể hóa, vẫn giữ mục đó và ghi `TBD` cùng câu hỏi cần giải quyết.

### 3.1 Mục tiêu và trách nhiệm

Nêu vấn đề module giải quyết, phạm vi vào/ra, vị trí trong Offline/Knowledge Storage/Online/C5, chủ sở hữu dữ liệu và ranh giới với module kế cận. Ghi điều kiện bắt đầu, điều kiện hoàn tất, invariant và nhãn quyết định. Không trộn `financial insight`, `risk warning` và `compliance finding`.

### 3.2 Sơ đồ Level 1 và Level 2 bằng Mermaid

Đặt **hai sơ đồ riêng** với tiêu đề và một câu giải thích đường chính. Level 1 thể hiện tương tác giữa module và subsystem; Level 2 thể hiện bước xử lý, kho dữ liệu, trạng thái hoặc điều kiện rẽ nhánh. Dùng nhãn nút ngắn, hướng `LR` hoặc `TB`, khoảng trắng dễ đọc. Chỉ vẽ luồng và phụ thuộc có trong nguồn hoặc được đánh dấu **Đề xuất/Giả định**. Khi sơ đồ có nhiều nhánh, tách thêm thay vì thu nhỏ chữ. Gắn C1–C5 đúng vị trí và thể hiện đường quay lại Offline của tài liệu tải lên trong Online.

### 3.3 Workflow theo từng bước

Viết danh sách đánh số: trigger → kiểm quyền/phạm vi → đọc input → biến đổi/kiểm tra → ghi trạng thái → phát output. Với mỗi bước ghi thành phần thực hiện, dữ liệu đọc/ghi, điều kiện chuyển bước và điểm dừng. Tách đường thành công, đường `unknown`/`needs_review`/`abstain`, lỗi có thể retry và lỗi dừng. Phân biệt bằng chứng tạo khi ingestion với claim/finding tạo khi phân tích.

### 3.4 Input/output và JSON schemas minh họa

Lập bảng cho từng interface: tên, producer, consumer, trường bắt buộc, kiểu, đơn vị, nguồn/phiên bản, quyền, trạng thái và lỗi. Thêm JSON ví dụ hợp lệ và JSON Schema minh họa (draft và mức ràng buộc: **Đề xuất** hoặc **Đã chốt**). Nếu schema chưa chốt, đánh dấu `TBD` ngay cạnh trường; không biến ví dụ thành hợp đồng đã duyệt. Dùng tên nhất quán giữa sơ đồ, workflow, bảng và API.

Các hợp đồng mang dữ liệu tài chính cần nêu tối thiểu: `document_id`, `document_version`, `company_id`, `fiscal_period`, `consolidation_scope`, `value`, `unit`, `source_ref`, `access_scope` khi phù hợp. Bằng chứng pháp lý thêm `rule_id`, `rule_version`, `effective_from/to`, điều kiện áp dụng/ngoại lệ và tham chiếu điều khoản. Claim/finding thêm `claim_id`, loại (`insight`/`risk_warning`/`compliance_finding`), provenance, trạng thái xác minh và quyết định C4. Danh sách trường này là yêu cầu mô tả; kiểu dữ liệu, tên chính thức và độ hạt vẫn **Đề xuất/TBD** cho đến khi được duyệt.

### 3.5 Database tables và cấu trúc dữ liệu

Với mỗi table/collection/index/graph node/edge, ghi: mục đích; khóa và quan hệ; cột/trường, kiểu, nullability; unique/index; tenant/ACL; `document_version`/`rule_version`; provenance; cách cập nhật, lưu giữ và vô hiệu hóa dữ liệu phụ thuộc. Nêu rõ kho chứa (raw storage, structured DB, vector DB, rule registry, C2 graph, ledger) và nguồn chân lý của từng trường. Nếu module không sở hữu bảng, ghi các bảng **đọc** và module sở hữu; không tạo bảng trùng vì tiện mô tả.

### 3.6 Functions/classes/APIs và tools cần phát triển

Liệt kê giao diện đề xuất ở mức chữ ký và hành vi: tên, trách nhiệm, input/output schema, side effects, quyền cần thiết, tính idempotent, timeout và lỗi trả về. Phân biệt API bên ngoài, service nội bộ, hàm thuần và tool của C3. Phép tính tài chính/validator phải dùng logic xác định; LLM có thể định tuyến, diễn giải và phản biện nhưng không là máy tính nguồn chân lý. Text2SQL chỉ là tùy chọn có giới hạn quyền và kiểm tra truy vấn.

### 3.7 Xử lý lỗi, retry và trạng thái

Có bảng `điều kiện → trạng thái/response → retry hoặc dừng → dấu vết cần ghi`. Nêu giới hạn retry, idempotency key hoặc cơ chế tránh ghi trùng, xử lý chạy lại sau Colab reset, dữ liệu một phần và hành động khôi phục; các con số chưa chốt ghi `TBD`. Dùng `needs_review`, `unknown`, `abstain` như trạng thái hợp lệ. C4 ghi rõ `ACCEPT`, `REVISE`, `INVESTIGATE`, `ABSTAIN` và vòng quay lại C3 có giới hạn. Tài liệu bị xử lý lại/quy tắc đổi phiên bản phải xét vô hiệu hóa chỉ mục, graph, claim và citation phụ thuộc.

### 3.8 Dependencies

Liệt kê upstream/downstream, kho dữ liệu, API/tool, thư viện/dịch vụ có thể dùng và quyết định còn phụ thuộc. Gắn **Đã chốt/Đề xuất/TBD** cho công nghệ. Nêu hợp đồng tối thiểu phải có để module chạy và đường fallback khi phụ thuộc không sẵn sàng; không coi phương án công nghệ là đã được chọn.

### 3.9 Acceptance criteria và test cases

Tiêu chí phải quan sát được: đầu vào, hành vi, đầu ra, trạng thái và trace kỳ vọng. Lập bảng test: ID, tình huống, fixture/dữ liệu, expected result, lớp test (unit/integration/end-to-end/evaluation). Có ca bình thường, thiếu bằng chứng, lỗi OCR/số liệu, đổi phiên bản, không có quyền, sai phạm vi doanh nghiệp/kỳ, quy định hết hiệu lực hoặc có ngoại lệ, retry, abstention và provenance khi phù hợp. Không tuyên bố đã chạy test hoặc đạt chỉ số chưa đo; ngưỡng đánh giá chưa chốt là `TBD`.

### 3.10 Implementation checklist

Checklist theo thứ tự phụ thuộc: chốt contract → lưu trữ/quyền → logic/tool → observability → test → tích hợp. Mỗi ô là một việc kiểm chứng được, không ghi tiến độ giả. Nếu có rollout theo giai đoạn, gắn rõ đó là **Đề xuất triển khai** và giữ kiến trúc mục tiêu C1–C5 nguyên vẹn.

## 4. Quy tắc xuyên suốt C1–C5

| Khu vực | Điều bắt buộc xuất hiện trong tài liệu liên quan |
|---|---|
| Offline và C1 | PDF/văn bản gốc; metadata, hash, phiên bản, ACL; OCR và bảng/điều khoản; kiểm tra chất lượng OCR riêng với kiểm tra kế toán; chuẩn hóa locale, đơn vị, kỳ, phạm vi, dấu và restatement; kiểm tra áp dụng quy định theo thời gian/ngoại lệ; hai nguồn provenance cho finding. |
| Knowledge Storage và C2 | Raw, structured, vector, rule registry và executable evidence graph; evidence nguồn ở ingestion, claim/finding ở runtime; cạnh tới chứng cứ tài chính và pháp lý; tính lại được; invalidation theo phiên bản. |
| Query/retrieval và C3 | Lựa chọn nhiều tài liệu/doanh nghiệp/kỳ trong phạm vi quyền; ACL tại chunk, SQL row, graph node/edge, rule/document metadata và tool; supervisor chọn tác nhân theo nhu cầu bằng chứng/rủi ro; shared state và vòng lặp có giới hạn. |
| C4 và outputs | Xác minh từng claim/finding; kiểm tra số bằng tool xác định và tính áp dụng pháp lý; bốn quyết định C4; verified ledger; information firewall; ba đầu ra riêng gồm compliance, QA, report. |
| C5 | Benchmark compliance/QA/report, trace, logging, giám sát hiệu năng/chi phí, feedback, bảo trì và ablation C1–C4 có kiểm soát. |

Một điều kiện tài chính bất lợi không đủ để kết luận vi phạm. `Compliance finding` cần quy tắc cụ thể còn hiệu lực, điều kiện áp dụng/ngoại lệ, chứng cứ tài chính, điều khoản trích dẫn và kết quả xác minh; khi không đủ căn cứ, ghi `unknown`/`abstain` theo ngữ cảnh.

## 5. Quy ước viết và tự kiểm

- Viết tiếng Việt ngắn, trực tiếp; giữ tên English technical trên lần xuất hiện đầu khi cần đối chiếu code. Dùng một thuật ngữ và một tên trường nhất quán trong cả file.
- Trích dẫn quyết định bằng ID `D01`–`D18`; link đến tài liệu cha/con. Sơ đồ, bảng và JSON phải khớp nhau về hướng dữ liệu và trạng thái.
- Không đưa pseudo-code triển khai dài, đoạn code chạy được, kết quả đo bịa đặt hoặc khảo cứu học thuật vào design spec. JSON/JSON Schema và chữ ký API chỉ là mô tả giao diện.
- Khi rà soát, kiểm đủ mười mục ở §3, hai sơ đồ Mermaid, các nhãn quyết định, quyền ở tầng truy xuất/dữ liệu, provenance, phiên bản, failure path, acceptance criteria và checklist. Ghi rõ phần nào chưa thể đặc tả vì `TBD`.
