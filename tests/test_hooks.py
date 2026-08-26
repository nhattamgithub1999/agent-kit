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

s, q = fresh()
flow(s, q, "Agent", subagent_type="agent-kit:verifier", prompt=PLAN_PROMPT)
check("15 verifier rồi builder có plan -> 0",
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
