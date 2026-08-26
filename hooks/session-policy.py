#!/usr/bin/env python3
"""
Inject khối policy (Bước 0 / delegation / no-fabrication) vào context.

VẤN ĐỀ NÓ GIẢI:
  Plugin KHÔNG load được CLAUDE.md — "A CLAUDE.md file at the plugin root is NOT
  loaded as project context" (https://code.claude.com/docs/en/plugins-reference).
  Bản cài thủ công dựa vào việc append policy vào ~/.claude/CLAUDE.md. Là plugin
  thì phải tự inject, nếu không kit mất trụ "có mục tiêu" ngay từ lượt đầu.

NGUỒN POLICY: <plugin root>/policy/{common,supervisor,worker}.md — nguồn DUY
NHẤT cho mỗi phần, không copy. Chọn file theo event:
  SessionStart     -> common.md + supervisor.md (phiên chính)
  SubagentStart    -> common.md + worker.md (subagent)
  event khác       -> không inject gì.

CHỐNG LẶP:
  SessionStart   KHÔNG chống lặp — nó bắn lại sau /compact và /resume, và đó
                 chính là lúc context vừa bị cắt nên cần nạp lại policy.
  SubagentStart  khoá theo agent_id — event này bắn lại mỗi khi subagent được
                 chạy tiếp sau một lần bị hook khác chặn, nên khoá theo
                 session_id sẽ chặn nhầm các subagent khác trong cùng phiên.
                 Thiếu agent_id thì rơi về session_id.

CHỈNH:
  POLICY_HOOK=off          tắt hẳn
  POLICY_FILE=<path>       ghi đè: dùng đúng một file này cho mọi event ở trên

FAIL-OPEN: không đọc được policy (từng file, hoặc toàn bộ) hay payload -> exit
0, không chặn gì.
"""
import json
import os
import pathlib
import sys
import tempfile

STATE = pathlib.Path(tempfile.gettempdir()) / "claude-policy-injected"


def policy_paths(event: str) -> list[pathlib.Path]:
    env = os.environ.get("POLICY_FILE")
    if env:
        if event in ("SessionStart", "SubagentStart"):
            return [pathlib.Path(env).expanduser()]
        return []
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    base = pathlib.Path(root) if root else pathlib.Path(__file__).resolve().parent.parent
    policy_dir = base / "policy"
    if event == "SessionStart":
        return [policy_dir / "common.md", policy_dir / "supervisor.md"]
    if event == "SubagentStart":
        return [policy_dir / "common.md", policy_dir / "worker.md"]
    return []


def already_done(key: str) -> bool:
    """Chỉ dùng cho event lặp lại nhiều lần trong 1 phiên/subagent."""
    if not key:
        return False
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        marker = STATE / key.replace("/", "_")[:120]
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

    # SessionStart KHÔNG chống lặp. Nó bắn lại sau /compact và /resume với cùng
    # session_id — đúng lúc context vừa bị cắt ngắn nên policy cần được nạp lại
    # nhất. Khoá theo session_id ở đây sẽ nuốt mất đúng lần tiêm quan trọng đó.
    if event == "SubagentStart":
        dedup_key = str(payload.get("agent_id") or payload.get("session_id") or "")
        if already_done(dedup_key):
            return 0

    paths = policy_paths(event)
    if not paths:
        return 0

    chunks = []
    for p in paths:
        try:
            chunk = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if chunk:
            chunks.append(chunk)
    text = "\n\n".join(chunks)
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
