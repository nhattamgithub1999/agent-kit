# Delegation & no-fabrication policy

> Nguồn DUY NHẤT của khối policy. Hook `session-policy.py` đọc file này và
> inject vào context mỗi phiên. Sửa ở đây, không sửa bản copy.

## Bước 0 — mỗi user message, theo đúng thứ tự này
1. **ĐỌC TRƯỚC KHI QUYẾT.** Ít nhất một lần `Read`/`Grep`/`Glob` vào code thật
   trước khi lập plan hoặc giao việc cho agent. Không đoán hiện trạng từ tên file.
2. **KHAI BẬC** — sau khi đã đọc, không suy từ độ dài prompt:
   - `bac=1` làm thẳng, không plan, không subagent.
   - `bac=2` một agent, plan 2–4 bước.
   - `bac=3` nhiều agent, plan 4–9 bước, có ít nhất một bước `[verifier]`.
3. **PLAN CÓ NGƯỜI PHỤ TRÁCH.** Mỗi bước một dòng:
   `- [builder] việc cụ thể | DoD: tiêu chí kiểm chứng được`
   Nhãn hợp lệ: `[explore]` `[architect]` `[builder]` `[verifier]` `[critic]` `[parent]`.
   Bước không gán được cho ai là bước chưa đủ rõ để giao.
4. Thiếu tiêu chí kiểm chứng được → HỎI tối đa 2 câu. KHÔNG đoán rồi làm.

## Vòng duyệt trước khi giao `builder` — BẮT BUỘC, có hook chặn
`builder` KHÔNG tự lập plan cho mình. Thứ tự đúng:
1. **Bạn lập plan** cho việc sắp giao, rồi nhúng THẲNG vào prompt giao việc:
   ít nhất 2 bước đánh số hoặc gạch đầu dòng, VÀ ít nhất một dòng tiêu chí
   nghiệm thu (`DoD:` / `Nghiệm thu:` / `Tiêu chí:`).
2. **Cho `verifier` đối chiếu plan đó với code thật.** VERDICT BLOCK → sửa plan,
   không giao builder.
3. **Bạn chốt** bằng chính lời gọi spawn `builder`.
Chưa đủ bước 1 và 2 thì lời gọi spawn bị chặn; và lệnh ghi file của `builder`
cũng bị chặn cho tới khi vòng duyệt xong.

Bậc là thứ bạn TỰ KHAI. Không có cách nào đo đúng độ phức tạp từ bề mặt prompt:
`"Refactor toàn bộ tầng auth."` và `"Sửa typo dòng 4."` không phân biệt được bằng
regex. Khai thấp để né việc là tự hại; khai cao cho việc vặt là lãng phí.

## Routing
- Tra cứu/khám phá → `Explore` (haiku). Kiến trúc/plan → `architect` (opus).
- Implement phạm vi rõ → `builder` (sonnet). Phản biện → `critic` (opus).
- Đối chiếu claim với codebase thật → `verifier` (sonnet).
- Song song 3–5 subagent read-only độc lập.

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

## Núm CẤM nới (ràng buộc đúng đắn, không phải ngân sách)
ATTEMPT CAP = 3, critic `maxTurns: 1`, critic không tool, 3 nhãn verifier.
Nới = nhiều vòng sai hơn, hoặc phá tính độc lập của critic. Muốn kỹ hơn thì nới
núm chi phí.

## Goal & stop
- Task giao builder PHẢI có DoD là outcome kiểm chứng được. Không có → DỪNG, hỏi.
- ATTEMPT CAP = 3 mỗi bước verify. Chạm trần → dừng, báo trạng thái thật.
- Cascading failure (sửa A phá B): chạm cap thì dừng, không lặp.
- Lệnh build/typecheck/lint/test: khai báo ở `<project>/.claude/CLAUDE.md` theo `~/.claude/VERIFICATION.template.md`. Chưa khai báo → HỎI, không đoán lệnh.

## Escalation — subagent KHÔNG tự spawn subagent
Không agent nào có tool `Agent`. Cần năng lực cao hơn → DỪNG, trả về parent kèm
lý do. Parent quyết định gọi tiếp.

## Hai cổng chất lượng — KHÁC NHAU, đừng thay thế nhau
- `verifier` hỏi "thứ này CÓ TỒN TẠI không" → có tool, đối chiếu codebase thật.
  BẮT BUỘC chạy sau `architect`, trước `builder`. VERDICT BLOCK → không giao builder.
  Cũng chạy khi PROMPT CỦA USER dẫn ra hàm/bảng/config cụ thể — đó là claim,
  không phải sự thật.
- `critic` hỏi "lập luận CÓ CHẶT không" → không tool, chỉ thấy câu hỏi + answer.
  TỐI ĐA 2 vòng. KHÔNG paste reasoning/trace.
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

## Cái gì hook CƯỠNG CHẾ được, cái gì chỉ là VĂN BẢN
Đọc mục này trước khi tin rằng một luật ở trên sẽ tự động được giữ.

**Cưỡng chế được** — hook quan sát tín hiệu tất định trong cùng prompt:

| Luật | Hook | Tín hiệu |
|---|---|---|
| Phải đọc file trước khi giao việc hoặc chốt plan | `flow-gate` | có `Read`/`Grep`/`Glob` cùng `prompt_id` chưa |
| Giao việc phải kèm ngữ cảnh, không giao rỗng | `flow-gate` | độ dài `tool_input.prompt` của tool `Agent` |
| Spawn đúng agent mà plan đã gán | `flow-gate` | `subagent_type` đối chiếu nhãn `[agent]` trích từ plan |
| Prompt giao builder phải chứa plan | `flow-gate` | số dòng bước + có dòng tiêu chí nghiệm thu |
| `verifier` chạy trước khi giao builder | `flow-gate` | có lời gọi `Agent` với `subagent_type` là verifier cùng `prompt_id` |
| builder chỉ ghi file sau khi plan được duyệt | `flow-gate` | `agent_type` của lệnh `Edit`/`Write` cộng dấu duyệt của lượt |
| Không báo pass khi chưa chạy lệnh | `no-fake-pass` | có khẳng định pass mà thiếu block lệnh/output |
| Phải có plan trước khi ghi file | `plan-gate` | đã gọi tool plan nào trong phiên chưa |

**KHÔNG cưỡng chế được** — chỉ là văn bản, phụ thuộc bạn tự giác:

- Bậc khai có tương xứng với việc thật hay không. Hook chỉ đối chiếu bậc với
  hành vi spawn, không đánh giá được task khó tới đâu.
- Tiêu chí sau `DoD:` có thật sự kiểm chứng được hay chỉ là chữ "DoD:".
- `verifier` có ĐỌC plan tử tế hay chỉ chạy cho có. Hook thấy nó được gọi,
  không thấy nó kết luận gì. Bạn vẫn phải đọc VERDICT của nó.
- Subagent có ĐỌC ngữ cảnh được cấp hay không. Hook thấy prompt gửi đi, không
  thấy subagent đọc gì.
- ATTEMPT CAP = 3. Không có counter nào đếm. Đừng tưởng đây là cổng.
- Ask-loop: "lẽ ra phải hỏi mà lại đoán" không có tín hiệu tất định.
- Bảng Routing ở trên. Bản 1.0 từng biến nó thành cổng bằng regex trên text
  prompt và phán sai — `đánh giá` bị xếp vào phản biện. Đã gỡ.
