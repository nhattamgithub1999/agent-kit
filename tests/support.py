"""Stdlib-only subprocess harness for agent-kit hook tests.

The harness intentionally executes hooks as separate Python processes.  This
matches Claude Code's hook boundary and prevents module globals from leaking
between events while still preserving the plugin data directory across a test
sequence.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
DEFAULT_BUILDER_AGENT = "agent-kit:builder"


class _OmitField:
    """Sentinel meaning "drop this key from the payload entirely".

    ``PayloadFactory`` builder methods give every field a product-realistic
    default so existing call sites keep behaving exactly as before. Passing a
    plain empty string only ever produced a key with an empty value, never an
    absent key, which cannot exercise "this field is missing from the event"
    behavior (e.g. a main-session event that never carries ``agent_id`` at
    all). Passing ``OMIT`` for a keyword instead removes that key from the
    built payload dict, matching a real event that never included it.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<OMIT>"


OMIT: Any = _OmitField()

# A developer's shell may contain overrides used while manually debugging a
# hook.  Tests must start from product defaults, then opt in to an override on
# the individual HookCall that needs it.
_HOOK_ENV_EXACT = {
    "DUMP",
    "GLOSS_GATE",
    "GLOSS_MIN_LEN",
    "PLAN_GATE",
    "PLAN_GATE_FREE_EDITS",
    "PLAN_GATE_PLAN_TOOLS",
    "POLICY_FILE",
    "POLICY_HOOK",
    "ROUTE_MIN_CHARS",
    "NOFAKEPASS_AGENTS",
    "NOFAKEPASS_STRICT",
}
_HOOK_ENV_PREFIXES = (
    "AGENT_KIT_",
    "GLOSS_GATE_",
    "NOFAKEPASS_",
    "PLAN_GATE_",
    "POLICY_HOOK_",
    "ROUTE_",
)


@dataclass(frozen=True)
class HookResult:
    """Captured result of one hook subprocess invocation."""

    hook_path: pathlib.Path
    payload: Mapping[str, Any]
    returncode: int
    stdout: str
    stderr: str
    json_output: Optional[Any]
    json_error: Optional[str]

    @property
    def blocked(self) -> bool:
        """Claude Code treats exit status 2 as a blocking hook decision."""

        return self.returncode == 2

    def require_json(self) -> Any:
        """Return parsed stdout, raising a useful assertion when it is invalid."""

        if self.json_error is not None:
            raise AssertionError(
                "Hook stdout is not one JSON document: "
                f"{self.json_error}; stdout={self.stdout!r}"
            )
        if self.json_output is None:
            raise AssertionError("Hook did not emit a JSON document")
        return self.json_output


@dataclass(frozen=True)
class HookCall:
    """One invocation used by :meth:`HookHarness.run_sequence`."""

    hook: Union[str, pathlib.Path]
    payload: Mapping[str, Any]
    env: Optional[Mapping[str, str]] = None
    cwd: Optional[Union[str, pathlib.Path]] = None
    timeout: Optional[float] = None


class PayloadFactory:
    """Build documented Claude Code event payloads with deterministic IDs."""

    def __init__(self, project_dir: pathlib.Path) -> None:
        self.project_dir = project_dir
        self.transcript_path = project_dir / "transcript.jsonl"
        self.transcript_path.touch()

    def _common(
        self,
        event: str,
        *,
        session_id: str = "session-1",
        prompt_id: str = "prompt-1",
        **extra: Any,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "session_id": session_id,
            "prompt_id": prompt_id,
            "transcript_path": str(self.transcript_path),
            "cwd": str(self.project_dir),
            "permission_mode": "default",
            "hook_event_name": event,
        }
        payload.update(extra)
        for key in [key for key, value in payload.items() if value is OMIT]:
            del payload[key]
        return payload

    def user_prompt_submit(
        self,
        prompt: str = "Hãy kiểm tra thay đổi này",
        **common: Any,
    ) -> Dict[str, Any]:
        return self._common("UserPromptSubmit", prompt=prompt, **common)

    def pre_tool_use(
        self,
        tool_name: str = "Write",
        tool_input: Optional[Mapping[str, Any]] = None,
        *,
        tool_use_id: str = "tool-1",
        agent_type: str = DEFAULT_BUILDER_AGENT,
        agent_id: str = "agent-1",
        **common: Any,
    ) -> Dict[str, Any]:
        return self._common(
            "PreToolUse",
            tool_name=tool_name,
            tool_input=dict(tool_input or {}),
            tool_use_id=tool_use_id,
            agent_type=agent_type,
            agent_id=agent_id,
            **common,
        )

    def post_tool_use(
        self,
        tool_name: str = "Bash",
        tool_input: Optional[Mapping[str, Any]] = None,
        tool_response: Optional[Mapping[str, Any]] = None,
        *,
        tool_use_id: str = "tool-1",
        agent_type: str = DEFAULT_BUILDER_AGENT,
        agent_id: str = "agent-1",
        **common: Any,
    ) -> Dict[str, Any]:
        return self._common(
            "PostToolUse",
            tool_name=tool_name,
            tool_input=dict(tool_input or {}),
            tool_response=dict(tool_response or {}),
            tool_use_id=tool_use_id,
            agent_type=agent_type,
            agent_id=agent_id,
            **common,
        )

    def post_tool_use_failure(
        self,
        tool_name: str = "Bash",
        tool_input: Optional[Mapping[str, Any]] = None,
        *,
        error: str = "Exit code 1",
        tool_use_id: str = "tool-1",
        agent_type: str = DEFAULT_BUILDER_AGENT,
        agent_id: str = "agent-1",
        **common: Any,
    ) -> Dict[str, Any]:
        return self._common(
            "PostToolUseFailure",
            tool_name=tool_name,
            tool_input=dict(tool_input or {}),
            error=error,
            tool_use_id=tool_use_id,
            agent_type=agent_type,
            agent_id=agent_id,
            **common,
        )

    def subagent_stop(
        self,
        last_assistant_message: str = "",
        *,
        agent_type: str = DEFAULT_BUILDER_AGENT,
        agent_id: str = "agent-1",
        **common: Any,
    ) -> Dict[str, Any]:
        return self._common(
            "SubagentStop",
            last_assistant_message=last_assistant_message,
            stop_hook_active=False,
            agent_type=agent_type,
            agent_id=agent_id,
            **common,
        )

    def stop(
        self,
        last_assistant_message: str = "",
        **common: Any,
    ) -> Dict[str, Any]:
        return self._common(
            "Stop",
            last_assistant_message=last_assistant_message,
            stop_hook_active=False,
            **common,
        )

    def session_start(self, source: str = "startup", **common: Any) -> Dict[str, Any]:
        return self._common("SessionStart", source=source, **common)

    def session_end(self, reason: str = "other", **common: Any) -> Dict[str, Any]:
        return self._common("SessionEnd", reason=reason, **common)


