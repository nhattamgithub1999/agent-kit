#!/usr/bin/env python3
"""Structured verification receipts for builder subagents.

The hook records exact foreground verification commands at ``PostToolUse``
time and validates the builder's machine-readable result at ``SubagentStop``.
Only hashes are persisted by :mod:`_shared`; this module never opens a
transcript and never treats prose or pasted output as evidence.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from typing import Any, Mapping, Optional, Tuple

from _shared import (
    InvalidVerificationContract,
    STOP_RETRY_CAP,
    StateError,
    StateStore,
    VerificationContract,
    bump_stop_retry_attempts,
    configure_stdio,
    get_field,
    secure_dump,
    secure_log,
    validate_verification_contract,
)


BUILDER_AGENTS = frozenset(("builder", "agent-kit:builder"))
# Per Claude Code's documented hook payload design, ``agent_id`` is populated
# only inside a subagent invocation; it is intentionally absent for
# main-session events (PostToolUse/Stop fired directly on the user's turn).
# This sentinel stands in for "the main session" so non-builder actors get a
# stable, non-empty identity to scope mutation/receipt state by, instead of
# being fail-closed for a field that was never supposed to exist for them.
MAIN_SESSION_ACTOR = "agent-kit:main-session"
SHELL_TOOLS = frozenset(("Bash", "PowerShell"))
DIRECT_MUTATION_TOOLS = frozenset(
    ("Edit", "Write", "NotebookEdit", "Monitor", "EnterWorktree", "ExitWorktree")
)
# Conservative, exact-prefix allowlist of shell invocations that cannot write
# to the project tree. Anything not on this list is still treated as a
# mutation, and anything on this list loses the exemption the moment it
# carries a redirect/pipe/chain/substitution sign (checked separately) —
# see Loi 6 point 2/3.
READ_ONLY_SHELL_PREFIXES = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "find",
    "wc",
    "pwd",
    "echo",
    "which",
)
_SHELL_WRITE_SIGNS = re.compile(r">>|>|\btee\b|\||;|&&|\|\||`|\$\(|\n")
RESULT_PREFIX = "AGENT_KIT_RESULT_V1="
RECEIPT_PREFIX = "AGENT_KIT_RECEIPT_V1="
MINIMUM_CLAUDE_CODE = "2.1.196"
MAX_CONTRACT_BYTES = 256 * 1024
MAX_RESULT_LINE_BYTES = 64 * 1024
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


class HookDecisionError(RuntimeError):
    """A watched builder event must be blocked with an actionable reason."""


class ContractError(HookDecisionError):
    """The project verification contract is absent or invalid."""


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _truthy(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "on")
    return False


def _safe_log(event: str, fields: Optional[Mapping[str, Any]] = None) -> None:
    """Best-effort structured logging with no raw runtime content."""

    try:
        secure_log(event, fields or {})
    except Exception:
        # Logging must not turn an honest NOT_READY report into a stop loop,
        # nor become a fail-open hole if it raises an exception type this
        # module did not anticipate (see Loi 5): any log failure here must
        # stay non-fatal, not escape and skip the caller's exit-2 decision.
        pass


def _agent_type(payload: Mapping[str, Any]) -> str:
    value = get_field(payload, "agent_type")
    if value is None:
        value = get_field(payload, "subagent_type")
    return value.strip() if isinstance(value, str) else ""


def _is_watched_builder(payload: Mapping[str, Any]) -> bool:
    return _agent_type(payload) in BUILDER_AGENTS


def _required_runtime_field(payload: Mapping[str, Any], name: str) -> str:
    value = get_field(payload, name)
    text = value.strip() if isinstance(value, str) else ""
    if text:
        return text
    if name == "prompt_id":
        raise HookDecisionError(
            "missing {}; agent-kit requires Claude Code >= {} for scoped "
            "verification state".format(name, MINIMUM_CLAUDE_CODE)
        )
    if name == "agent_id":
        # Unlike prompt_id, agent_id is not gated by any Claude Code version:
        # per the documented hook payload design it is populated only inside
        # a subagent invocation. This branch is only ever reached for a
        # watched builder (see _actor_identity), which is a subagent by
        # definition and therefore must carry it.
        raise HookDecisionError(
            "missing agent_id; a watched builder subagent must carry "
            "agent_id for scoped verification state"
        )
    raise HookDecisionError("missing required runtime field {}".format(name))


def _actor_identity(payload: Mapping[str, Any]) -> str:
    """Resolve the actor identity used to scope mutation/receipt state.

    ``agent_id`` is subagent-only by Claude Code's documented hook payload
    design; it is intentionally absent for main-session events. A watched
    builder is a subagent by definition and must still carry it (fail
    closed, same as before). Any other actor (main session, non-watched
    subagents) legitimately never has it, so it gets a stable sentinel
    identity instead of being fail-closed for a field that was never
    supposed to exist for it.
    """

    value = get_field(payload, "agent_id")
    text = value.strip() if isinstance(value, str) else ""
    if text:
        return text
    if _is_watched_builder(payload):
        return _required_runtime_field(payload, "agent_id")
    return MAIN_SESSION_ACTOR


def _project_root(payload: Mapping[str, Any]) -> pathlib.Path:
    configured = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    raw = configured or get_field(payload, "cwd")
    if not isinstance(raw, str) or not raw.strip():
        raise ContractError("missing project cwd; cannot locate .claude/verification.json")
    candidate = pathlib.Path(raw).expanduser()
    if configured and not candidate.is_absolute():
        raise ContractError("CLAUDE_PROJECT_DIR must be an absolute directory")
    try:
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ContractError("project cwd cannot be resolved") from exc
    if not root.is_dir():
        raise ContractError("project cwd is not a directory")
    return root


def _read_contract(payload: Mapping[str, Any]) -> VerificationContract:
    root = _project_root(payload)
    declared = root / ".claude" / "verification.json"
    try:
        if declared.is_symlink():
            raise ContractError("verification contract must not be a symlink")
        resolved = declared.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ContractError("verification contract is not a regular file")
        if resolved.stat().st_size > MAX_CONTRACT_BYTES:
            raise ContractError("verification contract exceeds {} bytes".format(MAX_CONTRACT_BYTES))
        raw = resolved.read_text(encoding="utf-8")
    except ContractError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ContractError(
            "missing or unreadable .claude/verification.json; define the four "
            "verification steps before reporting READY"
        ) from exc
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractError("verification contract is not valid JSON") from exc
    try:
        return validate_verification_contract(document, root)
    except InvalidVerificationContract as exc:
        # Contract keys are untrusted too; never echo validator details.
        raise ContractError(
            "invalid verification contract schema or project-relative cwd"
        ) from exc


def _actual_command(payload: Mapping[str, Any]) -> str:
    tool_input = get_field(payload, "tool_input", {})
    if not isinstance(tool_input, Mapping):
        raise HookDecisionError("shell tool_input must be an object")
    command = get_field(tool_input, "command")
    if not isinstance(command, str) or not command:
        raise HookDecisionError("shell tool_input.command must be a non-empty string")
    return command


def _actual_cwd(payload: Mapping[str, Any], project_root: pathlib.Path) -> pathlib.Path:
    tool_input = get_field(payload, "tool_input", {})
    raw = get_field(tool_input, "cwd") if isinstance(tool_input, Mapping) else None
    if raw is None or raw == "":
        raw = get_field(payload, "cwd")
    if not isinstance(raw, str) or not raw.strip():
        raise HookDecisionError("missing shell cwd")
    candidate = pathlib.Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HookDecisionError("shell cwd escapes or cannot resolve inside project") from exc
    if not resolved.is_dir():
        raise HookDecisionError("shell cwd is not a directory")
    return resolved


def _tool_name(payload: Mapping[str, Any]) -> str:
    value = get_field(payload, "tool_name")
    return value.strip() if isinstance(value, str) else ""


def _is_mutation_tool(tool_name: str) -> bool:
    return tool_name in DIRECT_MUTATION_TOOLS or tool_name.startswith("mcp__")


def _is_read_only_shell_command(command: str) -> bool:
    """Conservative check: true only for an exact, unmodified read-only call.

    Any redirect/pipe/chain/substitution sign disqualifies the command
    outright, regardless of prefix (Loi 6 point 3); everything not on the
    allowlist keeps counting as a mutation exactly as before (Loi 6 point 2).
    """

    if _SHELL_WRITE_SIGNS.search(command):
        return False
    stripped = command.strip()
    return any(
        stripped == prefix or stripped.startswith(prefix + " ")
        for prefix in READ_ONLY_SHELL_PREFIXES
    )


def _shell_outcome(payload: Mapping[str, Any], event: str) -> str:
    if event == "PostToolUseFailure":
        return "runtime_failure"
    tool_input = get_field(payload, "tool_input", {})
    response = get_field(payload, "tool_response", {})
    if not isinstance(tool_input, Mapping):
        tool_input = {}
    if not isinstance(response, Mapping):
        response = {}
    background_task_id = get_field(response, "background_task_id")
    has_background_task_id = (
        bool(background_task_id.strip())
        if isinstance(background_task_id, str)
        else bool(background_task_id)
    )
    if _truthy(get_field(tool_input, "run_in_background")) or _truthy(
        get_field(response, "is_background")
    ) or _truthy(get_field(response, "background")) or has_background_task_id:
        return "background"
    if _truthy(get_field(response, "interrupted")):
        return "interrupted"
    return "runtime_success"


def _runtime_scope(payload: Mapping[str, Any], needs_tool: bool) -> Tuple[str, str, str, Optional[str]]:
    session_id = _required_runtime_field(payload, "session_id")
    prompt_id = _required_runtime_field(payload, "prompt_id")
    agent_id = _actor_identity(payload)
    tool_use_id = _required_runtime_field(payload, "tool_use_id") if needs_tool else None
    return session_id, prompt_id, agent_id, tool_use_id


def _emit_receipt(step: str, receipt: str, epoch: int) -> None:
    context = RECEIPT_PREFIX + _compact({"epoch": epoch, "receipt": receipt, "step": step})
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }
    print(_compact(output))


def _record_shell(payload: Mapping[str, Any], event: str) -> int:
    project_root = _project_root(payload)
    try:
        contract = _read_contract(payload)
        command = _actual_command(payload)
        cwd = _actual_cwd(payload, project_root)
    except HookDecisionError:
        # The shell has already run. If it cannot be proven to be an exact
        # contract step, invalidate old evidence before returning BLOCKED.
        _record_mutation(payload, event, project_root=project_root)
        raise
    project_root = contract.project_root
    if project_root is None:
        raise ContractError("verification contract is not project-bound")
    step = contract.match(command, cwd)
    session_id, prompt_id, agent_id, tool_use_id = _runtime_scope(payload, True)
    assert tool_use_id is not None
    store = StateStore()

    if step is None:
        return _record_mutation(payload, event, project_root=project_root, store=store)

    outcome = _shell_outcome(payload, event)
    result = store.record_verification(
        project_id=project_root,
        session_id=session_id,
        prompt_id=prompt_id,
        agent_id=agent_id,
        step=step.name,
        tool_use_id=tool_use_id,
        command=command,
        cwd=str(cwd),
        outcome=outcome,
        contract=contract,
    )
    _safe_log(
        "verification.recorded",
        {"step": step.name, "outcome": outcome, "created": result.created},
    )
    if result.receipt is not None:
        _emit_receipt(step.name, result.receipt, result.epoch)
    return 0


def _record_mutation(
    payload: Mapping[str, Any],
    event: str,
    *,
    project_root: Optional[pathlib.Path] = None,
    store: Optional[StateStore] = None,
) -> int:
    root = _project_root(payload) if project_root is None else project_root
    session_id, prompt_id, agent_id, tool_use_id = _runtime_scope(payload, True)
    assert tool_use_id is not None
    tool = _tool_name(payload)
    if not tool:
        raise HookDecisionError("missing required runtime field tool_name")
    tool_input = get_field(payload, "tool_input", {})
    if not isinstance(tool_input, Mapping):
        raise HookDecisionError("mutation tool_input must be an object")
    if tool in SHELL_TOOLS:
        raw_command = get_field(tool_input, "command")
        if isinstance(raw_command, str) and _is_read_only_shell_command(raw_command):
            # Loi 6: a provably read-only shell call from any actor must not
            # advance the project epoch or wipe another agent's live receipt
            # chain; everything else still counts as a mutation as before.
            _safe_log("verification.readonly_shell_ignored", {"tool": tool, "event_name": event})
            return 0
    outcome = _shell_outcome(payload, event)
    mutation_fact = {
        "background": outcome == "background",
        "event_name": event,
        "outcome": outcome,
        "tool_input": dict(tool_input),
        "tool_name": tool,
    }
    (store or StateStore()).bump_mutation_epoch(
        project_id=root,
        session_id=session_id,
        prompt_id=prompt_id,
        actor_id=agent_id,
        tool_use_id=tool_use_id,
        mutation_fact=mutation_fact,
        background=outcome == "background",
    )
    _safe_log(
        "verification.mutation",
        {"event_name": event, "outcome": outcome, "tool": tool},
    )
    return 0


def _extract_result(last_message: Any) -> Mapping[str, Any]:
    if not isinstance(last_message, str) or not last_message.strip():
        raise HookDecisionError(
            "builder must end with exactly one AGENT_KIT_RESULT_V1 line "
            "(READY with receipts, or NOT_READY with a reason)"
        )
    if len(last_message.encode("utf-8")) > MAX_RESULT_LINE_BYTES * 4:
        raise HookDecisionError("builder result message is too large")

    prefix_occurrences = last_message.count(RESULT_PREFIX)
    candidates = []
    inside_fence = False
    fence_marker = ""
    for line in last_message.splitlines():
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not inside_fence:
                inside_fence = True
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                inside_fence = False
                fence_marker = ""
            continue
        stripped = line.strip()
        if not inside_fence and stripped.startswith(RESULT_PREFIX):
            candidates.append(stripped)

    if prefix_occurrences != 1 or len(candidates) != 1:
        raise HookDecisionError(
            "builder must provide exactly one unfenced AGENT_KIT_RESULT_V1 line; "
            "prose, code fences, and transcript content are not evidence"
        )
    line = candidates[0]
    if len(line.encode("utf-8")) > MAX_RESULT_LINE_BYTES:
        raise HookDecisionError("AGENT_KIT_RESULT_V1 line is too large")
    raw_json = line[len(RESULT_PREFIX) :]
    try:
        result = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HookDecisionError("AGENT_KIT_RESULT_V1 must contain one valid JSON object") from exc
    if not isinstance(result, dict):
        raise HookDecisionError("AGENT_KIT_RESULT_V1 must contain a JSON object")
    return result


def _validate_result_claim(payload: Mapping[str, Any], last_message: Any) -> int:
    """Validate one AGENT_KIT_RESULT_V1 claim; raise to block an unproven one."""

    result = _extract_result(last_message)
    status = result.get("status")

    if status == "NOT_READY":
        if set(result) != {"status", "reason"}:
            raise HookDecisionError("NOT_READY result must contain only status and reason")
        reason = result.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise HookDecisionError("NOT_READY result requires a non-empty reason")
        _safe_log("verification.not_ready", {"status": "NOT_READY"})
        return 0

    if status != "READY":
        raise HookDecisionError("result status must be READY or NOT_READY")
    if set(result) != {"status", "receipts"}:
        raise HookDecisionError("READY result must contain only status and receipts")
    receipts = result.get("receipts")
    if not isinstance(receipts, dict):
        raise HookDecisionError("READY receipts must be an object")

    contract = _read_contract(payload)
    project_root = contract.project_root
    if project_root is None:
        raise ContractError("verification contract is not project-bound")
    expected = set(contract.required_steps)
    if set(receipts) != expected:
        raise HookDecisionError(
            "READY receipt set must exactly match active verification steps"
        )
    if any(not isinstance(value, str) or not value.strip() for value in receipts.values()):
        raise HookDecisionError("READY receipt values must be non-empty strings")

    session_id, prompt_id, agent_id, _ = _runtime_scope(payload, False)
    validation = StateStore().validate_receipts(
        project_id=project_root,
        session_id=session_id,
        prompt_id=prompt_id,
        agent_id=agent_id,
        receipts=receipts,
        contract=contract,
    )
    if not validation.valid:
        raise HookDecisionError("READY receipts rejected: {}".format("; ".join(validation.errors)))
    _safe_log("verification.ready", {"status": "READY", "steps": list(contract.required_steps)})
    return 0


def _bump_stop_retry_attempts(payload: Mapping[str, Any]) -> int:
    project_root = _project_root(payload)
    session_id, prompt_id, agent_id, _ = _runtime_scope(payload, False)
    return bump_stop_retry_attempts(project_root, session_id, prompt_id, agent_id)


def _handle_stop_like(payload: Mapping[str, Any], *, require_marker: bool) -> int:
    """Shared SubagentStop/Stop enforcement (Loi 3: Stop is always main session).

    ``require_marker`` distinguishes a watched builder subagent (which must
    always end with exactly one AGENT_KIT_RESULT_V1 line) from the main
    session's own Stop event (which fires after every ordinary turn and must
    not be blocked unless the turn itself makes a machine-checkable claim).
    """

    last_message = get_field(payload, "last_assistant_message")
    if not require_marker:
        if not isinstance(last_message, str) or RESULT_PREFIX not in last_message:
            return 0

    if not _truthy(get_field(payload, "stop_hook_active")):
        return _validate_result_claim(payload, last_message)

    # Loi 4: a retried stop must be re-validated, not waved through
    # unconditionally — but re-blocking the exact same identity forever would
    # deadlock the session, so concede after STOP_RETRY_CAP consecutive
    # blocks, loudly, instead of silently.
    try:
        return _validate_result_claim(payload, last_message)
    except (HookDecisionError, StateError) as exc:
        try:
            attempts = _bump_stop_retry_attempts(payload)
        except (HookDecisionError, StateError):
            print(
                "no-fake-pass: không đếm được attempt cap (state không khả dụng "
                "cho lần dừng lặp lại); cho qua để tránh deadlock — claim KHÔNG "
                "được xác minh.",
                file=sys.stderr,
            )
            _safe_log("verification.stop_retry_unavailable", {})
            return 0
        if attempts > STOP_RETRY_CAP:
            print(
                "no-fake-pass: đã chạm attempt cap ({}) cho lần dừng lặp lại của "
                "identity này; cho qua để tránh deadlock — claim KHÔNG được xác "
                "minh: {}".format(STOP_RETRY_CAP, exc),
                file=sys.stderr,
            )
            _safe_log("verification.stop_retry_cap", {"count": attempts})
            return 0
        raise


def _handle_subagent_stop(payload: Mapping[str, Any]) -> int:
    return _handle_stop_like(payload, require_marker=True)


def _handle_stop(payload: Mapping[str, Any]) -> int:
    return _handle_stop_like(payload, require_marker=False)


def _dispatch(payload: Mapping[str, Any]) -> int:
    event = get_field(payload, "hook_event_name")
    event_name = event.strip() if isinstance(event, str) else ""
    if event_name == "SubagentStop":
        return _handle_subagent_stop(payload) if _is_watched_builder(payload) else 0
    if event_name == "Stop":
        return _handle_stop(payload)
    if event_name not in ("PostToolUse", "PostToolUseFailure"):
        return 0

    tool = _tool_name(payload)
    if not tool:
        raise HookDecisionError("missing required runtime field tool_name")
    if tool in SHELL_TOOLS:
        if _is_watched_builder(payload):
            return _record_shell(payload, event_name)
        return _record_mutation(payload, event_name)
    if _is_mutation_tool(tool):
        return _record_mutation(payload, event_name)
    return 0


def main() -> int:
    configure_stdio()
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _safe_log("verification.malformed_payload", {"json": False})
        return 0
    if not isinstance(payload, dict):
        _safe_log("verification.malformed_payload", {"object": False})
        return 0

    if os.environ.get("DUMP"):
        try:
            secure_dump(payload, label="no-fake-pass-payload")
        except (StateError, OSError, ValueError):
            # secure_dump rejects symlinks/unsafe paths. Debugging is optional.
            _safe_log("verification.dump_refused", {"safe": False})

    try:
        return _dispatch(payload)
    except (HookDecisionError, StateError) as exc:
        # Error text contains schema/field names, never raw prompt/command/output.
        print("BLOCKED bởi no-fake-pass: {}".format(exc), file=sys.stderr)
        _safe_log("verification.blocked", {"reason_type": type(exc).__name__})
        return 2


if __name__ == "__main__":
    sys.exit(main())
