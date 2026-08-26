#!/usr/bin/env python3
"""
PreToolUse hook — flow gate. Chặn giao việc cho subagent khi lượt này chưa đọc
file thật (recon), và sau khi có plan, khi agent được gọi không khớp nhãn
`[agent]` mà plan đã ghi.

VẤN ĐỀ NÓ GIẢI: model có thể ExitPlanMode rồi gọi Agent ngay mà chưa từng
Read/Grep/Glob gì (plan dựng từ suy đoán), hoặc plan ghi `[builder]` cho một
bước nhưng lúc thực thi lại gọi agent khác. Không có chốt tất định nào chặn
trước hook này.

TÍN HIỆU TẤT ĐỊNH: (1) đã có ít nhất một lần Read/Grep/Glob trong lượt hiện
tại (file `recon` trong state) trước khi ExitPlanMode/Agent được cho qua;
(2) nhãn dòng `[tên-agent]` trong `plan` của ExitPlanMode, trích bằng regex,
hạ chữ thường, lưu làm tập nhãn hợp lệ; (3) subagent_type của tool Agent so
bằng TÊN TRẦN (sau dấu hai chấm cuối, hạ chữ thường) với tập nhãn đó.

VÒNG DUYỆT TRƯỚC BUILDER: parent lập plan và nhúng vào prompt giao việc;
`verifier` đối chiếu plan với code thật; parent chốt bằng chính lời gọi spawn.
Chỉ khi cả hai điều đó xảy ra trong CÙNG lượt thì builder mới được ghi file.
Tín hiệu: lời gọi `Edit`/`Write` của subagent mang `agent_type` (đã đo thật),
nên hook phân biệt được lệnh ghi này đến từ builder hay từ phiên chính.

STATE: thư mục phẳng, không SQLite, không module dùng chung (cố ý):
  <tempfile.gettempdir()>/agent-kit-flow/<session_id>/<prompt_id>/
      recon         đã có lần đọc file nào trong lượt
      agents        tập nhãn [agent] trích từ plan
      verified      verifier đã được gọi trong lượt (chỉ còn dùng cho chế độ
                    hạ cấp FLOW_GATE_REQUIRE_APPROVAL=0, xem bên dưới)
      builder_ok    builder đã được spawn kèm plan hợp lệ, qua vòng duyệt
      verification  verdict thật của verifier, ghi bởi hook verdict-gate.py
                    (SubagentStop): một dòng, ba field cách nhau bằng TAB
                    `<approved|blocked>\tsubagent_stop\t<verdict thô>`.
                    Không có file này nghĩa là trạng thái `pending`.
BIẾN MÔI TRƯỜNG: FLOW_GATE=off tắt hẳn (exit 0 ngay từ đầu);
FLOW_GATE_MIN_PROMPT ngưỡng ký tự tối thiểu cho prompt giao Agent (mặc định
200); FLOW_GATE_MIN_STEPS số bước tối thiểu trong plan giao builder (mặc định
2); FLOW_GATE_REQUIRE_VERIFIER=0 bỏ yêu cầu chạy verifier trước builder (chế
độ cũ, dựa trên marker `verified`); FLOW_GATE_REQUIRE_APPROVAL=0 hạ cấp cổng
duyệt thật (`verification`) về lại cổng cũ dựa trên `verified`/FLOW_GATE_REQUIRE_VERIFIER
— mọi lần hạ cấp đều bị ghi log vào ~/.claude/agent-kit-gate.log vì một lần
chặn oan dễ thành `export` vĩnh viễn trong ~/.zshrc mà không ai biết.

FAIL-OPEN (cố ý) khi: stdin không phải JSON; thiếu session_id/prompt_id;
không ghi được state (đĩa/quyền); ExitPlanMode mà `tool_input` rỗng hoặc
không có key `plan` (Claude Code ĐÔI KHI gửi `tool_input={}` cho
ExitPlanMode — đã đo thật; chặn ở đây sẽ deadlock vì hết đường lấy plan).
Ngoài các trường hợp trên, thiếu `recon` khi tới ExitPlanMode/Agent là CHẶN
(exit 2), không fail-open.
Matcher đề nghị: "Read|Grep|Glob|Bash|Agent|ExitPlanMode|Edit|Write|NotebookEdit". Exit 2 = chặn tool
call, stderr quay lại cho model làm lý do. Nguồn: https://code.claude.com/docs/en/hooks
"""
import json
import os
import pathlib
import re
import sys
import tempfile

