#!/usr/bin/env python3
"""
PostToolUse + Stop hook — kích hoạt tự học: khi lượt vừa xong có TÍN HIỆU đáng học
mà chưa ghi memory, gợi ý chạy skill `learn`. CHỈ GỢI Ý, không chặn, không tự ghi.

v2 (2026-09-23): thay regex trên câu trả lời của model bằng tín hiệu đọc từ
transcript của CHÍNH lượt hiện tại:
  - correction : prompt của user có dấu hiệu sửa hướng / nêu quy ước.
  - fixed      : CÙNG một lệnh verify (build/test/lint/typecheck) is_error rồi chạy lại OK
                 (fail/pass lấy từ tool_result.is_error trong transcript).
  - lesson     : report subagent (tool Agent) có mục "### Bài học" không rỗng.
Lượt đã ghi vào */memory/*.md hoặc */learnings/*.md → im lặng. Mỗi lượt gợi ý tối đa 1 lần.
Bằng chứng giữ cơ chế additionalContext ở Stop (đo 2026-09-23 trên transcript): sau
gợi ý của v1, model CHẠY TIẾP NGAY trong cùng lượt 88/92 lần — không chen vào task sau.

CƠ CHẾ:
  PostToolUse (matcher Write|Edit): path chứa /memory/ hoặc /learnings/ → chạm marker;
    nếu là /learnings/ → chạy check.py, có ERROR thì báo model (additionalContext).
  Stop: stop_hook_active → thoát. Đọc transcript từ prompt thật cuối cùng của user.

CHỈNH:  MEMORY_NUDGE=off tắt hẳn;  MEMORY_NUDGE_STATE=<dir>, MEMORY_NUDGE_LOG=<file> đổi chỗ ghi;
        LEARN_CHECK=<file> đổi check.py (mặc định ../skills/learn/scripts/check.py cạnh hooks/).
FAIL-OPEN: mọi lỗi parse/IO → exit 0, không in gì.
"""
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

STATE = pathlib.Path(os.environ.get("MEMORY_NUDGE_STATE") or
                     pathlib.Path(tempfile.gettempdir()) / "agent-kit-memory")
# hooks/ và skills/ nằm cạnh nhau ở cả plugin root lẫn ~/.claude
CHECK = pathlib.Path(os.environ.get("LEARN_CHECK") or
                     pathlib.Path(__file__).resolve().parent.parent / "skills" / "learn" / "scripts" / "check.py")
LOG = pathlib.Path(os.environ.get("MEMORY_NUDGE_LOG") or pathlib.Path.home() / ".claude" / "memory-nudge.log")

MEMORY_PATH = re.compile(r"/(memory|learnings)/[^\s\"']*\.md")
CORRECTION = re.compile(
    r"(từ giờ|từ nay|lần sau|sai rồi|nhầm rồi|không đúng|không cần thiết|"
    r"nhớ (giúp|là|rằng)|ghi nhớ|rút kinh nghiệm|quy ước|luôn luôn)",
    re.I,
)  # đo 2026-09-23 trên 495 prompt thật: bắt 1.8%; vế "^đừng/không" bị bỏ vì báo nhầm
VERIFY_CMD = re.compile(
    r"\b(dotnet (build|test)|npm (run )?(test|build|lint|typecheck)|pnpm (run )?(test|build|lint)|"
    r"yarn (test|build|lint)|pytest|python3? -m (pytest|unittest)|tsc\b|go (test|build|vet)|"
    r"cargo (test|build|clippy)|mvn|gradle|eslint|ruff|mypy|jest|vitest|test_\w+\.py)"
)
LESSON = re.compile(r"###\s*Bài học[^\n]*\n+\s*[-*]\s*(?!\(?\s*(không|chưa|none)\b)\S", re.I)
FIXED = "lỗi verify đã sửa (fail → pass)"
SUB_LESSON = "subagent báo bài học"
NOT_A_PROMPT = ("<local-command", "<command-", "<task-notification", "<system-reminder",
                "[Request interrupted")


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg.rstrip()}\n")
    except OSError:
        pass


def user_text(rec) -> str:
    """Text của một prompt THẬT do user gõ; "" nếu là tool_result/meta/lệnh hệ thống."""
    if rec.get("type") != "user" or rec.get("isMeta"):
        return ""
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return ""
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    if not isinstance(c, str) or not c.strip() or c.lstrip().startswith(NOT_A_PROMPT):
        return ""
    return c


