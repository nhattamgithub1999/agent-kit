#!/usr/bin/env python3
"""
PreToolUse hook — plan gate. Chặn ghi file khi phiên CHƯA có plan.

VẤN ĐỀ NÓ GIẢI:
  Prompt và policy chỉ giảm xác suất "nhảy vào code luôn". Hook này làm việc đó
  thành KHÔNG THỂ. Đây là chốt tất định duy nhất cho trụ "có mục tiêu".

ĐẾM LÀ PLAN KHI: một trong PLAN_TOOLS được gọi — EnterPlanMode, ExitPlanMode,
EnterWorktree, TodoWrite. State lưu theo session_id.

CẢNH BÁO ĐÃ VERIFY (19/08/2026): bản Claude Code hiện tại KHÔNG còn tool
TodoWrite. Nếu PLAN_TOOLS chỉ có TodoWrite thì gate không có đường thoát khả
thi → model bị chặn lặp rồi quay ra xin bypass permissions. Vì vậy PLAN_TOOLS
phải luôn chứa ExitPlanMode/EnterPlanMode, và thông điệp chặn phải nêu đường
thoát CÓ THẬT.

NGƯỠNG: PLAN_GATE_FREE_EDITS (mặc định 0 ở profile thorough) = chặn ngay từ lần
ghi đầu nếu chưa có plan. Đặt =1 hoặc =3 nếu thấy bị cản ở việc vặt.
Thoát khẩn: PLAN_GATE=off

MIỄN TRỪ: khi permission_mode == "plan", gate đứng ngoài — plan mode của Claude
Code đã tự chặn ghi file, gate chồng thêm chỉ tạo deadlock. Và file plan của
plan mode (~/.claude/plans/*.md) LUÔN được ghi. Không có
miễn trừ này thì gate tự khoá chính nó: plan mode bắt ghi plan ra file TRƯỚC rồi
mới ExitPlanMode, nhưng lệnh ghi đó lại bị gate chặn → deadlock nếu phiên không
có sẵn tool TodoWrite.

Matcher đề nghị: "Edit|Write|NotebookEdit|TodoWrite|ExitPlanMode|EnterPlanMode|EnterWorktree"
Exit 2 = chặn tool call, stderr quay lại cho model làm lý do.
Nguồn: https://code.claude.com/docs/en/hooks
"""
import json
import os
import pathlib
import sys
import tempfile

STATE = pathlib.Path(tempfile.gettempdir()) / "claude-plan-gate"
FREE = int(os.environ.get("PLAN_GATE_FREE_EDITS", "0"))
PLAN_TOOLS = {"TodoWrite", "ExitPlanMode", "EnterPlanMode", "EnterWorktree"}
PLAN_TOOLS |= set(filter(None, os.environ.get("PLAN_GATE_PLAN_TOOLS", "").split(",")))
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
PLAN_DIR = pathlib.Path.home() / ".claude" / "plans"


def is_plan_file(payload) -> bool:
    """File plan của plan mode — miễn trừ, xem docstring đầu file."""
    path = (payload.get("tool_input") or {}).get("file_path")
    if not path:
        return False
    try:
        return pathlib.Path(path).resolve().parent == PLAN_DIR.resolve()
    except OSError:
        return False


def main() -> int:
    if os.environ.get("PLAN_GATE", "").lower() == "off":
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return 0  # FAIL-OPEN

    tool = payload.get("tool_name") or payload.get("toolName") or ""
    mode = payload.get("permission_mode") or payload.get("permissionMode") or ""
    sid = str(payload.get("session_id") or payload.get("sessionId") or "default")
    sid = "".join(ch for ch in sid if ch.isalnum() or ch in "-_")[:64] or "default"

    try:
        STATE.mkdir(parents=True, exist_ok=True)
        planned = STATE / f"{sid}.plan"
        counter = STATE / f"{sid}.edits"
    except OSError:
        return 0  # FAIL-OPEN

    # PHẢI đứng TRƯỚC mọi return sớm khác: ExitPlanMode luôn tới với
    # permission_mode == "plan", nếu bị short-circuit thì gate mất đường mở khoá.
    if tool in PLAN_TOOLS:
        planned.touch()
        counter.unlink(missing_ok=True)
        return 0
    if mode == "plan":
        return 0  # plan mode tự chặn ghi rồi; gate chồng lên = deadlock
    if tool not in WRITE_TOOLS:
        return 0
    if planned.exists():
        return 0
    if is_plan_file(payload):
        return 0

    n = int(counter.read_text() or 0) if counter.exists() else 0
    n += 1
    counter.write_text(str(n))
    if n <= FREE:
        return 0

    print(
        f"BLOCKED bởi plan-gate: đây là lần ghi file thứ {n} trong phiên mà chưa "
        "có plan nào. Ba đường thoát, dùng cái nào cũng được — KHÔNG xin nâng "
        "quyền / bypass permissions, nâng quyền KHÔNG gỡ được gate này: "
        "(1) vào plan mode rồi ExitPlanMode với plan 3–7 bước kèm DoD kiểm chứng "
        "được; (2) gọi TodoWrite nếu phiên có tool đó; (3) task quá nhỏ để cần "
        "plan → chạy lại với PLAN_GATE=off.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
