"""Regression tests for project-scoped, ordered verification receipts."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import unittest
from typing import Dict, Iterable, Mapping, Optional, Tuple

from hooks._shared import StateStore, hash_value
from tests.support import OMIT, HookHarness, HookResult


HOOK = "no-fake-pass.py"
RESULT_PREFIX = "AGENT_KIT_RESULT_V1="
RECEIPT_PREFIX = "AGENT_KIT_RECEIPT_V1="
ORDERED_STEPS = ("build", "lint", "test")
ACTIVE_COMMANDS = {
    "build": "python3 -m compileall -q src",
    "lint": "python3 tools/lint.py",
    "test": "python3 -m unittest discover -v",
}


def result_line(status: str, **fields: object) -> str:
    value = {"status": status}
    value.update(fields)
    return RESULT_PREFIX + json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class NoFakePassV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = HookHarness(timeout=20)
        self.addCleanup(self.harness.close)
        self.write_contract()

    def contract_document(
        self,
        commands: Optional[Mapping[str, Optional[str]]] = None,
        cwd_by_step: Optional[Mapping[str, str]] = None,
    ) -> dict:
        declared: Dict[str, Optional[str]] = {
            "build": ACTIVE_COMMANDS["build"],
            "typecheck": None,
            "lint": ACTIVE_COMMANDS["lint"],
            "test": ACTIVE_COMMANDS["test"],
        }
        declared.update(commands or {})
        cwd_by_step = dict(cwd_by_step or {})
        steps = {}
        reasons = {}
        for name in ("build", "typecheck", "lint", "test"):
            command = declared[name]
            if command is None:
                steps[name] = None
                reasons[name] = "N/A: {} is not used by this project".format(name)
            else:
                steps[name] = {
                    "command": command,
                    "cwd": cwd_by_step.get(name, "."),
                }
        return {"version": 1, "steps": steps, "n_a_reasons": reasons}

    def write_contract(
        self,
        commands: Optional[Mapping[str, Optional[str]]] = None,
        cwd_by_step: Optional[Mapping[str, str]] = None,
        *,
        project: Optional[pathlib.Path] = None,
        document: Optional[Mapping[str, object]] = None,
    ) -> pathlib.Path:
        root = project or self.harness.project_dir
        path = root / ".claude" / "verification.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = dict(document) if document is not None else self.contract_document(
            commands, cwd_by_step
        )
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def run_exact(
        self,
        step: str,
        *,
        prompt_id: str = "prompt-1",
        agent_id: str = "agent-1",
        agent_type: str = "agent-kit:builder",
        tool_use_id: Optional[str] = None,
        tool_name: str = "Bash",
        command: Optional[str] = None,
        response: Optional[Mapping[str, object]] = None,
        extra_input: Optional[Mapping[str, object]] = None,
        failure: bool = False,
        project: Optional[pathlib.Path] = None,
        cwd: Optional[pathlib.Path] = None,
    ) -> HookResult:
        root = project or self.harness.project_dir
        tool_input: Dict[str, object] = {
            "command": command if command is not None else ACTIVE_COMMANDS[step]
        }
        tool_input.update(extra_input or {})
        common = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_use_id": tool_use_id or "verify-{}-{}".format(prompt_id, step),
            "agent_type": agent_type,
            "agent_id": agent_id,
            "prompt_id": prompt_id,
        }
        if failure:
            payload = self.harness.payloads.post_tool_use_failure(**common)
        else:
            payload = self.harness.payloads.post_tool_use(
                tool_response=response
                if response is not None
                else {"stdout": "ok", "stderr": "", "interrupted": False},
                **common,
            )
        payload["cwd"] = str(cwd or root)
        return self.harness.run(HOOK, payload, cwd=cwd or root)

    def receipt_or_none(self, result: HookResult) -> Optional[str]:
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return None
        output = result.require_json()
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith(RECEIPT_PREFIX), context)
        document = json.loads(context[len(RECEIPT_PREFIX) :])
        self.assertEqual(set(document), {"epoch", "receipt", "step"})
        self.assertIsInstance(document["epoch"], int)
        self.assertIsInstance(document["receipt"], str)
        return document["receipt"]

    def receipt(self, result: HookResult) -> str:
        receipt = self.receipt_or_none(result)
        self.assertIsNotNone(receipt)
        return receipt or ""

    def collect_receipts(
        self,
        *,
        prompt_id: str = "prompt-1",
        agent_id: str = "agent-1",
        suffix: str = "chain",
        project: Optional[pathlib.Path] = None,
    ) -> Dict[str, str]:
        receipts = {}
        for step in ORDERED_STEPS:
            receipts[step] = self.receipt(
                self.run_exact(
                    step,
                    prompt_id=prompt_id,
                    agent_id=agent_id,
                    tool_use_id="{}-{}-{}".format(suffix, prompt_id, step),
                    project=project,
                )
            )
        return receipts

    def ready(
        self,
        receipts: Mapping[str, str],
        *,
        prompt_id: str = "prompt-1",
        agent_id: str = "agent-1",
        agent_type: str = "agent-kit:builder",
        project: Optional[pathlib.Path] = None,
    ) -> HookResult:
        root = project or self.harness.project_dir
        payload = self.harness.payloads.subagent_stop(
            result_line("READY", receipts=dict(receipts)),
            prompt_id=prompt_id,
            agent_id=agent_id,
            agent_type=agent_type,
        )
        payload["cwd"] = str(root)
        return self.harness.run(HOOK, payload, cwd=root)

    def run_mutation(
        self,
        tool: str,
        *,
        prompt_id: str = "prompt-1",
        agent_id: str = "actor-1",
        agent_type: str = "Explore",
        tool_use_id: str = "mutation-1",
        tool_input: Optional[Mapping[str, object]] = None,
        failure: bool = False,
        response: Optional[Mapping[str, object]] = None,
        project: Optional[pathlib.Path] = None,
    ) -> HookResult:
        root = project or self.harness.project_dir
        common = {
            "tool_name": tool,
            "tool_input": dict(tool_input or {"path": "source.py"}),
            "tool_use_id": tool_use_id,
            "agent_type": agent_type,
            "agent_id": agent_id,
            "prompt_id": prompt_id,
        }
        if failure:
            payload = self.harness.payloads.post_tool_use_failure(**common)
        else:
            payload = self.harness.payloads.post_tool_use(
                tool_response=dict(response or {"ok": True}), **common
            )
        payload["cwd"] = str(root)
        return self.harness.run(HOOK, payload, cwd=root)

    def state_epoch(self, prompt_id: str = "prompt-1") -> int:
        return StateStore(
            self.harness.plugin_data_dir / "agent-kit"
        ).current_mutation_epoch(
            self.harness.project_dir, "session-1", prompt_id
        )

    def test_full_ordered_chain_passes_and_exact_verification_does_not_mutate(self) -> None:
        receipts = self.collect_receipts()
        self.assertEqual(set(receipts), set(ORDERED_STEPS))
        self.assertEqual(self.state_epoch(), 0)
        result = self.ready(receipts)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

        self.assertTrue(
            self.ready({"build": receipts["build"], "lint": receipts["lint"]}).blocked
        )
        self.assertTrue(self.ready(dict(receipts, typecheck="invented")).blocked)

    def test_bash_and_powershell_exact_steps_share_one_ordered_chain(self) -> None:
        build = self.receipt(self.run_exact("build", tool_name="Bash"))
        lint = self.receipt(
            self.run_exact("lint", tool_name="PowerShell", tool_use_id="pwsh-lint")
        )
        test = self.receipt(
            self.run_exact("test", tool_name="Bash", tool_use_id="bash-test")
        )
        self.assertEqual(
            self.ready({"build": build, "lint": lint, "test": test}).returncode,
            0,
        )

    def test_test_lint_build_order_cannot_claim_ready(self) -> None:
        test_result = self.run_exact("test", tool_use_id="reverse-test")
        lint_result = self.run_exact("lint", tool_use_id="reverse-lint")
        build_receipt = self.receipt(
            self.run_exact("build", tool_use_id="reverse-build")
        )
        self.assertIsNone(self.receipt_or_none(test_result))
        self.assertIsNone(self.receipt_or_none(lint_result))
        bypass = self.ready(
            {"build": build_receipt, "lint": "invented", "test": "invented"}
        )
        self.assertTrue(bypass.blocked, bypass.stderr)

    def test_rerun_build_invalidates_all_downstream_receipts(self) -> None:
        old = self.collect_receipts()
        new_build = self.receipt(
            self.run_exact("build", tool_use_id="rerun-build")
        )
        bypass = self.ready(dict(old, build=new_build))
        self.assertTrue(bypass.blocked, bypass.stderr)
        lint = self.receipt(self.run_exact("lint", tool_use_id="rerun-lint"))
        test = self.receipt(self.run_exact("test", tool_use_id="rerun-test"))
        current = {"build": new_build, "lint": lint, "test": test}
        self.assertEqual(self.ready(current).returncode, 0)

    def test_exact_failure_interruption_and_background_reject_old_ready(self) -> None:
        cases: Tuple[Tuple[str, Mapping[str, object]], ...] = (
            ("failure", {"failure": True}),
            ("interrupted", {"response": {"interrupted": True}}),
            ("background", {"extra_input": {"run_in_background": True}}),
        )
        for index, (name, options) in enumerate(cases):
            prompt = "outcome-{}".format(index)
            receipts = self.collect_receipts(prompt_id=prompt, suffix=name)
            event = self.run_exact(
                "build",
                prompt_id=prompt,
                tool_use_id="{}-build".format(name),
                **options,
            )
            self.assertIsNone(self.receipt_or_none(event))
            bypass = self.ready(receipts, prompt_id=prompt)
            self.assertTrue(bypass.blocked, bypass.stderr)

    def test_cross_actor_mutations_from_main_explore_and_agent_b_stale_builder_a(self) -> None:
        actors = (
            ("main", "main-actor"),
            ("Explore", "explore-agent"),
            ("agent-kit:builder", "agent-B"),
        )
        for index, (agent_type, actor_id) in enumerate(actors):
            prompt = "cross-actor-{}".format(index)
            receipts = self.collect_receipts(
                prompt_id=prompt, agent_id="builder-A", suffix=agent_type
            )
            mutation = self.run_mutation(
                "Edit",
                prompt_id=prompt,
                agent_type=agent_type,
                agent_id=actor_id,
                tool_use_id="edit-{}".format(index),
            )
            self.assertEqual(mutation.returncode, 0, mutation.stderr)
            bypass = self.ready(receipts, prompt_id=prompt, agent_id="builder-A")
            self.assertTrue(bypass.blocked, bypass.stderr)

    def test_all_mutation_families_success_and_failure_advance_project_epoch(self) -> None:
        tools: Iterable[str] = (
            "Edit",
            "Write",
            "NotebookEdit",
            "Monitor",
            "EnterWorktree",
            "ExitWorktree",
            "mcp__server__write",
            "Bash",
            "PowerShell",
        )
        count = 0
        for failure in (False, True):
            for tool in tools:
                count += 1
                tool_input = (
                    {"command": "python3 unknown.py"}
                    if tool in ("Bash", "PowerShell")
                    else {"path": "source.py"}
                )
                result = self.run_mutation(
                    tool,
                    tool_use_id="family-{}".format(count),
                    tool_input=tool_input,
                    failure=failure,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(self.state_epoch(), count)

    def test_nonbuilder_shell_and_generic_background_are_project_mutations(self) -> None:
        receipts = self.collect_receipts(prompt_id="generic-background")
        background = self.run_mutation(
            "Bash",
            prompt_id="generic-background",
            tool_use_id="generic-background-shell",
            tool_input={"command": ACTIVE_COMMANDS["build"], "run_in_background": True},
            response={"backgroundTaskId": "task-1"},
        )
        self.assertEqual(background.returncode, 0, background.stderr)
        fresh = self.collect_receipts(
            prompt_id="generic-background", suffix="after-background"
        )
        self.assertTrue(self.ready(receipts, prompt_id="generic-background").blocked)
        self.assertTrue(self.ready(fresh, prompt_id="generic-background").blocked)

    def test_same_mutation_tool_id_same_fact_is_idempotent_but_collision_blocks(self) -> None:
        first = self.run_mutation(
            "Edit", tool_use_id="collision", tool_input={"path": "one.py"}
        )
        duplicate = self.run_mutation(
            "Edit", tool_use_id="collision", tool_input={"path": "one.py"}
        )
        conflict = self.run_mutation(
            "Edit", tool_use_id="collision", tool_input={"path": "two.py"}
        )
        self.assertEqual((first.returncode, duplicate.returncode), (0, 0))
        self.assertEqual(self.state_epoch(), 1)
        self.assertTrue(conflict.blocked, conflict.stderr)

    def test_contract_command_cwd_and_na_reason_drift_reject_old_receipts(self) -> None:
        receipts = self.collect_receipts()
        child = self.harness.project_dir / "child"
        child.mkdir()
        variants = []
        command_drift = self.contract_document(
            commands={"build": "python3 different-build.py"}
        )
        variants.append(command_drift)
        cwd_drift = self.contract_document(cwd_by_step={"lint": "child"})
        variants.append(cwd_drift)
        na_drift = self.contract_document()
        na_drift["n_a_reasons"]["typecheck"] = "N/A: reason changed"
        variants.append(na_drift)
        for document in variants:
            self.write_contract(document=document)
            result = self.ready(receipts)
            self.assertTrue(result.blocked, result.stderr)

    def test_contract_validator_rejects_bool_version_na_whitespace_and_wrong_schema(self) -> None:
        variants = []
        bool_version = self.contract_document()
        bool_version["version"] = True
        variants.append(bool_version)
        whitespace_reason = self.contract_document()
        whitespace_reason["n_a_reasons"]["typecheck"] = " N/A: disabled"
        variants.append(whitespace_reason)
        extra = self.contract_document()
        extra["raw-contract-secret"] = True
        variants.append(extra)
        missing = self.contract_document()
        del missing["steps"]["test"]
        variants.append(missing)
        for index, document in enumerate(variants):
            self.write_contract(document=document)
            result = self.run_exact(
                "build", tool_use_id="invalid-contract-{}".format(index)
            )
            self.assertTrue(result.blocked, result.stderr)
            self.assertIn("invalid verification contract", result.stderr)
            self.assertNotIn("raw-contract-secret", result.stderr)

    def test_project_identity_and_owner_agent_isolate_receipts(self) -> None:
        receipts = self.collect_receipts(agent_id="builder-A")
        self.assertTrue(self.ready(receipts, agent_id="builder-B").blocked)
        other = self.harness.root / "other-project"
        other.mkdir()
        self.write_contract(project=other)
        cross_project = self.ready(receipts, agent_id="builder-A", project=other)
        self.assertTrue(cross_project.blocked, cross_project.stderr)

    def test_missing_runtime_ids_fail_closed_only_for_relevant_events(self) -> None:
        base = self.harness.payloads.post_tool_use(
            tool_name="Edit",
            tool_input={"path": "source.py"},
            tool_response={"ok": True},
            tool_use_id="missing-field",
            agent_id="actor",
        )
        # agent_id is intentionally excluded from this "any missing field
        # always blocks" loop: per Claude Code's documented hook payload
        # design it is subagent-only, so it is no longer unconditionally
        # required for every actor -- only a watched builder must still
        # carry it (see test_subagent_stop_watched_builder_without_agent_id
        # _still_blocks), while a non-builder actor legitimately omits it
        # (see test_non_builder_mutation_without_agent_type_or_agent_id
        # _bumps_epoch_and_stales_receipts). Asserting an unconditional
        # block here would encode the very bug this fix removes.
        for field in ("session_id", "prompt_id", "tool_use_id", "cwd"):
            payload = dict(base)
            payload.pop(field, None)
            result = self.harness.run(HOOK, payload)
            self.assertTrue(result.blocked, (field, result.stderr))

        missing_tool = dict(base)
        missing_tool.pop("tool_name")
        self.assertTrue(self.harness.run(HOOK, missing_tool).blocked)

        irrelevant = dict(base)
        irrelevant["tool_name"] = "Read"
        for field in ("session_id", "prompt_id", "agent_id", "tool_use_id"):
            irrelevant.pop(field, None)
        ignored = self.harness.run(HOOK, irrelevant)
        self.assertEqual(ignored.returncode, 0, ignored.stderr)

    def test_non_builder_mutation_without_agent_type_or_agent_id_bumps_epoch_and_stales_receipts(
        self,
    ) -> None:
        # Root cause: per Claude Code's documented hook payload design,
        # agent_id is present only inside a subagent; it is intentionally
        # absent for main-session events, and the 2.1.196 milestone is tied
        # to prompt_id, not agent_id. A PostToolUse Edit from a non-builder
        # actor (no agent_type, no agent_id at all -- e.g. the main session)
        # must therefore still count as a real project mutation instead of
        # being rejected outright, and it must still stale any builder
        # receipt chain collected earlier under the same session/prompt,
        # exactly like every other mutation actor already does.
        receipts = self.collect_receipts()
        self.assertEqual(self.state_epoch(), 0)
        payload = self.harness.payloads.post_tool_use(
            tool_name="Edit",
            tool_input={"path": "source.py"},
            tool_response={"ok": True},
            tool_use_id="edit-no-identity",
            agent_type=OMIT,
            agent_id=OMIT,
        )
        result = self.harness.run(HOOK, payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state_epoch(), 1)
        self.assertTrue(self.ready(receipts).blocked)

    def test_subagent_stop_watched_builder_without_agent_id_still_blocks(self) -> None:
        # A watched builder is a different case from the plain main-session
        # actor above: unlike a non-builder, it is still required to carry
        # agent_id, so a missing one must still block. The block reason must
        # not misquote the 2.1.196 Claude Code milestone for agent_id (that
        # milestone genuinely only governs prompt_id); this also proves the
        # error-text fix in _required_runtime_field applies to every caller,
        # not only the non-builder path.
        payload = self.harness.payloads.subagent_stop(
            result_line(
                "READY",
                receipts={"build": "invented", "lint": "invented", "test": "invented"},
            ),
            agent_type="agent-kit:builder",
            agent_id=OMIT,
        )
        result = self.harness.run(HOOK, payload)
        self.assertTrue(result.blocked, result.stderr)
        self.assertNotIn("2.1.196", result.stderr, result.stderr)

    def test_stop_fabricated_ready_without_agent_id_blocks_on_receipts_not_version(
        self,
    ) -> None:
        # The main session's Stop event never carries agent_id at all (it is
        # a subagent-only field); blocking a bogus claim here must not
        # depend on treating that absence as a version-gated runtime error.
        # The claim is still fabricated (invented receipts that were never
        # recorded), so it must still block -- but for the honest reason
        # (the receipt chain is rejected), never by citing the 2.1.196
        # Claude Code milestone, which only actually governs prompt_id.
        payload = self.harness.payloads.stop(
            result_line(
                "READY",
                receipts={"build": "invented", "lint": "invented", "test": "invented"},
            ),
        )
        result = self.harness.run(HOOK, payload)
        self.assertTrue(result.blocked, result.stderr)
        self.assertIn("receipt", result.stderr.lower(), result.stderr)
        self.assertNotIn("2.1.196", result.stderr, result.stderr)

    def test_watched_builder_stop_is_enforced_but_other_agents_are_ignored(self) -> None:
        fake = self.harness.payloads.subagent_stop(
            "All tests passed", agent_type="agent-kit:builder"
        )
        self.assertTrue(self.harness.run(HOOK, fake).blocked)
        explore = self.harness.payloads.subagent_stop(
            "All tests passed", agent_type="Explore"
        )
        ignored = self.harness.run(HOOK, explore)
        self.assertEqual(ignored.returncode, 0, ignored.stderr)

    def test_stop_hook_active_still_blocks_until_the_attempt_cap(self) -> None:
        """Loi 4: a stop_hook_active=True retry must be re-validated exactly
        like any other attempt, not waved through unconditionally.

        The old behavior returned 0 the instant stop_hook_active was truthy,
        with no re-validation at all. That meant a builder blocked for a
        fabricated receipt could resend the identical, still-invalid
        AGENT_KIT_RESULT_V1 line on the very next turn and pass immediately
        (see Loi 4). This test's name and assertion used to encode that hole
        as the expected behavior ("prevents recursive block" -> returncode
        0); it is now the spec being fixed, so the test is renamed and
        inverted to assert the retry is still blocked while under
        STOP_RETRY_CAP, matching the equivalent fix in hooks/gloss-gate.py.
        """
        payload = self.harness.payloads.subagent_stop("All tests passed")
        payload["stop_hook_active"] = True
        result = self.harness.run(HOOK, payload)
        self.assertTrue(result.blocked, result.stderr)

    def test_stop_hook_active_cap_hit_passes_through_with_warning(self) -> None:
        """Loi 4: after STOP_RETRY_CAP consecutive blocked retries for the
        exact same (project, session, prompt, agent) identity, no-fake-pass
        must concede rather than deadlock the session — but only with a
        loud stderr warning naming the cap and the still-unverified
        violation, never silently. Exercised for both SubagentStop (a
        watched builder) and Stop (the main session), each under its own
        session_id so the two subtests' retry counters cannot bleed into
        each other or into any other test in this module.
        """
        from hooks._shared import STOP_RETRY_CAP

        def subagent_stop_retry_payload(session_id: str) -> Dict[str, object]:
            payload = self.harness.payloads.subagent_stop(
                "All tests passed", session_id=session_id
            )
            payload["stop_hook_active"] = True
            return payload

        def main_session_retry_payload(session_id: str) -> Dict[str, object]:
            payload = self.harness.payloads.stop(
                result_line("READY", receipts={"build": "x", "lint": "y", "test": "z"}),
                agent_id="main-agent",
                session_id=session_id,
            )
            payload["stop_hook_active"] = True
            return payload

        cases = (
            # subagent_stop's "All tests passed" has no result marker at all,
            # so the underlying violation is the missing-marker error; stop's
            # fabricated receipts do parse as a claim, so its violation is
            # the receipt-chain rejection instead. Each case checks the
            # violation text that its own payload actually produces.
            (
                "subagent_stop",
                subagent_stop_retry_payload,
                "AGENT_KIT_RESULT_V1",
            ),
            (
                "stop",
                main_session_retry_payload,
                "receipts rejected",
            ),
        )
        for label, build_payload, violation_fragment in cases:
            with self.subTest(event=label):
                payload = build_payload("cap-session-{}".format(label))

                for attempt in range(1, STOP_RETRY_CAP + 1):
                    result = self.harness.run(HOOK, payload)
                    self.assertTrue(
                        result.blocked,
                        "attempt {} expected to still block: {}".format(
                            attempt, result.stderr
                        ),
                    )

                final = self.harness.run(HOOK, payload)
                self.assertEqual(final.returncode, 0, final.stderr)
                self.assertIn(str(STOP_RETRY_CAP), final.stderr)
                self.assertIn("KHÔNG được xác minh", final.stderr)
                self.assertIn(
                    violation_fragment,
                    final.stderr,
                    "warning must still name the underlying violation",
                )

    def test_result_requires_one_unfenced_machine_line(self) -> None:
        messages = (
            "All tests passed, 0 errors.",
            "$ python3 -m unittest\nOK",
            "CHƯA VERIFY: environment unavailable",
            "```json\n{}{{\"status\":\"READY\",\"receipts\":{{}}}}\n```".format(
                RESULT_PREFIX
            ),
        )
        for message in messages:
            result = self.harness.run(
                HOOK, self.harness.payloads.subagent_stop(message)
            )
            self.assertTrue(result.blocked, result.stderr)

    def test_missing_or_escaped_contract_and_shell_cwd_fail_closed(self) -> None:
        contract_path = self.harness.project_dir / ".claude" / "verification.json"
        contract_path.unlink()
        self.assertTrue(self.run_exact("build").blocked)
        self.write_contract()
        outside = self.harness.root / "outside"
        outside.mkdir()
        escaped = self.run_exact(
            "build", tool_use_id="outside-cwd", cwd=outside
        )
        self.assertTrue(escaped.blocked, escaped.stderr)

    def test_claude_project_dir_locates_contract_from_declared_child_cwd(self) -> None:
        child = self.harness.project_dir / "child"
        child.mkdir()
        self.write_contract(cwd_by_step={"build": "child"})
        payload = self.harness.payloads.post_tool_use(
            tool_name="Bash",
            tool_input={"command": ACTIVE_COMMANDS["build"]},
            tool_response={"interrupted": False},
            tool_use_id="child-build",
        )
        payload["cwd"] = str(child)
        result = self.harness.run(
            HOOK,
            payload,
            env={"CLAUDE_PROJECT_DIR": str(self.harness.project_dir)},
            cwd=child,
        )
        self.assertTrue(self.receipt(result))

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_debug_dump_is_private_redacted_and_symlink_safe(self) -> None:
        payload = self.harness.payloads.subagent_stop(
            result_line("NOT_READY", reason="raw-secret-reason")
        )
        payload["api_token"] = "raw-secret-token"
        payload["body"] = {"data": "raw-secret-body"}
        result = self.harness.run(HOOK, payload, env={"DUMP": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        label_hash = hash_value("no-fake-pass-payload", "dump_label")[:16]
        dump = self.harness.plugin_data_dir / "agent-kit" / (
            "dump-" + label_hash + ".json"
        )
        self.assertTrue(dump.is_file())
        self.assertEqual(stat.S_IMODE(dump.stat().st_mode), 0o600)
        content = dump.read_text(encoding="utf-8")
        for secret in ("raw-secret-reason", "raw-secret-token", "raw-secret-body"):
            self.assertNotIn(secret, content)

        outside = self.harness.root / "outside.json"
        outside.write_text("preserve-me", encoding="utf-8")
        dump.unlink()
        dump.symlink_to(outside)
        second = self.harness.run(HOOK, payload, env={"DUMP": "1"})
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve-me")
        self.assertTrue(dump.is_symlink())

    def test_runtime_and_receipt_secrets_are_not_persisted(self) -> None:
        receipts = self.collect_receipts(
            prompt_id="raw-prompt", agent_id="raw-agent", suffix="raw-tool"
        )
        self.assertEqual(
            self.ready(
                receipts, prompt_id="raw-prompt", agent_id="raw-agent"
            ).returncode,
            0,
        )
        state_bytes = b""
        state_root = self.harness.plugin_data_dir / "agent-kit"
        for path in state_root.iterdir():
            if path.is_file():
                state_bytes += path.read_bytes()
        forbidden = (
            "session-1",
            "raw-prompt",
            "raw-agent",
            ACTIVE_COMMANDS["build"],
            str(self.harness.project_dir),
            *receipts.values(),
        )
        for raw in forbidden:
            self.assertNotIn(raw.encode("utf-8"), state_bytes)

    def test_stop_event_ignores_ordinary_turns_without_a_result_marker(self) -> None:
        # Loi 3: the Stop event now runs no-fake-pass.py for the main session,
        # but an ordinary end-of-turn reply (no machine-checkable claim) must
        # not be blocked, or every normal conversation turn would break.
        payload = self.harness.payloads.stop(
            "Da tra loi xong cau hoi cua nguoi dung, khong co claim pass/fail nao.",
            agent_id="main-agent",
        )
        result = self.harness.run(HOOK, payload)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stop_event_blocks_a_fabricated_ready_claim_from_main_session(self) -> None:
        # Loi 3: if the main session itself emits a structured READY claim at
        # Stop without ever running the tracked verification steps, no-fake-
        # pass must block it exactly as it already does for SubagentStop.
        payload = self.harness.payloads.stop(
            result_line(
                "READY",
                receipts={"build": "invented", "lint": "invented", "test": "invented"},
            ),
            agent_id="main-agent",
        )
        result = self.harness.run(HOOK, payload)
        self.assertTrue(result.blocked, result.stderr)

    def test_stop_event_blocks_ambiguous_multi_marker_claim_from_main_session(self) -> None:
        # Same "exactly one unfenced line" discipline applies once a claim is
        # made at all; two occurrences must fail closed, not pick one.
        message = result_line("READY", receipts={}) + "\n" + result_line("NOT_READY", reason="x")
        payload = self.harness.payloads.stop(message, agent_id="main-agent")
        result = self.harness.run(HOOK, payload)
        self.assertTrue(result.blocked, result.stderr)

    def test_readonly_shell_from_other_agent_does_not_wipe_builder_chain(self) -> None:
        # Loi 6 point 2/3: a provably read-only shell command from any actor
        # (e.g. the main session checking git status mid-verification) must
        # not advance the project epoch or invalidate the builder's chain.
        receipts = self.collect_receipts(prompt_id="readonly-shell")
        result = self.run_mutation(
            "Bash",
            prompt_id="readonly-shell",
            agent_type="main",
            agent_id="main-actor",
            tool_use_id="readonly-git-status",
            tool_input={"command": "git status --short"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.state_epoch(prompt_id="readonly-shell"), 0)
        self.assertEqual(self.ready(receipts, prompt_id="readonly-shell").returncode, 0)

    def test_shell_command_with_write_sign_still_counts_as_mutation(self) -> None:
        # Loi 6 point 3: a redirect after an otherwise-allowlisted prefix must
        # still be treated as a mutation, not exempted.
        receipts = self.collect_receipts(prompt_id="disguised-shell")
        result = self.run_mutation(
            "Bash",
            prompt_id="disguised-shell",
            agent_type="main",
            agent_id="main-actor",
            tool_use_id="disguised-git-status",
            tool_input={"command": "git status > /tmp/agent-kit-test-out.txt"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state_epoch(prompt_id="disguised-shell"), 1)
        self.assertTrue(self.ready(receipts, prompt_id="disguised-shell").blocked)

    def test_find_with_delete_flag_still_counts_as_mutation(self) -> None:
        # Loi 7: `find` is on the read-only allowlist, but `-delete` (and the
        # other `-exec`/`-ok`/`-fprint*` action primitives) write to disk or
        # execute an arbitrary sub-command without ever tripping a redirect,
        # pipe, chain, or substitution sign. The prefix match alone must not
        # exempt it, or a destructive find call would sail through as
        # read-only, keep the epoch at 0, and leave an already-collected
        # builder receipt chain silently valid.
        receipts = self.collect_receipts(prompt_id="find-delete-shell")
        result = self.run_mutation(
            "Bash",
            prompt_id="find-delete-shell",
            agent_type="main",
            agent_id="main-actor",
            tool_use_id="find-delete",
            tool_input={"command": "find . -name '*.tmp' -delete"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state_epoch(prompt_id="find-delete-shell"), 1)
        self.assertTrue(self.ready(receipts, prompt_id="find-delete-shell").blocked)

    def test_git_diff_with_output_flag_still_counts_as_mutation(self) -> None:
        # Same hole, different allowlisted prefix: `git diff --output=<file>`
        # writes the diff to disk instead of stdout, again with no
        # redirect/pipe/chain/substitution sign for `_SHELL_WRITE_SIGNS` to
        # catch.
        receipts = self.collect_receipts(prompt_id="git-diff-output-shell")
        result = self.run_mutation(
            "Bash",
            prompt_id="git-diff-output-shell",
            agent_type="main",
            agent_id="main-actor",
            tool_use_id="git-diff-output",
            tool_input={
                "command": "git diff --output=/tmp/agent-kit-test-diff.txt"
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state_epoch(prompt_id="git-diff-output-shell"), 1)
        self.assertTrue(
            self.ready(receipts, prompt_id="git-diff-output-shell").blocked
        )

    def test_malformed_json_payload_is_nonblocking_and_does_not_invent_state(self) -> None:
        hook_path = self.harness.repo_root / "hooks" / HOOK
        for raw in ("", "{", "[]", '"text"'):
            completed = subprocess.run(
                [sys.executable, str(hook_path)],
                input=raw,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.harness.project_dir),
                env=self.harness.environment(),
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