class HookHarness:
    """Isolated environment shared by one hook behavior test."""

    def __init__(
        self,
        repo_root: Union[str, pathlib.Path] = REPO_ROOT,
        *,
        timeout: float = 10.0,
    ) -> None:
        self.repo_root = pathlib.Path(repo_root).resolve()
        self.timeout = timeout
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="agent-kit-hook-test-"
        )
        self.root = pathlib.Path(self._temporary_directory.name)
        self.home_dir = self.root / "home"
        self.temp_dir = self.root / "tmp"
        self.plugin_data_dir = self.root / "plugin-data"
        self.project_dir = self.root / "project"
        for directory in (
            self.home_dir,
            self.temp_dir,
            self.plugin_data_dir,
            self.project_dir,
        ):
            directory.mkdir(mode=0o700)

        self.payloads = PayloadFactory(self.project_dir)
        self._base_env = self._make_base_env()

    def _make_base_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        for key in list(env):
            if key in _HOOK_ENV_EXACT or key.startswith(_HOOK_ENV_PREFIXES):
                env.pop(key, None)

        isolated = {
            "HOME": str(self.home_dir),
            "USERPROFILE": str(self.home_dir),
            "TMPDIR": str(self.temp_dir),
            "TMP": str(self.temp_dir),
            "TEMP": str(self.temp_dir),
            "CLAUDE_PLUGIN_DATA": str(self.plugin_data_dir),
            "CLAUDE_PLUGIN_ROOT": str(self.repo_root),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(isolated)
        return env

    def environment(
        self, overrides: Optional[Mapping[str, str]] = None
    ) -> Dict[str, str]:
        """Return a fresh isolated environment with explicit call overrides."""

        env = dict(self._base_env)
        if overrides:
            env.update({str(key): str(value) for key, value in overrides.items()})
        return env

    def _resolve_hook(self, hook: Union[str, pathlib.Path]) -> pathlib.Path:
        path = pathlib.Path(hook)
        if path.is_absolute():
            return path
        if len(path.parts) == 1:
            return self.repo_root / "hooks" / path
        return self.repo_root / path

    def run(
        self,
        hook: Union[str, pathlib.Path],
        payload: Mapping[str, Any],
        *,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[Union[str, pathlib.Path]] = None,
        timeout: Optional[float] = None,
    ) -> HookResult:
        """Execute one hook with JSON stdin and capture its complete result."""

        hook_path = self._resolve_hook(hook)
        completed = subprocess.run(
            [sys.executable, str(hook_path)],
            input=json.dumps(payload, ensure_ascii=False),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(pathlib.Path(cwd) if cwd is not None else self.project_dir),
            env=self.environment(env),
            timeout=self.timeout if timeout is None else timeout,
            check=False,
        )

        stdout = completed.stdout
        json_output: Optional[Any] = None
        json_error: Optional[str] = None
        if stdout.strip():
            try:
                json_output = json.loads(stdout)
            except json.JSONDecodeError as exc:
                json_error = str(exc)

        return HookResult(
            hook_path=hook_path,
            payload=dict(payload),
            returncode=completed.returncode,
            stdout=stdout,
            stderr=completed.stderr,
            json_output=json_output,
            json_error=json_error,
        )

    def run_sequence(self, calls: Iterable[HookCall]) -> List[HookResult]:
        """Run events serially while retaining this harness's state directory."""

        return [
            self.run(
                call.hook,
                call.payload,
                env=call.env,
                cwd=call.cwd,
                timeout=call.timeout,
            )
            for call in calls
        ]

    def run_concurrent(
        self,
        calls: Sequence[HookCall],
        *,
        max_workers: Optional[int] = None,
    ) -> List[HookResult]:
        """Launch independent hook subprocesses concurrently, preserving order."""

        def invoke(call: HookCall) -> HookResult:
            return self.run(
                call.hook,
                call.payload,
                env=call.env,
                cwd=call.cwd,
                timeout=call.timeout,
            )

        workers = max_workers or max(1, len(calls))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(invoke, calls))

    def close(self) -> None:
        self._temporary_directory.cleanup()

    def __enter__(self) -> "HookHarness":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


__all__ = [
    "DEFAULT_BUILDER_AGENT",
    "HOOKS_DIR",
    "OMIT",
    "REPO_ROOT",
    "HookCall",
    "HookHarness",
    "HookResult",
    "PayloadFactory",
]
