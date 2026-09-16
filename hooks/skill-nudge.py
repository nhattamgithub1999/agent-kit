#!/usr/bin/env python3
"""
SubagentStop hook (builder) — gợi ý đúc kết skill sau task đủ phức tạp.

VẤN ĐỀ NÓ GIẢI:
  Đối chiếu hermes-agent (NousResearch): agent tự tạo skill từ kinh nghiệm sau
  task phức tạp (không rõ có gate review hay không, theo docs công khai của
  họ). Ở đây làm bản CÓ GATE: chỉ GỢI Ý cho model/parent cân nhắc viết DRAFT
  SKILL.md, KHÔNG tự ghi vào skills/ — giữ đúng nguyên tắc "không thêm
  abstraction khi chưa chắc cần" (decision ladder) và tránh đúng cái gap mà
  hermes-agent đang thiếu (không có review trước khi áp dụng).

TIÊU CHÍ "TASK PHỨC TẠP" — tái dùng ngưỡng ĐÃ CÓ trong builder.md (không bịa
ngưỡng mới):
  - report của builder có heading "### Files changed" với >= 3 dòng "- `...`"
    (đúng ngưỡng "Thay đổi chạm >= 3 file" ở builder.md, mục Cổng escalation).
  - VÀ report có "VERDICT: READY" (output contract của skill verify-loop).

CHỈNH:
  SKILL_NUDGE=off              tắt hẳn.
  SKILLNUDGE_AGENTS=builder    đổi tập agent áp dụng (mặc định chỉ builder).

FAIL-OPEN: giống no-fake-pass.py — payload không có tên agent thì suy từ
matcher settings.json nếu WATCHED chỉ có đúng 1 tên; không parse được JSON
hay thiếu report -> exit 0, không chặn gì.
"""
import json
import os
import pathlib
import re
import sys

LOG = pathlib.Path.home() / ".claude" / "skill-nudge.log"
WATCHED = set(filter(None, os.environ.get("SKILLNUDGE_AGENTS", "builder").split(",")))
AGENT_KEYS = (
    "agent_type", "subagent_type", "agentType", "subagentType",
    "agent_name", "agentName", "agent", "name",
)

READY = re.compile(r"VERDICT:\s*READY", re.I)
FILES_BLOCK = re.compile(r"###\s*Files changed\s*\n((?:.*\n?)*?)(?=\n#{1,3}\s|\Z)")
FILE_LINE = re.compile(r"^\s*-\s*`", re.M)


def bare_name(agent: str) -> str:
    return agent.rsplit(":", 1)[-1]


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


def walk_strings(obj, depth=0):
    if depth > 8:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from walk_strings(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_strings(v, depth + 1)


def read_transcript(payload) -> str:
    for s in walk_strings(payload):
        if s.endswith(".jsonl") and os.path.isfile(s):
            try:
                return pathlib.Path(s).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    return ""


def last_message(payload) -> str:
    if isinstance(payload, dict):
        for k in ("last_assistant_message", "lastAssistantMessage"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, dict):
                parts = [b.get("text", "") for b in (v.get("content") or [])
                         if isinstance(b, dict)]
                if any(parts):
                    return "\n".join(parts)
    return ""


def files_changed_count(text: str) -> int:
    m = FILES_BLOCK.search(text)
    if not m:
        return 0
    return len(FILE_LINE.findall(m.group(1)))


def main() -> int:
    if os.environ.get("SKILL_NUDGE", "").lower() == "off":
        return 0
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        log("FAIL-OPEN: stdin không phải JSON")
        return 0
    if not isinstance(payload, dict):
        return 0

    agent = ""
    for k in AGENT_KEYS:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            agent = v.strip()
            break
    if WATCHED:
        if not agent:
            if len(WATCHED) == 1:
                agent = next(iter(WATCHED))
            else:
                log("FAIL-OPEN: không xác định được agent, WATCHED có >=2 tên")
                return 0
        elif agent not in WATCHED and bare_name(agent) not in WATCHED:
            return 0

    if payload.get("stop_hook_active") or payload.get("stopHookActive"):
        return 0

    text = last_message(payload) or read_transcript(payload) or "\n".join(walk_strings(payload))
    if not text.strip():
        return 0

    if files_changed_count(text) >= 3 and READY.search(text):
        msg = (
            "Task vừa hoàn tất chạm >=3 file và verify PASS — có thể là quy "
            "trình đáng đúc kết. Cân nhắc viết DRAFT SKILL.md (không tự ghi "
            "vào skills/, chờ user duyệt)."
        )
        log(f"NUDGE agent={agent or '?'}")
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SubagentStop",
                "additionalContext": msg,
            }
        }, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
