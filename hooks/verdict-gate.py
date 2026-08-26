#!/usr/bin/env python3
"""
SubagentStop hook — verdict gate. Ghi verdict THẬT của `verifier` (SAFE_TO_BUILD /
NEEDS_FIX / BLOCK) thành state để flow-gate.py đọc lại khi builder được spawn, thay vì
chỉ tin dòng `VERIFIER VERDICT:` mà parent chép tay vào prompt giao việc.

VẤN ĐỀ NÓ GIẢI: flow-gate.py chặn builder khi lượt chưa có verdict SAFE_TO_BUILD, nhưng
trước hook này nguồn duy nhất là chuỗi parent tự chép vào prompt — không có gì đảm bảo
khớp với verdict THẬT verifier vừa trả. Hook này bắt verdict trực tiếp từ SubagentStop
của chính verifier và ghi ra state độc lập (`verification`) để flow-gate.py ưu tiên đọc.

TÍN HIỆU TẤT ĐỊNH: `matcher` của event SubagentStop KHÔNG lọc theo tên agent trên thực
tế (đã đo thật: log của no-fake-pass tăng đều mỗi lượt dù matcher không khớp) — nên hook
này KHÔNG dựa vào matcher, tự nhận dạng verifier bằng hai đường: (a) tên agent lấy từ
AGENT_KEYS, so `bare_name(...).lower() == "verifier"`; (b) không xác định được tên agent
thì CHỈ ghi state khi text khớp regex verdict thật của verifier (xem VERDICT_LINE_RE).

QUAN TRỌNG — regex phải ANCHOR ĐẦU DÒNG, không phải substring bừa: output contract của
verifier (agents/verifier.md:57) là `### VERDICT: SAFE_TO_BUILD | NEEDS_FIX | BLOCK`,
NHƯNG flow-gate.py:81 dùng `PROMPT_VERDICT_RE = re.compile(r"VERIFIER VERDICT:\s*(...)")`
để đọc dòng `VERIFIER VERDICT: SAFE_TO_BUILD` mà PARENT bắt buộc nhúng vào prompt giao
việc cho builder. Chuỗi "VERIFIER VERDICT: SAFE_TO_BUILD" CHỨA substring con
"VERDICT: SAFE_TO_BUILD". Một regex không anchor (`VERDICT:\s*(...)`) sẽ khớp NGAY TRONG
PROMPT CỦA CHÍNH BUILDER — vốn nằm trong `walk_strings`/`read_transcript` của payload
SubagentStop của builder đó, ở đường nhận dạng (b) khi payload không xác định được tên
agent (thực tế RẤT THƯỜNG XUYÊN — đã đo trên máy này: phần lớn dòng log của
no-fake-pass.py là "payload không có tên agent"). Kết quả nếu không anchor: BUILDER TỰ
MỞ CỔNG CHO CHÍNH NÓ bằng cách chép lại dòng verdict trong report của nó — đúng thứ cổng
này tồn tại để chặn. VERDICT_LINE_RE do đó: (1) anchor `^` (với re.M) + cho phép tối đa
một tiền tố heading Markdown (`#{0,6}`) và khoảng trắng trước `VERDICT:`, KHÔNG cho phép
chữ nào khác (như "VERIFIER ") đứng trước `VERDICT:` trên cùng dòng — dòng
"VERIFIER VERDICT: SAFE_TO_BUILD" do đó KHÔNG khớp; (2) lớp chặn thứ hai cho chắc: bất kỳ
dòng nào khớp mà toàn bộ dòng đó còn chứa chuỗi con "VERIFIER VERDICT" (không phân biệt
hoa/thường) đều bị loại, phòng biến thể chưa lường tới.

STATE: thư mục phẳng, chung gốc với flow-gate.py, không SQLite, không module dùng chung:
  <tempfile.gettempdir()>/agent-kit-flow/<session_id>/<prompt_id>/
      verification   một dòng, ba field cách nhau bằng TAB:
                      `<approved|blocked>\tsubagent_stop\t<verdict thô>`.
                      SAFE_TO_BUILD -> approved; NEEDS_FIX/BLOCK -> blocked.

BIẾN MÔI TRƯỜNG: VERDICT_GATE=off tắt hẳn (exit 0 ngay từ đầu, không đọc stdin).

FAIL-OPEN (cố ý) khi: stdin không phải JSON; thiếu session_id/prompt_id (KHÔNG ghi
state — tránh rò rỉ approval sang lượt khác); tên agent xác định được mà không phải
verifier (không ghi gì); không trích được verdict hợp lệ (đúng dòng, không phải dòng
`VERIFIER VERDICT:` do parent nhúng) từ text. Hook này CHỈ ghi state, KHÔNG BAO GIỜ chặn
tool nào — luôn exit 0.
"""
import json
import os
import pathlib
import re
import sys
import tempfile

