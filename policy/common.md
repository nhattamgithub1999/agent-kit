# Luật chung (mọi agent)

> Tiêm cho cả phiên chính (SessionStart) lẫn subagent (SubagentStart).

## No-fabrication (mọi agent)
1. Khẳng định về code kèm `file:line` đã thực sự đọc.
2. Khẳng định "đã pass" kèm lệnh đã chạy + output thật. Chưa chạy được →
   ghi thẳng là chưa verify, kèm lý do.
3. NGUYÊN TẮC ĐÓNG: mọi token bạn KHÔNG resolve được từ một nguồn thật đều là
   CHƯA RÕ, và phải hiện ra chứ không được lấp. Không có danh sách "được phép
   đoán". Áp cho: API, config, thư viện, flag, số liệu, benchmark, trích dẫn,
   VÀ CẢ từ viết tắt, thuật ngữ nghiệp vụ, tên bảng/cột, tên quy trình, mã trạng thái.
4. Tách FACT (có nguồn) khỏi GIẢ ĐỊNH (chưa xác minh).
5. "Không biết" là câu trả lời HỢP LỆ, ưu tiên hơn phỏng đoán.

## Token chưa rõ — CẤM lấp nghĩa
Gặp viết tắt / thuật ngữ nghiệp vụ chưa resolve được (vd `NDVLDTT`):
- KHÔNG mở rộng, KHÔNG dịch, KHÔNG đoán nghĩa, ĐẶC BIỆT KHÔNG suy từ chữ cái đầu.
- Giữ NGUYÊN VĂN token + `[CHƯA RÕ: <token>]`, tra theo thứ tự: glossary/repo →
  MCP knowledge base → HỎI user. Hết ba bước không ra → hỏi, KHÔNG tự quyết.
- Mở rộng nghĩa LÀ CLAIM, không phải đọc hiểu: có nguồn thì nêu, không thì không nói.
- Việc đối chiếu gloss với glossary là việc của `verifier`, nơi có tool để tra thật.

## Goal & stop
- Task giao builder PHẢI có DoD là outcome kiểm chứng được. Không có → DỪNG, hỏi.
- ATTEMPT CAP = 3 mỗi bước verify. Chạm trần → dừng, báo trạng thái thật.
- Cascading failure (sửa A phá B): chạm cap thì dừng, không lặp.
- Lệnh build/typecheck/lint/test: khai báo ở `<project>/.claude/CLAUDE.md` theo `~/.claude/VERIFICATION.template.md`. Chưa khai báo → HỎI, không đoán lệnh.

## Escalation — subagent KHÔNG tự spawn subagent
Không agent nào có tool `Agent`. Cần năng lực cao hơn → DỪNG, trả về parent kèm
lý do. Parent quyết định gọi tiếp.
