# Luật điều phối (phiên chính)

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

## Núm CẤM nới (ràng buộc đúng đắn, không phải ngân sách)
ATTEMPT CAP = 3, critic `maxTurns: 1`, critic không tool, 3 nhãn verifier.
Nới = nhiều vòng sai hơn, hoặc phá tính độc lập của critic. Muốn kỹ hơn thì nới
núm chi phí.

## Hai cổng chất lượng — KHÁC NHAU, đừng thay thế nhau
- `verifier` hỏi "thứ này CÓ TỒN TẠI không" → có tool, đối chiếu codebase thật.
  BẮT BUỘC chạy sau `architect`, trước `builder`. VERDICT BLOCK → không giao builder.
  Cũng chạy khi PROMPT CỦA USER dẫn ra hàm/bảng/config cụ thể — đó là claim,
  không phải sự thật.
- `critic` hỏi "lập luận CÓ CHẶT không" → không tool, chỉ thấy câu hỏi + answer.
  TỐI ĐA 2 vòng. KHÔNG paste reasoning/trace.
Chỉ bật cho quyết định/plan quan trọng.

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
- Findings của `architect` có được copy NGUYÊN VĂN sang prompt giao `builder` hay không.
  Không có artifact trung gian nào để hook đối chiếu, và `architect` không có tool `Write`
  nên không tự ghi ra file được. Đây là kỷ luật của bạn, không phải cổng.
- ATTEMPT CAP = 3. Không có counter nào đếm. Đừng tưởng đây là cổng.
- Ask-loop: "lẽ ra phải hỏi mà lại đoán" không có tín hiệu tất định.
- Bảng Routing ở trên. Bản 1.0 từng biến nó thành cổng bằng regex trên text
  prompt và phán sai — `đánh giá` bị xếp vào phản biện. Đã gỡ.
