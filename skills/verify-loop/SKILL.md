---
name: verify-loop
description: >-
  Chạy verification loop trên thay đổi hiện tại: build, typecheck, lint, test,
  cộng project-specific rule. Sửa và chạy lại tới khi pass hoặc chạm attempt
  cap. Use when a code change is complete and needs verifying before report.
allowed-tools: Read, Edit, Bash, Grep, Glob
---

# Verification loop

Đọc mục "Verification contract" trong CLAUDE.md CỦA PROJECT để lấy LỆNH CHÍNH XÁC.
Không tự suy đoán lệnh; không có lệnh khai báo → báo và dừng.

## Các bước
1. Chạy lần lượt: build → typecheck → lint → test.
2. Mỗi lỗi: báo `file:line` + thông điệp lỗi THẬT (copy từ output).
3. Sửa nguyên nhân, chạy lại bước fail.
4. ATTEMPT CAP = 3 mỗi bước. Chạm trần → DỪNG, báo trạng thái thật.

## Cấm
- KHÔNG báo pass khi chưa chạy được lệnh → ghi "CHƯA VERIFY: <lý do>".
- KHÔNG tóm tắt output test bằng trí nhớ; trích output thật.

## Output
### Kết quả từng bước
- build / typecheck / lint / test: pass|fail (lệnh + trích output)
### Vi phạm project rules
- `file:line` — rule bị vi phạm → đã sửa / chưa sửa
### VERDICT: READY | NOT READY (+ lý do)
