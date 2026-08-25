#!/usr/bin/env python3
"""Read-only, stdlib-only static checks for the agent-kit repository."""

from __future__ import annotations

import ast
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Mapping, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
HOOKS_ROOT = ROOT / "hooks"
if str(HOOKS_ROOT) not in sys.path:
    # The checker is executed as ``python3 tests/static_check.py``; bootstrap
    # only the pure shared primitives instead of importing a hook executable.
    sys.path.insert(0, str(HOOKS_ROOT))

from _shared import (  # noqa: E402 - ROOT must be known before bootstrap.
    InvalidVerificationContract,
    VERIFICATION_STEPS,
    VerificationContract,
    validate_verification_contract,
)


SOURCE_DIRECTORIES = (Path("hooks"), Path("tests"))
JSON_FILES = (
    Path(".claude-plugin/plugin.json"),
    Path(".claude-plugin/marketplace.json"),
    Path("hooks/hooks.json"),
    Path(".claude/verification.json"),
)
VERIFICATION_FILE = Path(".claude/verification.json")
CLAUDE_VALIDATIONS = (
    ("claude", "plugin", "validate", ".", "--strict"),
    (
        "claude",
        "plugin",
        "validate",
        ".claude-plugin/plugin.json",
        "--strict",
    ),
    ("claude", "plugin", "validate", "agents", "--strict"),
    ("claude", "plugin", "validate", "skills", "--strict"),
)

