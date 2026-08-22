#!/usr/bin/env python3
"""
Validator ngữ nghĩa cho agent-kit.

Khác các bản trước: bản cũ gần như toàn bộ là `"<chuỗi>" in text` — đo "có gõ đúng từ khoá"
chứ không đo "cấu hình có đúng". Bản này kiểm GIÁ TRỊ (model tier, ngưỡng số,
tools rỗng, nhất quán chéo file) và tự chứng minh bằng negative control tích hợp.

    python3 validate.py              # kiểm kit
    python3 validate.py --selftest   # tiêm 10 defect thật, phải bắt được 10/10

GIỚI HẠN: vẫn là kiểm TĨNH. Không đo agent có thật sự ngừng bịa hay không.
"""
from __future__ import annotations

import copy
import pathlib
import re
import shutil
import sys
import tempfile

# Alias theo docs Claude Code; full model ID khớp regex bên dưới.
MODEL_ALIASES = {"haiku", "sonnet", "opus", "fable", "inherit"}
MODEL_ID = re.compile(r"^claude-[a-z0-9][a-z0-9.\-]*$")
KNOWN_TOOLS = {
    "Read", "Write", "Edit", "NotebookEdit", "Bash", "PowerShell", "Glob",
    "Grep", "WebFetch", "WebSearch", "Agent", "Skill", "TodoWrite",
    "SendMessage", "ToolSearch", "Artifact", "Monitor", "TaskStop",
}
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}
READ_ONLY_AGENTS = {"Explore", "architect", "critic", "verifier"}
FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


def policy_file(root):
    """Kit cũ: CLAUDE.delegation.md ở root. Plugin: policy/delegation.md."""
    a = root / "CLAUDE.delegation.md"
    return a if a.exists() else root / "policy" / "delegation.md"


# ---------------------------------------------------------------- frontmatter
def parse_fm(text: str):
    """Parser YAML tối giản: scalar, folded '>-', và list '- item'."""
    m = FM_RE.match(text)
    if not m:
        return None, None, "frontmatter không parse được"
    fm, key, mode = {}, None, None
    for raw in m.group(1).split("\n"):
        if not raw.strip():
            continue
        if re.match(r"^[\w-]+:", raw):
            key, _, val = raw.partition(":")
            key = key.strip()
            val = re.sub(r"\s+#.*$", "", val).strip()
            if val in (">-", ">", "|", "|-", ""):
                fm[key], mode = ("" if val else None), "fold" if val else "maybe_list"
            else:
                fm[key], mode = val, None
        elif raw.lstrip().startswith("- "):
            item = raw.lstrip()[2:].strip()
            if not isinstance(fm.get(key), list):
                fm[key] = []
            fm[key].append(item)
        elif key is not None:
            prev = fm.get(key) or ""
            fm[key] = (prev + " " + raw.strip()).strip() if isinstance(prev, str) else prev
    return fm, m.group(2), None


def tool_list(val) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [x.strip() for x in val if x.strip()]
    val = val.strip()
    if val in ("[]", "[ ]"):
        return []
    val = val.strip("[]")
    return [x.strip() for x in val.split(",") if x.strip()]


# -------------------------------------------------------------------- harness
class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, bool, str]] = []

    def add(self, area, name, ok, detail=""):
        self.rows.append((area, name, bool(ok), detail))

    @property
    def fails(self):
        return [r for r in self.rows if not r[2]]


