# How to use in VS Code Codex

1. Extract this entire directory into your VS Code workspace, keeping the PNG reference image under `context/`.
2. Open the project folder as workspace.
3. Paste the prompt below into Codex:

> Hãy đọc toàn bộ `AGENTS.md`, `context/PROJECT_CONTEXT.md`, `context/ARCHITECTURE_DECISIONS.md` và xem sơ đồ `context/architecture-reference.png`. Đây là kiến trúc C1–C5 đã chốt; không thay đổi hoặc đơn giản hóa. Trước tiên hãy tạo `docs/00-overview.md` theo thiết kế top-down, có sơ đồ Mermaid Level 0 rõ ràng, bố cục thoáng, tách Offline/Online/Storage/Evaluation, giải thích dữ liệu đi qua từng khối và mối quan hệ C1–C5. Sau đó hãy nêu ngắn gọn dàn ý các file Level 1/2 còn lại để tôi duyệt; chưa viết tất cả một lần. Phân biệt kiến trúc đề xuất với phần đã triển khai và đánh dấu TBD khi thiếu thông tin.

4. Check output against source image. Iterate on the overview before asking Codex to expand one Markdown file at a time.

Keep the source PNG and project context under version control, along with generated docs.
