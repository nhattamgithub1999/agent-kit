# Machine verification contract v1

Tạo contract tại `<project>/.claude/verification.json`. Đây là nguồn duy nhất cho
lệnh build, typecheck, lint và test mà builder được phép chạy để báo READY.

## Cách tạo hoặc cập nhật an toàn

1. Nếu file đã tồn tại, đọc và parse object hiện có trước khi sửa. Merge đúng các
   field của schema v1 rồi serialize lại thành một JSON object duy nhất; không
   append object thứ hai và không overwrite mù command/lý do riêng của project.
2. Nếu file chưa tồn tại, tạo parent directory và một file từ schema dưới đây.
3. Chạy lại cùng một thao tác với cùng input phải cho cùng nội dung ngữ nghĩa,
   không nhân đôi key hoặc lý do. Gặp conflict hay schema lạ thì dừng để xác nhận.
4. Thay các command ví dụ bằng lệnh chính xác của project và kiểm tra từng `cwd`
   resolve thành directory tồn tại bên trong project trước khi lưu.

Schema v1 không có nhánh theo platform. Mỗi command phải chạy nguyên văn trên mọi
platform mà project hỗ trợ; nếu cần, dùng một wrapper cross-platform được track
trong repo và khai báo exact command gọi wrapper đó. Không thêm field tự chế.

## JSON mẫu hợp lệ

Các command dưới đây chỉ là ví dụ, không phải mặc định cho mọi project:

```json
{
  "version": 1,
  "steps": {
    "build": {
      "command": "npm run build",
      "cwd": "."
    },
    "typecheck": null,
    "lint": {
      "command": "npm run lint",
      "cwd": "."
    },
    "test": {
      "command": "npm test",
      "cwd": "."
    }
  },
  "n_a_reasons": {
    "typecheck": "N/A: project này chưa cấu hình static type checker"
  }
}
```

## Bất biến schema

- Root có đúng `version`, `steps`, `n_a_reasons`; `version` bằng `1`.
- `steps` có đúng bốn key `build`, `typecheck`, `lint`, `test`.
- Step active có đúng `command` và `cwd`, đều là chuỗi không rỗng; `cwd` là path
  tương đối và không được thoát khỏi project.
- Step N/A là `null`; `n_a_reasons` chứa đúng các step `null`, với lý do không
  rỗng bắt đầu bằng `N/A:`. Step active không có entry trong `n_a_reasons`.
- Không dùng `null` để che command đang fail hoặc tool chưa được cài.
