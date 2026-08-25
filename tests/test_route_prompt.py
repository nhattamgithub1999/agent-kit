"""Regression tests for the UserPromptSubmit routing hook."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unicodedata
import unittest
from typing import Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "route-prompt.py"

ROUTE_MARKERS = {
    "EXPLORE": "lớp TRA CỨU",
    "BUILD": "lớp IMPLEMENT",
    "DESIGN": "lớp THIẾT KẾ",
    "CODE_REVIEW": "lớp CODE_REVIEW",
    "REVIEW": "lớp PHẢN BIỆN",
    "AMBIGUOUS": "Chưa xác định được prompt này thuộc lớp nào",
}


class HookResult:
    def __init__(self, completed: subprocess.CompletedProcess) -> None:
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr

    def json_output(self) -> dict:
        return json.loads(self.stdout)


def run_hook(
    payload: object = None,
    *,
    raw_stdin: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> HookResult:
    child_env = os.environ.copy()
    child_env.pop("ROUTE_MIN_CHARS", None)
    if env:
        child_env.update(env)
    stdin = raw_stdin if raw_stdin is not None else json.dumps(payload, ensure_ascii=False)
    completed = subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=str(ROOT),
        env=child_env,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return HookResult(completed)


class RoutePromptTests(unittest.TestCase):
    def assert_route(self, prompt: str, expected: str, **kwargs: object) -> dict:
        result = run_hook({"prompt": prompt}, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        output = result.json_output()
        self.assertEqual(set(output), {"hookSpecificOutput"})
        hook_output = output["hookSpecificOutput"]
        self.assertEqual(
            set(hook_output), {"hookEventName", "additionalContext"}
        )
        self.assertEqual(hook_output["hookEventName"], "UserPromptSubmit")
        self.assertIn(ROUTE_MARKERS[expected], hook_output["additionalContext"])
        return output

    def assert_no_output(self, result: HookResult) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_classification_table_vietnamese_and_english(self) -> None:
        cases = (
            ("Review hooks/route-prompt.py rồi sửa lỗi giúp tôi", "BUILD"),
            ("Audit repository security and fix every vulnerability", "BUILD"),
            (
                "Review code in hooks/route-prompt.py for security issues",
                "CODE_REVIEW",
            ),
            ("Rà soát bảo mật của repo agent-kit", "CODE_REVIEW"),
            ("Phản biện lập luận trong câu trả lời này", "REVIEW"),
            ("Critique this architecture design and reasoning", "REVIEW"),
            ("Thiết kế kiến trúc caching cho ứng dụng", "DESIGN"),
            ("Design a schema for the event service", "DESIGN"),
            ("Tìm nơi cấu hình route trong repository", "EXPLORE"),
            ("Locate the routing configuration", "EXPLORE"),
            ("Hãy giúp tôi xử lý yêu cầu này", "AMBIGUOUS"),
        )
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                self.assert_route(prompt, expected)

    def test_precedence_build_then_code_review_then_logic_review(self) -> None:
        cases = (
            (
                "Critique the plan for hooks/route-prompt.py and implement fixes",
                "BUILD",
            ),
            (
                "Review the reasoning and code in hooks/route-prompt.py",
                "CODE_REVIEW",
            ),
            ("Review the reasoning in this answer", "REVIEW"),
        )
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                self.assert_route(prompt, expected)

    def test_negated_write_verbs_route_review_as_read_only(self) -> None:
        cases = (
            ("Review code này, không sửa file", "CODE_REVIEW"),
            ("Audit repository security, đừng sửa gì", "CODE_REVIEW"),
            ("review only, do not edit", "CODE_REVIEW"),
            ("REVIEW CODE; DO NOT EDIT!", "CODE_REVIEW"),
            ("Rà soát module này — KHÔNG ĐƯỢC PHÉP chỉnh sửa.", "CODE_REVIEW"),
            ("Audit this file without editing", "CODE_REVIEW"),
            ("Check the repository, no code changes", "CODE_REVIEW"),
            ("Critique this reasoning; do not edit the answer", "REVIEW"),
        )
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                self.assert_route(prompt, expected)

    def test_fix_recommendations_with_read_only_constraint_stay_review(self) -> None:
        cases = (
            "Review code và đề xuất cách sửa, không implement",
            "Review repository and suggest a fix, do not implement",
            "Rà soát module, khuyến nghị sửa lỗi nhưng đừng sửa file",
            "Review module and recommend changes, no implementation",
            "Audit code and propose a fix without applying it",
            "Rà soát code, đề xuất sửa; không thay đổi file",
            "Rà soát module, khuyến nghị cách khắc phục; không implement",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_route(prompt, "CODE_REVIEW")

    def test_fix_recommendation_followed_by_positive_action_still_builds(self) -> None:
        cases = (
            "Review code, đề xuất cách sửa rồi implement luôn",
            "Review repository and suggest a fix, then implement it",
            "Audit the module and propose changes; apply them",
            "Rà soát module, khuyến nghị cách khắc phục rồi sửa file",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_route(prompt, "BUILD")

    def test_recommendation_with_unresolved_action_conflict_is_ambiguous(self) -> None:
        cases = (
            "Review code, đề xuất cách sửa rồi sửa file nhưng đừng thay đổi gì",
            "Audit module; recommend changes and apply them but do not edit files",
            "Review repository, suggest a fix and implement it but no code changes",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_route(prompt, "AMBIGUOUS")

    def test_not_only_and_positive_write_in_another_clause_still_build(self) -> None:
        cases = (
            "không chỉ review, hãy sửa hooks/route-prompt.py",
            "do not only review; fix it",
            "Review code, do not edit it; update the documentation",
            "Audit only, no code changes; please add a report",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_route(prompt, "BUILD")

    def test_unresolved_same_clause_write_read_only_conflict_is_ambiguous(self) -> None:
        cases = (
            "Review and fix this code but do not edit files",
            "Audit rồi sửa module này without changing anything",
            "Please edit this file but don't change it",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_route(prompt, "AMBIGUOUS")

    def test_negation_does_not_reach_a_distant_positive_write(self) -> None:
        cases = (
            "Không review phần mô tả quá dài rồi sửa hooks/route-prompt.py",
            "Do not review the old design notes before fix hooks/route-prompt.py",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assert_route(prompt, "BUILD")

    def test_at_file_is_actionable_and_ack_prefix_does_not_skip(self) -> None:
        self.assert_route(
            "@hooks/route-prompt.py review lỗi bảo mật", "CODE_REVIEW"
        )
        self.assert_route("ok, giờ fix hooks/route-prompt.py", "BUILD")

    def test_only_full_acknowledgements_are_skipped(self) -> None:
        for prompt in (" ok ", "Cảm ơn!", "Thanks.", "ĐÃ RÕ…", "Got it"):
            with self.subTest(prompt=prompt):
                self.assert_no_output(run_hook({"prompt": prompt}))

    def test_unicode_is_normalised_before_classification(self) -> None:
        decomposed = unicodedata.normalize(
            "NFD", "Triển khai thay đổi cho hooks/route-prompt.py"
        )
        self.assert_route(decomposed, "BUILD")

    def test_min_chars_exact_boundary(self) -> None:
        self.assert_no_output(
            run_hook({"prompt": "abcd"}, env={"ROUTE_MIN_CHARS": "5"})
        )
        self.assert_route("abcde", "AMBIGUOUS", env={"ROUTE_MIN_CHARS": "5"})

    def test_invalid_negative_and_oversized_min_chars_are_safe(self) -> None:
        # Invalid values fall back to 12.
        self.assert_no_output(
            run_hook({"prompt": "short"}, env={"ROUTE_MIN_CHARS": "invalid"})
        )
        # Negative values clamp to zero.
        self.assert_route("abc", "AMBIGUOUS", env={"ROUTE_MIN_CHARS": "-10"})
        # Oversized values clamp to 4096, including that exact boundary.
        self.assert_no_output(
            run_hook({"prompt": "x" * 4095}, env={"ROUTE_MIN_CHARS": "999999"})
        )
        self.assert_route(
            "x" * 4096, "AMBIGUOUS", env={"ROUTE_MIN_CHARS": "999999"}
        )

    def test_malformed_or_missing_payload_fails_open(self) -> None:
        cases = (
            {"raw_stdin": ""},
            {"raw_stdin": "{"},
            {"payload": []},
            {"payload": "prompt"},
            {"payload": {}},
            {"payload": {"prompt": "   "}},
            {"payload": {"prompt": 123}},
        )
        for case in cases:
            with self.subTest(case=case):
                if "raw_stdin" in case:
                    result = run_hook(raw_stdin=case["raw_stdin"])
                else:
                    result = run_hook(case["payload"])
                self.assert_no_output(result)

    def test_supported_alternate_prompt_key_uses_structured_output(self) -> None:
        result = run_hook({"user_prompt": "Please implement the requested change"})
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.json_output()
        self.assertEqual(
            output["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit"
        )
        self.assertIn(
            ROUTE_MARKERS["BUILD"],
            output["hookSpecificOutput"]["additionalContext"],
        )


if __name__ == "__main__":
    unittest.main()