STATE_ROOT = pathlib.Path(tempfile.gettempdir()) / "agent-kit-flow"
RECON_TOOLS = {"Read", "Grep", "Glob"}
# Bash CHỈ tính là khảo sát khi lệnh là lệnh ĐỌC. Rất nhiều phiên đọc code bằng
# `cat`/`sed -n`/`rg` thay vì tool Read (hướng dẫn auto mode còn khuyến khích thế).
# Không tính thì gate chặn oan đúng lối làm việc đó, mà nới cho MỌI lệnh Bash thì
# `echo hi` cũng thành recon — nên khớp theo đầu lệnh, kể cả sau `&&` hoặc `|`.
BASH_READ_RE = re.compile(
    r"(^|[;&|]\s*)(cat|head|tail|sed|grep|rg|ag|less|find|ls|wc|diff|"
    r"git\s+(show|diff|log|status|blame))\b"
)
MIN_PROMPT = int(os.environ.get("FLOW_GATE_MIN_PROMPT", "200"))
LABEL_RE = re.compile(r"^\s*[-*]?\s*\[([a-zA-Z][a-zA-Z-]*)\]", re.MULTILINE)

# Vòng duyệt trước khi builder được ghi file: parent nhúng plan vào prompt giao
# việc, `verifier` đối chiếu plan với code thật, rồi parent mới spawn builder.
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
MIN_STEPS = int(os.environ.get("FLOW_GATE_MIN_STEPS", "2"))
NEED_VERIFIER = os.environ.get("FLOW_GATE_REQUIRE_VERIFIER", "1") != "0"
NEED_APPROVAL = os.environ.get("FLOW_GATE_REQUIRE_APPROVAL", "1") != "0"
STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+\S", re.MULTILINE)
DOD_RE = re.compile(r"(DoD|nghi[ệe]m thu|ti[êe]u ch[íi]|acceptance|verification)\s*[:：]", re.I)
PROMPT_VERDICT_RE = re.compile(r"VERIFIER VERDICT:\s*(SAFE_TO_BUILD|NEEDS_FIX|BLOCK)", re.I)

GATE_LOG = pathlib.Path.home() / ".claude" / "agent-kit-gate.log"

def log_decision(result: str, pid, detail: str) -> None:
    """Ghi một dòng quyết định của cổng vào ~/.claude/agent-kit-gate.log.
    Lỗi ghi log (đĩa/quyền) KHÔNG được làm hook fail: bọc OSError."""
    try:
        line = f"flow-gate {result} pid={str(pid)[:8]} {detail}\n"
        with GATE_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass

def safe(name) -> str:
    """Lọc về ký tự an toàn cho tên thư mục, giống plan-gate.py:68-69."""
    return "".join(ch for ch in str(name) if ch.isalnum() or ch in "-_")[:64] or "default"

def bare_name(agent: str) -> str:
    """Tên agent sau dấu hai chấm cuối ("agent-kit:builder" -> "builder")."""
    return agent.rsplit(":", 1)[-1].strip().lower()