def run_checks(root: pathlib.Path) -> Report:
    r = Report()
    agents_dir = root / "agents"
    agents: dict[str, dict] = {}

    for p in sorted(agents_dir.glob("*.md")):
        fm, body, err = parse_fm(p.read_text(encoding="utf-8"))
        a = p.stem
        if err:
            r.add(a, "frontmatter parse", False, err)
            continue
        r.add(a, "frontmatter parse", True)
        name = fm.get("name") or ""
        agents[name or a] = {"fm": fm, "body": body, "file": p.name}

        r.add(a, "có name", bool(name))
        desc = fm.get("description") or ""
        r.add(a, "có description", bool(desc))
        r.add(a, "description có mệnh đề loại trừ", "KHÔNG dùng" in desc or "KHÔNG kèm" in desc)

        mdl = fm.get("model") or "inherit"
        r.add(a, "model hợp lệ", mdl in MODEL_ALIASES or bool(MODEL_ID.match(mdl)), f"model={mdl}")

        allow = tool_list(fm.get("tools"))
        deny = tool_list(fm.get("disallowedTools"))
        has_tools_key = "tools" in fm

        # v7.3 defect: `tools: []` -> subagent không launch được.
        r.add(a, "tools không rỗng (nếu khai)", not (has_tools_key and not allow),
              "tools rỗng => agent fail-to-launch")
        bad = [t for t in allow + deny if t not in KNOWN_TOOLS]
        r.add(a, "tên tool hợp lệ", not bad, f"tool lạ: {bad}")

        if name in READ_ONLY_AGENTS:
            writes = (set(allow) & WRITE_TOOLS) if allow else set()
            denied_all = WRITE_TOOLS <= set(deny)
            r.add(a, "read-only (chặn Write/Edit)", (not writes) and (denied_all or bool(allow)),
                  f"allow={allow} deny={deny}")

        # Không agent nào được tự spawn subagent -> escalation phải là DỪNG-báo-parent.
        can_spawn = "Agent" in allow or (not allow and "Agent" not in deny)
        # Bỏ qua dòng nói PARENT gọi — chỉ bắt câu bảo CHÍNH agent này tự gọi.
        refs = set()
        for ln in body.split("\n"):
            if re.search(r"[Pp]arent", ln):
                continue
            refs |= set(re.findall(r"gọi\s+`([\w-]+)`", ln))
        needs_spawn = bool(refs & (set(READ_ONLY_AGENTS) | {"builder", "advisor"}))
        r.add(a, "không yêu cầu spawn subagent khi thiếu tool Agent",
              not (needs_spawn and not can_spawn), f"body bảo gọi {sorted(refs)} nhưng không có tool Agent")
        r.add(a, "không tự spawn subagent (escalation về parent)", not can_spawn,
              "agent có tool Agent => nhánh delegation xác suất thừa")

        r.add(a, "có No-fabrication rule", "No-fabrication" in body)
        r.add(a, "có Output contract", "Output contract" in body)

    # ---------------------------------------------------------- per-agent sâu
    def body_of(n):
        return agents.get(n, {}).get("body", "")

    def fm_of(n):
        return agents.get(n, {}).get("fm", {})

    r.add("roster", "đủ 5 agent", set(agents) == {"Explore", "architect", "builder", "critic", "verifier"},
          f"có: {sorted(agents)}")

    # Explore: agent tần suất cao nhất phải ở tier rẻ nhất (cost guard).
    r.add("Explore", "chạy tier rẻ nhất (haiku)", fm_of("Explore").get("model") == "haiku",
          f"model={fm_of('Explore').get('model')}")
    r.add("Explore", "override built-in Explore (name viết hoa)", "Explore" in agents)

    b = body_of("builder")
    r.add("builder", "có Goal contract", "Goal contract" in b)
    r.add("builder", "giữ escape hatch", "Escape hatch" in b)
    r.add("builder", "cấm báo pass khi chưa verify", "CHƯA VERIFY" in b)
    r.add("builder", "cổng escalation tất định", "TIÊU CHÍ TẤT ĐỊNH" in b)
    r.add("builder", "preload skill verify-loop", "verify-loop" in (fm_of("builder").get("skills") or []))
    # Câu nào cho phép ghi pass mà không có "KHÔNG" trong cùng dòng = mâu thuẫn policy.
    contradictions = [ln.strip() for ln in b.split("\n")
                      if re.search(r"ghi\s+(là\s+)?pass", ln, re.I) and "KHÔNG" not in ln]
    r.add("builder", "không có câu cho phép báo pass giả", not contradictions,
          f"dòng mâu thuẫn: {contradictions[:1]}")
    # Ngưỡng escalation phải còn kích hoạt được.
    r.add("builder", "có ask-loop có cấu trúc", "ĐÃ THỬ" in b and "CẦN BIẾT" in b)
    ask = re.search(r"TỐI ĐA (\d+) lượt hỏi", b)
    r.add("builder", "ask-loop có cap (chống vòng lặp)", bool(ask) and int(ask.group(1)) <= 3,
          f"cap={ask.group(1) if ask else 'không có'}")

    th = re.search(r"≥\s*(\d+)\s*file", b)
    r.add("builder", "ngưỡng escalation hợp lý (≤5 file)", bool(th) and int(th.group(1)) <= 5,
          f"ngưỡng={th.group(1) if th else 'không tìm thấy'}")

    # verifier: grounding gate. Phải KHÁC critic về câu hỏi nó trả lời.
    vf = body_of("verifier")
    r.add("verifier", "dùng đúng 3 nhãn phán quyết",
          all(k in vf for k in ["GROUNDED", "UNVERIFIABLE", "FABRICATED"]))
    r.add("verifier", "không nhầm 'không tìm thấy' với 'bịa'",
          "KHÔNG có nghĩa là không tồn tại" in vf)
    r.add("verifier", "có VERDICT chặn được", "BLOCK" in vf and "SAFE_TO_BUILD" in vf)
    vcap = re.search(r"Tối đa (\d+) claim", vf)
    r.add("verifier", "có ngưỡng dừng (chống chạy vô hạn)",
          bool(vcap) and 5 <= int(vcap.group(1)) <= 50,
          f"cap={vcap.group(1) if vcap else 'không có'}")
    r.add("verifier", "tier rẻ hơn critic (đối chiếu là việc cơ học)",
          fm_of("verifier").get("model") == "sonnet",
          f"model={fm_of('verifier').get('model')}")
    r.add("verifier", "phân vai khác critic tường minh", "việc của `critic`" in vf)

    for nm in ("architect", "builder"):
        r.add(nm, "có stop-rule: user dẫn ra thứ không tồn tại → DỪNG hỏi",
              "KHÔNG tự chế một cái thay thế" in body_of(nm))

    c = body_of("critic")
    r.add("critic", "cấm PASS + ISSUES rỗng", "Cấm PASS + ISSUES rỗng" in c)
    r.add("critic", "có WEAKEST_POINT bắt buộc", "WEAKEST_POINT" in c)
    r.add("critic", "có maxTurns (ràng buộc runtime, không chỉ prompt)",
          str(fm_of("critic").get("maxTurns", "")).isdigit(),
          f"maxTurns={fm_of('critic').get('maxTurns')}")
    r.add("critic", "maxTurns = 1", str(fm_of("critic").get("maxTurns")) == "1")

    # ------------------------------------------------------- nhất quán chéo
    pol = policy_file(root).read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    # ATTEMPT CAP phải giống nhau ở mọi file nhắc tới nó.
    caps = set()
    for f in [policy_file(root), root / "skills/verify-loop/SKILL.md",
              *agents_dir.glob("*.md")]:
        caps |= set(re.findall(r"ATTEMPT CAP\s*=\s*(\d+)", f.read_text(encoding="utf-8")))
    r.add("consistency", "ATTEMPT CAP nhất quán mọi file", len(caps) == 1, f"giá trị: {sorted(caps)}")
    # ATTEMPT CAP là STOP CONDITION, không phải ngân sách. Nới = nhiều vòng sai hơn.
    r.add("consistency", "ATTEMPT CAP giữ = 3 (núm đúng đắn, cấm nới)", caps == {"3"},
          f"giá trị: {sorted(caps)}")
    r.add("policy", "có mục Núm CẤM nới", "Núm CẤM nới" in pol)

    # Model tier nhắc trong tài liệu phải khớp frontmatter thật.
    mismatch = []
    for doc_name, doc in (("policy", pol), ("readme", readme)):
        for nm, alias in re.findall(r"`([\w-]+)`[^\n`]{0,30}?\((haiku|sonnet|opus|fable)\)", doc):
            real = fm_of(nm).get("model")
            if nm in agents and real != alias:
                mismatch.append(f"{doc_name}: {nm} ghi {alias}, thật {real}")
    r.add("consistency", "model tier trong tài liệu khớp frontmatter", not mismatch, "; ".join(mismatch))

    # Agent được nhắc trong tài liệu phải tồn tại.
    referenced = set(re.findall(r"→\s*`([\w-]+)`", pol))
    ghosts = sorted(referenced - set(agents))
    r.add("consistency", "không tham chiếu agent không tồn tại", not ghosts, f"thiếu: {ghosts}")

    # ------------------------------------------------------------- policy
    for k in ["No-fabrication", "Goal & stop", "ATTEMPT CAP", "cổng chất lượng",
              "Context injection contract", "Escalation"]:
        r.add("policy", f"có mục: {k}", k in pol)
    r.add("policy", "KHÔNG chứa lệnh build/test per-project",
          not re.search(r"^\s*(build|typecheck|lint|test):", pol, re.M))
    r.add("policy", "trỏ tới VERIFICATION.template.md", "VERIFICATION.template.md" in pol)
    bad_blocks = [blk.strip().split("\n")[0][:45] for blk in re.split(r"\n\s*\n", pol)
                  if re.findall(r"\b\d+%", blk) and "https://" not in blk]
    r.add("policy", "mọi số liệu có nguồn TRONG CÙNG KHỐI", not bad_blocks, f"{bad_blocks}")
    # Global block nạp vào main + MỌI subagent, mỗi lượt -> giới hạn ngân sách token.
    r.add("policy", "global block ≤ 75 dòng (chi phí token)", len(pol.split("\n")) <= 75,
          f"{len(pol.split(chr(10)))} dòng")

    # ------------------------------------------------------ template / skill
    v = (root / "VERIFICATION.template.md").read_text(encoding="utf-8")
    r.add("template", "cảnh báo không dán vào global", "KHÔNG PHẢI GLOBAL" in v)
    r.add("template", "có 4 lệnh build/typecheck/lint/test",
          all(f"{k}:" in v for k in ["build", "typecheck", "lint", "test"]))

    sk = (root / "skills/verify-loop/SKILL.md").read_text(encoding="utf-8")
    r.add("skill", "có allowed-tools", "allowed-tools" in sk)
    r.add("skill", "có ATTEMPT CAP", "ATTEMPT CAP" in sk)
    r.add("skill", "không tự đoán lệnh", "Không tự suy đoán lệnh" in sk)

    # --- gloss-gate: chống bịa nghĩa từ viết tắt
    gl = root / "hooks/gloss-gate.py"
    r.add("hook-gloss", "tồn tại", gl.exists())
    if gl.exists():
        h = gl.read_text(encoding="utf-8")
        r.add("hook-gloss", "fail-open", "FAIL-OPEN" in h)
        r.add("hook-gloss", "trả exit 2 để chặn", "return 2" in h)
        r.add("hook-gloss", "đối chiếu chữ cái đầu (tín hiệu tất định)", "def initials" in h)
        r.add("hook-gloss", "bỏ dấu tiếng Việt trước khi so", "strip_accents" in h)
        r.add("hook-gloss", "có glossary tổ chức", "load_glossary" in h)
        r.add("hook-gloss", "mâu thuẫn glossary = tín hiệu mạnh nhất", "MÂU THUẪN glossary" in h)
        r.add("hook-gloss", "bỏ qua viết tắt kỹ thuật phổ quát (chống nhiễu)", "KNOWN = {" in h)
        r.add("hook-gloss", "có chế độ warn + off", '"warn"' in h and '"off"' in h)

    # Rule token-chưa-rõ phải có ở MỌI agent sinh văn bản, không chỉ policy
    for nm in ("Explore", "architect", "builder", "verifier"):
        r.add(nm, "có rule Token chưa rõ (cấm lấp nghĩa viết tắt)",
              "Token chưa rõ" in body_of(nm) and "chữ cái đầu" in body_of(nm))
    r.add("policy", "no-fabrication là NGUYÊN TẮC ĐÓNG, không phải danh sách liệt kê",
          "NGUYÊN TẮC ĐÓNG" in pol)
    r.add("policy", "phủ cả thuật ngữ nghiệp vụ / viết tắt",
          "thuật ngữ nghiệp vụ" in pol and "viết tắt" in pol)
    r.add("verifier", "coi gloss là CLAIM, không phải diễn giải",
          "Mở rộng viết tắt LÀ claim" in body_of("verifier"))
    r.add("builder", "với tới được MCP knowledge base (tra glossary nội bộ)",
          "disallowedTools" in fm_of("builder"),
          "builder dùng tools allowlist => mất hết MCP => không tra được KB nội bộ")

    hook = root / "hooks/no-fake-pass.py"
    r.add("hook", "tồn tại", hook.exists())
    if hook.exists():
        h = hook.read_text(encoding="utf-8")
        r.add("hook", "fail-open khi thiếu dữ liệu", "FAIL-OPEN" in h)
        r.add("hook", "trả exit 2 để chặn", "return 2" in h)

    # ---------------------------------------------- workflow entry
    # Vấn đề: kit chỉ ràng buộc subagent, không ràng buộc lượt đầu của main session.
    r.add("policy", "có Bước 0 (entry point, không phải bảng tra cứu)", "Bước 0" in pol)
    r.add("policy", "phân biệt verifier vs critic tường minh",
          "CÓ TỒN TẠI" in pol and "CÓ CHẶT" in pol)
    r.add("policy", "có Ask-loop", "Ask-loop" in pol)
    r.add("policy", "điều kiện KHÔNG delegate là tất định",
          "TẤT ĐỊNH" in pol and "Cấm tự chấm" in pol)
    r.add("policy", "không còn escape hatch chủ quan",
          not re.search(r"KHÔNG delegate khi:.*đủ ngữ cảnh", pol, re.S))

    route = root / "hooks/route-prompt.py"
    r.add("hook-route", "tồn tại", route.exists())
    if route.exists():
        h = route.read_text(encoding="utf-8")
        r.add("hook-route", "fail-open", "FAIL-OPEN" in h)
        r.add("hook-route", "phủ đủ 5 lớp task",
              all(k in h for k in ["EXPLORE", "BUILD", "DESIGN", "REVIEW", "AMBIGUOUS"]))
        r.add("hook-route", "bỏ qua lượt hội thoại ngắn (chi phí token)", "MIN_CHARS" in h)
        # Lớp task tiêm ra phải trỏ tới agent CÓ THẬT.
        named = set(re.findall(r"agent `([\w-]+)`", h, re.I))
        r.add("hook-route", "chỉ trỏ agent có thật", named <= set(agents),
              f"lạ: {sorted(named - set(agents))}")

    # Docs: transcript ghi bất đồng bộ, có thể chưa có lượt hiện tại.
    # Hook cần text lượt cuối PHẢI dùng last_assistant_message.
    for hn, fname in (("hook", "no-fake-pass.py"), ("hook-gloss", "gloss-gate.py")):
        f = root / "hooks" / fname
        if f.exists():
            r.add(hn, "dùng last_assistant_message (không đọc transcript trễ)",
                  "last_assistant_message" in f.read_text(encoding="utf-8"))
    rp = (root / "hooks/route-prompt.py").read_text(encoding="utf-8") if route.exists() else ""
    r.add("hook-route", "tiêm qua additionalContext (kênh có cấu trúc)",
          "additionalContext" in rp)
    r.add("hook-route", "phrasing dạng phát biểu, không phải mệnh lệnh out-of-band",
          "PHRASING" in rp and "BẮT BUỘC" not in rp,
          "mệnh lệnh out-of-band có thể kích hoạt phòng thủ prompt-injection")

    gate = root / "hooks/plan-gate.py"
    r.add("hook-gate", "tồn tại", gate.exists())
    if gate.exists():
        h = gate.read_text(encoding="utf-8")
        r.add("hook-gate", "fail-open", "FAIL-OPEN" in h)
        r.add("hook-gate", "trả exit 2 để chặn", "return 2" in h)
        free = re.search(r'PLAN_GATE_FREE_EDITS"\s*,\s*"(\d+)"\s*\)', h)
        r.add("hook-gate", "ngưỡng edit miễn phí đọc được từ env, trong 0..3",
              bool(free) and 0 <= int(free.group(1)) <= 3,
              f"ngưỡng={free.group(1) if free else 'không đọc được từ env'}")
        r.add("hook-gate", "có thoát khẩn", 'PLAN_GATE", ""' in h or "PLAN_GATE" in h)
        r.add("hook-gate", "coi TodoWrite/ExitPlanMode là plan",
              "TodoWrite" in h and "ExitPlanMode" in h)

    orch = root / "optional/orchestrator.md"
    r.add("optional", "orchestrator nằm ngoài agents/ (không auto-load)",
          orch.exists() and not (agents_dir / "orchestrator.md").exists())
    if orch.exists():
        o = orch.read_text(encoding="utf-8")
        r.add("optional", "orchestrator cảnh báo thay thế system prompt",
              "THAY THẾ" in o and "KHÔNG BẬT MẶC ĐỊNH" in o)
    return r


