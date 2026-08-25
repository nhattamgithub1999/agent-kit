#!/usr/bin/env python3
"""Deterministic, prompt-scoped plan approval gate for Claude Code hooks.

The gate is intentionally fail-closed only where a missing decision could let a
mutation through: ``PreToolUse`` mutation calls and the ``ExitPlanMode``
approval path. Lifecycle and unrelated tool events fail open so a malformed
diagnostic event cannot make the whole Claude session unusable.

An approval is created only by a successful ``PostToolUse ExitPlanMode`` event
whose documented ``tool_response.plan`` contains the required Markdown
sections. Merely entering plan mode, writing a todo, entering a worktree, or
receiving the pre-tool event never creates approval.

Requires Claude Code 2.1.196 or newer because prompt-scoped isolation depends
on the common ``prompt_id`` hook field. Set ``PLAN_GATE=off`` for the explicit
emergency bypass.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import sys
from typing import Any, Mapping, Optional, Sequence, Tuple

from _shared import StateError, StateStore, configure_stdio, get_field, secure_log


MINIMUM_CLAUDE_CODE = "2.1.196"
MUTATION_TOOLS = frozenset(
    {
        "Edit",
        "Write",
        "NotebookEdit",
        "Bash",
        "PowerShell",
        "Monitor",
        "EnterWorktree",
        "ExitWorktree",
    }
)
HANDLED_EVENTS = frozenset(
    {"UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionEnd"}
)

_SECTION_HEADING = re.compile(
    r"^[ \t]*##[ \t]+(Plan|DoD)[ \t]*#*[ \t]*$"
)
_ANY_HEADING = re.compile(r"^[ \t]*#{1,6}(?:[ \t]+|$)")
_NUMBERED_STEP = re.compile(r"^[ \t]*(\d+)[.)][ \t]+(.+?)[ \t]*$")
_LIST_PREFIX = re.compile(r"^[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?")
_CHECKBOX_PREFIX = re.compile(r"^\[[ xX]\](?:[ \t]+|$)")


def _best_effort_log(event: str, **fields: Any) -> None:
    """Log only redacted metadata; logging must not alter the gate decision."""

    try:
        secure_log(event, fields)
    except (StateError, OSError, ValueError, TypeError):
        pass


def _block(message: str, reason: str) -> int:
    _best_effort_log("plan_gate.block", reason=reason)
    print("BLOCKED bởi plan-gate: {}".format(message), file=sys.stderr)
    return 2


def _scope(payload: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    session_id = get_field(payload, "session_id")
    prompt_id = get_field(payload, "prompt_id")
    session = str(session_id).strip() if session_id is not None else ""
    prompt = str(prompt_id).strip() if prompt_id is not None else ""
    if not session or not prompt:
        return None
    return session, prompt


def _scope_block() -> int:
    return _block(
        "thiếu session_id hoặc prompt_id nên không thể cô lập approval theo "
        "prompt. Hãy dùng Claude Code >= {} rồi thử lại.".format(
            MINIMUM_CLAUDE_CODE
        ),
        "missing_scope",
    )


def _state_block(exc: Optional[BaseException] = None) -> int:
    message = (
        "không truy cập được kho trạng thái an toàn; mutation/approval bị chặn "
        "để tránh dùng nhầm trạng thái. Kiểm tra Python sqlite3 và "
        "CLAUDE_PLUGIN_DATA rồi thử lại."
    )
    if exc is not None:
        # The exception message never contains raw paths/secrets beyond what
        # StateStore already puts there; surfacing it turns an opaque CI
        # failure into a diagnosable one (see StateUnavailable call sites in
        # _shared.py). Exit code and the Vietnamese prefix stay unchanged.
        message = "{} Nguyên nhân: {}".format(message, exc)
        _best_effort_log("plan_gate.state_unavailable", detail=str(exc))
    return _block(message, "state_unavailable")


def is_mutation_tool(tool_name: Any) -> bool:
    tool = str(tool_name or "")
    return tool in MUTATION_TOOLS or tool.startswith("mcp__")


def validate_plan(plan: Any) -> Tuple[bool, str]:
    """Validate the exact, deterministic Markdown plan contract.

    A plan has one ``## Plan`` section containing 3--7 sequential numbered
    steps (``1.`` or ``1)``) and one ``## DoD`` section with at least one
    non-empty criterion. Repeated sections are rejected because choosing one
    would make approval ambiguous.
    """

    if not isinstance(plan, str) or not plan.strip():
        return False, "tool_response.plan phải là chuỗi Markdown không rỗng"

    sections = {"plan": [], "dod": []}
    seen = []
    active: Optional[str] = None
    for line in plan.splitlines():
        heading = _SECTION_HEADING.match(line)
        if heading is not None:
            active = heading.group(1).casefold()
            seen.append(active)
            continue
        if _ANY_HEADING.match(line):
            active = None
            continue
        if active is not None:
            sections[active].append(line)

    for required in ("plan", "dod"):
        count = seen.count(required)
        if count == 0:
            return False, "thiếu section `## {}`".format(
                "Plan" if required == "plan" else "DoD"
            )
        if count > 1:
            return False, "section `## {}` bị lặp".format(
                "Plan" if required == "plan" else "DoD"
            )

    numbered_steps = []
    for line in sections["plan"]:
        match = _NUMBERED_STEP.match(line)
        if match is not None:
            numbered_steps.append((int(match.group(1)), match.group(2).strip()))
    if not 3 <= len(numbered_steps) <= 7:
        return False, "`## Plan` phải có đúng 3–7 numbered steps không rỗng"
    if any(not content for _, content in numbered_steps):
        return False, "mọi numbered step trong `## Plan` phải có nội dung"
    expected_numbers = list(range(1, len(numbered_steps) + 1))
    actual_numbers = [number for number, _ in numbered_steps]
    if actual_numbers != expected_numbers:
        return False, "numbered steps trong `## Plan` phải liên tục từ 1"

    dod_criteria = []
    for line in sections["dod"]:
        candidate = _LIST_PREFIX.sub("", line, count=1).strip()
        candidate = _CHECKBOX_PREFIX.sub("", candidate, count=1).strip()
        if candidate:
            dod_criteria.append(candidate)
    if not dod_criteria:
        return False, "`## DoD` phải có ít nhất một tiêu chí không rỗng"
    return True, ""


def _is_link_or_reparse(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(
        stat,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        0x0400 if os.name == "nt" else 0,
    )
    return bool(reparse_flag and attributes & reparse_flag)


def _safe_existing_component(path: pathlib.Path, directory: bool) -> bool:
    try:
        info = path.lstat()
    except (OSError, RuntimeError, ValueError):
        return False
    if _is_link_or_reparse(info):
        return False
    if directory:
        return stat.S_ISDIR(info.st_mode)
    return stat.S_ISREG(info.st_mode)


def _native_plan_file(raw_path: str) -> bool:
    """Verify a plan file using only the host's native filesystem semantics.

    This intentionally has no lexical Windows fallback on POSIX. The plan root,
    every existing descendant component, and an existing leaf must be real
    non-reparse filesystem entries. A new leaf is allowed only below an
    existing verified parent.
    """

    try:
        candidate = pathlib.Path(raw_path).expanduser()
        home = pathlib.Path.home()
    except (OSError, RuntimeError, ValueError):
        return False
    if not candidate.is_absolute() or not home.is_absolute():
        return False
    if ".." in candidate.parts:
        return False
    if os.name == "nt" and ":" in candidate.name:
        # Do not exempt an NTFS alternate data stream as a plan file.
        return False

    root = home / ".claude" / "plans"
    try:
        relative = candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    if not relative.parts:
        return False

    # Checking from HOME avoids treating an already-canonical system prefix
    # (for example macOS /var -> /private/var) as a user-controlled bypass.
    components = [home, home / ".claude", root]
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        components.append(current)
    if any(not _safe_existing_component(path, True) for path in components):
        return False

    try:
        canonical_root = root.resolve(strict=True)
        canonical_parent = candidate.parent.resolve(strict=True)
        canonical_parent.relative_to(canonical_root)
    except (OSError, RuntimeError, ValueError):
        return False

    try:
        candidate_info = candidate.lstat()
    except FileNotFoundError:
        # Parent verification above is sufficient for one new leaf only.
        canonical_candidate = canonical_parent / candidate.name
    except (OSError, RuntimeError, ValueError):
        return False
    else:
        if _is_link_or_reparse(candidate_info) or not stat.S_ISREG(
            candidate_info.st_mode
        ):
            return False
        try:
            canonical_candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return False

    try:
        relative_canonical = canonical_candidate.relative_to(canonical_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(relative_canonical.parts)


def is_plan_file(payload: Mapping[str, Any]) -> bool:
    """Allow only ``Write`` to a file contained by the plan-mode directory."""

    if str(get_field(payload, "permission_mode", "")).casefold() != "plan":
        return False
    if str(get_field(payload, "tool_name", "")) != "Write":
        return False
    tool_input = get_field(payload, "tool_input", {})
    if not isinstance(tool_input, Mapping):
        return False
    file_path = get_field(tool_input, "file_path")
    if not isinstance(file_path, str) or not file_path.strip() or "\x00" in file_path:
        return False
    return _native_plan_file(file_path)


def _response_plan(payload: Mapping[str, Any]) -> Any:
    response = get_field(payload, "tool_response")
    if not isinstance(response, Mapping):
        return None
    return get_field(response, "plan")


def _input_plan(payload: Mapping[str, Any]) -> Any:
    tool_input = get_field(payload, "tool_input")
    if not isinstance(tool_input, Mapping):
        return None
    return get_field(tool_input, "plan")


def _handle_user_prompt(payload: Mapping[str, Any]) -> int:
    scope = _scope(payload)
    if scope is None:
        _best_effort_log("plan_gate.prompt_ignored", reason="missing_scope")
        return 0
    try:
        StateStore().cleanup_prompt(*scope)
    except StateError:
        # The next mutation/approval will retry and fail closed if state remains
        # unavailable. A lifecycle notification itself must not block input.
        _best_effort_log("plan_gate.prompt_cleanup_failed", reason="state_error")
        return 0
    _best_effort_log("plan_gate.prompt_initialized", result="clean")
    return 0


def _handle_session_end(payload: Mapping[str, Any]) -> int:
    session_id = get_field(payload, "session_id")
    if session_id is None or not str(session_id).strip():
        _best_effort_log("plan_gate.session_cleanup_ignored", reason="missing_session")
        return 0
    try:
        StateStore().cleanup_session(session_id)
    except StateError:
        _best_effort_log("plan_gate.session_cleanup_failed", reason="state_error")
        return 0
    _best_effort_log("plan_gate.session_cleaned", result="clean")
    return 0


def _handle_pre_tool(payload: Mapping[str, Any], tool: str) -> int:
    approval_path = tool == "ExitPlanMode"
    mutation = is_mutation_tool(tool)
    if not approval_path and not mutation:
        # EnterPlanMode and TodoWrite are intentionally passive: allowed, but
        # neither creates nor implies an approval.
        return 0

    scope = _scope(payload)
    if scope is None:
        return _scope_block()
    try:
        store = StateStore()
    except StateError as exc:
        return _state_block(exc)

    if approval_path:
        valid, detail = validate_plan(_input_plan(payload))
        if not valid:
            return _block(
                "plan chưa hợp lệ ở PreToolUse ExitPlanMode: {}. Event này chỉ "
                "validate, chưa tạo approval.".format(detail),
                "invalid_pre_exit_plan",
            )
        _best_effort_log("plan_gate.pre_exit_validated", result="valid")
        return 0

    if is_plan_file(payload):
        _best_effort_log("plan_gate.plan_file_allowed", result="exempt")
        return 0

    try:
        approved = store.check_plan(*scope)
    except StateError as exc:
        return _state_block(exc)
    if approved:
        return 0
    return _block(
        "mutation `{}` chưa có plan được duyệt cho prompt hiện tại. Vào plan "
        "mode, cung cấp `## Plan` gồm 3–7 bước và `## DoD`, rồi hoàn tất "
        "ExitPlanMode; EnterPlanMode/TodoWrite/EnterWorktree không tự mở gate.".format(
            tool if not tool.startswith("mcp__") else "mcp__*"
        ),
        "approval_required",
    )


def _handle_post_tool(payload: Mapping[str, Any], tool: str) -> int:
    if tool != "ExitPlanMode":
        return 0
    scope = _scope(payload)
    if scope is None:
        return _scope_block()
    plan = _response_plan(payload)
    valid, detail = validate_plan(plan)
    if not valid:
        return _block(
            "PostToolUse ExitPlanMode không có approved tool_response.plan hợp "
            "lệ: {}.".format(detail),
            "invalid_approved_plan",
        )
    try:
        StateStore().approve_plan(scope[0], scope[1], plan)
    except StateError as exc:
        return _state_block(exc)
    _best_effort_log("plan_gate.plan_approved", result="approved")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    del argv
    configure_stdio()
    if os.environ.get("PLAN_GATE", "").strip().casefold() == "off":
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, Mapping):
        return 0

    event = str(get_field(payload, "hook_event_name", "") or "")
    tool = str(get_field(payload, "tool_name", "") or "")
    if event not in HANDLED_EVENTS:
        if is_mutation_tool(tool) or tool == "ExitPlanMode":
            return _block(
                "event mutation/approval bị thiếu hoặc không hợp lệ; không thể "
                "đưa ra quyết định an toàn.",
                "invalid_event",
            )
        return 0

    if event == "UserPromptSubmit":
        return _handle_user_prompt(payload)
    if event == "SessionEnd":
        return _handle_session_end(payload)
    if event == "PreToolUse":
        return _handle_pre_tool(payload, tool)
    if event == "PostToolUse":
        return _handle_post_tool(payload, tool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
