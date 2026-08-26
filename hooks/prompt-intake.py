#!/usr/bin/env python3
"""
UserPromptSubmit hook — tiêm chỉ dẫn workflow KỀ BÊN prompt của user.

VẤN ĐỀ NÓ GIẢI:
  CLAUDE.md nạp một lần lúc khởi phiên. Đến message thứ 20 nó cách xa vài chục
  nghìn token và cạnh tranh attention với message vừa gõ → agent "quên" workflow.
  Hook này fire MỖI LƯỢT, khoảng cách vị trí = 0.

  Hook KHÔNG tự phân loại prompt (bản trước làm vậy bằng heuristic từ khoá —
  dễ sai và không đáng tin). Hook chỉ nhắc lại quy ước cố định của workspace:
  đọc file thật trước khi lập plan/giao việc, plan ghi rõ người phụ trách mỗi
  bước, và khai bậc thực thi trước khi làm.

CƠ CHẾ: exit 0 + stdout → Claude Code inject stdout làm context cho model.
Nguồn: https://code.claude.com/docs/en/hooks

CHI PHÍ: ~60–100 token mỗi lượt. Ngưỡng INTAKE_MIN_CHARS lọc slash command và
xác nhận ngắn; các lượt giao việc còn lại đều được tiêm.
"""
import json
import os
import re
import sys

MIN_CHARS = int(os.environ.get("INTAKE_MIN_CHARS", "12"))

# Lượt không phải giao task -> không tiêm.
SKIP = re.compile(
    r"^\s*(/|@|ok\b|oke\b|ừ\b|đúng\b|tiếp\b|continue\b|thanks|cảm ơn|"
    r"yes\b|no\b|y\b|n\b)",
    re.I,
)

# PHRASING: docs cảnh báo rằng text đóng khung như MỆNH LỆNH HỆ THỐNG out-of-band
# có thể kích hoạt phòng thủ prompt-injection của Claude, khiến nó HIỆN text ra cho
# user thay vì coi là context. Nên chuỗi dưới viết ở dạng PHÁT BIỂU SỰ THẬT về
# quy ước của workspace, không phải câu lệnh, và không khẳng định prompt hiện tại
# thuộc lớp nào.
# Nguồn: https://code.claude.com/docs/en/hooks (mục "Add context for Claude")
BASE = (
    "Quy ước xử lý task trong workspace này: trước khi lập plan hoặc giao việc, "
    "người thực thi đọc file thật liên quan thay vì suy đoán từ tên hàm/config. "
    "Plan ghi người phụ trách mỗi bước theo dạng `[tên-agent]` kèm tiêu chí "
    "nghiệm thu cho bước đó. Trước khi làm, phạm vi được khai theo bậc: bac=1 "
    "là làm thẳng không giao ai, bac=2 là giao một agent đảm nhiệm toàn bộ, "
    "bac=3 là chia nhiều agent theo từng bước trong plan."
)


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
            "additionalContext": BASE,
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
