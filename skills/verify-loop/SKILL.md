---
name: verify-loop
description: >-
  Chạy verification loop cho thay đổi vừa làm: build → typecheck → lint → test
  + rule riêng của project; sửa và chạy lại tới khi pass hoặc hết 3 lần mỗi
  bước. Use when a code change is complete and needs verifying before report.
allowed-tools: Read, Edit, Bash, Grep, Glob
---

# Verification loop

## 1. Xác định lệnh (theo thứ tự ưu tiên)
1. "Verification contract" hoặc lệnh build/test ghi trong CLAUDE.md của
   project (kể cả thư mục cha trong repo).
2. Không có → suy từ manifest trong repo: `package.json` scripts, `*.sln` /
   `*.csproj` (`dotnet build`, `dotnet test`), `pyproject.toml` / `pytest.ini`,
   `go.mod`, `Cargo.toml`, `Makefile`… Chỉ dùng script/target thật sự có trong
   file đã đọc.
3. Trước khi chạy script lấy từ manifest, đọc nội dung script (kể cả hook
   `pre*`/`post*` đi kèm). Script deploy, migrate, ghi vào DB/service thật,
   hoặc chỉ là placeholder (`echo "no test" && exit 1`) → không chạy.
4. Không xác định được lệnh an toàn → ghi "CHƯA VERIFY: <lý do>".
Bước nào project không có (vd không có lint) → "không áp dụng", không bịa lệnh.

## 2. Chạy và sửa
- Chạy lần lượt các bước. Nếu project hỗ trợ, chạy phạm vi hẹp trước (test của
  module vừa sửa), rồi full nếu chi phí hợp lý.
- Lỗi: ghi `file:line` + thông điệp thật từ output, sửa nguyên nhân, chạy lại
  bước đó. Tối đa 3 lần mỗi bước; hết lượt hoặc sửa A hỏng B → dừng, báo thật.
- Lỗi có sẵn không do thay đổi này gây ra → ghi nhận, không sửa lan ra ngoài
  phạm vi.
- Không tóm tắt output test theo trí nhớ; trích output thật.

## Output
### Lệnh (nguồn: CLAUDE.md | manifest `<file>`)
### Kết quả từng bước
- build / typecheck / lint / test: pass | fail | không áp dụng — trích output
### VERDICT: READY | NOT READY (+ lý do)