def deny(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2

NO_RECON_PLAN = ("BLOCKED bởi flow-gate: chưa có lần đọc file (Read/Grep/Glob) nào trong lượt này trước khi lập plan. "
                 "Đọc một file liên quan rồi ExitPlanMode lại, hoặc FLOW_GATE=off nếu task này không cần đọc gì trước.")
NO_RECON_AGENT = ("BLOCKED bởi flow-gate: chưa có lần đọc file nào trong lượt này trước khi giao việc cho subagent. "
                   "Đọc file liên quan trước, hoặc FLOW_GATE=off nếu chắc chắn không cần.")

def main() -> int:
    if os.environ.get("FLOW_GATE", "").lower() == "off":
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return 0  # FAIL-OPEN
    tool = payload.get("tool_name") or payload.get("toolName") or ""
    tin = payload.get("tool_input") or payload.get("toolInput") or {}
    sid, pid = payload.get("session_id"), payload.get("prompt_id")
    if not sid or not pid:
        return 0  # FAIL-OPEN: không định vị được state cho lượt này
    state_dir = STATE_ROOT / safe(sid) / safe(pid)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        recon, agents = state_dir / "recon", state_dir / "agents"
        verified, builder_ok = state_dir / "verified", state_dir / "builder_ok"
        verification = state_dir / "verification"
    except OSError:
        return 0  # FAIL-OPEN

    # Lệnh ghi file CỦA BUILDER: chỉ cho qua sau khi vòng duyệt đã xong.
    # `agent_type` có mặt ở PreToolUse của subagent — đã đo thật.
    if tool in WRITE_TOOLS:
        if bare_name(str(payload.get("agent_type") or "")) == "builder" \
                and not builder_ok.exists():
            return deny(
                "BLOCKED bởi flow-gate: builder ghi file mà lượt này chưa có plan nào được "
                "duyệt cho nó. Parent phải nhúng plan (các bước + tiêu chí nghiệm thu) vào "
                "prompt giao việc, cho `verifier` đối chiếu plan với code, rồi mới spawn "
                "builder. Thoát khẩn: FLOW_GATE=off.")
        return 0

    is_recon = tool in RECON_TOOLS
    if tool == "Bash" and isinstance(tin, dict):
        is_recon = bool(BASH_READ_RE.search(str(tin.get("command") or "")))
    if is_recon:
        try:
            recon.touch()
        except OSError:
            pass
        return 0
    if tool == "Bash":
        return 0  # lệnh Bash không phải lệnh đọc: không tính recon, cũng không chặn

    if tool == "ExitPlanMode":
        if not recon.exists():
            return deny(NO_RECON_PLAN)
        plan = tin.get("plan") if isinstance(tin, dict) else None
        if not isinstance(plan, str) or not plan.strip():
            return 0  # tool_input rỗng/không có plan -> fail-open, tránh deadlock
        labels = sorted(set(m.lower() for m in LABEL_RE.findall(plan)))
        try:
            agents.write_text("\n".join(labels), encoding="utf-8")
        except OSError:
            pass
        return 0

    if tool == "Agent":
        if not recon.exists():
            return deny(NO_RECON_AGENT)
        prompt = tin.get("prompt") if isinstance(tin, dict) else ""
        if not isinstance(prompt, str) or len(prompt) < MIN_PROMPT:
            return deny(f"BLOCKED bởi flow-gate: prompt giao việc ngắn hơn {MIN_PROMPT} ký tự, không đủ để agent con "
                        f"hiểu DoD/phạm vi. Viết lại prompt đầy đủ hơn, hoặc FLOW_GATE_MIN_PROMPT=<số nhỏ hơn>.")
        sub = tin.get("subagent_type") if isinstance(tin, dict) else ""
        sub = bare_name(sub) if isinstance(sub, str) else ""
        if agents.exists():
            labels = {ln.strip() for ln in agents.read_text(encoding="utf-8").splitlines() if ln.strip()}
            if labels and sub not in labels:
                return deny(f"BLOCKED bởi flow-gate: plan của lượt này không ghi nhãn [{sub or '?'}] cho "
                            f"bước nào. Lập lại plan có nhãn đúng rồi ExitPlanMode lại, hoặc FLOW_GATE=off.")
        if sub == "verifier":
            try:
                verified.touch()
            except OSError:
                pass
            log_decision("allow", pid, "verifier spawned sub=verifier")
        if sub == "builder":
            if not NEED_APPROVAL:
                # Chế độ hạ cấp: quay lại hành vi cũ dựa trên marker `verified`.
                log_decision(
                    "downgrade", pid,
                    "FLOW_GATE_REQUIRE_APPROVAL=0 -- cong duyet that (verification) bi bo qua, "
                    "dung lai kiem tra marker verified cu")
                if NEED_VERIFIER and not verified.exists():
                    return deny(
                        "BLOCKED bởi flow-gate: chưa cho `verifier` đối chiếu plan với code thật "
                        "trước khi giao builder. Gọi `verifier` với plan định giao, rồi spawn "
                        "builder. Bỏ yêu cầu này: FLOW_GATE_REQUIRE_VERIFIER=0.")
            else:
                approval = "pending"
                if verification.exists():
                    try:
                        content = verification.read_text(encoding="utf-8")
                        field0 = content.split("\t")[0].strip().lower()
                        if field0 in ("approved", "blocked"):
                            approval = field0
                    except OSError:
                        approval = "pending"
                if approval == "blocked":
                    return deny(
                        "BLOCKED bởi flow-gate: verifier đã kết luận BLOCKED cho lượt này nên "
                        "builder không được giao việc. Parent phải recon lại, sửa plan, rồi cho "
                        "verifier chạy lại và ra verdict mới. Thoát khẩn (không khuyến khích): "
                        "FLOW_GATE_REQUIRE_APPROVAL=0.")
                if approval == "pending":
                    m = PROMPT_VERDICT_RE.search(prompt)
                    if not m:
                        return deny(
                            "BLOCKED bởi flow-gate: lượt này chưa có verdict nào của verifier cho "
                            "builder. Hoặc (1) gọi `verifier` để nó chạy và ghi verdict, hoặc (2) "
                            "trích nguyên văn dòng `VERIFIER VERDICT: <SAFE_TO_BUILD|NEEDS_FIX|BLOCK>` "
                            "vào prompt giao builder.")
                    verdict = m.group(1).upper()
                    if verdict != "SAFE_TO_BUILD":
                        return deny(
                            f"BLOCKED bởi flow-gate: dòng VERIFIER VERDICT trong prompt là "
                            f"{verdict}, không phải SAFE_TO_BUILD, nên builder không được giao "
                            "việc. Sửa plan theo góp ý của verifier rồi cho verdict lại.")
                    log_decision("allow", pid, "nguon=prompt verdict=SAFE_TO_BUILD")
                # approval == "approved": qua, không cần log riêng (verdict-gate.py đã ghi).
            if len(STEP_RE.findall(prompt)) < MIN_STEPS or not DOD_RE.search(prompt):
                return deny(
                    f"BLOCKED bởi flow-gate: prompt giao builder chưa chứa plan. Cần ít nhất "
                    f"{MIN_STEPS} bước đánh số hoặc gạch đầu dòng, VÀ ít nhất một tiêu chí "
                    "nghiệm thu (dòng có `DoD:` / `Nghiệm thu:` / `Tiêu chí:`). Parent lập "
                    "plan rồi nhúng vào prompt, đừng giao việc trống.")
            try:
                builder_ok.touch()
            except OSError:
                pass
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
