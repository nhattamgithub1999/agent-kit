#!/usr/bin/env python3
"""
SubagentStop hook — chặn báo cáo "đã pass" mà KHÔNG kèm bằng chứng lệnh chạy.

Đây là cơ chế TẤT ĐỊNH duy nhất trong kit. Prompt chỉ giảm xác suất bịa;
hook chặn thật ở exit code.

CÀI: xem README §Hook. Chạy được với python3 chuẩn, không cần jq.

TRẠNG THÁI SCHEMA: payload JSON của SubagentStop CHƯA ĐƯỢC VERIFY trên máy anh.
Script này FAIL-OPEN: không tìm thấy nội dung report → exit 0 (không chặn),
và ghi log.

ĐÃ SỬA (19/08/2026): trước đây khi payload KHÔNG chứa tên agent, biến `agent`
rỗng nên điều kiện lọc `agent and agent not in WATCHED` bị bỏ qua → hook áp cho
MỌI subagent, kể cả Explore/critic/verifier read-only. Bằng chứng: 8/12 dòng
trong no-fake-pass.log là "BLOCK agent=?". Nay không xác định được agent →
FAIL-OPEN. Muốn giữ hành vi cũ: NOFAKEPASS_STRICT=1.

Chạy một lần với DUMP=1 để xem payload thật rồi siết lại:

    DUMP=1 <lệnh làm subagent chạy>     # ghi ~/.claude/hook-payload-sample.json
"""
import json
import os
import pathlib
import re
import sys

LOG = pathlib.Path.home() / ".claude" / "no-fake-pass.log"

# Chỉ áp cho agent ghi file. Đọc từ env để đổi mà không sửa code.
WATCHED = set(filter(None, os.environ.get("NOFAKEPASS_AGENTS", "builder").split(",")))
# Không nhận diện được agent → chặn bừa mọi subagent. Mặc định FAIL-OPEN.
STRICT = os.environ.get("NOFAKEPASS_STRICT", "") == "1"
AGENT_KEYS = (
    "agent_type", "subagent_type", "agentType", "subagentType",
    "agent_name", "agentName", "agent", "name",
)

# Khẳng định "đã xanh"
PASS_CLAIM = re.compile(
    r"(test(s)?\s+pass|all tests passed|build (thành công|succeeded)|"
    r"0 lỗi|no errors|✅|VERDICT:\s*READY)",
    re.I,
)
# Bằng chứng: block code, dấu nhắc lệnh, hoặc thừa nhận chưa verify
EVIDENCE = re.compile(r"(```|^\s*\$\s+\S|CHƯA VERIFY)", re.M)


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


def walk_strings(obj, depth=0):
    """Gom mọi string trong payload — không phụ thuộc tên field cụ thể."""
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
    """Nếu payload trỏ tới file transcript, đọc nó."""
    for s in walk_strings(payload):
        if s.endswith(".jsonl") and os.path.isfile(s):
            try:
                return pathlib.Path(s).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    return ""


def last_message(payload) -> str:
    """Stop/SubagentStop cấp sẵn text lượt cuối. Transcript ghi BẤT ĐỒNG BỘ và có
    thể chưa có lượt hiện tại -> đọc transcript là bỏ lọt đúng thứ cần kiểm.
    (bổ sung 22/08/2026)"""
    if isinstance(payload, dict):
        for k in ("last_assistant_message", "lastAssistantMessage"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, dict):  # có thể là message object
                parts = [b.get("text", "") for b in (v.get("content") or [])
                         if isinstance(b, dict)]
                if any(parts):
                    return "\n".join(parts)
    return ""


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        log("FAIL-OPEN: stdin không phải JSON")
        return 0

    if os.environ.get("DUMP"):
        p = pathlib.Path.home() / ".claude" / "hook-payload-sample.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(raw, encoding="utf-8")
        log(f"DUMP → {p}")

    agent = ""
    if isinstance(payload, dict):
        for k in AGENT_KEYS:
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                agent = v.strip()
                break
    if WATCHED:
        if not agent:
            if not STRICT:
                log("FAIL-OPEN: payload không có tên agent — không chặn "
                    "(NOFAKEPASS_STRICT=1 để chặn như cũ)")
                return 0
        elif agent not in WATCHED:
            return 0

    text = last_message(payload) or read_transcript(payload) or "\n".join(walk_strings(payload))
    if not text.strip():
        log(f"FAIL-OPEN: không lấy được report (agent={agent or '?'})")
        return 0

    if PASS_CLAIM.search(text) and not EVIDENCE.search(text):
        msg = (
            "BLOCKED bởi no-fake-pass hook: report khẳng định đã pass nhưng "
            "KHÔNG kèm lệnh đã chạy + output thật. Chạy lại lệnh verify và dán "
            "output, hoặc ghi 'CHƯA VERIFY: <lý do>'."
        )
        log(f"BLOCK agent={agent or '?'}")
        print(msg, file=sys.stderr)
        return 2  # exit 2 = chặn, stderr quay lại cho model

    return 0


if __name__ == "__main__":
    sys.exit(main())
