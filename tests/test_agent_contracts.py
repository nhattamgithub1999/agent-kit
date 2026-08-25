"""Regression tests for agent roles, tool boundaries, and verify-loop."""

from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "agents"
OPTIONAL_DIR = REPO_ROOT / "optional"
SKILLS_DIR = REPO_ROOT / "skills"
MUTATING_OR_EXTERNAL_TOOLS = {
    "Write",
    "Edit",
    "NotebookEdit",
    "Bash",
    "PowerShell",
    "Agent",
    "WebFetch",
    "WebSearch",
}


def read_markdown_contract(path: Path):
    """Return top-level scalar frontmatter and body without a YAML dependency."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise AssertionError(f"Invalid frontmatter delimiters: {path}")

    frontmatter = {}
    for raw_line in parts[1].splitlines():
        if not raw_line or raw_line[0].isspace() or ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        frontmatter[key.strip()] = value.strip()
    return path, frontmatter, parts[2]


def read_agent(name: str):
    return read_markdown_contract(AGENTS_DIR / f"{name}.md")


def tool_set(raw_value: str):
    return {item.strip() for item in raw_value.split(",") if item.strip()}


class AgentFrontmatterContractTests(unittest.TestCase):
    EXPECTATIONS = {
        "Explore": {
            "model": "haiku",
            "tools": {"Read", "Grep", "Glob"},
            "role_fragment": "agent khảo sát codebase",
        },
        "verifier": {
            "model": "sonnet",
            "tools": {"Read", "Grep", "Glob"},
            "role_fragment": "fact-checker",
        },
        "architect": {
            "model": "opus",
            "tools": {"Read", "Grep", "Glob", "WebSearch", "WebFetch"},
            "role_fragment": "agent kiến trúc & lập kế hoạch",
        },
        "reviewer": {
            "model": "sonnet",
            "tools": {"Read", "Grep", "Glob"},
            "role_fragment": "code reviewer read-only",
        },
        "builder": {
            "model": "sonnet",
            "tools": {
                "Read",
                "Grep",
                "Glob",
                "Write",
                "Edit",
                "Bash",
                "PowerShell",
            },
            "role_fragment": "worker thực thi thay đổi",
        },
        "critic": {
            "model": "opus",
            "tools": {"Read"},
            "role_fragment": "logic critic độc lập",
        },
    }

    def test_agent_names_models_tools_and_roles_are_exact(self):
        for filename, expected in self.EXPECTATIONS.items():
            with self.subTest(agent=filename):
                _, frontmatter, body = read_agent(filename)
                self.assertEqual(frontmatter.get("name"), filename)
                self.assertEqual(frontmatter.get("model"), expected["model"])
                self.assertEqual(
                    tool_set(frontmatter.get("tools", "")), expected["tools"]
                )
                self.assertIn(expected["role_fragment"], body)

    def test_read_only_agents_have_no_mutation_shell_or_agent_tools(self):
        read_only_agents = ("Explore", "verifier", "reviewer")
        for name in read_only_agents:
            with self.subTest(agent=name):
                _, frontmatter, _ = read_agent(name)
                allowed = tool_set(frontmatter.get("tools", ""))
                disallowed = tool_set(frontmatter.get("disallowedTools", ""))
                self.assertTrue(allowed.isdisjoint(MUTATING_OR_EXTERNAL_TOOLS))
                self.assertTrue(MUTATING_OR_EXTERNAL_TOOLS <= disallowed)

    def test_architect_is_read_only_but_can_use_web(self):
        _, frontmatter, _ = read_agent("architect")
        allowed = tool_set(frontmatter.get("tools", ""))
        disallowed = tool_set(frontmatter.get("disallowedTools", ""))

        self.assertEqual(
            allowed, {"Read", "Grep", "Glob", "WebSearch", "WebFetch"}
        )
        self.assertTrue(
            {"Write", "Edit", "NotebookEdit", "Bash", "PowerShell", "Agent"}
            <= disallowed
        )

    def test_builder_has_explicit_code_tools_without_agent_web_or_mcp(self):
        _, frontmatter, body = read_agent("builder")
        allowed = tool_set(frontmatter.get("tools", ""))
        disallowed = tool_set(frontmatter.get("disallowedTools", ""))

        self.assertEqual(allowed, self.EXPECTATIONS["builder"]["tools"])
        self.assertTrue(
            {"NotebookEdit", "Agent", "WebFetch", "WebSearch"} <= disallowed
        )
        self.assertNotIn("Agent", allowed)
        self.assertFalse(any(tool.casefold().startswith("mcp") for tool in allowed))
        self.assertIn("mọi MCP", body)

    def test_critic_is_single_turn_without_mutation_shell_web_or_agent(self):
        _, frontmatter, body = read_agent("critic")
        allowed = tool_set(frontmatter.get("tools", ""))
        disallowed = tool_set(frontmatter.get("disallowedTools", ""))

        self.assertEqual(frontmatter.get("maxTurns"), "1")
        self.assertEqual(allowed, {"Read"})
        self.assertTrue(allowed.isdisjoint(MUTATING_OR_EXTERNAL_TOOLS))
        self.assertTrue(MUTATING_OR_EXTERNAL_TOOLS <= disallowed)
        self.assertIn("TUYỆT ĐỐI KHÔNG gọi", body)

    def test_optional_orchestrator_has_only_delegation_tools(self):
        path = OPTIONAL_DIR / "orchestrator.md"
        _, frontmatter, body = read_markdown_contract(path)
        allowed = tool_set(frontmatter.get("tools", ""))
        disallowed = tool_set(frontmatter.get("disallowedTools", ""))

        self.assertEqual(frontmatter.get("name"), "orchestrator")
        self.assertEqual(frontmatter.get("model"), "opus")
        self.assertEqual(allowed, {"Read", "Grep", "Glob", "Agent"})
        self.assertTrue(
            {"Write", "Edit", "NotebookEdit", "Bash", "PowerShell", "WebSearch", "WebFetch"}
            <= disallowed
        )
        self.assertIn("Bạn là orchestrator", body)


class ReviewerOutputContractTests(unittest.TestCase):
    def test_reviewer_frontmatter_is_explicitly_read_only(self):
        path, frontmatter, _ = read_agent("reviewer")

        self.assertTrue(path.is_file())
        self.assertEqual(frontmatter.get("name"), "reviewer")
        self.assertEqual(frontmatter.get("model"), "sonnet")
        self.assertEqual(
            tool_set(frontmatter.get("tools", "")), {"Read", "Grep", "Glob"}
        )
        self.assertTrue(
            MUTATING_OR_EXTERNAL_TOOLS
            <= tool_set(frontmatter.get("disallowedTools", ""))
        )
        self.assertTrue(
            tool_set(frontmatter.get("tools", "")).isdisjoint(
                MUTATING_OR_EXTERNAL_TOOLS
            )
        )

    def test_reviewer_body_defines_evidence_and_output_contract(self):
        _, _, body = read_agent("reviewer")

        for required_fragment in (
            "path:line",
            "P0",
            "P1",
            "P2",
            "P3",
            "Tác động:",
            "Bằng chứng/tái hiện:",
            "Khuyến nghị:",
            "VERDICT: FINDINGS | NO_FINDINGS",
            "REMAINING_RISKS:",
            "TEST_GAPS:",
            "FINDINGS: []",
            "[CHƯA RÕ: <token>]",
        ):
            with self.subTest(required_fragment=required_fragment):
                self.assertIn(required_fragment, body)

        for priority in ("bug", "bảo mật", "regression", "missing tests"):
            with self.subTest(priority=priority):
                self.assertIn(priority, body)

        self.assertIn("KHÔNG sửa/tạo/xóa file", body)
        self.assertIn("spawn agent", body)


class VerifyLoopContractTests(unittest.TestCase):
    def test_verify_loop_uses_machine_contract_and_both_shell_tools(self):
        path = SKILLS_DIR / "verify-loop" / "SKILL.md"
        _, frontmatter, body = read_markdown_contract(path)

        self.assertEqual(frontmatter.get("name"), "verify-loop")
        self.assertEqual(
            tool_set(frontmatter.get("allowed-tools", "")),
            {"Read", "Edit", "Bash", "PowerShell", "Grep", "Glob"},
        )
        self.assertIn("<project>/.claude/verification.json", body)
        self.assertIn("ATTEMPT CAP = 3", body)
        self.assertIn("AGENT_KIT_RECEIPT_V1=", body)
        self.assertIn("AGENT_KIT_RESULT_V1=", body)
        self.assertIn("`Bash`", body)
        self.assertIn("`PowerShell`", body)


if __name__ == "__main__":
    unittest.main()
