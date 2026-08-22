#!/usr/bin/env python3
"""
Benchmark harness — ĐỌC TRANSCRIPT THẬT của Claude Code, không mô phỏng.

    python3 bench/score.py --label nhanh-a --session <sessionId>
    python3 bench/score.py --label nhanh-b --session <sessionId>
    python3 bench/score.py --compare nhanh-a nhanh-b

Transcript nằm ở ~/.claude/projects/{project}/{sessionId}/ ; transcript subagent
ở .../subagents/agent-{agentId}.jsonl
Nguồn: https://code.claude.com/docs/en/sub-agents

CẢNH BÁO VỀ SCHEMA: schema JSONL của transcript KHÔNG được tài liệu hoá đầy đủ và
có thể đổi theo version. Harness này đọc TOLERANT: quét mọi key, không giả định
cấu trúc cố định. Chỉ số nào không trích được sẽ hiện `n/a` chứ KHÔNG đoán.
Chạy `--dump-keys` một lần để xem schema thật trên máy anh rồi siết lại.

CHỈ SỐ ĐO ĐƯỢC TỰ ĐỘNG (proxy, không phải chân lý):
  plan_rate        % lượt user có TodoWrite/ExitPlanMode TRƯỚC lần ghi file đầu
  delegate_rate    % lượt user có ≥1 Agent call
  agent_mix        số lần XUẤT HIỆN của agent_type (đếm cả input lẫn transcript
                   con → over-count, chỉ dùng để so tương đối giữa 2 nhánh)
  edits_before_plan số lần Edit/Write xảy ra trước plan đầu tiên
  ask_loops        số khối QUESTION của builder
  tool_calls       tổng, và theo loại
  turns            số lượt user

CHỈ SỐ PHẢI CHẤM TAY (không tự động được — xem bench/TASKS.md):
  fabrication_rate, escaped_defect_rate, DoD_quality
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter

BENCH = pathlib.Path(__file__).resolve().parent
RESULTS = BENCH / "results"
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
PLAN_TOOLS = {"TodoWrite", "ExitPlanMode"}


def iter_events(session_dir: pathlib.Path):
    """Duyệt mọi dòng JSONL trong phiên, gồm cả subagent."""
    files = sorted(session_dir.rglob("*.jsonl"))
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield f, json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


def find_session(session_id: str) -> pathlib.Path | None:
    root = pathlib.Path.home() / ".claude" / "projects"
    if not root.exists():
        return None
    for p in root.glob(f"*/{session_id}"):
        if p.is_dir():
            return p
    return None


def walk(obj, depth=0):
    if depth > 10:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, depth + 1)


def tool_name_of(node: dict) -> str | None:
    if node.get("type") == "tool_use" and isinstance(node.get("name"), str):
        return node["name"]
    for k in ("tool_name", "toolName"):
        if isinstance(node.get(k), str):
            return node[k]
    return None


def is_user_turn(ev: dict) -> bool:
    # Lượt user thật: role/type == user VÀ không phải tool_result quay lại.
    role = ev.get("role") or ev.get("type") or (ev.get("message") or {}).get("role")
    if role != "user":
        return False
    return not any(n.get("type") == "tool_result" for n in walk(ev))


def score(session_dir: pathlib.Path) -> dict:
    seq: list[tuple[str, str]] = []  # (kind, value) theo thứ tự xuất hiện
    agent_types = Counter()
    tools = Counter()
    questions = 0

    for _f, ev in iter_events(session_dir):
        if is_user_turn(ev):
            seq.append(("user", ""))
        for node in walk(ev):
            t = tool_name_of(node)
            if t:
                tools[t] += 1
                seq.append(("tool", t))
                if t in ("Agent", "Task"):
                    inp = node.get("input") or node.get("tool_input") or {}
                    at = inp.get("subagent_type") or inp.get("agent_type") or "?"
                    agent_types[str(at)] += 1
            for k in ("agent_type", "subagent_type"):
                if isinstance(node.get(k), str):
                    agent_types[node[k]] += 1
        blob = json.dumps(ev, ensure_ascii=False)
        questions += len(re.findall(r"ĐÃ THỬ:", blob))

    turns = planned = delegated = 0
    edits_before_plan = 0
    cur_planned = cur_delegated = False
    cur_edits_pre_plan = 0

    def flush():
        nonlocal turns, planned, delegated, edits_before_plan
        if turns == 0:
            return
        planned += cur_planned
        delegated += cur_delegated
        edits_before_plan += cur_edits_pre_plan

    for kind, val in seq:
        if kind == "user":
            flush()
            turns += 1
            cur_planned = cur_delegated = False
            cur_edits_pre_plan = 0
        elif kind == "tool":
            if val in PLAN_TOOLS:
                cur_planned = True
            elif val in ("Agent", "Task"):
                cur_delegated = True
            elif val in WRITE_TOOLS and not cur_planned:
                cur_edits_pre_plan += 1
    flush()

    pct = lambda n: round(100 * n / turns, 1) if turns else None
    return {
        "turns": turns,
        "plan_rate_pct": pct(planned),
        "delegate_rate_pct": pct(delegated),
        "edits_before_plan": edits_before_plan,
        "ask_loops": questions,
        "tool_calls_total": sum(tools.values()),
        "tools": dict(tools.most_common(12)),
        "agent_mix": dict(agent_types.most_common()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    ap.add_argument("--path", help="đường dẫn thư mục phiên, thay cho --session")
    ap.add_argument("--label")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--dump-keys", action="store_true")
    a = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)

    if a.compare:
        rows = []
        for lab in a.compare:
            f = RESULTS / f"{lab}.json"
            if not f.exists():
                print(f"Chưa có {f} — chạy --label {lab} trước.")
                return 1
            rows.append((lab, json.loads(f.read_text())))
        keys = ["turns", "plan_rate_pct", "delegate_rate_pct", "edits_before_plan",
                "ask_loops", "tool_calls_total"]
        w = max(len(k) for k in keys)
        print(f"{'chỉ số':<{w}}  {rows[0][0]:>10}  {rows[1][0]:>10}   delta")
        for k in keys:
            x, y = rows[0][1].get(k), rows[1][1].get(k)
            d = "n/a" if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) \
                else f"{y - x:+.1f}"
            print(f"{k:<{w}}  {str(x):>10}  {str(y):>10}   {d}")
        print("\nLƯU Ý: đây là proxy metric từ transcript. fabrication_rate và")
        print("escaped_defect_rate PHẢI chấm tay theo bench/TASKS.md.")
        return 0

    d = pathlib.Path(a.path) if a.path else (find_session(a.session) if a.session else None)
    if not d or not d.exists():
        print("Không tìm thấy thư mục phiên. Dùng --path, hoặc kiểm ~/.claude/projects/")
        return 1

    if a.dump_keys:
        seen = Counter()
        for _f, ev in iter_events(d):
            for n in walk(ev):
                seen.update(n.keys())
        for k, v in seen.most_common(40):
            print(f"{v:>6}  {k}")
        return 0

    res = score(d)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if a.label:
        (RESULTS / f"{a.label}.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ lưu {RESULTS / (a.label + '.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
