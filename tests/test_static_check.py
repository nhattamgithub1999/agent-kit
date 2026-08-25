"""Regression tests for the repository static checker."""

from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import static_check
from _shared import InvalidVerificationContract, validate_verification_contract


class TemporaryRepositoryTestCase(unittest.TestCase):
    """Provide a disposable repository shape without touching the real checkout."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-kit-static-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.root = self.workspace / "repo"
        (self.root / "hooks").mkdir(parents=True)
        (self.root / "tests").mkdir()

    def write(self, relative_path: str, content: str) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def valid_verification() -> dict:
        return {
            "version": 1,
            "steps": {
                "build": {"command": "python3 -m compileall hooks", "cwd": "."},
                "typecheck": None,
                "lint": {"command": "python3 tests/static_check.py", "cwd": "."},
                "test": {"command": "python3 -m unittest", "cwd": "tests"},
            },
            "n_a_reasons": {
                "typecheck": "N/A: No configured static type checker"
            },
        }


class PythonSourceTests(TemporaryRepositoryTestCase):
    def test_discovers_and_parses_real_untracked_source(self) -> None:
        self.write("hooks/existing.py", "VALUE = 1\n")
        untracked = self.write("tests/new_untracked.py", "def added():\n    return 2\n")
        self.write("hooks/__pycache__/ignored.py", "this is not valid Python !!!")

        discovered = static_check.check_python_sources(self.root)

        self.assertIn(Path("hooks/existing.py"), discovered)
        self.assertIn(Path("tests/new_untracked.py"), discovered)
        self.assertNotIn(Path("hooks/__pycache__/ignored.py"), discovered)
        self.assertFalse((self.root / ".git").exists())
        self.assertTrue(untracked.is_file())

    def test_invalid_untracked_source_fails_ast_parse(self) -> None:
        self.write("hooks/valid.py", "VALUE = 1\n")
        self.write("tests/brand_new.py", "def broken(:\n")

        with self.assertRaisesRegex(
            static_check.CheckFailure, r"cannot parse tests/brand_new\.py"
        ):
            static_check.check_python_sources(self.root)

    @unittest.skipUnless(hasattr(os, "symlink"), "platform has no symlink support")
    def test_source_symlink_is_rejected(self) -> None:
        outside = self.workspace / "outside.py"
        outside.write_text("VALUE = 1\n", encoding="utf-8")
        link = self.root / "hooks" / "outside.py"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"cannot create symlink on this platform: {exc}")

        with self.assertRaisesRegex(static_check.CheckFailure, "symlink"):
            static_check.discover_python_sources(self.root)

    def test_tracked_pyc_helper_rejects_without_git_index_mutation(self) -> None:
        static_check.check_tracked_pyc(
            [Path("hooks/source.py"), Path("tests/test_source.py")]
        )
        with self.assertRaisesRegex(
            static_check.CheckFailure, r"Git tracks forbidden \*\.pyc"
        ):
            static_check.check_tracked_pyc(
                [Path("hooks/source.py"), Path("tests/__pycache__/source.pyc")]
            )
        self.assertFalse((self.root / ".git").exists())


class JsonAndVerificationSchemaTests(TemporaryRepositoryTestCase):
    def test_required_json_set_includes_machine_verification_contract(self) -> None:
        self.assertEqual(
            set(static_check.JSON_FILES),
            {
                Path(".claude-plugin/plugin.json"),
                Path(".claude-plugin/marketplace.json"),
                Path("hooks/hooks.json"),
                Path(".claude/verification.json"),
            },
        )

    def test_invalid_json_is_rejected_from_injected_root(self) -> None:
        self.write("broken.json", '{"unterminated": true')
        with self.assertRaisesRegex(
            static_check.CheckFailure, r"cannot load JSON broken\.json"
        ):
            static_check.load_json_files(self.root, (Path("broken.json"),))

    def test_valid_exact_verification_schema_passes(self) -> None:
        static_check.check_verification_schema(self.valid_verification(), self.root)

    def test_static_and_runtime_validator_have_fixture_corpus_parity(self) -> None:
        cases = []

        value = self.valid_verification()
        cases.append(("valid", value, True, None))

        value = self.valid_verification()
        value["version"] = True
        cases.append(("boolean version", value, False, "version must be integer 1"))

        value = self.valid_verification()
        value["n_a_reasons"]["typecheck"] = " N/A: leading whitespace"
        cases.append(("leading whitespace N/A", value, False, "N/A reason"))

        value = self.valid_verification()
        del value["n_a_reasons"]
        cases.append(("missing top-level key", value, False, "contract fields invalid"))

        value = self.valid_verification()
        value["extra"] = True
        cases.append(("extra top-level key", value, False, "contract fields invalid"))

        value = self.valid_verification()
        del value["steps"]["test"]
        cases.append(("missing step key", value, False, "steps fields invalid"))

        value = self.valid_verification()
        value["steps"]["lint"]["timeout"] = 5
        cases.append(("extra step key", value, False, "step lint fields invalid"))

        value = self.valid_verification()
        value["steps"]["lint"] = None
        cases.append(("null reason mismatch", value, False, "null verification steps"))

        value = self.valid_verification()
        value["n_a_reasons"]["typecheck"] = ""
        cases.append(("empty reason", value, False, "N/A reason"))

        for label, cwd, reason in (
            ("absolute cwd", "/tmp/outside", "project-relative"),
            ("escaping cwd", "../outside", "parent traversal"),
            ("missing cwd", "missing-directory", "cannot resolve inside project"),
        ):
            value = self.valid_verification()
            value["steps"]["lint"]["cwd"] = cwd
            cases.append((label, value, False, reason))

        value = self.valid_verification()
        value["steps"]["lint"] = dict(value["steps"]["build"])
        cases.append(("duplicate command cwd", value, False, "pairs must be unique"))

        for label, document, expected_valid, reason_fragment in cases:
            with self.subTest(label=label):
                try:
                    validate_verification_contract(document, project_dir=self.root)
                except InvalidVerificationContract as exc:
                    runtime = (False, str(exc))
                else:
                    runtime = (True, "")

                try:
                    static_check.check_verification_schema(document, self.root)
                except static_check.CheckFailure as exc:
                    static = (False, str(exc))
                else:
                    static = (True, "")

                self.assertEqual(runtime, static)
                self.assertEqual(runtime[0], expected_valid)
                if reason_fragment is not None:
                    self.assertIn(reason_fragment, runtime[1])

    def test_static_schema_path_delegates_once_to_shared_validator(self) -> None:
        document = self.valid_verification()
        with mock.patch.object(
            static_check,
            "validate_verification_contract",
            wraps=validate_verification_contract,
        ) as validator:
            contract = static_check.check_verification_schema(document, self.root)
        validator.assert_called_once_with(document, project_dir=self.root)
        self.assertEqual(contract.required_steps, ("build", "lint", "test"))

    def test_invalid_verification_schema_shapes_are_rejected(self) -> None:
        cases = {}

        value = self.valid_verification()
        value["unexpected"] = True
        cases["extra top-level key"] = value

        value = self.valid_verification()
        value["version"] = True
        cases["boolean version"] = value

        value = self.valid_verification()
        value["version"] = 2
        cases["unsupported version"] = value

        value = self.valid_verification()
        del value["steps"]["test"]
        cases["missing required step"] = value

        value = self.valid_verification()
        value["steps"]["deploy"] = None
        cases["extra step"] = value

        value = self.valid_verification()
        value["steps"]["lint"] = {"command": "lint"}
        cases["step missing cwd"] = value

        value = self.valid_verification()
        value["steps"]["lint"]["timeout"] = 5
        cases["step extra key"] = value

        value = self.valid_verification()
        value["steps"]["lint"]["command"] = "  "
        cases["empty command"] = value

        value = self.valid_verification()
        value["steps"]["lint"] = ["not", "an", "object"]
        cases["step wrong type"] = value

        value = self.valid_verification()
        value["n_a_reasons"] = {}
        cases["null step without reason"] = value

        value = self.valid_verification()
        value["n_a_reasons"]["lint"] = "not applicable"
        cases["reason for configured step"] = value

        value = self.valid_verification()
        value["n_a_reasons"]["typecheck"] = ""
        cases["empty reason"] = value

        value = self.valid_verification()
        value["n_a_reasons"]["typecheck"] = "No configured static type checker"
        cases["reason missing N/A prefix"] = value

        value = self.valid_verification()
        value["n_a_reasons"]["typecheck"] = "N/A:   "
        cases["reason missing explanation"] = value

        value = self.valid_verification()
        value["n_a_reasons"]["typecheck"] = " N/A: leading whitespace"
        cases["reason does not begin with N/A prefix"] = value

        for label, document in cases.items():
            with self.subTest(label=label), self.assertRaises(
                static_check.CheckFailure
            ):
                static_check.check_verification_schema(document, self.root)

    def test_active_cwd_must_be_an_existing_directory(self) -> None:
        invalid_cwds = ("missing-directory", "not-a-directory")
        self.write("not-a-directory", "plain file\n")
        for cwd in invalid_cwds:
            document = self.valid_verification()
            document["steps"]["lint"]["cwd"] = cwd
            with self.subTest(cwd=cwd), self.assertRaisesRegex(
                static_check.CheckFailure,
                "cannot resolve inside project|is not a directory",
            ):
                static_check.check_verification_schema(document, self.root)

    def test_cwd_absolute_and_traversal_escape_forms_are_rejected(self) -> None:
        invalid_cwds = (
            "../outside",
            r"..\outside",
            "/tmp/outside",
            r"\outside",
            r"C:\outside",
            r"\\server\share",
        )
        for cwd in invalid_cwds:
            document = self.valid_verification()
            document["steps"]["lint"]["cwd"] = cwd
            with self.subTest(cwd=cwd), self.assertRaisesRegex(
                static_check.CheckFailure, "cwd"
            ):
                static_check.check_verification_schema(document, self.root)

    @unittest.skipUnless(hasattr(os, "symlink"), "platform has no symlink support")
    def test_cwd_symlink_escape_is_rejected(self) -> None:
        outside = self.workspace / "outside"
        outside.mkdir()
        link = self.root / "outside-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"cannot create symlink on this platform: {exc}")

        document = self.valid_verification()
        document["steps"]["lint"]["cwd"] = "outside-link"
        with self.assertRaisesRegex(
            static_check.CheckFailure, "escapes or cannot resolve inside project"
        ):
            static_check.check_verification_schema(document, self.root)

    def test_main_renders_shared_contract_failure_with_fail_prefix(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            static_check,
            "check_python_sources",
            side_effect=static_check.CheckFailure("shared contract reason"),
        ), mock.patch("sys.stderr", stderr):
            result = static_check.main(self.root)
        self.assertEqual(result, 1)
        self.assertIn("[FAIL] static_check: shared contract reason", stderr.getvalue())


class VersionAndValidationTests(TemporaryRepositoryTestCase):
    @staticmethod
    def loaded(version: object, marketplace_version: object = "1.2.3") -> dict:
        plugin = {"name": "agent-kit"}
        if marketplace_version is not _OMITTED:
            plugin["version"] = marketplace_version
        return {
            Path(".claude-plugin/plugin.json"): {
                "name": "agent-kit",
                "version": version,
            },
            Path(".claude-plugin/marketplace.json"): {"plugins": [plugin]},
        }

    def test_manifest_accepts_semver_2_versions(self) -> None:
        versions = (
            "0.0.0",
            "1.2.3",
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-0A+build.01",
        )
        for version in versions:
            with self.subTest(version=version):
                static_check.check_version_sync(self.loaded(version, version))

    def test_manifest_rejects_invalid_semver(self) -> None:
        versions = (
            "1",
            "v1.2.3",
            "01.2.3",
            "1.02.3",
            "1.2.03",
            "1.0.0-01",
            "1.0.0-",
            "1.0.0+",
            "1.0.0+bad_identifier",
            None,
        )
        for version in versions:
            with self.subTest(version=version), self.assertRaisesRegex(
                static_check.CheckFailure, "SemVer"
            ):
                static_check.check_version_sync(self.loaded(version, version))

    def test_marketplace_version_must_match_when_present(self) -> None:
        with self.assertRaisesRegex(static_check.CheckFailure, "version mismatch"):
            static_check.check_version_sync(self.loaded("1.2.3", "1.2.4"))

    def test_marketplace_omission_reports_manifest_as_single_authority(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            static_check.check_version_sync(self.loaded("1.2.3", _OMITTED))
        self.assertIn("manifest is the single authority", stdout.getvalue())

    def test_missing_claude_binary_is_hard_failure(self) -> None:
        with mock.patch.object(static_check.shutil, "which", return_value=None):
            with self.assertRaisesRegex(
                static_check.CheckFailure,
                "strict plugin validation was not run",
            ):
                static_check.check_claude_validations(self.root)


_OMITTED = object()


if __name__ == "__main__":
    unittest.main()
