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

ĐÃ SIẾT (26/08/2026): gloss-gate — hook tiền nhiệm theo hướng tương tự — bị gỡ
khỏi hooks.json vì PHẠT SỰ TRUNG THỰC: đo thật 30/60 lần chặn là chặn nhầm
đúng câu "CHƯA VERIFY: <lý do>" mà policy bắt buộc khi không chạy được lệnh
verify. Cổng ở đây giữ nguyên nguyên tắc đó — luật mới KHÔNG được chặn một
báo cáo trung thực nói rõ chưa verify được. Hai luật chặn áp dụng độc lập:

  1. MÂU THUẪN NỘI TẠI: report vừa khẳng định pass (PASS_CLAIM) vừa chứa dấu
     hiệu thất bại (FAIL_SIGN, vd "2 failed", "Error:", "traceback") → chặn
     luôn, bất kể có "bằng chứng" nào đi kèm hay không — output dán vào có
     thể tự chứng minh report vừa nói dối vừa vô tình lộ ra kết quả FAIL.
  2. THIẾU BẰNG CHỨNG: report khẳng định pass mà không có ÍT NHẤT MỘT trong
     ba dạng bằng chứng:
       a. dòng dấu nhắc lệnh (`$ ...`),
       b. block ba backtick có nội dung KHÔNG RỖNG và chứa dấu hiệu kết quả
          chạy lệnh thật (pass/fail/ok/error/warning/exit code/N passed/...),
       c. lời khai "CHƯA VERIFY: <lý do>" ĐÚNG DẠNG (có dấu hai chấm + lý do
          phía sau, không phải chữ trần) — nhưng CHỈ tính khi report đó
          KHÔNG đồng thời chứa PASS_CLAIM. Một report đã tự nhận CHƯA VERIFY
          thì việc chặn ở đây không xảy ra nữa vì không còn PASS_CLAIM nào
          để xét (xem điều kiện ở main()); dạng (c) không được dùng như tấm
          khiên đứng cạnh một khẳng định pass giả trong CÙNG report.
     Một report CHỈ có "CHƯA VERIFY: <lý do>" và không có PASS_CLAIM nào luôn
     đi qua (exit 0) — đây chính là ca gloss-gate từng chặn sai, KHÔNG được
     lặp lại.

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
# Dấu hiệu THẤT BẠI. Khớp CÙNG PASS_CLAIM trong một report -> mâu thuẫn nội
# tại, chặn ngay bất kể "bằng chứng" gì đi kèm.
FAIL_SIGN = re.compile(
    r"(\d+\s+failed|\bFAILED\b|\bError:|exit (code )?[1-9]|NOT READY|\btraceback\b)",
    re.I,
)
# Bằng chứng dạng (a): dòng dấu nhắc lệnh.
CMD_PROMPT = re.compile(r"^\s*\$\s+\S", re.M)
# Nội dung bên trong block ba backtick — hai dạng: nhiều dòng (có newline sau
# dấu mở, dòng đầu có thể là tên ngôn ngữ) và một dòng (không newline nào ở
# giữa, không có cách nào tách "ngôn ngữ" khỏi "nội dung" nên coi cả chuỗi là
# nội dung).
CODE_BLOCK_MULTILINE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
CODE_BLOCK_INLINE = re.compile(r"```([^`\n]*)```")
# Bằng chứng dạng (b): nội dung block phải chứa dấu hiệu kết quả chạy lệnh thật.
RESULT_SIGN = re.compile(
    r"(pass|fail|ok\b|error|warning|exit code|\d+\s*/\s*\d+|\d+\s+(passed|failed|error))",
    re.I,
)
# Bằng chứng dạng (c): lời khai chưa verify ĐÚNG DẠNG — có dấu hai chấm + lý do.
CHUA_VERIFY = re.compile(r"CHƯA VERIFY\s*:\s*\S", re.I)


def bare_name(agent: str) -> str:
    """Tên agent sau dấu hai chấm cuối cùng. "agent-kit:builder" -> "builder";
    "builder" -> "builder" (không đổi khi không có dấu hai chấm)."""
    return agent.rsplit(":", 1)[-1]


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


def code_block_contents(text: str):
    """Nội dung bên trong mọi block ba backtick trong `text`, đa dòng lẫn một
    dòng (xem CODE_BLOCK_MULTILINE / CODE_BLOCK_INLINE ở trên)."""
    return CODE_BLOCK_MULTILINE.findall(text) + CODE_BLOCK_INLINE.findall(text)


def has_evidence(text: str) -> bool:
    """Bằng chứng hợp lệ theo ba dạng (a)/(b)/(c) — xem docstring đầu file."""
    if CMD_PROMPT.search(text):
        return True
    for block in code_block_contents(text):
        stripped = block.strip()
        if stripped and RESULT_SIGN.search(stripped):
            return True
    if not PASS_CLAIM.search(text) and CHUA_VERIFY.search(text):
        return True
    return False


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
        elif agent not in WATCHED and bare_name(agent) not in WATCHED:
            return 0

    # CHẶN TỐI ĐA MỘT LẦN mỗi lượt dừng. `stop_hook_active` là true khi lượt hiện
    # tại SINH RA TỪ một hook chặn trước đó — đã đo thật: lượt hai tới với cờ này
    # bật và nội dung đúng thứ stderr yêu cầu. Chặn tiếp ở đây thì agent nào không
    # đưa nổi bằng chứng sẽ quay vòng vô hạn. Thà bỏ lọt còn hơn treo phiên.
    if payload.get("stop_hook_active") or payload.get("stopHookActive"):
        log(f"BỎ QUA: lượt này đã do hook chặn sinh ra (agent={agent or '?'}) — "
            "không chặn lần hai")
        return 0

    text = last_message(payload) or read_transcript(payload) or "\n".join(walk_strings(payload))
    if not text.strip():
        log(f"FAIL-OPEN: không lấy được report (agent={agent or '?'})")
        return 0

    if PASS_CLAIM.search(text):
        if FAIL_SIGN.search(text):
            msg = (
                "BLOCKED bởi no-fake-pass hook: report vừa khẳng định đã pass "
                "vừa chứa dấu hiệu THẤT BẠI (vd 'failed', 'Error:', 'traceback', "
                "'NOT READY'). Báo trạng thái THẬT: nếu có lỗi thì ghi rõ lỗi, "
                "đừng ghi 'pass'/'READY'. Chạy lại lệnh verify và dán output "
                "thật, hoặc ghi 'CHƯA VERIFY: <lý do>' mà KHÔNG kèm khẳng định "
                "pass."
            )
            log(f"BLOCK(mâu thuẫn pass+fail) agent={agent or '?'}")
            print(msg, file=sys.stderr)
            return 2  # exit 2 = chặn, stderr quay lại cho model
        if not has_evidence(text):
            msg = (
                "BLOCKED bởi no-fake-pass hook: report khẳng định đã pass nhưng "
                "KHÔNG kèm bằng chứng hợp lệ (dòng `$ <lệnh>`, hoặc block "
                "```...``` không rỗng có kết quả chạy lệnh thật như 'pass'/"
                "'failed'/'N/N'). Chạy lại lệnh verify và dán output thật, "
                "hoặc ghi 'CHƯA VERIFY: <lý do>' mà KHÔNG kèm khẳng định pass."
            )
            log(f"BLOCK(thiếu bằng chứng) agent={agent or '?'}")
            print(msg, file=sys.stderr)
            return 2  # exit 2 = chặn, stderr quay lại cho model

    return 0


if __name__ == "__main__":
    sys.exit(main())
