"""Regression tests for the deterministic glossary gate."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import unittest
from typing import Mapping, Optional

from hooks._shared import hash_value
from tests.support import HookCall, HookHarness, HookResult, REPO_ROOT


HOOK = "gloss-gate.py"


def _load_gloss_gate_module():
    """Import the hook's real extractor in-process for a precise corpus check.

    Every other test in this file exercises the hook through the documented
    subprocess boundary (see ``tests/support.py``). This one helper is used
    only so the corpus test below can call ``_extract_definitions`` directly
    and inspect *which character precedes a matched token*, which stderr
    prose cannot answer precisely. The module file uses a hyphenated name, so
    it is loaded by path rather than by a normal ``import`` statement.
    """

    hooks_dir = REPO_ROOT / "hooks"
    hooks_dir_str = str(hooks_dir)
    inserted = hooks_dir_str not in sys.path
    if inserted:
        sys.path.insert(0, hooks_dir_str)
    try:
        spec = importlib.util.spec_from_file_location(
            "gloss_gate_module_under_test", hooks_dir / "gloss-gate.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if inserted:
            sys.path.remove(hooks_dir_str)


class GlossGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = HookHarness(timeout=20)

    def tearDown(self) -> None:
        self.harness.close()

    def _environment(
        self, overrides: Optional[Mapping[str, str]] = None
    ) -> dict[str, str]:
        values = {"CLAUDE_PROJECT_DIR": ""}
        if overrides:
            values.update(overrides)
        return values

    def run_message(
        self,
        message: str,
        *,
        env: Optional[Mapping[str, str]] = None,
        payload: Optional[dict] = None,
        cwd: Optional[pathlib.Path] = None,
    ) -> HookResult:
        event = payload or self.harness.payloads.stop(message)
        return self.harness.run(
            HOOK,
            event,
            env=self._environment(env),
            cwd=cwd,
        )

    def write_home_glossary(self, text: str) -> pathlib.Path:
        path = self.harness.home_dir / ".claude" / "glossary.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_project_glossary(self, text: str) -> pathlib.Path:
        path = self.harness.project_dir / ".claude" / "glossary.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def assert_passes(self, result: HookResult) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def assert_blocks(self, result: HookResult, token: str) -> None:
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("BLOCKED bởi gloss-gate", result.stderr)
        self.assertIn(token, result.stderr)

    def test_all_supported_definition_forms_require_exact_glossary_meaning(self) -> None:
        self.write_home_glossary(
            "ABC = Alpha Beta Charlie\n"
            "DEF = Delta Echo Foxtrot\n"
            "GHI = Gamma Hotel India\n"
            "JKL = Juliet Kilo Lima\n"
        )
        accepted = (
            "ABC (Alpha Beta Charlie)\n"
            "DEF=Delta Echo Foxtrot\n"
            "GHI là Gamma Hotel India\n"
            "JKL - Juliet Kilo Lima"
        )
        self.assert_passes(self.run_message(accepted))

        wrong_forms = (
            "ABC (Apple Boat Cat)",
            "DEF = Different Exact Form",
            "GHI là Great Hotel India",
            "JKL — Just Kidding Later",
        )
        for message in wrong_forms:
            with self.subTest(message=message):
                self.assert_blocks(self.run_message(message), message[:3])

    def test_same_initials_and_accent_changes_do_not_count_as_exact_matches(self) -> None:
        self.write_home_glossary(
            "ABC = Alpha Beta Charlie\nXYZ = Cơ sở dữ liệu\n"
        )
        self.assert_blocks(self.run_message("ABC = Apple Boat Cat"), "ABC")
        self.assert_blocks(self.run_message("XYZ = Co sở dữ liệu"), "XYZ")

        exact_after_nfc_casefold_and_space_collapse = "XYZ = CƠ   SỞ DỮ LIỆU"
        self.assert_passes(self.run_message(exact_after_nfc_casefold_and_space_collapse))

    def test_home_is_authoritative_and_project_cannot_override(self) -> None:
        self.write_home_glossary("ABC = Alpha Beta Charlie\n")
        self.write_project_glossary("ABC = Apple Boat Cat\n")
        result = self.run_message("Không có explicit definition trong lượt này.")
        self.assert_blocks(result, "ABC")
        self.assertIn("mâu thuẫn", result.stderr)

        warned = self.run_message(
            "Không có explicit definition trong lượt này.",
            env={"GLOSS_GATE": "warn"},
        )
        self.assert_passes(warned)

    def test_normalized_duplicate_is_allowed_and_project_can_add_new_token(self) -> None:
        self.write_home_glossary("ABC = Cơ sở dữ liệu\n")
        self.write_project_glossary(
            "ABC = CƠ   SỞ DỮ LIỆU\nXYZ = Xylophone Yield Zone\n"
        )
        self.assert_passes(
            self.run_message("ABC = cơ sở dữ liệu\nXYZ = Xylophone Yield Zone")
        )

    def test_valid_local_citation_allows_unknown_definition(self) -> None:
        docs = self.harness.project_dir / "docs"
        docs.mkdir()
        source = docs / "terms.md"
        source.write_text(
            "The approved XYZ meaning is Xylophone Yield Zone.\n",
            encoding="utf-8",
        )
        result = self.run_message(
            "XYZ = Xylophone Yield Zone (nguồn: `docs/terms.md:1`)"
        )
        self.assert_passes(result)
        self.assert_passes(
            self.run_message("XYZ = Xylophone Yield Zone (docs/terms.md:1)")
        )

    def test_multiple_definitions_on_one_line_cannot_hide_a_later_violation(self) -> None:
        self.write_home_glossary("ABC = Alpha Beta Charlie\n")
        result = self.run_message(
            "ABC = Alpha Beta Charlie; XYZ = Xylophone Yield Zone"
        )
        self.assert_blocks(result, "XYZ")

    def test_citation_must_resolve_inside_project_and_match_line(self) -> None:
        docs = self.harness.project_dir / "docs"
        docs.mkdir()
        source = docs / "terms.md"
        source.write_text(
            "XYZ = A different meaning\nXYZ = Xylophone Yield Zone\n",
            encoding="utf-8",
        )
        outside = self.harness.root / "outside.md"
        outside.write_text("XYZ = Xylophone Yield Zone\n", encoding="utf-8")

        invalid_messages = (
            "XYZ = Xylophone Yield Zone theo tài liệu",
            "XYZ = Xylophone Yield Zone (nguồn docs/terms.md)",
            "XYZ = Xylophone Yield Zone (`missing.md:1`)",
            "XYZ = Xylophone Yield Zone (`../outside.md:1`)",
            "XYZ = Xylophone Yield Zone (`{}:1`)".format(outside),
            "XYZ = Xylophone Yield Zone (`docs/terms.md:99`)",
            "XYZ = Xylophone Yield Zone (`docs/terms.md:1`)",
        )
        for message in invalid_messages:
            with self.subTest(message=message):
                self.assert_blocks(self.run_message(message), "XYZ")

        self.assert_passes(
            self.run_message("XYZ = Xylophone Yield Zone (`docs/terms.md:2`)")
        )

    def test_source_words_and_code_fences_are_not_evidence(self) -> None:
        cases = (
            "XYZ = Xylophone Yield Zone theo tài liệu",
            "XYZ = Xylophone Yield Zone, nguồn nội bộ",
            "```text\nXYZ = Xylophone Yield Zone\n```",
        )
        for message in cases:
            with self.subTest(message=message):
                self.assert_blocks(self.run_message(message), "XYZ")

    def test_known_and_kit_tokens_are_only_exempt_when_bare(self) -> None:
        self.write_home_glossary("API = Giao diện lập trình\n")
        self.assert_passes(
            self.run_message(
                "API READY HEAD HTTP 200. VERDICT: READY. Giữ nguyên NDVLDTT."
            )
        )
        self.assert_blocks(
            self.run_message("API = Application Programming Interface"), "API"
        )
        self.assert_blocks(self.run_message("READY = Release is done"), "READY")

    def test_unknown_marker_is_not_parsed_as_a_definition(self) -> None:
        self.assert_passes(
            self.run_message("Giữ nguyên [CHƯA RÕ: ABC] và hỏi người dùng.")
        )

    def test_comparison_operators_labels_and_codes_are_not_definitions(self) -> None:
        report = (
            "HEAD == local HEAD\n"
            "API => request\n"
            "READY != false\n"
            "HTTP >= 200\n"
            "HTTP <= 599\n"
            "ABC := value\n"
            "VERDICT: READY\n"
            "BLOCK: reason\n"
            "HTTP 200"
        )
        self.assert_passes(self.run_message(report))
        self.assert_blocks(self.run_message("HEAD = local HEAD"), "HEAD")

    def test_only_last_assistant_message_is_read_and_camel_case_is_supported(self) -> None:
        self.harness.payloads.transcript_path.write_text(
            '{"message":"ABC = Apple Boat Cat"}\n', encoding="utf-8"
        )
        clean = self.harness.payloads.stop("Giữ nguyên [CHƯA RÕ: ABC].")
        clean["nested_noise"] = {"message": "ABC = Apple Boat Cat"}
        self.assert_passes(self.run_message("", payload=clean))

        no_last_message = {
            "transcript_path": str(self.harness.payloads.transcript_path),
            "report": "ABC = Apple Boat Cat",
        }
        self.assert_passes(self.run_message("", payload=no_last_message))

        camel = {
            "cwd": str(self.harness.project_dir),
            "lastAssistantMessage": "ABC = Apple Boat Cat",
        }
        self.assert_blocks(self.run_message("", payload=camel), "ABC")

        message_object = {
            "cwd": str(self.harness.project_dir),
            "last_assistant_message": {
                "content": [{"type": "text", "text": "ABC = Apple Boat Cat"}]
            },
        }
        self.assert_blocks(self.run_message("", payload=message_object), "ABC")

    def test_malformed_json_and_missing_message_fail_open(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(self.harness.repo_root / "hooks" / HOOK)],
            input="{not-json",
            text=True,
            capture_output=True,
            cwd=str(self.harness.project_dir),
            env=self.harness.environment(self._environment()),
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

        invalid_payloads = (None, [], "ABC = Apple Boat Cat", {"other": 42})
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                completed = subprocess.run(
                    [sys.executable, str(self.harness.repo_root / "hooks" / HOOK)],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    cwd=str(self.harness.project_dir),
                    env=self.harness.environment(self._environment()),
                    timeout=10,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout, "")
                self.assertEqual(completed.stderr, "")

    def test_stop_hook_active_still_blocks_until_the_attempt_cap(self) -> None:
        """A repeat pass re-validates; re-emitting the same text is not a bypass.

        The old behaviour returned 0 on the first repeat, so an agent could take
        the block, resend the identical message and walk straight through. Now
        the repeat is validated like any other pass, and only the attempt cap
        releases it - loudly, on stderr.
        """

        factories = (
            self.harness.payloads.stop,
            self.harness.payloads.subagent_stop,
        )
        truthy_values = (True, 1, "1", "true", " YES ", "on")
        for factory in factories:
            event_name = factory.__name__
            for field_name in ("stop_hook_active", "stopHookActive"):
                for value in truthy_values:
                    with self.subTest(
                        event=event_name, field=field_name, value=value
                    ):
                        payload = factory("ABC = Apple Boat Cat")
                        payload.pop("stop_hook_active", None)
                        payload[field_name] = value
                        # Each case is an independent situation, so give it its
                        # own session: the repeat counter is scoped per session
                        # and would otherwise reach the cap mid-loop.
                        payload["session_id"] = "repeat-{}-{}-{}".format(
                            event_name, field_name, value
                        )
                        self.assert_blocks(
                            self.run_message("", payload=payload), "ABC"
                        )

    def test_repeat_attempt_cap_releases_loudly_instead_of_deadlocking(self) -> None:
        payload = self.harness.payloads.stop("ABC = Apple Boat Cat")
        payload["stop_hook_active"] = True
        env = {"GLOSS_REPEAT_CAP": "2"}

        first = self.run_message("", payload=payload, env=env)
        self.assertEqual(first.returncode, 2, first.stderr)

        second = self.run_message("", payload=payload, env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("attempt cap", second.stderr)
        self.assertIn("ABC", second.stderr)

    def test_false_or_missing_stop_hook_active_still_blocks(self) -> None:
        factories = (
            self.harness.payloads.stop,
            self.harness.payloads.subagent_stop,
        )
        falsey_values = (False, 0, "0", "false", "off", "", None)
        for factory in factories:
            event_name = factory.__name__
            for field_name in ("stop_hook_active", "stopHookActive"):
                for value in falsey_values:
                    with self.subTest(
                        event=event_name, field=field_name, value=value
                    ):
                        payload = factory("ABC = Apple Boat Cat")
                        payload.pop("stop_hook_active", None)
                        if value is not None:
                            payload[field_name] = value
                        self.assert_blocks(
                            self.run_message("", payload=payload), "ABC"
                        )

    def test_modes_and_minimum_length_environment_are_safe(self) -> None:
        for mode, expected in (("block", 2), ("warn", 0), ("off", 0), ("bad", 2)):
            with self.subTest(mode=mode):
                result = self.run_message(
                    "ABC = Apple Boat Cat", env={"GLOSS_GATE": mode}
                )
                self.assertEqual(result.returncode, expected, result.stderr)

        invalid = self.run_message(
            "ABC = Apple Boat Cat", env={"GLOSS_MIN_LEN": "not-an-int"}
        )
        self.assert_blocks(invalid, "ABC")

        clamped_low = self.run_message(
            "AB = Alpha Beta", env={"GLOSS_MIN_LEN": "-999"}
        )
        self.assert_blocks(clamped_low, "AB")

        long_token = "A" * 32
        clamped_high = self.run_message(
            "{} = unsupported meaning".format(long_token),
            env={"GLOSS_MIN_LEN": "999"},
        )
        self.assert_blocks(clamped_high, long_token)
        self.assert_passes(
            self.run_message(
                "ABC = ignored at configured minimum",
                env={"GLOSS_MIN_LEN": "999"},
            )
        )

    def test_claude_project_dir_wins_when_payload_cwd_is_a_child(self) -> None:
        self.write_project_glossary("ABC = Alpha Beta Charlie\n")
        child = self.harness.project_dir / "nested" / "child"
        child.mkdir(parents=True)
        payload = self.harness.payloads.stop(
            "ABC = Alpha Beta Charlie", cwd=str(child)
        )

        self.assert_blocks(
            self.run_message("", payload=payload, cwd=child),
            "ABC",
        )
        self.assert_passes(
            self.run_message(
                "",
                payload=payload,
                cwd=child,
                env={"CLAUDE_PROJECT_DIR": str(self.harness.project_dir)},
            )
        )

    def test_concurrent_blocks_are_deterministic_and_log_is_private_jsonl(self) -> None:
        payload = self.harness.payloads.stop("ABC = Apple Boat Cat")
        calls = [
            HookCall(
                HOOK,
                payload,
                env=self._environment(),
            )
            for _ in range(32)
        ]
        results = self.harness.run_concurrent(calls, max_workers=32)
        self.assertEqual([result.returncode for result in results], [2] * 32)
        self.assertEqual(len({result.stderr for result in results}), 1)

        log_path = (
            self.harness.plugin_data_dir / "agent-kit" / "agent-kit.log"
        )
        lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 32)
        records = [json.loads(line) for line in lines]
        expected_event_hash = hash_value(
            "gloss_gate_violation", "log_event"
        )
        self.assertEqual(
            [record["event_hash"] for record in records],
            [expected_event_hash] * 32,
        )
        self.assertTrue(all("event" not in record for record in records))
        log_text = log_path.read_text(encoding="utf-8")
        self.assertNotIn("gloss_gate_violation", log_text)
        self.assertNotIn("Apple Boat Cat", log_text)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

    def test_underscore_joined_identifier_does_not_split_into_a_bare_tail_token(
        self,
    ) -> None:
        """A `_`-joined identifier must not be torn into a standalone token.

        Before the token-boundary fix, the segment after the last underscore
        of an all-caps identifier was recognized as its own token, so an
        assignment of the *whole* identifier (for example a Python constant
        declaration) was misread as an explicit definition of just the tail.
        """
        self.assert_passes(
            self.run_message("MUTATION_TOOLS = frozenset chứa Bash")
        )

    def test_underscore_citation_shorthand_is_not_treated_as_a_definition(
        self,
    ) -> None:
        """A shortened `NAME_WITH_UNDERSCORE:line` citation is not a definition.

        Before the fix, the capitalized segment after the underscore was
        recognized as a bare token and the trailing `:<digits>` was read as
        the extended colon form, turning a citation shorthand into a
        fabricated-looking definition.
        """
        self.assert_passes(self.run_message("MINIMUM_CLAUDE_CODE:64"))

    def test_known_bare_tokens_still_blocked_via_strict_paren_and_dash_forms(
        self,
    ) -> None:
        """The underscore fix must not loosen the four strict forms.

        `PASS` and `CAP` are members of ``KNOWN_BARE_TOKENS``. The design
        intent documented at :41-46 is that this vocabulary is only exempt
        from the *extended* forms (colon, arrow, table row); the four strict
        forms - parenthesis, `=`, "là", dash - still apply to every token,
        known or not. This pins that an explicit meaning attached to a known
        status word is still an unproven definition and must stay blocked.
        """
        self.assert_blocks(self.run_message("PASS (exit 0)"), "PASS")
        self.assert_blocks(self.run_message("CAP — gồm cả 4 lệnh"), "CAP")

    def test_fabricated_definition_without_underscore_is_still_blocked(
        self,
    ) -> None:
        """The fix is scoped to the underscore boundary, not a general loosening.

        An unresolved business acronym paired with a made-up meaning in plain
        prose has no underscore and no known-token membership, so it must
        still be rejected for lack of a glossary entry or citation.
        """
        self.assert_blocks(
            self.run_message(
                "NDVLDTT = Nghiệp vụ đối soát liên doanh tự tạo"
            ),
            "NDVLDTT",
        )

    def test_repo_corpus_has_no_underscore_tail_token_extracted_as_a_definition(
        self,
    ) -> None:
        """Narrow regression net for exactly the class of bug this patch fixes.

        Running this same real extractor over this real corpus before the
        token-boundary fix surfaced genuine false positives, unrelated to any
        single fixture string: the segment after the last underscore of an
        all-caps identifier (for example the tail of a Python-style constant
        assignment, or the tail of a shortened `NAME_WITH_UNDERSCORE:line`
        citation) was extracted as its own definition. This asserts that
        class is gone from the real corpus by calling the real extractor
        directly, instead of re-typing the original fixture strings.

        This corpus is known to still trip the four strict/extended forms in
        unrelated ways: a status code paired with an explicit meaning, a
        labeled acronym followed by a description, a bare identifier used as
        a worked example next to a parenthetical. That is a different class
        of finding, the strict/extended forms and `KNOWN_BARE_TOKENS`
        behaving exactly as designed, and fixing it would mean loosening
        those forms or rewriting the documentation itself - neither of which
        this patch does. So this test deliberately does not assert the
        corpus is free of every gate violation; it only locks the
        underscore-tail class this patch actually fixes.
        """
        module = _load_gloss_gate_module()
        repo_root = self.harness.repo_root
        paths = [repo_root / "README.md", repo_root / "policy" / "delegation.md"]
        paths.extend(sorted((repo_root / "agents").glob("*.md")))
        paths.extend(sorted((repo_root / "skills").glob("**/SKILL.md")))
        self.assertTrue(paths, "corpus file list must not be empty")
        for path in paths:
            relative = path.relative_to(repo_root)
            with self.subTest(path=str(relative)):
                text = path.read_text(encoding="utf-8")
                definitions = module._extract_definitions(text, 3)
                offenders = [
                    definition
                    for definition in definitions
                    if definition.start > 0 and text[definition.start - 1] == "_"
                ]
                self.assertEqual(
                    [item.token for item in offenders],
                    [],
                    "{}: underscore-tail token(s) still extracted as a "
                    "definition".format(relative),
                )


if __name__ == "__main__":
    unittest.main()