# ------------------------------------------------------------------ selftest
# Mỗi mutation là một defect THẬT (đổi giá trị/xoá ràng buộc), không phải xoá từ khoá.
MUTATIONS = [
    ("critic tools rỗng → fail-to-launch", "agents/critic.md", "tools: Read", "tools: []"),
    ("Explore leo tier đắt", "agents/explore.md", "model: haiku", "model: opus"),
    ("ngưỡng escalation vô hiệu", "agents/builder.md", "≥ 3 file", "≥ 30 file"),
    ("xoá escape hatch builder", "agents/builder.md",
     "- Escape hatch: fact MÂU THUẪN RÕ với file thực tế → DỪNG, báo kèm path:line.\n", ""),
    ("xoá chặn PASS rỗng của critic", "agents/critic.md", "- PASS vẫn phải nêu điểm yếu nhất. Cấm PASS + ISSUES rỗng.\n", ""),
    ("ATTEMPT CAP lệch giữa các file", "agents/builder.md", "ATTEMPT CAP = 3", "ATTEMPT CAP = 25"),
    ("câu cho phép báo pass giả", "agents/builder.md",
     "## Output contract", "Không chạy được test thì cứ ghi là pass.\n\n## Output contract"),
    ("tài liệu ghi sai model tier", "policy/delegation.md", "`architect` (opus)", "`architect` (fable)"),
    ("critic mất maxTurns", "agents/critic.md", "maxTurns: 1\n", ""),
    ("builder được bảo gọi subagent dù thiếu tool Agent", "agents/builder.md",
     "Parent quyết định gọi `architect`/`critic`.", "Tự gọi `critic` để phản biện."),
    ("policy mất Bước 0 (quay lại bảng tra cứu)", "policy/delegation.md",
     "## Bước 0 — mỗi user message, trước khi hành động", "## Gợi ý"),
    ("điều kiện KHÔNG delegate quay lại chủ quan", "policy/delegation.md",
     "Cấm tự chấm", "Có thể tự chấm"),
    ("route-hook trỏ agent không tồn tại", "hooks/route-prompt.py",
     "agent `Explore` (haiku)", "agent `advisor` (fable)"),
    ("plan-gate hardcode ngưỡng, mất núm profile", "hooks/plan-gate.py",
     'FREE = int(os.environ.get("PLAN_GATE_FREE_EDITS", "0"))', "FREE = 9"),
    ("orchestrator bị đưa vào agents/ → auto-load ngoài ý muốn", "optional/orchestrator.md",
     "⚠ TUỲ CHỌN, KHÔNG BẬT MẶC ĐỊNH.", "⚠ TUỲ CHỌN."),
    ("verifier leo tier đắt bằng critic", "agents/verifier.md", "model: sonnet", "model: opus"),
    ("verifier coi 'không tìm thấy' là bịa", "agents/verifier.md",
     "Không tìm thấy KHÔNG có nghĩa là không tồn tại", "Không tìm thấy nghĩa là bịa"),
    ("verifier mất ngưỡng dừng", "agents/verifier.md", "Tối đa 30 claim", "Không giới hạn claim"),
    ("ask-loop mất cap → vòng lặp hỏi vô hạn", "agents/builder.md",
     "TỐI ĐA 3 lượt hỏi", "Hỏi bao nhiêu lượt cũng được"),
    ("policy gộp verifier với critic làm một", "policy/delegation.md",
     '`critic` hỏi "lập luận CÓ CHẶT không"', "`critic` làm luôn việc đối chiếu"),
    ("nới ATTEMPT CAP như thể nó là ngân sách", "policy/delegation.md",
     "ATTEMPT CAP = 3", "ATTEMPT CAP = 10"),
    ("xoá stop-rule không-tồn-tại của builder", "agents/builder.md",
     "TUYỆT ĐỐI KHÔNG tự chế một cái thay thế rồi làm tiếp như thể nó đã có.", ""),
    ("verifier cap phi lý (chạy vô tận)", "agents/verifier.md",
     "Tối đa 30 claim mỗi lần chạy", "Tối đa 500 claim mỗi lần chạy"),
    ("no-fabrication quay lại danh sách liệt kê (tập đóng)", "policy/delegation.md",
     "NGUYÊN TẮC ĐÓNG", "Cấm bịa các thứ sau"),
    ("xoá rule token-chưa-rõ của builder", "agents/builder.md",
     "## Token chưa rõ — CẤM lấp nghĩa", "## Ghi chú"),
    ("verifier bỏ qua gloss như diễn giải", "agents/verifier.md",
     "Mở rộng viết tắt LÀ claim", "Mở rộng viết tắt là diễn giải"),
    ("gloss-gate mất đối chiếu chữ cái đầu", "hooks/gloss-gate.py",
     "def initials", "def _unused_initials"),
    ("route-hook quay lại mệnh lệnh out-of-band", "hooks/route-prompt.py",
     "Quy ước xử lý task trong workspace này", "Bước 0 BẮT BUỘC: làm ngay"),
    ("gloss-gate quay lại đọc transcript trễ", "hooks/gloss-gate.py",
     "last_assistant_message", "stale_transcript_only"),
    ("builder quay lại allowlist => mất MCP, không tra được glossary nội bộ",
     "agents/builder.md", "disallowedTools: NotebookEdit, Agent, WebFetch, WebSearch",
     "tools: Read, Write, Edit, Bash, Glob, Grep"),
]


