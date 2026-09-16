#!/usr/bin/env python3
"""
PostToolUse + Stop hook — nhắc lưu memory khi phiên có tín hiệu quyết định/điều
chỉnh đáng nhớ nhưng chưa thấy ghi vào memory/*.md.

VẤN ĐỀ NÓ GIẢI:
  Hạ tầng memory (type user/feedback/project/reference + MEMORY.md index) đã
  có sẵn, nhưng không có gì NHẮC dùng nó — phiên có correction/quyết định rồi
  trôi qua, không ai ghi lại. Đối chiếu hermes-agent (NousResearch): họ có
  "agent-curated memory with periodic persistence nudges" — đây là bản tối
  giản của ý tưởng đó, CHỈ GỢI Ý, không tự ghi, không chặn (khớp luật đã có:
  chỉ lưu memory khi user yêu cầu hoặc tín hiệu rõ).

CƠ CHẾ (dual-event single-script, giống session-policy.py):
  PostToolUse (matcher Write|Edit) -> nếu path ghi có chứa "/memory/" (đúng
    thư mục memory của dự án) -> đánh dấu marker cho session này, IM LẶNG.
  Stop (phiên chính, không matcher) -> nếu session CHƯA có marker VÀ
    last-message/transcript khớp regex tín hiệu quyết định/điều chỉnh ->
    in additionalContext dạng GỢI Ý (không exit 2, không ép làm gì).

CHỈNH:
  MEMORY_NUDGE=off    tắt hẳn.

FAIL-OPEN: stdin không phải JSON; thiếu session_id; không ghi/đọc được state
-> exit 0, không chặn gì, không crash.
"""
import json
import os
import pathlib
import re
import sys
import tempfile

STATE = pathlib.Path(tempfile.gettempdir()) / "agent-kit-memory"
LOG = pathlib.Path.home() / ".claude" / "memory-nudge.log"

CUE = re.compile(
    r"(từ giờ|từ nay|lần sau|luôn luôn|luôn phải|"
    r"quy tắc|quy ước|chốt (lại|vậy)|nhớ (giúp|là|rằng)|"
    r"ghi nhớ|correction|feedback)",
    re.I,
)


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


def marker_path(session_id: str) -> pathlib.Path:
    return STATE / session_id / "written"


def handle_post_tool_use(payload, session_id: str) -> int:
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    if not isinstance(tool_input, dict):
        return 0
    for s in walk_strings(tool_input):
        if "/memory/" in s and s.endswith(".md"):
            try:
                p = marker_path(session_id)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
                log(f"MARK session={session_id} path={s}")
            except OSError:
                pass
            break
    return 0


def handle_stop(payload, session_id: str) -> int:
    if marker_path(session_id).exists():
        return 0

    text = last_message(payload) or read_transcript(payload) or "\n".join(walk_strings(payload))
    if not text.strip() or not CUE.search(text):
        return 0

    msg = (
        "Phiên này có thể có quyết định/điều chỉnh đáng lưu "
        "vào memory (user/feedback/project/reference) nhưng chưa thấy ghi. "
        "Cân nhắc lưu nếu còn giá trị cho phiên sau."
    )
    log(f"NUDGE session={session_id}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": msg,
        }
    }, ensure_ascii=False))
    return 0


def main() -> int:
    if os.environ.get("MEMORY_NUDGE", "").lower() == "off":
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        log("FAIL-OPEN: stdin không phải JSON")
        return 0
    if not isinstance(payload, dict):
        return 0

    event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    if not session_id:
        log("FAIL-OPEN: không có session_id")
        return 0

    if event == "PostToolUse":
        return handle_post_tool_use(payload, session_id)
    if event == "Stop":
        return handle_stop(payload, session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
