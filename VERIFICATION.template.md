# Verification contract — DÁN VÀO `<project>/.claude/CLAUDE.md`, KHÔNG PHẢI GLOBAL

Lệnh build/test khác nhau theo từng project, nên khối này KHÔNG được đặt trong
`~/.claude/CLAUDE.md`. Copy vào file CLAUDE.md của TỪNG project và điền lệnh thật.

## Verification contract

```
build:     <vd: dotnet build>
typecheck: <vd: tsc --noEmit>
lint:      <vd: npm run lint>
test:      <vd: dotnet test>
```

Chưa khai báo → agent PHẢI hỏi, KHÔNG được tự đoán lệnh.

## Project-specific deterministic rules
Rule mà linter tổng quát không bắt được; viết dạng kiểm chứng được:
- vd: từ chối migration xóa cột mà không có bước backfill.
- vd: log ở error path phải có request ID, không chứa request body.

## Tiêu chí thành công phải quan sát được
- ĐÚNG: "0 lỗi console mới", "test suite pass", "không element chồng nhau".
- SAI: "trông ổn", "đủ tốt", "hợp lý".