def current_turn(transcript: str):
    recs = []
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return None, "", []
    start = max((i for i, r in enumerate(recs) if user_text(r)), default=-1)
    if start < 0:
        return None, "", []
    return recs[start], user_text(recs[start]), recs[start + 1:]


def signals(prompt: str, turn: list):
    uses, results = {}, {}
    for r in turn:
        for b in ((r.get("message") or {}).get("content") or []):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                uses[b.get("id")] = b
            elif b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = b
    found, wrote, failed = [], False, {}
    if CORRECTION.search(prompt):
        found.append("user sửa hướng / nêu quy ước")
    for uid, u in uses.items():
        inp = u.get("input") or {}
        if u.get("name") in ("Write", "Edit") and MEMORY_PATH.search(str(inp.get("file_path", ""))):
            wrote = True
        res = results.get(uid) or {}
        if u.get("name") == "Bash" and VERIFY_CMD.search(inp.get("command", "")):
            key = normalize(inp.get("command", ""))
            if res.get("is_error"):
                failed[key] = True
            elif failed.get(key) and FIXED not in found:
                found.append(FIXED)
        if u.get("name") == "Agent":
            content = res.get("content")
            text = content if isinstance(content, str) else "\n".join(
                x.get("text", "") for x in (content or []) if isinstance(x, dict))
            if LESSON.search(text) and SUB_LESSON not in found:
                found.append(SUB_LESSON)
    return found, wrote


def normalize(cmd: str) -> str:
    """Cùng một lệnh verify = cùng chuỗi sau khi bỏ `cd <dir> &&` đầu và gộp khoảng trắng."""
    cmd = re.sub(r"^\s*(cd\s+\S+\s*&&\s*)+", "", cmd)
    return " ".join(cmd.split())


def marker(session_id: str, name: str) -> pathlib.Path:
    return STATE / session_id / name


def handle_post_tool_use(payload, session_id: str) -> int:
    path = str((payload.get("tool_input") or {}).get("file_path", ""))
    if not MEMORY_PATH.search(path):
        return 0
    try:
        p = marker(session_id, "written")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except OSError:
        pass
    # Ghi vào kho learnings → chạy check.py ngay; có ERROR thì báo model sửa trong cùng lượt.
    root = next((pathlib.Path(path[:m.end()]) for m in [re.search(r".*/learnings(?=/)", path)] if m), None)
    if root is None or not CHECK.exists():
        return 0
    try:
        r = subprocess.run([sys.executable, str(CHECK), "--root", str(root)],
                           capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return 0
    errs = [l for l in r.stdout.splitlines() if l.startswith("ERROR:")]
    if errs:
        log(f"CHECK-ERROR session={session_id} n={len(errs)}")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "check.py báo lỗi kho learnings — sửa ngay:\n" + "\n".join(errs[:8]),
        }}, ensure_ascii=False))
    return 0


def handle_stop(payload, session_id: str) -> int:
    if payload.get("stop_hook_active"):
        return 0
    start_rec, prompt, turn = current_turn(str(payload.get("transcript_path") or ""))
    if start_rec is None:
        return 0
    turn_id = str(start_rec.get("uuid") or hashlib.sha1(prompt.encode()).hexdigest()[:12])
    nudged = marker(session_id, f"nudged-{turn_id}")
    if nudged.exists():
        return 0
    found, wrote = signals(prompt, turn)
    written = marker(session_id, "written")
    try:
        ts = dt.datetime.fromisoformat(str(start_rec.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
        if written.exists() and written.stat().st_mtime >= ts:
            wrote = True
    except ValueError:
        pass
    if not found or wrote:
        return 0
    try:
        nudged.parent.mkdir(parents=True, exist_ok=True)
        nudged.touch()
    except OSError:
        pass
    log(f"NUDGE session={session_id} signals={'; '.join(found)}")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "Stop",
        "additionalContext": (
            f"Tín hiệu học ở lượt này: {'; '.join(found)}. Nếu có bài học tái dùng "
            "được thì chạy skill `learn`; không có gì đáng lưu thì bỏ qua."),
    }}, ensure_ascii=False))
    return 0


def main() -> int:
    if os.environ.get("MEMORY_NUDGE", "").lower() == "off":
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return 0
    event = payload.get("hook_event_name") or ""
    try:
        if event == "PostToolUse":
            return handle_post_tool_use(payload, session_id)
        if event == "Stop":
            return handle_stop(payload, session_id)
    except Exception as e:  # FAIL-OPEN: hook không bao giờ được làm hỏng lượt chính
        log(f"FAIL-OPEN {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
