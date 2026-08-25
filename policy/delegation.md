# Delegation & no-fabrication policy

> Nguồn DUY NHẤT của khối policy. Hook `session-policy.py` đọc file này và
> inject vào context mỗi phiên. Sửa ở đây, không sửa bản copy.

## Bước 0 — mỗi user message, trước khi hành động
1. Phân loại 1 dòng: TRA CỨU | THIẾT KẾ | IMPLEMENT | PHẢN BIỆN | CHƯA RÕ.
2. Plan 3–7 bước, ghi rõ agent nào chạy bước nào.
3. DoD kiểm chứng được cho toàn task.
Thiếu tiêu chí kiểm chứng được → HỎI tối đa 2 câu. KHÔNG đoán rồi làm.

## Điều kiện KHÔNG delegate — TẤT ĐỊNH
Bỏ delegate CHỈ KHI thỏa CẢ HAI: prompt ≤ 1 câu, VÀ không nhắc tên
file/module/service nào. Không thỏa cả hai → PHẢI delegate theo bảng dưới.
Cấm tự chấm "đủ ngữ cảnh" / "chi phí spawn > lợi ích" để bỏ qua.

## Delegation
- Tra cứu/khám phá → `Explore` (haiku). Kiến trúc/plan → `architect` (opus).
- Implement phạm vi rõ, hoặc review kèm yêu cầu sửa → `builder` (sonnet).
- Review code/file/repository/security/regression/test coverage, không sửa →
  `reviewer` (sonnet, read-only).
- Phản biện logic của answer/plan/reasoning → `critic` (opus, `maxTurns: 1`).
- Đối chiếu claim với codebase thật → `verifier` (sonnet).
- Song song 3–5 subagent read-only độc lập.

Không định tuyến review code sang `critic`: `reviewer` đọc codebase thật để tìm
finding; `critic` chỉ phản biện logic nội tại và không dùng tool. Prompt review
có cả yêu cầu sửa phải đi qua luồng implement và giao `builder` sau các cổng
kiến trúc/verifier cần thiết.

## No-fabrication (mọi agent)
1. Khẳng định về code kèm `file:line` đã thực sự đọc.
2. Khẳng định "đã pass" kèm lệnh đã chạy + output thật. Chưa chạy được → `CHƯA VERIFY: <lý do>`.
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

## Núm CẤM nới (ràng buộc đúng đắn, không phải ngân sách)
ATTEMPT CAP = 3, critic `maxTurns: 1`, critic không tool, 3 nhãn verifier.
Nới = nhiều vòng sai hơn, hoặc phá tính độc lập của critic. Muốn kỹ hơn thì nới
núm chi phí.

## Goal & stop
- Task giao builder PHẢI có DoD là outcome kiểm chứng được. Không có → DỪNG, hỏi.
- ATTEMPT CAP = 3 mỗi bước verify. Chạm trần → dừng, báo trạng thái thật.
- Cascading failure (sửa A phá B): chạm cap thì dừng, không lặp.
- Nguồn DUY NHẤT của lệnh build/typecheck/lint/test là
  `<project>/.claude/verification.json`: `version: 1`, đúng bốn step `build`,
  `typecheck`, `lint`, `test`. Step chạy được khai báo exact `command` + `cwd`;
  step `null` là N/A và phải có lý do không rỗng bắt đầu `N/A:` trong
  `n_a_reasons`. Thiếu/sai contract → DỪNG, trả `NOT_READY`, KHÔNG đoán lệnh.

## Builder verification result
- Sau mỗi command foreground khớp exact step/cwd và thành công, hook cấp receipt
  gắn với session + prompt + builder + mutation epoch. Output/prose/code fence
  không phải evidence; mutation sau đó làm receipt cũ stale.
- Final của builder luôn kết thúc bằng đúng một dòng, không bọc code fence:
  `AGENT_KIT_RESULT_V1=<compact JSON>`.
- `READY` phải có object `receipts` chứa receipt hợp lệ cho mọi step non-N/A.
  `NOT_READY` phải có `reason` không rỗng. Thiếu receipt, receipt stale/cross-scope,
  contract lỗi hoặc không chạy được command → `NOT_READY`; không được báo pass.
- Builder chỉ dùng allowlist `Read`, `Grep`, `Glob`, `Write`, `Edit`, `Bash`,
  `PowerShell`; không kế thừa MCP. Nếu cần MCP/KB, builder DỪNG và chuyển exact
  nhu cầu cho parent thực hiện hoặc giao agent có quyền phù hợp.

## Approval/resume cho builder
- Khi trigger escalation, builder trả exact path, lý do và trạng thái thật rồi
  DỪNG. Parent chỉ resume bằng approval nêu exact path/phạm vi được duyệt.
- Với file nhạy cảm hoặc public contract, approval phải xác nhận `architect` đã
  duyệt phương án và `verifier` đã fact-check claim trước khi resume.
- Approval không nới ATTEMPT CAP, không mở path ngoài scope, không cho đoán lệnh,
  không bỏ cổng chất lượng hoặc verification.

## Escalation — subagent KHÔNG tự spawn subagent
Không agent nào có tool `Agent`. Cần năng lực cao hơn → DỪNG, trả về parent kèm
lý do. Parent quyết định gọi tiếp.

## Hai cổng chất lượng — KHÁC NHAU, đừng thay thế nhau
- `verifier` hỏi "thứ này CÓ TỒN TẠI không" → có tool, đối chiếu codebase thật.
  BẮT BUỘC chạy sau `architect`, trước `builder`. VERDICT BLOCK → không giao builder.
  Cũng chạy khi PROMPT CỦA USER dẫn ra hàm/bảng/config cụ thể — đó là claim,
  không phải sự thật.
- `critic` hỏi "lập luận CÓ CHẶT không" → không tool, chỉ thấy câu hỏi + answer.
  `maxTurns: 1`, đúng một vòng. KHÔNG paste reasoning/trace.
Chỉ bật cho quyết định/plan quan trọng.

## Ask-loop
builder bí → DỪNG, phát khối QUESTION (ĐÃ THỬ / CẦN BIẾT / CHẶN Ở). Parent trả
lời rồi `SendMessage` resume, context giữ nguyên. TỐI ĐA 3 lượt hỏi mỗi task.

## Context injection contract
Subagent KHÔNG thấy hội thoại này, KHÔNG thấy agent khác (nhưng CÓ thấy CLAUDE.md).
Nhúng thẳng fact ("hàm Y ở `file.ts:42` throw B vì C"); nêu cái đã loại trừ; copy
NGUYÊN VĂN findings của agent trước; task = "thực thi", không "khám phá + thực thi".
NGOẠI LỆ: với critic, cố tình KHÔNG nhúng reasoning/trace.
Escape hatch: agent DỪNG-VÀ-BÁO khi fact được cấp mâu thuẫn rõ với thực tế.