STATE_ROOT = pathlib.Path(tempfile.gettempdir()) / "agent-kit-flow"
LOG = pathlib.Path.home() / ".claude" / "agent-kit-gate.log"

AGENT_KEYS = (
    "agent_type", "subagent_type", "agentType", "subagentType",
    "agent_name", "agentName", "agent", "name",
)

# Anchor đầu dòng (re.M): chỉ cho phép khoảng trắng + tối đa 6 dấu '#' (heading Markdown)
# trước literal "VERDICT:". Dòng "VERIFIER VERDICT: SAFE_TO_BUILD" KHÔNG khớp vì "VERIFIER"
# không nằm trong tiền tố được phép. Xem giải thích đầy đủ ở docstring đầu file.
VERDICT_LINE_RE = re.compile(
    r"^[ \t]*#{0,6}[ \t]*VERDICT:[ \t]*(SAFE_TO_BUILD|NEEDS_FIX|BLOCK)\b",
    re.I | re.M,
)
# Lớp chặn thứ hai: loại mọi dòng khớp mà bản thân dòng đó còn chứa "VERIFIER VERDICT".
VERIFIER_LINE_RE = re.compile(r"VERIFIER VERDICT", re.I)

MAP = {"SAFE_TO_BUILD": "approved", "NEEDS_FIX": "blocked", "BLOCK": "blocked"}


def safe(name) -> str:
    """Lọc về ký tự an toàn cho tên thư mục, giống plan-gate.py:68-69."""
    return "".join(ch for ch in str(name) if ch.isalnum() or ch in "-_")[:64] or "default"


def bare_name(agent: str) -> str:
    """Tên agent sau dấu hai chấm cuối cùng. "agent-kit:builder" -> "builder";
    "builder" -> "builder" (không đổi khi không có dấu hai chấm)."""
    return agent.rsplit(":", 1)[-1]


def log(result: str, pid, agent: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        pid_disp = str(pid)[:8] if pid else "?"
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"verdict-gate {result} pid={pid_disp} agent={agent or '?'}\n")
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
    """Stop/SubagentStop cấp sẵn text lượt cuối; transcript ghi bất đồng bộ nên ưu tiên
    field này trước, giống no-fake-pass.py."""
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


def extract_verdict(text: str):
    """Trả về verdict THẬT cuối cùng trong text, hoặc None. Bỏ qua mọi dòng khớp
    VERDICT_LINE_RE mà chính dòng đó còn chứa "VERIFIER VERDICT" — chặn ca builder
    tự khớp dòng `VERIFIER VERDICT:` mà parent nhúng vào prompt giao việc của nó."""
    verdict = None
    for m in VERDICT_LINE_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start: line_end if line_end != -1 else len(text)]
        if VERIFIER_LINE_RE.search(line):
            continue
        verdict = m.group(1).upper()
    return verdict


def main() -> int:
    if os.environ.get("VERDICT_GATE", "").lower() == "off":
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return 0  # FAIL-OPEN

    sid = payload.get("session_id") if isinstance(payload, dict) else None
    pid = payload.get("prompt_id") if isinstance(payload, dict) else None
    if not sid or not pid:
        log("FAIL-OPEN(no-sid-pid)", pid, "")
        return 0  # FAIL-OPEN: không ghi state, tránh rò rỉ approval sang lượt khác

    agent = ""
    if isinstance(payload, dict):
        for k in AGENT_KEYS:
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                agent = v.strip()
                break

    if agent and bare_name(agent).lower() != "verifier":
        return 0  # tên agent xác định được mà không phải verifier: không ghi gì

    text = last_message(payload) or read_transcript(payload) or "\n".join(walk_strings(payload))
    verdict = extract_verdict(text)
    if not verdict:
        log("FAIL-OPEN(no-verdict)", pid, agent)
        return 0  # FAIL-OPEN: không trích được verdict hợp lệ, không ghi state

    result = MAP[verdict]

    state_dir = STATE_ROOT / safe(sid) / safe(pid)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "verification").write_text(
            f"{result}\tsubagent_stop\t{verdict}\n", encoding="utf-8"
        )
    except OSError:
        log("FAIL-OPEN(oserror)", pid, agent)
        return 0

    log(result, pid, agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
