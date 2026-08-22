#!/usr/bin/env python3
"""
Inject khối policy (Bước 0 / delegation / no-fabrication) vào context.

VẤN ĐỀ NÓ GIẢI:
  Plugin KHÔNG load được CLAUDE.md — "A CLAUDE.md file at the plugin root is NOT
  loaded as project context" (https://code.claude.com/docs/en/plugins-reference).
  Bản cài thủ công dựa vào việc append policy vào ~/.claude/CLAUDE.md. Là plugin
  thì phải tự inject, nếu không kit mất trụ "có mục tiêu" ngay từ lượt đầu.

NGUỒN POLICY: <plugin root>/policy/delegation.md — nguồn DUY NHẤT, không copy.

CHẠY ĐƯỢC Ở CẢ HAI EVENT:
  SessionStart      -> inject 1 lần/phiên (rẻ nhất)
  UserPromptSubmit  -> inject ở prompt ĐẦU TIÊN của phiên, các lượt sau bỏ qua
                       (state theo session_id ở tmp), tránh trả token mỗi lượt.
  `hookEventName` lấy TỪ PAYLOAD, không hard-code, nên gắn event nào cũng đúng.

CHỈNH:
  POLICY_HOOK=off          tắt hẳn
  POLICY_FILE=<path>       dùng file policy khác

FAIL-OPEN: không đọc được policy hoặc payload -> exit 0, không chặn gì.
"""
import json
import os
import pathlib
import sys
import tempfile

STATE = pathlib.Path(tempfile.gettempdir()) / "claude-policy-injected"


def policy_path() -> pathlib.Path:
    env = os.environ.get("POLICY_FILE")
    if env:
        return pathlib.Path(env).expanduser()
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    base = pathlib.Path(root) if root else pathlib.Path(__file__).resolve().parent.parent
    return base / "policy" / "delegation.md"


def already_done(session: str) -> bool:
    """Chỉ dùng cho event lặp lại nhiều lần trong 1 phiên."""
    if not session:
        return False
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        marker = STATE / session.replace("/", "_")[:120]
        if marker.exists():
            return True
        marker.touch()
    except OSError:
        return False
    return False


def main() -> int:
    if os.environ.get("POLICY_HOOK", "").lower() == "off":
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    event = payload.get("hook_event_name") or payload.get("hookEventName") or "SessionStart"
    if event != "SessionStart" and already_done(str(payload.get("session_id") or "")):
        return 0

    try:
        text = policy_path().read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    if not text:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