def selftest(root: pathlib.Path) -> int:
    base = run_checks(root)
    print(f"Baseline: {len(base.rows)} checks | PASS {len(base.rows) - len(base.fails)} | FAIL {len(base.fails)}")
    if base.fails:
        print("Baseline chưa sạch — sửa trước khi selftest.")
        return 1

    caught = 0
    print(f"\nNegative control — {len(MUTATIONS)} defect, tiêm từng cái một:\n")
    for label, rel, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td) / "kit"
            shutil.copytree(root, tmp)
            f = tmp / rel
            txt = f.read_text(encoding="utf-8")
            if old not in txt:
                print(f"  ✗ {label:<48} MUTATION KHÔNG ÁP ĐƯỢC (test hỏng)")
                continue
            f.write_text(txt.replace(old, new, 1), encoding="utf-8")
            rep = run_checks(tmp)
            hit = [n for _, n, ok, _ in rep.fails for n in [n]]
            if rep.fails:
                caught += 1
                print(f"  ✓ {label:<48} trip {len(rep.fails)} check → {hit[0]}")
            else:
                print(f"  ✗ {label:<48} BỎ LỌT")
    print(f"\nBắt {caught}/{len(MUTATIONS)} defect.")
    return 0 if caught == len(MUTATIONS) else 1


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent
    # Layout plugin: script ở docs/, kit ở repo root -> lùi một cấp.
    if not (root / "agents").is_dir() and (root.parent / "agents").is_dir():
        root = root.parent
    if "--selftest" in sys.argv:
        return selftest(root)
    rep = run_checks(root)
    w = max((len(x[0]) for x in rep.rows), default=8)
    for area, name, ok, detail in rep.rows:
        if not ok:
            print(f"FAIL  {area:<{w}}  {name}" + (f"   [{detail}]" if detail else ""))
    print(f"Tổng: {len(rep.rows)} checks | PASS {len(rep.rows) - len(rep.fails)} | FAIL {len(rep.fails)}")
    return 1 if rep.fails else 0


if __name__ == "__main__":
    sys.exit(main())
