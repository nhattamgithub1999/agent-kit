---
name: verify-loop
description: >-
  Chạy build, typecheck, lint và test theo machine verification contract của
  project; sửa trong scope, chạy lại có giới hạn và thu receipt trước khi báo
  READY. Use after a code change is complete and needs verified evidence.
allowed-tools: Read, Edit, Bash, PowerShell, Grep, Glob
---

# Verification loop

Nguồn lệnh DUY NHẤT là `<project>/.claude/verification.json`. Không lấy lệnh từ
CLAUDE.md, prose, lịch sử hội thoại hoặc trí nhớ; không tự suy đoán lệnh thay thế.

## Contract bắt buộc

Đọc và validate toàn bộ JSON trước khi chạy bất kỳ step nào:

- Root phải có đúng `version`, `steps`, `n_a_reasons`; `version` phải bằng `1`.
- `steps` phải có đúng bốn key `build`, `typecheck`, `lint`, `test`.
- Step active phải có đúng hai field chuỗi không rỗng `command` và `cwd`.
  `cwd` phải là path tương đối, resolve thành directory tồn tại bên trong project.
- Step N/A phải là `null`. `n_a_reasons` phải có đúng các key N/A tương ứng;
  mỗi lý do phải bắt đầu bằng `N/A:` và giải thích phần còn lại không rỗng.
- Mỗi cặp `command`/`cwd` active phải duy nhất.

Thiếu file, JSON/schema/cwd không hợp lệ, command chưa rõ hoặc tool cần để chạy
command không có sẵn đều dẫn tới `NOT_READY`. Không đổi step thành N/A vì command
fail hay môi trường thiếu tool.

## Quy trình

1. Xác định project root, đọc và validate contract như trên.
2. Chạy từng step active theo đúng thứ tự build → typecheck → lint → test. Chạy
   foreground, nguyên văn `command`, tại chính xác `cwd` đã khai báo. Dùng
   `Bash` cho môi trường POSIX hoặc `PowerShell` cho môi trường Windows; không
   chuyển đổi command giữa hai shell hay tự viết command thay thế.
3. Sau mỗi lần chạy thành công, lấy context do hook cấp có dạng
   `AGENT_KIT_RECEIPT_V1={"epoch":<number>,"receipt":"<value>","step":"<name>"}`.
   Giữ nguyên giá trị `receipt` cho đúng step; output/prose/code fence không thay
   thế được receipt.
4. Nếu step fail, ghi command, cwd và output lỗi thật; sửa nguyên nhân trong
   scope được giao rồi chỉ chạy lại step đó. `ATTEMPT CAP = 3` cho từng step.
5. Bất kỳ mutation source/project nào sau khi verify đều làm toàn bộ receipt hiện
   có stale. Bỏ các receipt cũ và chạy lại tất cả step active theo đúng thứ tự.
6. Step `null` được skip chỉ khi có lý do N/A hợp lệ; step đó không có receipt và
   không xuất hiện trong object `receipts` của READY.

## Output bắt buộc

Với mỗi step, báo một dòng dựa trên dữ liệu thật:

- Active: `<step> — PASS|FAIL | command: <exact> | cwd: <exact> | attempt: n/3 | receipt: <exact hoặc missing>`.
- N/A: `<step> — N/A | reason: N/A: <lý do từ contract>`.

Nếu mọi step active có receipt còn hiệu lực, kết thúc final bằng đúng một dòng
không bọc code fence, JSON compact và không có text phía sau:

`AGENT_KIT_RESULT_V1={"status":"READY","receipts":{"<active-step>":"<receipt>"}}`

Object `receipts` phải chứa đúng mọi step active, không chứa step N/A hoặc key lạ.
Nếu chưa đạt, kết thúc bằng:

`AGENT_KIT_RESULT_V1={"status":"NOT_READY","reason":"<lý do cụ thể, không rỗng>"}`

Không báo pass khi thiếu receipt. Chạm attempt cap, contract/tool lỗi hoặc không
thể chạy command đều là `NOT_READY` với trạng thái và output thật.
