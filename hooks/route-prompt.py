#!/usr/bin/env python3
"""
UserPromptSubmit hook — tiêm chỉ dẫn workflow KỀ BÊN prompt của user.

VẤN ĐỀ NÓ GIẢI:
  CLAUDE.md nạp một lần lúc khởi phiên. Đến message thứ 20 nó cách xa vài chục
  nghìn token và cạnh tranh attention với message vừa gõ → agent "quên" workflow.
  Hook này fire MỖI LƯỢT, khoảng cách vị trí = 0.

  Đồng thời hook TỰ PHÂN LOẠI prompt bằng heuristic tất định, nên bước phân loại
  không còn nằm trong model (nơi nó hay bị bỏ khi model vội làm luôn).

CƠ CHẾ: exit 0 + stdout → Claude Code inject stdout làm context cho model.
Nguồn: https://code.claude.com/docs/en/hooks

CHI PHÍ: ~60–130 token mỗi lượt. Profile thorough đặt MIN_CHARS=12 nên gần
như mọi lượt giao việc đều bị tiêm; chỉ slash command và xác nhận ngắn được bỏ qua.
"""
import json
import os
import re
import sys

MIN_CHARS = int(os.environ.get("ROUTE_MIN_CHARS", "12"))

# Lượt không phải giao task -> không tiêm.
SKIP = re.compile(
    r"^\s*(/|@|ok\b|oke\b|ừ\b|đúng\b|tiếp\b|continue\b|thanks|cảm ơn|"
    r"yes\b|no\b|y\b|n\b)",
    re.I,
)

WRITE_VERB = r"(sửa|fix|thêm|add|implement|triển khai|viết|tạo|refactor|xoá|xóa|remove|đổi|update|migrate)"
READ_VERB = r"(tìm|ở đâu|where|find|grep|liệt kê|list|đọc|xem|check|tra|search|nằm ở)"
DESIGN_VERB = r"(kiến trúc|architecture|thiết kế|design|nên dùng|so sánh|compare|trade.?off|phương án|option|chọn|đánh đổi|scale|schema)"
REVIEW_VERB = r"(review|đánh giá|phản biện|critique|kiểm tra lại|rà lại|có vấn đề gì|audit)"

# PHRASING: docs cảnh báo rằng text đóng khung như MỆNH LỆNH HỆ THỐNG out-of-band
# có thể kích hoạt phòng thủ prompt-injection của Claude, khiến nó HIỆN text ra cho
# user thay vì coi là context. Nên các chuỗi dưới viết ở dạng PHÁT BIỂU SỰ THẬT về
# quy ước của workspace, không phải câu lệnh.
# Nguồn: https://code.claude.com/docs/en/hooks (mục "Add context for Claude")
BASE = ("Quy ước xử lý task trong workspace này: mỗi task đi theo trình tự phân loại "
        "→ plan 3–7 bước → DoD kiểm chứng được. Task không có tiêu chí kiểm chứng "
        "được thì được làm rõ bằng câu hỏi trước, không suy đoán.")

ROUTES = {
    "EXPLORE": "Prompt này thuộc lớp TRA CỨU. Lớp này do agent `Explore` (haiku) đảm nhiệm; main session không grep dàn trải.",
    "BUILD": "Prompt này thuộc lớp IMPLEMENT. Lớp này do agent `builder` đảm nhiệm khi đã có DoD; chưa có DoD thì task được làm rõ trước khi viết code.",
    "DESIGN": "Prompt này thuộc lớp THIẾT KẾ. Lớp này do agent `architect` đảm nhiệm, đầu ra là 2–3 phương án kèm tiêu chí và khuyến nghị; lượt này không sửa code.",
    "REVIEW": "Prompt này thuộc lớp PHẢN BIỆN. Lớp này do agent `critic` đảm nhiệm, và chỉ nhận câu hỏi gốc + answer, không nhận reasoning/trace.",
    "AMBIGUOUS": "Chưa xác định được prompt này thuộc lớp nào. Quy ước ở đây là làm rõ phạm vi và DoD bằng tối đa 2 câu hỏi trước khi hành động.",
}


def classify(p: str) -> str:
    low = p.lower()
    design = bool(re.search(DESIGN_VERB, low))
    review = bool(re.search(REVIEW_VERB, low))
    write = bool(re.search(WRITE_VERB, low))
    read = bool(re.search(READ_VERB, low))
    has_target = bool(re.search(r"[\w/.-]+\.\w{1,5}\b|`[^`]+`", p))

    if review:
        return "REVIEW"
    if design:
        return "DESIGN"
    if write and (has_target or len(p) > 80):
        return "BUILD"
    if read and not write:
        return "EXPLORE"
    if write:
        return "BUILD"
    return "AMBIGUOUS"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return 0  # FAIL-OPEN: không parse được thì không cản trở gì

    prompt = ""
    if isinstance(payload, dict):
        for k in ("prompt", "user_prompt", "message", "text"):
            if isinstance(payload.get(k), str) and payload[k].strip():
                prompt = payload[k]
                break
    if not prompt or len(prompt) < MIN_CHARS or SKIP.match(prompt):
        return 0

    # additionalContext là kênh CÓ CẤU TRÚC: Claude Code bọc nó trong system reminder
    # và chèn đúng cạnh prompt. Ổn định hơn plain stdout.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": f"{BASE}\n{ROUTES[classify(prompt)]}",
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
