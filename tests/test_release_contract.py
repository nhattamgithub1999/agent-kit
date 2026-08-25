"""Release and CI contract tests without third-party YAML dependencies."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shlex
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
PLUGIN_MANIFEST_PATH = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_PATH = ROOT / ".claude-plugin" / "marketplace.json"
VERIFICATION_PATH = ROOT / ".claude" / "verification.json"

EXPECTED_PLUGIN_VERSION = "1.0.1"
EXPECTED_AUTHOR_EMAIL = "tambn3@fpt.com"
EXPECTED_CLAUDE_CODE_PIN = "2.1.241"
MINIMUM_CLAUDE_CODE_VERSION = "2.1.196"
EXPECTED_OPERATING_SYSTEMS = {
    "ubuntu-latest",
    "macos-latest",
    "windows-latest",
}
EXPECTED_PYTHON_VERSIONS = {"3.9", "3.13"}
EXPECTED_VERIFICATION_COMMANDS = {
    "build": ("python", "-m", "compileall", "-q", "hooks", "tests"),
    "lint": ("python", "tests/static_check.py"),
    "test": (
        "python",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_*.py",
        "-v",
    ),
}

# The release itself is intentionally fixed to 1.0.1, while this expression
# independently proves that the value is valid SemVer 2.0.0 syntax.
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _top_level_section(document: str, key: str) -> str:
    """Extract one top-level YAML section from this repository-owned subset."""

    lines = document.splitlines()
    marker = f"{key}:"
    try:
        start = lines.index(marker)
    except ValueError as exc:
        raise AssertionError(f"Missing top-level YAML key {key!r}") from exc

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith((" ", "\t", "#")):
            end = index
            break
    return "\n".join(lines[start:end])


def _job_section(document: str, job_name: str) -> str:
    """Extract a job whose key is indented exactly two spaces under jobs."""

    lines = document.splitlines()
    marker = f"  {job_name}:"
    try:
        start = lines.index(marker)
    except ValueError as exc:
        raise AssertionError(f"Missing workflow job {job_name!r}") from exc

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _keys_at_indent(section: str, spaces: int) -> set[str]:
    pattern = re.compile(rf"^ {{{spaces}}}([A-Za-z0-9_-]+):", re.MULTILINE)
    return set(pattern.findall(section))


def _indented_section(section: str, key: str, spaces: int) -> str:
    """Extract a nested mapping while retaining its original indentation."""

    lines = section.splitlines()
    marker = f"{' ' * spaces}{key}:"
    try:
        start = lines.index(marker)
    except ValueError as exc:
        raise AssertionError(f"Missing nested YAML key {key!r}") from exc

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        indentation = len(line) - len(line.lstrip(" "))
        if line.strip() and indentation <= spaces:
            end = index
            break
    return "\n".join(lines[start:end])


def _list_at_indent(section: str, key: str, spaces: int) -> list[str]:
    """Read a scalar YAML list at a known indentation level."""

    lines = section.splitlines()
    marker = f"{' ' * spaces}{key}:"
    try:
        start = lines.index(marker)
    except ValueError as exc:
        raise AssertionError(f"Missing YAML list {key!r}") from exc

    values = []
    item_prefix = " " * (spaces + 2) + "- "
    for line in lines[start + 1 :]:
        if line.startswith(item_prefix):
            values.append(line.removeprefix(item_prefix).strip().strip('"\''))
            continue
        if line.strip() and len(line) - len(line.lstrip(" ")) <= spaces:
            break
    return values


def _normalize_python_command(command: str) -> tuple[str, ...]:
    """Compare command semantics while accepting the python/python3 launcher alias."""

    tokens = shlex.split(command)
    if tokens and tokens[0] in {"python", "python3"}:
        tokens[0] = "python"
    return tuple(tokens)


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", version)
    if match is None:
        raise AssertionError(f"Expected a three-part numeric version, got {version!r}")
    return tuple(int(part) for part in match.groups())


class ReleaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.manifest = json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        cls.verification = json.loads(VERIFICATION_PATH.read_text(encoding="utf-8"))

    def test_workflow_events_and_permissions_are_least_privilege(self) -> None:
        events = _top_level_section(self.workflow, "on")
        permissions = _top_level_section(self.workflow, "permissions")

        self.assertEqual(_keys_at_indent(events, 2), {"push", "pull_request"})
        self.assertEqual(_keys_at_indent(permissions, 2), {"contents"})
        self.assertRegex(permissions, r"(?m)^  contents: read$")
        self.assertEqual(self.workflow.count("\npermissions:"), 1)
        self.assertNotIn("pull_request_target", self.workflow)
        self.assertNotIn("workflow_run", self.workflow)
        self.assertNotRegex(self.workflow, r"(?im)^\s*[A-Za-z0-9_-]+:\s*write\s*$")
        self.assertNotIn("${{ secrets.", self.workflow)
        self.assertNotIn("continue-on-error", self.workflow)
        self.assertEqual(self.workflow.count("persist-credentials: false"), 2)

    def test_behavior_job_has_the_exact_supported_matrix_and_commands(self) -> None:
        jobs = _top_level_section(self.workflow, "jobs")
        behavior = _job_section(self.workflow, "behavior")
        matrix = _indented_section(behavior, "matrix", 6)

        self.assertEqual(_keys_at_indent(jobs, 2), {"behavior", "strict"})
        self.assertRegex(behavior, r"(?m)^      fail-fast: false$")
        self.assertEqual(
            set(_list_at_indent(behavior, "os", 8)),
            EXPECTED_OPERATING_SYSTEMS,
        )
        self.assertEqual(
            set(_list_at_indent(behavior, "python-version", 8)),
            EXPECTED_PYTHON_VERSIONS,
        )
        self.assertEqual(_keys_at_indent(matrix, 8), {"os", "python-version"})
        self.assertIn("runs-on: ${{ matrix.os }}", behavior)
        self.assertIn("python-version: ${{ matrix.python-version }}", behavior)
        self.assertIn("uses: actions/checkout@v4", behavior)
        self.assertIn("uses: actions/setup-python@v5", behavior)
        self.assertIn("run: python -m compileall -q hooks tests", behavior)
        self.assertIn(
            "run: python -m unittest discover -s tests -p 'test_*.py' -v",
            behavior,
        )
        self._assert_reasonable_timeout(behavior)

    def test_strict_job_pins_official_validator_and_cannot_skip_it(self) -> None:
        strict = _job_section(self.workflow, "strict")
        install_pattern = re.compile(
            r"(?m)^\s*run: npm install --global "
            r"@anthropic-ai/claude-code@([0-9]+\.[0-9]+\.[0-9]+)$"
        )
        pins = install_pattern.findall(strict)

        self.assertEqual(pins, [EXPECTED_CLAUDE_CODE_PIN])
        self.assertGreaterEqual(
            _version_tuple(pins[0]),
            _version_tuple(MINIMUM_CLAUDE_CODE_VERSION),
        )
        self.assertRegex(
            _top_level_section(self.workflow, "env"),
            rf'(?m)^  MINIMUM_CLAUDE_CODE_VERSION: "{MINIMUM_CLAUDE_CODE_VERSION}"$',
        )
        self.assertIn("runs-on: ubuntu-latest", strict)
        self.assertIn("uses: actions/setup-python@v5", strict)
        self.assertIn("uses: actions/setup-node@v4", strict)
        self.assertRegex(strict, r'(?m)^          node-version: "18"$')
        self.assertEqual(strict.count("run: claude --version"), 1)
        self.assertEqual(strict.count("run: python tests/static_check.py"), 1)
        self.assertNotRegex(strict, r"\|\|\s*(?:true|exit\s+0)\b")
        self._assert_reasonable_timeout(strict)

    def test_release_metadata_has_one_semver_authority_and_fpt_email(self) -> None:
        version = self.manifest.get("version")
        self.assertEqual(version, EXPECTED_PLUGIN_VERSION)
        self.assertIsNotNone(SEMVER.fullmatch(version))
        self.assertEqual(
            self.manifest.get("author", {}).get("email"),
            EXPECTED_AUTHOR_EMAIL,
        )

        plugins = self.marketplace.get("plugins")
        self.assertIsInstance(plugins, list)
        matching = [
            plugin
            for plugin in plugins
            if isinstance(plugin, dict)
            and plugin.get("name") == self.manifest.get("name")
        ]
        self.assertEqual(len(matching), 1)
        self.assertNotIn("version", matching[0])
        self.assertEqual(matching[0].get("source"), "./")

    def test_machine_verification_contract_has_exact_command_semantics(self) -> None:
        contract = self.verification
        self.assertEqual(set(contract), {"version", "steps", "n_a_reasons"})
        self.assertEqual(contract["version"], 1)
        self.assertIs(type(contract["version"]), int)

        steps = contract["steps"]
        self.assertEqual(set(steps), {"build", "typecheck", "lint", "test"})
        self.assertIsNone(steps["typecheck"])
        self.assertEqual(set(contract["n_a_reasons"]), {"typecheck"})
        self.assertTrue(contract["n_a_reasons"]["typecheck"].startswith("N/A:"))

        for step_name, expected_command in EXPECTED_VERIFICATION_COMMANDS.items():
            with self.subTest(step=step_name):
                step = steps[step_name]
                self.assertEqual(set(step), {"command", "cwd"})
                self.assertEqual(_normalize_python_command(step["command"]), expected_command)
                self.assertEqual(step["cwd"], ".")
                self.assertFalse(PurePosixPath(step["cwd"]).is_absolute())
                self.assertFalse(PureWindowsPath(step["cwd"]).is_absolute())
                self.assertNotIn("..", PurePosixPath(step["cwd"]).parts)

    def _assert_reasonable_timeout(self, job: str) -> None:
        match = re.search(r"(?m)^    timeout-minutes: ([0-9]+)$", job)
        self.assertIsNotNone(match)
        timeout = int(match.group(1))
        self.assertGreaterEqual(timeout, 5)
        self.assertLessEqual(timeout, 30)


if __name__ == "__main__":
    unittest.main()