# SemVer 2.0.0, expressed locally so this checker remains dependency-free.
_PRERELEASE_IDENTIFIER = (
    r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
)
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-("
    + _PRERELEASE_IDENTIFIER
    + r"(?:\."
    + _PRERELEASE_IDENTIFIER
    + r")*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class CheckFailure(Exception):
    """A static check failed with a user-facing message."""


def run(
    command: Sequence[str],
    root: Path = ROOT,
    *,
    show_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one read-only command and print its command line and exit status."""

    print(f"$ {shlex.join(command)}", flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError as exc:
        print(f"[FAIL] command not found: {command[0]}", flush=True)
        raise CheckFailure(f"command not found: {command[0]}") from exc

    if show_output and result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if show_output and result.stderr:
        print(
            result.stderr,
            end="" if result.stderr.endswith("\n") else "\n",
            file=sys.stderr,
        )

    status = "PASS" if result.returncode == 0 else "FAIL"
    print(f"[{status}] exit={result.returncode}: {shlex.join(command)}", flush=True)
    return result


def _resolved_root(root: Path) -> Path:
    try:
        resolved = Path(root).resolve(strict=True)
    except OSError as exc:
        raise CheckFailure(f"cannot resolve repository root {root}: {exc}") from exc
    if not resolved.is_dir():
        raise CheckFailure(f"repository root is not a directory: {resolved}")
    return resolved


def _relative_to_root(path: Path, root: Path, label: str) -> Path:
    """Return a display path, rejecting any path that resolves outside root."""

    try:
        return path.relative_to(root)
    except ValueError as exc:
        raise CheckFailure(f"{label} escapes repository root: {path}") from exc


def discover_python_sources(root: Path = ROOT) -> list[Path]:
    """Discover real ``*.py`` files under hooks/tests, including untracked ones.

    ``__pycache__`` is deliberately ignored. Symlinks are rejected instead of
    followed so a source cannot silently redirect the checker outside the repo.
    Returned paths are repository-relative and deterministic.
    """

    resolved_root = _resolved_root(root)
    sources: list[Path] = []

    for relative_directory in SOURCE_DIRECTORIES:
        source_root = resolved_root / relative_directory
        if source_root.is_symlink():
            raise CheckFailure(
                f"Python source directory must not be a symlink: {relative_directory}"
            )
        if not source_root.is_dir():
            raise CheckFailure(
                f"Python source directory is missing: {relative_directory}"
            )

        # Reject symlinks before selecting Python files. pathlib does not follow
        # directory symlinks during this traversal on supported Python versions,
        # so the link itself is still visible and can be rejected explicitly.
        for entry in source_root.rglob("*"):
            relative_entry = _relative_to_root(
                entry, resolved_root, "Python source entry"
            )
            if "__pycache__" in relative_entry.parts:
                continue
            if entry.is_symlink():
                raise CheckFailure(
                    "symlink is not allowed under Python source directories: "
                    f"{relative_entry.as_posix()}"
                )

        for candidate in source_root.rglob("*.py"):
            relative_candidate = _relative_to_root(
                candidate, resolved_root, "Python source"
            )
            if "__pycache__" in relative_candidate.parts:
                continue
            try:
                resolved_candidate = candidate.resolve(strict=True)
            except OSError as exc:
                raise CheckFailure(
                    f"cannot resolve Python source {relative_candidate.as_posix()}: {exc}"
                ) from exc
            _relative_to_root(resolved_candidate, resolved_root, "Python source")
            if not resolved_candidate.is_file():
                raise CheckFailure(
                    f"Python source is not a file: {relative_candidate.as_posix()}"
                )
            sources.append(relative_candidate)

    return sorted(set(sources), key=lambda path: path.as_posix())


def check_python_sources(root: Path = ROOT) -> list[Path]:
    """Parse every current Python source, not just files already tracked by Git."""

    resolved_root = _resolved_root(root)
    python_paths = discover_python_sources(resolved_root)
    for relative_path in python_paths:
        path = resolved_root / relative_path
        try:
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=relative_path.as_posix())
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise CheckFailure(f"cannot parse {relative_path.as_posix()}: {exc}") from exc

    print(
        "[PASS] ast.parse: "
        f"{len(python_paths)} Python source file(s) under hooks/tests"
    )
    return python_paths


def tracked_paths(root: Path = ROOT) -> list[Path]:
    """List tracked paths used only for repository-index-specific checks."""

    result = run(
        ("git", "ls-files", "-z", "--", "hooks", "tests"),
        root,
        show_output=False,
    )
    if result.returncode != 0:
        raise CheckFailure("cannot list Git-tracked files under hooks/tests")
    return [Path(item) for item in result.stdout.split("\0") if item]


def check_tracked_pyc(paths: Sequence[Path]) -> None:
    """Reject bytecode committed to Git without conflating it with source scan."""

    pyc_paths = sorted(
        (path for path in paths if path.suffix.casefold() == ".pyc"),
        key=lambda path: path.as_posix(),
    )
    if pyc_paths:
        joined = ", ".join(path.as_posix() for path in pyc_paths)
        raise CheckFailure(f"Git tracks forbidden *.pyc files: {joined}")
    print("[PASS] tracked *.pyc: 0 file(s)")


def _safe_repo_json_path(relative_path: Path, root: Path) -> Tuple[Path, Path]:
    """Resolve a configured JSON path while keeping it inside the repository."""

    if relative_path.is_absolute() or PureWindowsPath(str(relative_path)).drive:
        raise CheckFailure(f"JSON path must be repository-relative: {relative_path}")
    resolved = (root / relative_path).resolve(strict=False)
    relative = _relative_to_root(resolved, root, "JSON path")
    return resolved, relative


def load_json_files(
    root: Path = ROOT,
    json_files: Sequence[Path] = JSON_FILES,
) -> Dict[Path, Any]:
    """Load required JSON documents from an injectable repository root."""

    resolved_root = _resolved_root(root)
    loaded: Dict[Path, Any] = {}
    for configured_path in json_files:
        relative_path = Path(configured_path)
        path, display_path = _safe_repo_json_path(relative_path, resolved_root)
        try:
            with path.open(encoding="utf-8") as stream:
                loaded[relative_path] = json.load(stream)
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise CheckFailure(
                f"cannot load JSON {display_path.as_posix()}: {exc}"
            ) from exc
        print(f"[PASS] json.load: {display_path.as_posix()}")
    return loaded


def _require_object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CheckFailure(f"{label} must be a JSON object")
    return value


def check_verification_schema(
    document: Any, root: Path = ROOT
) -> VerificationContract:
    """Validate via the same pure contract validator used by runtime hooks."""

    try:
        contract = validate_verification_contract(document, project_dir=root)
    except InvalidVerificationContract as exc:
        # Preserve the shared reason verbatim so static/runtime verdicts cannot
        # drift while main() still renders it through the standard [FAIL] path.
        raise CheckFailure(str(exc)) from exc

    print(
        "[PASS] verification schema: version=1, "
        f"steps={list(VERIFICATION_STEPS)!r}, "
        f"null={sorted(contract.n_a_reasons)!r}"
    )
    return contract


def _check_semver(version: Any) -> str:
    if not isinstance(version, str) or SEMVER_PATTERN.fullmatch(version) is None:
        raise CheckFailure(
            f"plugin manifest version must be valid SemVer 2.0.0: {version!r}"
        )
    return version


def check_version_sync(loaded: Mapping[Path, Any]) -> None:
    """Validate manifest SemVer and its optional marketplace duplication."""

    manifest = _require_object(
        loaded[Path(".claude-plugin/plugin.json")], "plugin manifest"
    )
    marketplace = _require_object(
        loaded[Path(".claude-plugin/marketplace.json")], "marketplace"
    )

    plugin_name = manifest.get("name")
    if not isinstance(plugin_name, str) or not plugin_name.strip():
        raise CheckFailure("plugin manifest must contain a non-empty string name")
    plugin_version = _check_semver(manifest.get("version"))
    plugins = marketplace.get("plugins")
    if not isinstance(plugins, list):
        raise CheckFailure("marketplace must contain a plugins array")

    matching_plugins = [
        plugin
        for plugin in plugins
        if isinstance(plugin, dict) and plugin.get("name") == plugin_name
    ]
    if not matching_plugins:
        raise CheckFailure(f"marketplace has no plugin named {plugin_name!r}")

    declared_versions = [
        plugin["version"] for plugin in matching_plugins if "version" in plugin
    ]
    for marketplace_version in declared_versions:
        if marketplace_version != plugin_version:
            raise CheckFailure(
                "version mismatch: "
                f"manifest={plugin_version!r}, marketplace={marketplace_version!r}"
            )

    print(f"[PASS] manifest SemVer: {plugin_version!r}")
    if declared_versions:
        print(
            "[PASS] version sync: "
            f"{len(declared_versions)} marketplace version field(s) match "
            f"{plugin_version!r}"
        )
    else:
        print(
            "[PASS] version authority: marketplace version omitted; "
            "manifest is the single authority"
        )


def check_claude_validations(root: Path = ROOT) -> None:
    """Run every required strict Claude validation; absence is a hard failure."""

    if shutil.which("claude") is None:
        raise CheckFailure(
            "required executable 'claude' was not found in PATH; "
            "strict plugin validation was not run"
        )

    failures: list[str] = []
    for command in CLAUDE_VALIDATIONS:
        result = run(command, root)
        if result.returncode != 0:
            failures.append(shlex.join(command))
    if failures:
        raise CheckFailure("strict validation failed: " + "; ".join(failures))


def main(root: Path = ROOT) -> int:
    """Run all checks against ``root`` and return a process-style status code."""

    try:
        check_python_sources(root)
        check_tracked_pyc(tracked_paths(root))
        loaded = load_json_files(root)
        check_verification_schema(loaded[VERIFICATION_FILE], root)
        check_version_sync(loaded)
        check_claude_validations(root)
    except (CheckFailure, KeyError) as exc:
        detail = str(exc) if not isinstance(exc, KeyError) else f"missing JSON input: {exc}"
        print(f"[FAIL] static_check: {detail}", file=sys.stderr)
        return 1

    print("[PASS] static_check: all checks completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
