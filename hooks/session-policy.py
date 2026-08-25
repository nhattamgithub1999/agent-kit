#!/usr/bin/env python3
"""Inject the canonical delegation policy into a Claude Code hook context.

The hook is intentionally stateless: every configured lifecycle invocation
reads the policy and emits one JSON response. A missing or malformed payload,
an unreadable policy, or invalid UTF-8 fails open without writing state or
printing diagnostics into the hook protocol stream.

Environment overrides:
  POLICY_HOOK=off     disable policy injection
  POLICY_FILE=<path>  read an alternate policy file (``~`` is expanded)
"""

import json
import os
import pathlib
import sys
from typing import Any, Dict, Optional

from _shared import configure_stdio


# Policy text is normally only a few KiB. Refuse unexpectedly large files
# rather than flooding Claude Code's context or injecting a truncated policy.
MAX_POLICY_BYTES = 128 * 1024


def policy_path() -> Optional[pathlib.Path]:
    """Return the policy path without using the process cwd by default."""

    override = os.environ.get("POLICY_FILE")
    if override:
        try:
            return pathlib.Path(override).expanduser()
        except (OSError, RuntimeError):
            return None

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        try:
            root = pathlib.Path(plugin_root).expanduser()
        except (OSError, RuntimeError):
            return None
        # Claude Code supplies an absolute root. Reject a malformed relative
        # value so the default never silently depends on cwd.
        if not root.is_absolute():
            return None
    else:
        root = pathlib.Path(__file__).resolve().parent.parent

    return root / "policy" / "delegation.md"


def read_payload() -> Optional[Dict[str, Any]]:
    """Read one JSON object from stdin, returning ``None`` on protocol errors."""

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def read_policy(path: pathlib.Path) -> Optional[str]:
    """Read a non-empty, strictly UTF-8 policy within ``MAX_POLICY_BYTES``."""

    try:
        with path.open("rb") as policy_file:
            raw = policy_file.read(MAX_POLICY_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_POLICY_BYTES:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return text or None


def main() -> int:
    configure_stdio()
    if os.environ.get("POLICY_HOOK", "").strip().casefold() == "off":
        return 0

    payload = read_payload()
    if payload is None:
        return 0

    event = payload.get("hook_event_name") or payload.get("hookEventName")
    if not isinstance(event, str) or not event.strip():
        return 0

    path = policy_path()
    if path is None:
        return 0
    text = read_policy(path)
    if text is None:
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }
    try:
        print(json.dumps(output, ensure_ascii=False))
    except (BrokenPipeError, OSError, UnicodeError):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
