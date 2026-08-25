"""Regression tests for the stateless session-policy hook."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest
from typing import List, Mapping, Optional

from tests.support import HookCall, HookHarness, HookResult


HOOK = "session-policy.py"
SESSION_SOURCES = ("startup", "resume", "clear", "compact", "fork")


class SessionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = HookHarness()
        self.expected_policy = (
            self.harness.repo_root / "policy" / "delegation.md"
        ).read_text(encoding="utf-8").strip()

    def tearDown(self) -> None:
        self.harness.close()

    def assert_silent_success(self, result: HookResult) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def assert_injection(
        self,
        result: HookResult,
        event: str,
        expected_text: Optional[str] = None,
    ) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        output = result.require_json()
        self.assertEqual(
            output,
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": expected_text or self.expected_policy,
                }
            },
        )

    def run_raw(
        self,
        raw_stdin: bytes,
        env: Optional[Mapping[str, str]] = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(self.harness.repo_root / "hooks" / HOOK)],
            input=raw_stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.harness.project_dir),
            env=self.harness.environment(env),
            timeout=10,
            check=False,
        )

    def fixture_files(self) -> List[pathlib.Path]:
        return sorted(
            path.relative_to(self.harness.root)
            for path in self.harness.root.rglob("*")
            if path.is_file()
        )

    def test_injects_every_documented_session_start_lifecycle(self) -> None:
        for source in SESSION_SOURCES:
            with self.subTest(source=source):
                payload = self.harness.payloads.session_start(
                    source=source,
                    session_id="same-lifecycle-session",
                )
                self.assert_injection(self.harness.run(HOOK, payload), "SessionStart")

        # Reusing one session still injects because this hook has no marker state.
        repeated = self.harness.payloads.session_start(
            source="resume", session_id="same-lifecycle-session"
        )
        self.assert_injection(self.harness.run(HOOK, repeated), "SessionStart")

    def test_uses_payload_event_for_subagent_start_and_camel_case(self) -> None:
        subagent_payload = {
            "session_id": "session-1",
            "agent_id": "agent-1",
            "agent_type": "agent-kit:builder",
            "hook_event_name": "SubagentStart",
        }
        self.assert_injection(
            self.harness.run(HOOK, subagent_payload), "SubagentStart"
        )

        camel_payload = {"session_id": "session-2", "hookEventName": "FutureEvent"}
        self.assert_injection(self.harness.run(HOOK, camel_payload), "FutureEvent")

    def test_policy_file_override_expands_home(self) -> None:
        override = self.harness.home_dir / "custom-policy.md"
        override.write_text("  chính sách tùy chỉnh 🚀  \n", encoding="utf-8")
        result = self.harness.run(
            HOOK,
            self.harness.payloads.session_start(),
            env={"POLICY_FILE": "~/custom-policy.md"},
        )
        self.assert_injection(result, "SessionStart", "chính sách tùy chỉnh 🚀")

    def test_missing_empty_invalid_utf8_and_oversized_policy_are_silent(self) -> None:
        cases = {
            "missing": self.harness.root / "missing.md",
            "empty": self.harness.root / "empty.md",
            "invalid-utf8": self.harness.root / "invalid.md",
            "oversized": self.harness.root / "oversized.md",
        }
        cases["empty"].write_bytes(b"  \n\t")
        cases["invalid-utf8"].write_bytes(b"policy:\xff\xfe")
        cases["oversized"].write_bytes(b"x" * (128 * 1024 + 1))

        payload = self.harness.payloads.session_start()
        for name, path in cases.items():
            with self.subTest(case=name):
                result = self.harness.run(
                    HOOK, payload, env={"POLICY_FILE": str(path)}
                )
                self.assert_silent_success(result)

    def test_invalid_json_non_object_and_missing_event_are_silent(self) -> None:
        for name, raw in (
            ("invalid-json", b"{not-json"),
            ("list", b"[]"),
            ("string", b'"SessionStart"'),
            ("invalid-stdin-utf8", b"\xff\xfe"),
        ):
            with self.subTest(case=name):
                completed = self.run_raw(raw)
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, b"")
                self.assertEqual(completed.stderr, b"")

        self.assert_silent_success(self.harness.run(HOOK, {}))
        self.assert_silent_success(
            self.harness.run(HOOK, {"hook_event_name": ["SessionStart"]})
        )

    def test_off_disables_hook_without_reading_or_writing(self) -> None:
        before = self.fixture_files()
        result = self.harness.run(
            HOOK,
            self.harness.payloads.session_start(),
            env={"POLICY_HOOK": " OFF ", "POLICY_FILE": "~/missing.md"},
        )
        self.assert_silent_success(result)
        self.assertEqual(self.fixture_files(), before)

    def test_session_ids_cannot_create_state_or_escape_directories(self) -> None:
        before = self.fixture_files()
        session_ids = (
            "../../../../outside-state",
            "/tmp/agent-kit-policy-escape",
            "phiên/路径/🚀/../marker",
        )
        for session_id in session_ids:
            payload = self.harness.payloads.session_start(session_id=session_id)
            self.assert_injection(self.harness.run(HOOK, payload), "SessionStart")
        self.assertEqual(self.fixture_files(), before)

    def test_32_concurrent_invocations_each_emit_one_valid_json_document(self) -> None:
        calls = [
            HookCall(
                HOOK,
                self.harness.payloads.session_start(
                    source=SESSION_SOURCES[index % len(SESSION_SOURCES)],
                    session_id=f"concurrent-{index}-🚀",
                ),
            )
            for index in range(32)
        ]
        results = self.harness.run_concurrent(calls, max_workers=32)
        self.assertEqual(len(results), 32)
        for result in results:
            self.assert_injection(result, "SessionStart")
            # ``require_json`` above verifies the entire stdout is one document.
            json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
