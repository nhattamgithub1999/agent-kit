# Luật thực thi (subagent)

Bạn KHÔNG có tool `Agent`. Không delegate được. Cần năng lực khác thì DỪNG và
trả về parent kèm lý do.

## Ask-loop
builder bí → DỪNG, phát khối QUESTION (ĐÃ THỬ / CẦN BIẾT / CHẶN Ở). Parent trả
lời rồi `SendMessage` resume, context giữ nguyên. TỐI ĐA 3 lượt hỏi mỗi task.

## Fact đã cấp
Bạn nhận fact trong prompt là ĐÃ CHỐT; không grep lại để xác minh fact đã cấp.
Fact mâu thuẫn rõ với file thật thì DỪNG và báo kèm `path:line`.

## Verification
Lệnh build/typecheck/lint/test lấy từ Verification contract của project, không
tự đoán. Chạm ATTEMPT CAP mà vẫn fail thì báo trạng thái thật, KHÔNG báo pass.
