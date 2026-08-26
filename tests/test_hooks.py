#!/usr/bin/env python3
"""
Test hộp đen cho hooks/*.py. KHÔNG import module hook, KHÔNG mock, KHÔNG dùng
pytest — chạy hook thật qua subprocess và khẳng định returncode/stdout.
Chạy: python3 tests/test_hooks.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import uuid

REPO = pathlib.Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"


def run(hook, payload, env_extra=None):
    env = dict(os.environ)
    env.pop("FLOW_GATE", None)
    env.pop("PLAN_GATE", None)
    env.pop("NOFAKEPASS_AGENTS", None)
    env.pop("NOFAKEPASS_STRICT", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(HOOKS / hook)],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=10,
    )
    return proc


PASS_MSG = "All tests passed, build thành công, 0 lỗi."

results = []


def check(name, got, want, extra_ok=True):
    ok = got == want and extra_ok
    results.append((name, ok, f"want={want} got={got}"))


# 1-4: no-fake-pass — khớp tên agent có tiền tố plugin
sid1, pid1 = str(uuid.uuid4()), str(uuid.uuid4())
p = run("no-fake-pass.py", {"agent_type": "agent-kit:builder", "last_assistant_message": PASS_MSG})
check("1 no-fake-pass agent-kit:builder -> 2", p.returncode, 2)

p = run("no-fake-pass.py", {"agent_type": "builder", "last_assistant_message": PASS_MSG})
check("2 no-fake-pass builder -> 2", p.returncode, 2)

p = run("no-fake-pass.py", {"agent_type": "agent-kit:Explore", "last_assistant_message": PASS_MSG})
check("3 no-fake-pass agent-kit:Explore -> 0", p.returncode, 0)

p = run("no-fake-pass.py", {"last_assistant_message": PASS_MSG})
check("4 no-fake-pass no agent_type -> 0", p.returncode, 0)

LONG_PROMPT = "x" * 220

# 5: flow-gate, Agent trước khi có recon -> 2
sid5, pid5 = str(uuid.uuid4()), str(uuid.uuid4())
p = run("flow-gate.py", {
    "tool_name": "Agent", "session_id": sid5, "prompt_id": pid5,
    "tool_input": {"subagent_type": "agent-kit:architect", "prompt": LONG_PROMPT},
})
check("5 flow-gate Agent no recon -> 2", p.returncode, 2)

# 6: Read rồi Agent (prompt dài, không plan) -> 0
sid6, pid6 = str(uuid.uuid4()), str(uuid.uuid4())
p = run("flow-gate.py", {
    "tool_name": "Read", "session_id": sid6, "prompt_id": pid6,
    "tool_input": {"file_path": str(REPO / "README.md")},
})
check("6a flow-gate Read -> 0", p.returncode, 0)
p = run("flow-gate.py", {
    "tool_name": "Agent", "session_id": sid6, "prompt_id": pid6,
    "tool_input": {"subagent_type": "agent-kit:architect", "prompt": LONG_PROMPT},
})
check("6b flow-gate Agent after recon, no plan -> 0", p.returncode, 0)

# 7: Read, ExitPlanMode có [builder], rồi Agent architect -> 2
sid7, pid7 = str(uuid.uuid4()), str(uuid.uuid4())
run("flow-gate.py", {
    "tool_name": "Read", "session_id": sid7, "prompt_id": pid7,
    "tool_input": {"file_path": str(REPO / "README.md")},
})
p = run("flow-gate.py", {
    "tool_name": "ExitPlanMode", "session_id": sid7, "prompt_id": pid7,
    "tool_input": {"plan": "- [builder] làm việc gì đó cụ thể\n"},
})
check("7a flow-gate ExitPlanMode with plan -> 0", p.returncode, 0)
p = run("flow-gate.py", {
    "tool_name": "Agent", "session_id": sid7, "prompt_id": pid7,
    "tool_input": {"subagent_type": "agent-kit:architect", "prompt": LONG_PROMPT},
})
check("7b flow-gate Agent mismatched label -> 2", p.returncode, 2)

# 8: Read, ExitPlanMode với tool_input rỗng {} -> 0
sid8, pid8 = str(uuid.uuid4()), str(uuid.uuid4())
run("flow-gate.py", {
    "tool_name": "Read", "session_id": sid8, "prompt_id": pid8,
    "tool_input": {"file_path": str(REPO / "README.md")},
})
p = run("flow-gate.py", {
    "tool_name": "ExitPlanMode", "session_id": sid8, "prompt_id": pid8,
    "tool_input": {},
})
check("8 flow-gate ExitPlanMode empty tool_input -> 0", p.returncode, 0)

# 9: prompt-intake, prompt dài -> 0, stdout không khẳng định lớp
p = run("prompt-intake.py", {
    "prompt": "Cần refactor lại module xử lý thanh toán cho gọn hơn và thêm test.",
})
check("9 prompt-intake -> 0, no 'thuộc lớp'", p.returncode, 0, "thuộc lớp" not in p.stdout)

# 10-12: Bash chỉ tính là recon khi là lệnh ĐỌC
def bash_then_agent(cmd):
    s, q = str(uuid.uuid4()), str(uuid.uuid4())
    run("flow-gate.py", {"tool_name": "Bash", "session_id": s, "prompt_id": q,
                         "tool_input": {"command": cmd}})
    return run("flow-gate.py", {
        "tool_name": "Agent", "session_id": s, "prompt_id": q,
        "tool_input": {"subagent_type": "agent-kit:architect", "prompt": LONG_PROMPT},
    }).returncode


check("10 Bash 'cat f' tính là recon -> Agent 0", bash_then_agent("cat hooks/flow-gate.py"), 0)
check("11 Bash 'echo hi' KHÔNG phải recon -> Agent 2", bash_then_agent("echo hi"), 2)
check("12 Bash ghép 'cd x && sed -n' tính là recon -> Agent 0",
      bash_then_agent("cd /tmp && sed -n '1,5p' foo.txt"), 0)

# 14-19: vòng duyệt trước builder — parent lập plan, verifier đối chiếu, rồi builder ghi
PLAN_PROMPT = ("Nhiệm vụ triển khai trong repo này, ngữ cảnh đã đọc đầy đủ ở app.py.\n"
               "1. Thêm hàm shout(s) vào app.py trả về s viết hoa.\n"
               "2. Thêm test cho hàm đó.\n"
               "DoD: `python3 -m pytest` pass, 0 lỗi.\n") + "x" * 120
NOPLAN_PROMPT = "Sửa giúp cái hàm đó cho đúng, làm sao cho nó chạy được là được." + "y" * 200


def flow(sid, pid, tool, **tin):
    p = {"tool_name": tool, "session_id": sid, "prompt_id": pid, "tool_input": tin}
    if "_agent_type" in tin:
        p["agent_type"] = tin.pop("_agent_type")
    return run("flow-gate.py", p).returncode


def fresh():
    s, q = str(uuid.uuid4()), str(uuid.uuid4())
    flow(s, q, "Read", file_path=str(REPO / "README.md"))
    return s, q


s, q = fresh()
check("14 builder chưa qua verifier -> 2",
      flow(s, q, "Agent", subagent_type="agent-kit:builder", prompt=PLAN_PROMPT), 2)

STATE_ROOT = pathlib.Path(tempfile.gettempdir()) / "agent-kit-flow"


def sdir(sid, pid):
    """Cùng cách định vị state với flow-gate.py:124 (safe() giữ nguyên uuid)."""
    return STATE_ROOT / sid / pid


def set_verification(sid, pid, status, verdict="SAFE_TO_BUILD"):
    """Mô phỏng đúng thứ verdict-gate.py ghi ra: một dòng, ba field cách bằng TAB."""
    d = sdir(sid, pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "verification").write_text(f"{status}\tsubagent_stop\t{verdict}\n", encoding="utf-8")


# 15: verifier ĐÃ ĐƯỢC SPAWN nhưng CHƯA có verdict -> builder vẫn bị chặn.
# Trước 1.0.3 ca này trả 0: marker `verified` được touch ngay lúc spawn verifier,
# nên nó chỉ chứng minh "đã gọi", không chứng minh "đã approve". Đổi có chủ đích.
s, q = fresh()
flow(s, q, "Agent", subagent_type="agent-kit:verifier", prompt=PLAN_PROMPT)
check("15 verifier đã spawn nhưng CHƯA có verdict -> builder 2",
      flow(s, q, "Agent", subagent_type="agent-kit:builder", prompt=PLAN_PROMPT), 2)

set_verification(s, q, "approved")
check("15b verdict approved -> builder 0",
      flow(s, q, "Agent", subagent_type="agent-kit:builder", prompt=PLAN_PROMPT), 0)

s2, q2 = fresh()
flow(s2, q2, "Agent", subagent_type="agent-kit:verifier", prompt=PLAN_PROMPT)
check("16 verifier rồi builder KHÔNG có plan -> 2",
      flow(s2, q2, "Agent", subagent_type="agent-kit:builder", prompt=NOPLAN_PROMPT), 2)

s3, q3 = fresh()
p3 = {"tool_name": "Edit", "session_id": s3, "prompt_id": q3,
      "agent_type": "agent-kit:builder", "tool_input": {"file_path": "/tmp/x.py"}}
check("17 builder ghi file khi chưa được duyệt -> 2", run("flow-gate.py", p3).returncode, 2)

# s,q ở ca 15 đã có builder_ok
p4 = {"tool_name": "Edit", "session_id": s, "prompt_id": q,
      "agent_type": "agent-kit:builder", "tool_input": {"file_path": "/tmp/x.py"}}
check("18 builder ghi file sau khi đã duyệt -> 0", run("flow-gate.py", p4).returncode, 0)

p5 = {"tool_name": "Edit", "session_id": s3, "prompt_id": q3,
      "tool_input": {"file_path": "/tmp/x.py"}}
check("19 parent ghi file (không phải builder) -> 0", run("flow-gate.py", p5).returncode, 0)


# 20: version trong plugin.json và marketplace.json phải khớp nhau.
# Repo này đã từng phát hành lệch hai file đó một lần.
pv = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
mv = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))["plugins"][0]["version"]
check(f"20 version đồng bộ (plugin={pv} marketplace={mv})", pv, mv)


# 21-26: session-policy — chọn policy theo hook_event_name, chống lặp theo
# agent_id cho SubagentStart (không phải session_id, vì event này bắn lại sau
# khi subagent bị chặn rồi chạy tiếp trong cùng phiên).

# 21: SessionStart -> nội dung supervisor.md có, worker.md không có
sid21 = str(uuid.uuid4())
p = run("session-policy.py", {"hook_event_name": "SessionStart", "session_id": sid21})
check("21 SessionStart -> supervisor có, worker không",
      p.returncode, 0, "Routing" in p.stdout and "Luật thực thi" not in p.stdout)

# 22: SubagentStart (agent_id riêng) -> nội dung worker.md có, '## Routing' không có
sid22, aid22 = str(uuid.uuid4()), str(uuid.uuid4())
p = run("session-policy.py", {
    "hook_event_name": "SubagentStart", "session_id": sid22, "agent_id": aid22,
})
check("22 SubagentStart -> worker có, '## Routing' không",
      p.returncode, 0, "Luật thực thi" in p.stdout and "## Routing" not in p.stdout)

# 23: cả hai event trên đều chứa nội dung common.md (No-fabrication)
sid23a = str(uuid.uuid4())
p23a = run("session-policy.py", {"hook_event_name": "SessionStart", "session_id": sid23a})
sid23b, aid23b = str(uuid.uuid4()), str(uuid.uuid4())
p23b = run("session-policy.py", {
    "hook_event_name": "SubagentStart", "session_id": sid23b, "agent_id": aid23b,
})
check("23 SessionStart và SubagentStart đều chứa common.md",
      p23a.returncode, 0,
      p23b.returncode == 0 and "No-fabrication" in p23a.stdout and "No-fabrication" in p23b.stdout)

# 24: event lạ -> exit 0, stdout rỗng
sid24 = str(uuid.uuid4())
p = run("session-policy.py", {"hook_event_name": "Stop", "session_id": sid24})
check("24 event lạ (Stop) -> stdout rỗng", p.returncode, 0, p.stdout.strip() == "")

# 25: hai SubagentStart khác agent_id -> cả hai đều có stdout không rỗng
sid25 = str(uuid.uuid4())
aid25a, aid25b = str(uuid.uuid4()), str(uuid.uuid4())
p25a = run("session-policy.py", {
    "hook_event_name": "SubagentStart", "session_id": sid25, "agent_id": aid25a,
})
p25b = run("session-policy.py", {
    "hook_event_name": "SubagentStart", "session_id": sid25, "agent_id": aid25b,
})
check("25 hai SubagentStart khác agent_id -> cả hai stdout không rỗng",
      p25a.returncode, 0,
      p25b.returncode == 0 and p25a.stdout.strip() != "" and p25b.stdout.strip() != "")

# 26: hai SubagentStart CÙNG agent_id -> lần hai stdout rỗng (chống lặp)
sid26 = str(uuid.uuid4())
aid26 = str(uuid.uuid4())
p26a = run("session-policy.py", {
    "hook_event_name": "SubagentStart", "session_id": sid26, "agent_id": aid26,
})
p26b = run("session-policy.py", {
    "hook_event_name": "SubagentStart", "session_id": sid26, "agent_id": aid26,
})
check("26 hai SubagentStart cùng agent_id -> lần hai stdout rỗng",
      p26a.returncode, 0,
      p26b.returncode == 0 and p26a.stdout.strip() != "" and p26b.stdout.strip() == "")


# 27: SessionStart KHÔNG chống lặp — nó bắn lại sau /compact và /resume với cùng
# session_id, đúng lúc context vừa bị cắt nên policy cần được nạp lại.
_s = str(uuid.uuid4())
_a = run("session-policy.py", {"hook_event_name": "SessionStart", "session_id": _s})
_b = run("session-policy.py", {"hook_event_name": "SessionStart", "session_id": _s})
check("27 SessionStart lặp lại vẫn tiêm (compact/resume)",
      [_a.returncode, bool(_a.stdout.strip()), bool(_b.stdout.strip())], [0, True, True])


# ---------------------------------------------------------------------------
# 28-40: verdict của verifier điều khiển quyền builder (lô 1), và cổng evidence
# siết theo output lệnh thật (lô 2).
# ---------------------------------------------------------------------------

# 28: verdict blocked -> builder bị chặn.
s28, q28 = fresh()
flow(s28, q28, "Agent", subagent_type="agent-kit:verifier", prompt=PLAN_PROMPT)
set_verification(s28, q28, "blocked", "BLOCK")
check("28 verdict blocked -> builder 2",
      flow(s28, q28, "Agent", subagent_type="agent-kit:builder", prompt=PLAN_PROMPT), 2)

# 29: NGUỒN MẠNH THẮNG NGUỒN YẾU. `verification` là blocked thì một dòng
# `VERIFIER VERDICT: SAFE_TO_BUILD` do parent chép vào prompt KHÔNG mở được cổng.
check("29 blocked + prompt tự khai SAFE_TO_BUILD -> vẫn 2",
      flow(s28, q28, "Agent", subagent_type="agent-kit:builder",
           prompt=PLAN_PROMPT + "\nVERIFIER VERDICT: SAFE_TO_BUILD\n"), 2)

# 30: pending + nguồn dự phòng trong cùng payload -> cho qua.
s30, q30 = fresh()
check("30 pending + VERIFIER VERDICT trong prompt -> 0",
      flow(s30, q30, "Agent", subagent_type="agent-kit:builder",
           prompt=PLAN_PROMPT + "\nVERIFIER VERDICT: SAFE_TO_BUILD\n"), 0)

# 31: nguồn dự phòng khai BLOCK -> chặn.
s31, q31 = fresh()
check("31 pending + VERIFIER VERDICT: BLOCK -> 2",
      flow(s31, q31, "Agent", subagent_type="agent-kit:builder",
           prompt=PLAN_PROMPT + "\nVERIFIER VERDICT: BLOCK\n"), 2)

# 32: approved nhưng prompt thiếu plan -> vẫn chặn (cổng plan không bị bỏ qua).
s32, q32 = fresh()
set_verification(s32, q32, "approved")
check("32 approved nhưng prompt thiếu plan -> 2",
      flow(s32, q32, "Agent", subagent_type="agent-kit:builder", prompt=NOPLAN_PROMPT), 2)

# 33: hạ cấp bằng env -> quay về hành vi cũ (chỉ cần verifier đã spawn).
s33, q33 = fresh()
p33 = {"tool_name": "Agent", "session_id": s33, "prompt_id": q33,
       "tool_input": {"subagent_type": "agent-kit:verifier", "prompt": PLAN_PROMPT}}
run("flow-gate.py", p33, {"FLOW_GATE_REQUIRE_APPROVAL": "0"})
p33b = {"tool_name": "Agent", "session_id": s33, "prompt_id": q33,
        "tool_input": {"subagent_type": "agent-kit:builder", "prompt": PLAN_PROMPT}}
check("33 FLOW_GATE_REQUIRE_APPROVAL=0 -> builder 0 (hành vi cũ)",
      run("flow-gate.py", p33b, {"FLOW_GATE_REQUIRE_APPROVAL": "0"}).returncode, 0)

# 34: FAST PATH. Task explore-only không bị kéo vào vòng duyệt của builder.
s34, q34 = fresh()
check("34 explore-only không bị đòi verdict -> 0",
      flow(s34, q34, "Agent", subagent_type="agent-kit:Explore", prompt=LONG_PROMPT), 0)


def verdict_gate(sid, pid, text, agent=None, omit_pid=False):
    """Chạy verdict-gate.py rồi trả (returncode, nội dung file verification hoặc None)."""
    p = {"session_id": sid, "last_assistant_message": text}
    if not omit_pid:
        p["prompt_id"] = pid
    if agent:
        p["agent_type"] = agent
    rc = run("verdict-gate.py", p).returncode
    f = sdir(sid, pid) / "verification"
    return rc, (f.read_text(encoding="utf-8") if f.exists() else None)

# 35: verifier trả SAFE_TO_BUILD -> ghi approved.
s35, q35 = str(uuid.uuid4()), str(uuid.uuid4())
rc, body = verdict_gate(s35, q35, "### VERDICT: SAFE_TO_BUILD", "agent-kit:verifier")
check("35 verdict-gate SAFE_TO_BUILD -> approved",
      [rc, (body or "").split("\t")[0]], [0, "approved"])

# 36: verifier trả BLOCK -> ghi blocked. Nhận dạng qua NỘI DUNG, payload không có tên agent
# (đã đo thật: payload SubagentStop thường thiếu tên agent).
s36, q36 = str(uuid.uuid4()), str(uuid.uuid4())
rc, body = verdict_gate(s36, q36, "### VERDICT: BLOCK")
check("36 verdict-gate BLOCK (không có tên agent) -> blocked",
      [rc, (body or "").split("\t")[0]], [0, "blocked"])

# 37: critic dùng từ vựng KHÁC (PASS|FAIL) -> không được ghi state.
s37, q37 = str(uuid.uuid4()), str(uuid.uuid4())
rc, body = verdict_gate(s37, q37, "VERDICT: PASS", "agent-kit:critic")
check("37 verdict-gate với critic -> không ghi state", [rc, body], [0, None])

# 38: HỒI QUY LỖ HỔNG TỰ DUYỆT. Payload không có tên agent, text chứa đúng dòng
# `VERIFIER VERDICT:` mà parent chép vào prompt giao builder — transcript của builder
# chứa lại prompt của chính nó. Regex không anchor sẽ khớp CHUỖI CON `VERDICT:
# SAFE_TO_BUILD` và tự ghi approved, tức builder tự mở cổng cho mình. Phải KHÔNG ghi gì.
s38, q38 = str(uuid.uuid4()), str(uuid.uuid4())
rc, body = verdict_gate(
    s38, q38,
    "1. buoc mot\n2. buoc hai\nDoD: pytest pass\nVERIFIER VERDICT: SAFE_TO_BUILD\n")
check("38 dòng VERIFIER VERDICT do parent chép -> KHÔNG tự duyệt", [rc, body], [0, None])

# 39: thiếu prompt_id -> không ghi state (approval không rò rỉ sang lượt khác).
s39, q39 = str(uuid.uuid4()), str(uuid.uuid4())
rc, body = verdict_gate(s39, q39, "### VERDICT: SAFE_TO_BUILD",
                        "agent-kit:verifier", omit_pid=True)
check("39 verdict-gate thiếu prompt_id -> không ghi state", [rc, body], [0, None])


def nfp(text, **extra):
    p = {"agent_type": "agent-kit:builder", "last_assistant_message": text}
    p.update(extra)
    return run("no-fake-pass.py", p).returncode

# 40: bốn báo cáo giả đã đo là LỌT trước 1.0.3 -> giờ phải bị chặn.
LEAKED = [
    ("code block là code nguồn",
     "Đã xong. All tests passed.\n\n```python\ndef shout(s):\n    return s.upper()\n```"),
    ("code block rỗng", "Tests pass, 0 lỗi.\n```\n```"),
    ("CHƯA VERIFY trần cạnh claim pass", "All tests passed. CHƯA VERIFY"),
    ("output tự chứng minh FAIL", "VERDICT: READY\n```\n2 failed, 1 passed\n```"),
]
for i, (name, text) in enumerate(LEAKED, 1):
    check(f"40.{i} no-fake-pass chặn: {name}", nfp(text), 2)

# 41: báo cáo TRUNG THỰC phải luôn đi qua. Đây là ca `gloss-gate` từng chặn sai
# (README: 30/60 lần chặn là chặn đúng câu policy bắt buộc phải viết).
check("41 CHƯA VERIFY kèm lý do, không claim pass -> 0",
      nfp("CHƯA VERIFY: không có lệnh test khai báo trong CLAUDE.md của project."), 0)

# 42: bằng chứng THẬT -> cho qua.
check("42 claim pass + output lệnh thật -> 0",
      nfp("Đã chạy xong.\n```\n$ python3 tests/test_hooks.py\n29/29 pass\n```"), 0)

# 43: chặn tối đa một lần mỗi lượt dừng — giữ nguyên, nếu bỏ sẽ treo phiên.
check("43 stop_hook_active -> không chặn lần hai",
      nfp("VERDICT: READY\n```\n2 failed, 1 passed\n```", stop_hook_active=True), 0)

# 44: fail-open khi payload không có tên agent — giữ nguyên có chủ đích.
check("44 không có tên agent -> fail-open",
      run("no-fake-pass.py",
          {"last_assistant_message": "VERDICT: READY\n```\n2 failed\n```"}).returncode, 0)


# 13: mọi hook trong hooks.json phải TỒN TẠI và CHẠY TRỰC TIẾP ĐƯỢC.
# `hooks.json` gọi thẳng đường dẫn file, không qua `python3 <file>`. Một hook thiếu
# bit thực thi sẽ im lặng không chạy, trong khi test gọi qua sys.executable vẫn xanh.
# Đúng lỗi đó đã xảy ra với flow-gate.py và chỉ lộ ra khi chạy phiên thật.
cfg = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
bad = []
for _ev, groups in cfg["hooks"].items():
    for g in groups:
        for h in g["hooks"]:
            f = REPO / "hooks" / h["command"].split("/hooks/")[-1]
            if not f.exists():
                bad.append(f"{f.name}: không tồn tại")
            elif not os.access(f, os.X_OK):
                bad.append(f"{f.name}: thiếu bit thực thi")
check("13 mọi hook trong hooks.json tồn tại và executable", bad, [])

fails = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(("PASS " if ok else "FAIL ") + name + " (" + detail + ")")
print(f"\n{len(results) - len(fails)}/{len(results)} pass")
sys.exit(0 if not fails else 1)
