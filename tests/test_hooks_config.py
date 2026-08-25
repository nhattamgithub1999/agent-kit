"""Contract tests for Claude Code hook wiring."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import unittest

from hooks._shared import MAXIMUM_BUSY_TIMEOUT_MS


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_CONFIG = REPO_ROOT / "hooks" / "hooks.json"
PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}"

PRE_MUTATION_MATCHER = (
    "Edit|Write|NotebookEdit|Bash|PowerShell|Monitor|EnterWorktree|"
    "ExitWorktree|ExitPlanMode|mcp__.*"
)
POST_MUTATION_MATCHER = (
    "Edit|Write|NotebookEdit|Bash|PowerShell|Monitor|EnterWorktree|"
    "ExitWorktree|mcp__.*"
)
FAILURE_MATCHER = POST_MUTATION_MATCHER
BUILDER_MATCHER = "^(?:agent-kit:)?builder$"
MUTATION_SOURCES = (
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash",
    "PowerShell",
    "Monitor",
    "EnterWorktree",
    "ExitWorktree",
    "mcp__server__write",
)
STATEFUL_SCRIPTS = frozenset({"plan-gate.py", "no-fake-pass.py"})
BUSY_TIMEOUT_SECONDS = (MAXIMUM_BUSY_TIMEOUT_MS + 999) // 1000
REQUIRED_STATEFUL_OUTER_TIMEOUT = BUSY_TIMEOUT_SECONDS + 2

# (matcher, ((script, timeout), ...)) per hook group.  Keeping this exhaustive
# makes any accidental event, matcher, omission, reordering, or duplicate fail.
EXPECTED = {
    "SessionStart": [
        (None, (("session-policy.py", 5),)),
    ],
    "SubagentStart": [
        (None, (("session-policy.py", 5),)),
    ],
    "UserPromptSubmit": [
        (None, (("route-prompt.py", 5), ("plan-gate.py", 5))),
    ],
    "PreToolUse": [
        (PRE_MUTATION_MATCHER, (("plan-gate.py", 5),)),
    ],
    "PostToolUse": [
        ("ExitPlanMode", (("plan-gate.py", 5),)),
        (POST_MUTATION_MATCHER, (("no-fake-pass.py", 10),)),
    ],
    "PostToolUseFailure": [
        (FAILURE_MATCHER, (("no-fake-pass.py", 10),)),
    ],
    "SubagentStop": [
        (BUILDER_MATCHER, (("no-fake-pass.py", 10),)),
        (None, (("gloss-gate.py", 10),)),
    ],
    "Stop": [
        (None, (("no-fake-pass.py", 10), ("gloss-gate.py", 10))),
    ],
    "SessionEnd": [
        (None, (("plan-gate.py", 5),)),
    ],
}


class HooksConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        cls.events = cls.config["hooks"]

    @staticmethod
    def script_name(hook: dict) -> str:
        args = hook.get("args")
        if not isinstance(args, list) or len(args) != 1:
            raise AssertionError(f"Expected exactly one exec arg: {hook!r}")
        return Path(args[0]).name

    def actual_contract(self):
        contract = {}
        for event, groups in self.events.items():
            contract[event] = []
            for group in groups:
                hooks = tuple(
                    (self.script_name(hook), hook.get("timeout"))
                    for hook in group["hooks"]
                )
                contract[event].append((group.get("matcher"), hooks))
        return contract

    def test_event_matcher_script_and_timeout_contract_is_exact(self) -> None:
        self.assertEqual(self.actual_contract(), EXPECTED)

    def test_every_hook_uses_exec_form_and_existing_plugin_path(self) -> None:
        for event, groups in self.events.items():
            for group in groups:
                expected_group_keys = {"hooks"}
                if "matcher" in group:
                    expected_group_keys.add("matcher")
                self.assertEqual(set(group), expected_group_keys)
                self.assertIsInstance(group["hooks"], list)
                self.assertTrue(group["hooks"])
                for hook in group["hooks"]:
                    with self.subTest(event=event, hook=hook):
                        self.assertEqual(hook.get("type"), "command")
                        self.assertEqual(hook.get("command"), "python3")
                        self.assertEqual(
                            set(hook),
                            {"type", "command", "args", "timeout"},
                        )
                        self.assertIsInstance(hook["timeout"], int)
                        self.assertGreaterEqual(hook["timeout"], 5)
                        self.assertLessEqual(hook["timeout"], 10)

                        arg = hook["args"][0]
                        prefix = f"{PLUGIN_ROOT_TOKEN}/hooks/"
                        self.assertTrue(arg.startswith(prefix), arg)
                        relative_path = arg.removeprefix(f"{PLUGIN_ROOT_TOKEN}/")
                        self.assertTrue((REPO_ROOT / relative_path).is_file(), arg)

    def test_stateful_hooks_have_busy_timeout_margin(self) -> None:
        self.assertEqual(MAXIMUM_BUSY_TIMEOUT_MS, 2500)
        for event, groups in self.events.items():
            for group in groups:
                for hook in group["hooks"]:
                    script = self.script_name(hook)
                    if script not in STATEFUL_SCRIPTS:
                        continue
                    with self.subTest(event=event, script=script):
                        self.assertGreaterEqual(
                            hook["timeout"], REQUIRED_STATEFUL_OUTER_TIMEOUT
                        )

    def test_builder_matcher_covers_bare_and_plugin_scoped_name_only(self) -> None:
        pattern = re.compile(BUILDER_MATCHER)
        for accepted in ("builder", "agent-kit:builder"):
            with self.subTest(accepted=accepted):
                self.assertIsNotNone(pattern.fullmatch(accepted))
        for rejected in (
            "other:builder",
            "agent-kit:builder-extra",
            "prefix-agent-kit:builder",
            "reviewer",
        ):
            with self.subTest(rejected=rejected):
                self.assertIsNone(pattern.fullmatch(rejected))

    def test_mutation_matchers_cover_shell_and_mcp_without_plan_shortcuts(self) -> None:
        pre_pattern = re.compile(PRE_MUTATION_MATCHER)
        post_pattern = re.compile(POST_MUTATION_MATCHER)
        failure_pattern = re.compile(FAILURE_MATCHER)

        for tool in MUTATION_SOURCES:
            with self.subTest(tool=tool):
                self.assertIsNotNone(pre_pattern.fullmatch(tool))
                self.assertIsNotNone(post_pattern.fullmatch(tool))
                self.assertIsNotNone(failure_pattern.fullmatch(tool))

        self.assertIsNotNone(pre_pattern.fullmatch("ExitPlanMode"))
        self.assertIsNone(post_pattern.fullmatch("ExitPlanMode"))
        for forbidden in ("EnterPlanMode", "TodoWrite"):
            with self.subTest(forbidden=forbidden):
                self.assertIsNone(pre_pattern.fullmatch(forbidden))
                self.assertIsNone(post_pattern.fullmatch(forbidden))

        approval_groups = [
            group
            for group in self.events["PostToolUse"]
            if any(
                self.script_name(hook) == "plan-gate.py"
                for hook in group["hooks"]
            )
        ]
        self.assertEqual(
            [group.get("matcher") for group in approval_groups],
            ["ExitPlanMode"],
        )

    def test_every_mutation_success_and_failure_reaches_no_fake_once(self) -> None:
        for event in ("PostToolUse", "PostToolUseFailure"):
            no_fake_groups = [
                group
                for group in self.events[event]
                if any(
                    self.script_name(hook) == "no-fake-pass.py"
                    for hook in group["hooks"]
                )
            ]
            self.assertEqual(len(no_fake_groups), 1, event)
            group = no_fake_groups[0]
            self.assertEqual(set(group), {"matcher", "hooks"})
            self.assertNotIn("builder", group["matcher"].casefold())
            pattern = re.compile(group["matcher"])
            for tool in MUTATION_SOURCES:
                with self.subTest(event=event, tool=tool):
                    self.assertIsNotNone(pattern.fullmatch(tool))
            self.assertIsNone(pattern.fullmatch("ExitPlanMode"))

    def test_no_duplicate_hook_registration_within_an_event(self) -> None:
        usage = Counter()
        for event, groups in self.events.items():
            registrations = []
            for group in groups:
                matcher = group.get("matcher")
                for hook in group["hooks"]:
                    script = self.script_name(hook)
                    registrations.append((matcher, script))
                    usage[script] += 1
            with self.subTest(event=event):
                self.assertEqual(len(registrations), len(set(registrations)))

        self.assertEqual(
            usage,
            Counter(
                {
                    "session-policy.py": 2,
                    "route-prompt.py": 1,
                    "plan-gate.py": 4,
                    "no-fake-pass.py": 4,
                    "gloss-gate.py": 2,
                }
            ),
        )

    def test_matchers_are_compilable_and_no_script_has_overlapping_groups(self) -> None:
        witnesses = MUTATION_SOURCES + (
            "ExitPlanMode",
            "EnterPlanMode",
            "Read",
            "builder",
            "agent-kit:builder",
            "reviewer",
        )
        for event, groups in self.events.items():
            by_script = {}
            for group in groups:
                matcher = group.get("matcher")
                pattern = re.compile(matcher) if matcher is not None else None
                if pattern is not None:
                    with self.subTest(event=event, matcher=matcher):
                        self.assertTrue(
                            any(pattern.fullmatch(witness) for witness in witnesses),
                            "matcher has no reachable witness",
                        )
                for hook in group["hooks"]:
                    by_script.setdefault(self.script_name(hook), []).append(pattern)

            for script, patterns in by_script.items():
                for witness in witnesses:
                    reachable = sum(
                        pattern is None or pattern.fullmatch(witness) is not None
                        for pattern in patterns
                    )
                    with self.subTest(
                        event=event, script=script, witness=witness
                    ):
                        self.assertLessEqual(reachable, 1)


if __name__ == "__main__":
    unittest.main()
