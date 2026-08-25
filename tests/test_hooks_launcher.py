"""Smoke-test the exact exec-form hook launchers declared by the plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Dict, NamedTuple, Sequence, Tuple
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HOOKS_CONFIG = ROOT / "hooks" / "hooks.json"
PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}"


class HookInvocation(NamedTuple):
    command: str
    args: Tuple[str, ...]
    timeout: int


def unique_command_hooks() -> Tuple[HookInvocation, ...]:
    """Return every unique command/args/timeout tuple without normalization."""

    document = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
    events = document.get("hooks")
    if not isinstance(events, dict):
        raise AssertionError("hooks/hooks.json must contain a hooks object")

    invocations = set()
    for event, groups in events.items():
        if not isinstance(groups, list):
            raise AssertionError("hook event {!r} must contain a list".format(event))
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise AssertionError("hook group for {!r} is invalid".format(event))
            for hook in group["hooks"]:
                if not isinstance(hook, dict) or hook.get("type") != "command":
                    raise AssertionError(
                        "every declared hook must use command type: {!r}".format(hook)
                    )
                command = hook.get("command")
                args = hook.get("args")
                timeout = hook.get("timeout")
                if not isinstance(command, str) or not command:
                    raise AssertionError("hook command must be a non-empty string")
                if not isinstance(args, list) or not all(
                    isinstance(arg, str) for arg in args
                ):
                    raise AssertionError("hook args must be a string array")
                if type(timeout) is not int or timeout <= 0:
                    raise AssertionError("hook timeout must be a positive integer")
                invocations.add(HookInvocation(command, tuple(args), timeout))
    return tuple(sorted(invocations))


def require_exact_executable(command: str) -> str:
    """Require the literal configured executable; never map python3 to python."""

    executable = shutil.which(command)
    if executable is None:
        raise AssertionError(
            "Configured hook executable {!r} is not available on PATH; "
            "install/provide that exact launcher (no python/python3 alias fallback)."
            .format(command)
        )
    return executable


def resolve_args(args: Sequence[str], root: Path = ROOT) -> Tuple[str, ...]:
    """Resolve only Claude's plugin-root variable and preserve every other byte."""

    replacement = str(root.resolve(strict=True))
    return tuple(arg.replace(PLUGIN_ROOT_TOKEN, replacement) for arg in args)


def isolated_environment(temporary_root: Path) -> Dict[str, str]:
    environment = dict(os.environ)
    plugin_data = temporary_root / "plugin-data"
    plugin_data.mkdir()
    environment.update(
        {
            "CLAUDE_PLUGIN_DATA": str(plugin_data),
            "CLAUDE_PLUGIN_ROOT": str(ROOT),
            "CLAUDE_PROJECT_DIR": str(ROOT),
            "TMPDIR": str(temporary_root),
            "TMP": str(temporary_root),
            "TEMP": str(temporary_root),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    environment.pop("DUMP", None)
    return environment


class HooksLauncherTests(unittest.TestCase):
    def test_every_unique_exec_form_hook_launches_exactly_as_declared(self) -> None:
        invocations = unique_command_hooks()
        self.assertTrue(invocations)
        scripts = set()
        canonical_root = ROOT.resolve(strict=True)

        with tempfile.TemporaryDirectory(prefix="agent-kit-launcher-") as directory:
            temporary_root = Path(directory)
            for index, invocation in enumerate(invocations):
                with self.subTest(invocation=invocation):
                    require_exact_executable(invocation.command)
                    args = resolve_args(invocation.args)

                    for literal in (invocation.command,) + args:
                        self.assertNotIn("${", literal, "unresolved runtime variable")

                    rooted_args = [
                        resolved
                        for raw, resolved in zip(invocation.args, args)
                        if PLUGIN_ROOT_TOKEN in raw
                    ]
                    self.assertEqual(len(rooted_args), 1)
                    script = Path(rooted_args[0])
                    self.assertTrue(script.is_absolute(), script)
                    canonical_script = script.resolve(strict=True)
                    canonical_script.relative_to(canonical_root)
                    self.assertTrue(canonical_script.is_file(), canonical_script)
                    self.assertEqual(canonical_script.suffix, ".py")
                    self.assertNotIn(canonical_script, scripts)
                    scripts.add(canonical_script)

                    invocation_root = temporary_root / "hook-{}".format(index)
                    invocation_root.mkdir()
                    completed = subprocess.run(
                        [invocation.command, *args],
                        input="{}",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        cwd=str(ROOT),
                        env=isolated_environment(invocation_root),
                        timeout=invocation.timeout,
                        check=False,
                        shell=False,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        "{} failed\nstdout:\n{}\nstderr:\n{}".format(
                            [invocation.command, *args],
                            completed.stdout,
                            completed.stderr,
                        ),
                    )

        self.assertEqual(
            {path.name for path in scripts},
            {
                "session-policy.py",
                "route-prompt.py",
                "plan-gate.py",
                "no-fake-pass.py",
                "gloss-gate.py",
            },
        )

    def test_missing_literal_executable_has_actionable_failure(self) -> None:
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(
                AssertionError,
                r"exact launcher \(no python/python3 alias fallback\)",
            ):
                require_exact_executable("python3")

    def test_argument_resolution_changes_only_plugin_root_token(self) -> None:
        args = (
            "${CLAUDE_PLUGIN_ROOT}/hooks/example.py",
            "--literal=python3",
            "${OTHER_VARIABLE}",
        )
        resolved = resolve_args(args)
        self.assertEqual(resolved[0], str(ROOT.resolve()) + "/hooks/example.py")
        self.assertEqual(resolved[1:], args[1:])


if __name__ == "__main__":
    unittest.main()
