"""Regression tests for the prompt-scoped plan approval gate."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
import unittest

from hooks._shared import StateStore
from tests.support import HookCall, HookHarness


HOOK = "plan-gate.py"
VALID_PLAN = """# Change

## Plan
1. Read the affected files
2. Implement the bounded change
3. Run the declared verification

## DoD
- The regression test passes with zero failures
"""


class PlanGateTests(unittest.TestCase):
    def approve(
        self,
        harness: HookHarness,
        *,
        session_id: str = "session-1",
        prompt_id: str = "prompt-1",
        plan: str = VALID_PLAN,
    ) -> None:
        pre = harness.run(
            HOOK,
            harness.payloads.pre_tool_use(
                "ExitPlanMode",
                {"plan": plan},
                session_id=session_id,
                prompt_id=prompt_id,
                permission_mode="plan",
            ),
        )
        self.assertEqual(pre.returncode, 0, pre.stderr)
        post = harness.run(
            HOOK,
            harness.payloads.post_tool_use(
                "ExitPlanMode",
                {"plan": plan},
                {"plan": plan},
                session_id=session_id,
                prompt_id=prompt_id,
                permission_mode="plan",
            ),
        )
        self.assertEqual(post.returncode, 0, post.stderr)

    def mutation(
        self,
        harness: HookHarness,
        tool: str = "Write",
        *,
        session_id: str = "session-1",
        prompt_id: str = "prompt-1",
        tool_input=None,
        hook_env=None,
        **common,
    ):
        return harness.run(
            HOOK,
            harness.payloads.pre_tool_use(
                tool,
                tool_input or {"file_path": str(harness.project_dir / "x.py")},
                session_id=session_id,
                prompt_id=prompt_id,
                **common,
            ),
            env=hook_env,
        )

    def test_approval_is_scoped_to_prompt_not_session(self) -> None:
        with HookHarness() as harness:
            self.approve(harness, prompt_id="prompt-A")
            allowed = self.mutation(harness, prompt_id="prompt-A")
            blocked = self.mutation(harness, prompt_id="prompt-B")

            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertTrue(blocked.blocked, blocked.stderr)

    def test_approval_is_scoped_to_session_too(self) -> None:
        with HookHarness() as harness:
            self.approve(harness, session_id="session-A", prompt_id="prompt-X")
            first = self.mutation(
                harness, session_id="session-A", prompt_id="prompt-X"
            )
            second = self.mutation(
                harness, session_id="session-B", prompt_id="prompt-X"
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(second.blocked, second.stderr)

    def test_user_prompt_submit_clears_only_that_prompt(self) -> None:
        with HookHarness() as harness:
            self.approve(harness, prompt_id="prompt-A")
            self.approve(harness, prompt_id="prompt-B")
            submit = harness.run(
                HOOK,
                harness.payloads.user_prompt_submit(prompt_id="prompt-A"),
            )

            self.assertEqual(submit.returncode, 0, submit.stderr)
            self.assertTrue(self.mutation(harness, prompt_id="prompt-A").blocked)
            self.assertEqual(
                self.mutation(harness, prompt_id="prompt-B").returncode, 0
            )

    def test_pre_exit_validates_but_does_not_approve(self) -> None:
        with HookHarness() as harness:
            pre = harness.run(
                HOOK,
                harness.payloads.pre_tool_use(
                    "ExitPlanMode",
                    {"plan": VALID_PLAN},
                    permission_mode="plan",
                ),
            )
            after = self.mutation(harness)

            self.assertEqual(pre.returncode, 0, pre.stderr)
            self.assertTrue(after.blocked, after.stderr)

    def test_passive_plan_tools_never_approve(self) -> None:
        with HookHarness() as harness:
            for tool in ("EnterPlanMode", "TodoWrite"):
                result = harness.run(
                    HOOK, harness.payloads.pre_tool_use(tool, {})
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.mutation(harness).blocked)

    def test_worktree_tools_are_mutations_not_approval(self) -> None:
        with HookHarness() as harness:
            for tool in ("EnterWorktree", "ExitWorktree"):
                result = self.mutation(harness, tool, tool_input={})
                self.assertTrue(result.blocked, (tool, result.stderr))

    def test_all_declared_mutation_families_are_blocked_then_allowed(self) -> None:
        tools = (
            "Edit",
            "Write",
            "NotebookEdit",
            "Bash",
            "PowerShell",
            "Monitor",
            "EnterWorktree",
            "ExitWorktree",
            "mcp__filesystem__write_file",
        )
        with HookHarness() as harness:
            for tool in tools:
                blocked = self.mutation(harness, tool)
                self.assertTrue(blocked.blocked, (tool, blocked.stderr))
            self.approve(harness)
            for tool in tools:
                allowed = self.mutation(harness, tool)
                self.assertEqual(allowed.returncode, 0, (tool, allowed.stderr))

    def test_post_exit_requires_valid_approved_response_plan(self) -> None:
        invalid_plans = (
            "",
            "## Plan\n1. One\n2. Two\n## DoD\n- passes",
            "## Plan\n1. One\n2. Two\n3. Three\n",
            "## Plan\n1. One\n3. Three\n4. Four\n## DoD\n- passes",
            "## Plan\n1. One\n2. Two\n3. Three\n## DoD\n- [ ]",
            "## plan\n1. One\n2. Two\n3. Three\n## dod\n- passes",
            (
                "## Plan\n1. One\n2. Two\n3. Three\n"
                "## Plan\n1. Again\n2. Again\n3. Again\n## DoD\n- passes"
            ),
        )
        with HookHarness() as harness:
            for index, plan in enumerate(invalid_plans):
                prompt_id = "invalid-{}".format(index)
                result = harness.run(
                    HOOK,
                    harness.payloads.post_tool_use(
                        "ExitPlanMode",
                        {"plan": plan},
                        {"plan": plan},
                        prompt_id=prompt_id,
                    ),
                )
                self.assertTrue(result.blocked, (plan, result.stderr))
                self.assertTrue(
                    self.mutation(harness, prompt_id=prompt_id).blocked
                )

    def test_valid_plan_accepts_three_and_seven_steps(self) -> None:
        seven = (
            "## Plan\n"
            + "\n".join("{}) Step {}".format(i, i) for i in range(1, 8))
            + "\n## DoD\n1. Verified"
        )
        with HookHarness() as harness:
            self.approve(harness, prompt_id="three")
            self.approve(harness, prompt_id="seven", plan=seven)
            self.assertEqual(self.mutation(harness, prompt_id="three").returncode, 0)
            self.assertEqual(self.mutation(harness, prompt_id="seven").returncode, 0)

    def test_missing_prompt_id_blocks_mutation_and_approval(self) -> None:
        with HookHarness() as harness:
            mutation_payload = harness.payloads.pre_tool_use("Write")
            mutation_payload.pop("prompt_id")
            approval_payload = harness.payloads.post_tool_use(
                "ExitPlanMode", {"plan": VALID_PLAN}, {"plan": VALID_PLAN}
            )
            approval_payload.pop("prompt_id")

            mutation = harness.run(HOOK, mutation_payload)
            approval = harness.run(HOOK, approval_payload)
            self.assertTrue(mutation.blocked, mutation.stderr)
            self.assertTrue(approval.blocked, approval.stderr)
            self.assertIn("2.1.196", mutation.stderr)
            self.assertIn("2.1.196", approval.stderr)

    def test_invalid_event_fails_closed_only_for_mutation_or_approval(self) -> None:
        with HookHarness() as harness:
            passive_payload = harness.payloads.pre_tool_use("Read")
            mutation_payload = harness.payloads.pre_tool_use("Bash")
            for payload in (passive_payload, mutation_payload):
                payload["hook_event_name"] = "UnknownEvent"

            passive = harness.run(HOOK, passive_payload)
            mutation = harness.run(HOOK, mutation_payload)
            self.assertEqual(passive.returncode, 0, passive.stderr)
            self.assertTrue(mutation.blocked, mutation.stderr)

    def test_state_unavailable_blocks_mutation(self) -> None:
        with HookHarness() as harness:
            unsafe = harness.plugin_data_dir / "agent-kit"
            unsafe.symlink_to(harness.project_dir, target_is_directory=True)
            result = self.mutation(harness)
            self.assertTrue(result.blocked, result.stderr)
            self.assertIn("trạng thái", result.stderr)

    def test_posix_plan_file_is_the_only_plan_mode_write_exemption(self) -> None:
        with HookHarness() as harness:
            plan_root = harness.home_dir / ".claude" / "plans"
            plan_root.mkdir(parents=True)
            existing_path = plan_root / "existing.md"
            existing_path.write_text("plan", encoding="utf-8")
            new_path = plan_root / "new.md"
            existing = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": str(existing_path)},
                permission_mode="plan",
            )
            new_leaf = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": str(new_path)},
                permission_mode="plan",
            )
            outside = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": str(harness.home_dir / "outside.md")},
                permission_mode="plan",
            )
            traversal = self.mutation(
                harness,
                "Write",
                tool_input={
                    "file_path": str(
                        harness.home_dir / ".claude" / "plans" / ".." / "outside.md"
                    )
                },
                permission_mode="plan",
            )
            edit_plan = self.mutation(
                harness,
                "Edit",
                tool_input={"file_path": str(existing_path)},
                permission_mode="plan",
            )

            self.assertEqual(existing.returncode, 0, existing.stderr)
            self.assertEqual(new_leaf.returncode, 0, new_leaf.stderr)
            self.assertTrue(outside.blocked, outside.stderr)
            self.assertTrue(traversal.blocked, traversal.stderr)
            self.assertTrue(edit_plan.blocked, edit_plan.stderr)

    def test_missing_plan_parent_and_symlink_escape_are_denied(self) -> None:
        with HookHarness() as harness:
            plan_root = harness.home_dir / ".claude" / "plans"
            plan_root.mkdir(parents=True)
            missing_parent = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": str(plan_root / "missing" / "safe.md")},
                permission_mode="plan",
            )

            outside = harness.root / "outside-plans"
            outside.mkdir()
            link = plan_root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                symlink_result = None
            else:
                symlink_result = self.mutation(
                    harness,
                    "Write",
                    tool_input={"file_path": str(link / "escape.md")},
                    permission_mode="plan",
                )

            self.assertTrue(missing_parent.blocked, missing_parent.stderr)
            if symlink_result is not None:
                self.assertTrue(symlink_result.blocked, symlink_result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX-only lexical Windows regression")
    def test_posix_rejects_lexical_windows_paths(self) -> None:
        env = {"USERPROFILE": r"C:\Users\Ada"}
        with HookHarness() as harness:
            lexical = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": r"C:\Users\Ada\.claude\plans\safe.md"},
                permission_mode="plan",
                hook_env=env,
            )
            traversal = self.mutation(
                harness,
                "Write",
                tool_input={
                    "file_path": r"C:\Users\Ada\.claude\plans\..\outside.md"
                },
                permission_mode="plan",
                hook_env=env,
            )
            cross_drive = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": r"D:\outside.md"},
                permission_mode="plan",
                hook_env=env,
            )

            self.assertTrue(lexical.blocked, lexical.stderr)
            self.assertTrue(traversal.blocked, traversal.stderr)
            self.assertTrue(cross_drive.blocked, cross_drive.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows native filesystem test")
    def test_windows_native_plan_paths_and_cross_drive(self) -> None:
        with HookHarness() as harness:
            plan_root = harness.home_dir / ".claude" / "plans"
            plan_root.mkdir(parents=True)
            existing = plan_root / "existing.md"
            existing.write_text("plan", encoding="utf-8")
            home_drive = plan_root.drive.casefold()
            other_drive = "Z:" if home_drive != "z:" else "Y:"

            allowed_existing = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": str(existing)},
                permission_mode="plan",
            )
            allowed_new = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": str(plan_root / "new.md")},
                permission_mode="plan",
            )
            traversal = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": str(plan_root / ".." / "outside.md")},
                permission_mode="plan",
            )
            cross_drive = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": other_drive + r"\outside.md"},
                permission_mode="plan",
            )

            self.assertEqual(allowed_existing.returncode, 0, allowed_existing.stderr)
            self.assertEqual(allowed_new.returncode, 0, allowed_new.stderr)
            self.assertTrue(traversal.blocked, traversal.stderr)
            self.assertTrue(cross_drive.blocked, cross_drive.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows junction/reparse test")
    def test_windows_junction_escape_is_denied(self) -> None:
        with HookHarness() as harness:
            plan_root = harness.home_dir / ".claude" / "plans"
            plan_root.mkdir(parents=True)
            outside = harness.root / "junction-target"
            outside.mkdir()
            junction = plan_root / "junction"
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest("cannot create junction: {}".format(created.stderr))
            result = self.mutation(
                harness,
                "Write",
                tool_input={"file_path": str(junction / "escape.md")},
                permission_mode="plan",
            )
            self.assertTrue(result.blocked, result.stderr)

    def test_lock_contention_fails_closed_within_outer_budget(self) -> None:
        with HookHarness(timeout=4.5) as harness:
            store = StateStore(harness.plugin_data_dir / "agent-kit")
            connection = sqlite3.connect(
                str(store.db_path), timeout=1.0, isolation_level=None
            )
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                result = self.mutation(
                    harness,
                    hook_env={"AGENT_KIT_SQLITE_BUSY_TIMEOUT_MS": "20000"},
                )
                elapsed = time.monotonic() - started
            finally:
                connection.execute("ROLLBACK")
                connection.close()

            self.assertTrue(result.blocked, result.stderr)
            self.assertIn("trạng thái", result.stderr)
            self.assertLess(elapsed, 4.0, elapsed)

    def test_plan_gate_off_is_explicit_bypass_and_bad_legacy_env_does_not_crash(self) -> None:
        with HookHarness() as harness:
            bypassed = harness.run(
                HOOK,
                harness.payloads.pre_tool_use("Write"),
                env={"PLAN_GATE": "OFF", "PLAN_GATE_FREE_EDITS": "not-an-int"},
            )
            normal = harness.run(
                HOOK,
                harness.payloads.pre_tool_use("Write"),
                env={"PLAN_GATE_FREE_EDITS": "not-an-int"},
            )
            self.assertEqual(bypassed.returncode, 0, bypassed.stderr)
            self.assertTrue(normal.blocked, normal.stderr)

    def test_concurrent_duplicate_approvals_are_idempotent(self) -> None:
        with HookHarness(timeout=20.0) as harness:
            calls = [
                HookCall(
                    HOOK,
                    harness.payloads.post_tool_use(
                        "ExitPlanMode",
                        {"plan": VALID_PLAN},
                        {"plan": VALID_PLAN},
                        tool_use_id="approval-{}".format(index),
                    ),
                )
                for index in range(32)
            ]
            results = harness.run_concurrent(calls, max_workers=32)
            self.assertEqual(
                [result.returncode for result in results],
                [0] * 32,
                [result.stderr for result in results if result.returncode],
            )
            self.assertEqual(self.mutation(harness).returncode, 0)

    def test_session_end_removes_only_that_sessions_state(self) -> None:
        with HookHarness() as harness:
            self.approve(harness, session_id="session-A", prompt_id="prompt-A")
            self.approve(harness, session_id="session-A", prompt_id="prompt-B")
            self.approve(harness, session_id="session-B", prompt_id="prompt-A")
            ended = harness.run(
                HOOK,
                harness.payloads.session_end(session_id="session-A", prompt_id="ignored"),
            )

            self.assertEqual(ended.returncode, 0, ended.stderr)
            self.assertTrue(
                self.mutation(
                    harness, session_id="session-A", prompt_id="prompt-A"
                ).blocked
            )
            self.assertTrue(
                self.mutation(
                    harness, session_id="session-A", prompt_id="prompt-B"
                ).blocked
            )
            self.assertEqual(
                self.mutation(
                    harness, session_id="session-B", prompt_id="prompt-A"
                ).returncode,
                0,
            )

    def test_state_files_do_not_store_raw_runtime_ids_or_plan(self) -> None:
        session = "RAW-SESSION-NEVER-PERSIST"
        prompt = "RAW-PROMPT-NEVER-PERSIST"
        plan = VALID_PLAN.replace("Read the affected files", "RAW-PLAN-SECRET")
        with HookHarness() as harness:
            self.approve(harness, session_id=session, prompt_id=prompt, plan=plan)
            state_root = harness.plugin_data_dir / "agent-kit"
            blobs = b"".join(
                path.read_bytes()
                for path in state_root.iterdir()
                if path.is_file()
            )
            self.assertNotIn(session.encode(), blobs)
            self.assertNotIn(prompt.encode(), blobs)
            self.assertNotIn(b"RAW-PLAN-SECRET", blobs)


if __name__ == "__main__":
    unittest.main()
