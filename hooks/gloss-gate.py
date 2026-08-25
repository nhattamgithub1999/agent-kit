#!/usr/bin/env python3
"""Stop/SubagentStop hook that rejects unsupported explicit token definitions.

The gate intentionally does not infer a meaning from initials. A definition is
accepted only when it exactly matches an approved glossary entry, or when a
local ``path:line`` citation resolves inside the current project and that line
contains the exact normalized token and meaning. Malformed hook payloads and
payloads without ``last_assistant_message`` fail open.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from _shared import (
    configure_stdio,
    default_state_root,
    get_field,
    hash_value,
    normalize_text,
    resolve_project_citation,
    safe_env_int,
    secure_log,
)


DEFAULT_MIN_TOKEN_LENGTH = 3
MAX_TOKEN_LENGTH = 32
MAX_MEANING_LENGTH = 240
VALID_MODES = frozenset({"off", "warn", "block"})
# A repeat pass must re-validate, otherwise re-emitting the same text is a free
# bypass. The cap stops the retry loop from becoming a deadlock.
DEFAULT_REPEAT_CAP = 3

# Bare occurrences are harmless. This vocabulary is not a bypass: an explicit
# definition of these tokens still needs a glossary entry or a citation, using
# the four strict forms below. What it does buy is that the *extended* forms
# (colon, arrow, markdown table, "nghia la", ...) are NOT applied to them, so
# everyday labels such as a verdict line or a status row are never mistaken for
# a definition. Unfamiliar business tokens get both pattern sets.
KNOWN_BARE_TOKENS = frozenset(
    {
        "API", "MCP", "HTTP", "HTTPS", "JSON", "XML", "SQL", "REST",
        "CRUD", "DTO", "ORM", "JWT", "SSO", "OIDC", "RBAC", "CQRS",
        "DDD", "TDD", "CI", "CD", "SDK", "URL", "URI", "UUID", "CSV",
        "YAML", "DNS", "TLS", "SLA", "DLQ", "MQ", "RAG", "LLM", "UAT",
        "PRD", "BRD", "URD", "BA", "PM", "QA", "POC", "MVP", "KPI",
        "IMPLEMENT", "EXPLORE", "DESIGN", "REVIEW", "BUILD", "AMBIGUOUS",
        "PLAN", "GROUNDED", "UNVERIFIABLE", "FABRICATED", "VERDICT",
        "BLOCK", "BLOCKED", "READY", "QUESTION", "FACT", "PASS", "FAIL",
        "STOP", "SKIP", "DONE", "WARN", "NOTE", "TODO", "ATTEMPT", "CAP",
        "OPEN", "STRICT", "OFF", "ON", "HEAD", "MAIN", "MASTER", "ORIGIN",
        "README", "LICENSE", "ROOT", "DUMP", "TOKEN", "NAME", "VALUE",
        "KEY", "PATH", "EVENT", "FOO", "BAR", "BAZ", "XXX",
        "WARNING", "ERROR", "INFO", "DEBUG", "TRACE", "FATAL", "CRITICAL",
        "SUCCESS", "FAILED", "FAILURE", "OK", "YES", "NO", "TRUE", "FALSE",
        "NULL", "NONE", "NIL", "GET", "POST", "PUT", "PATCH", "DELETE",
        "OPTIONS", "FIXME", "HACK", "BUG", "WIP", "DRAFT", "FINAL", "BEFORE",
        "AFTER", "INPUT", "OUTPUT", "RESULT", "STATUS", "TYPE", "KIND", "MODE",
        "LEVEL", "SCOPE", "STEP", "TASK", "JOB", "RUN", "TEST", "TESTS",
        "LINT", "TYPECHECK", "DOD", "WHY", "HOW", "WHAT", "WHEN", "WHERE",
        "SETUP", "USAGE", "EXAMPLE", "OUT", "IN", "SRC", "DST", "TMP",
    }
)

_UNKNOWN_MARKER = re.compile(r"\[CHƯA\s+RÕ\s*:\s*[^\]\r\n]+\]", re.IGNORECASE)
_CITATION = re.compile(
    r"`(?P<quoted>[^`\r\n]+:[1-9][0-9]*)`"
    r"|(?<![^\s(])(?P<bare>[^\s`(),;]+:[1-9][0-9]*)(?=$|[\s),.;])"
)
_TRAILING_SOURCE_CUE = re.compile(
    r"(?:\(?\s*(?:(?:theo|nguồn|source)\s*:?\s*)?)$", re.IGNORECASE
)
_GLOSSARY_LINE = re.compile(r"^([A-ZĐ][A-ZĐ0-9]{1,31})\s*=\s*(\S(?:.*\S)?)$")
# ``#`` opens a comment at line start, or mid-line only when surrounded by
# whitespace. A meaning such as "ho so #42 cua phong ban" keeps its tail
# instead of being truncated at the number sign.
_COMMENT_SUFFIX = re.compile(r"^[ \t]*#.*$|(?<=[ \t])#(?=[ \t]).*$")


@dataclass(frozen=True)
class Definition:
    token: str
    meaning: str
    start: int
    end: int


@dataclass(frozen=True)
class Violation:
    token: str
    meaning: str
    reason: str


@dataclass(frozen=True)
class GlossaryData:
    entries: Mapping[str, Tuple[str, str]]
    conflicts: Tuple[str, ...]


def _mode() -> str:
    value = os.environ.get("GLOSS_GATE", "block").strip().casefold()
    return value if value in VALID_MODES else "block"


def _truthy(value: object) -> bool:
    """Match Claude hook boolean coercion used by the other blocking hook."""

    if value is True or value == 1:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "on")
    return False


def _definition_pattern(minimum_length: int) -> re.Pattern[str]:
    token_tail_minimum = max(0, minimum_length - 1)
    token_tail_maximum = MAX_TOKEN_LENGTH - 1
    token = (
        r"(?<![A-ZĐ0-9_])"
        r"\*{0,2}"
        r"(?P<token>[A-ZĐ][A-ZĐ0-9]{%d,%d})"
        r"\*{0,2}"
        r"(?![A-ZĐ0-9_])" % (token_tail_minimum, token_tail_maximum)
    )
    inline_end = r"(?=$|[;,\r\n]|[.](?:[ \t\r\n]|$))"
    return re.compile(
        token
        + r"(?:"
        + r"[ \t]*\((?P<paren>[^()\r\n]{1,%d})\)" % MAX_MEANING_LENGTH
        + r"|[ \t]*(?<![=!<>:])=(?!=|>)[ \t]*(?P<equals>[^\r\n]{1,%d}?)" % MAX_MEANING_LENGTH
        + inline_end
        + r"|[ \t]+(?i:là)[ \t]+(?P<la>[^\r\n]{1,%d}?)" % MAX_MEANING_LENGTH
        + inline_end
        + r"|[ \t]+[–—-][ \t]+(?P<dash>[^\r\n]{1,%d}?)" % MAX_MEANING_LENGTH
        + inline_end
        + r")"
    )


def _extended_definition_pattern(minimum_length: int) -> re.Pattern[str]:
    """Looser definition forms, applied only to tokens outside the known set.

    Agents write definitions far more often with a colon, an arrow, a markdown
    table cell or a phrase like "nghia la" than with the four strict forms.
    Applying these to every capitalised word would flag ordinary label lines,
    so ``_extract_definitions`` filters the matches by token.
    """

    token_tail_minimum = max(0, minimum_length - 1)
    token_tail_maximum = MAX_TOKEN_LENGTH - 1
    token = (
        r"(?<![A-ZĐ0-9_])"
        r"\*{0,2}"
        r"(?P<token>[A-ZĐ][A-ZĐ0-9]{%d,%d})"
        r"\*{0,2}"
        r"(?![A-ZĐ0-9_])" % (token_tail_minimum, token_tail_maximum)
    )
    inline_end = r"(?=$|[;,\r\n]|[.](?:[ \t\r\n]|$))"
    phrases = r"viết\s+tắt\s+của|nghĩa\s+là|tức\s+là|chính\s+là|được\s+hiểu\s+là|stands\s+for|tức"
    return re.compile(
        token
        + r"(?:"
        + r"[ \t]*(?<![:=!<>])::?(?![=:])[ \t]*(?P<colon>[^\r\n]{1,%d}?)" % MAX_MEANING_LENGTH
        + inline_end
        + r"|[ \t]*(?:->|=>|→|⇒)[ \t]*(?P<arrow>[^\r\n]{1,%d}?)" % MAX_MEANING_LENGTH
        + inline_end
        + r"|[ \t]+(?i:%s)[ \t]+(?P<phrase>[^\r\n]{1,%d}?)" % (phrases, MAX_MEANING_LENGTH)
        + inline_end
        + r")"
    )


def _table_row_pattern(minimum_length: int) -> re.Pattern[str]:
    """A markdown table cell pairing a token with a meaning is a definition."""

    token_tail_minimum = max(0, minimum_length - 1)
    token_tail_maximum = MAX_TOKEN_LENGTH - 1
    return re.compile(
        r"^[ \t]*\|[ \t]*\*{0,2}"
        r"(?P<token>[A-ZĐ][A-ZĐ0-9]{%d,%d})"
        r"\*{0,2}[ \t]*\|[ \t]*"
        r"(?P<cell>[^|\r\n]{1,%d}?)[ \t]*\|"
        % (token_tail_minimum, token_tail_maximum, MAX_MEANING_LENGTH),
        re.MULTILINE,
    )


def _strip_unknown_markers(text: str) -> str:
    """Blank markers while retaining offsets used to locate nearby citations."""

    return _UNKNOWN_MARKER.sub(lambda match: " " * len(match.group(0)), text)


def _citations(text: str) -> Iterable[Tuple[str, int, int]]:
    for match in _CITATION.finditer(text):
        value = match.group("quoted") or match.group("bare")
        if value:
            yield value, match.start(), match.end()


def _clean_inline_meaning(raw: str) -> str:
    citation_matches = list(_citations(raw))
    if citation_matches:
        raw = raw[: citation_matches[0][1]]
        raw = _TRAILING_SOURCE_CUE.sub("", raw)
    else:
        raw = re.split(r"[;,]", raw, maxsplit=1)[0]
        raw = re.split(r"\.(?:\s|$)", raw, maxsplit=1)[0]
    return raw.strip(" \t\r\n`*_()[]{}:.,;–—-")


def _is_known_bare_token(token: str) -> bool:
    return token.upper() in KNOWN_BARE_TOKENS


def _extract_definitions(text: str, minimum_length: int) -> List[Definition]:
    sanitized = _strip_unknown_markers(text)
    definitions: List[Definition] = _extract_strict_definitions(sanitized, minimum_length)
    seen = {(item.token, item.start) for item in definitions}
    for item in _extract_extended_definitions(sanitized, minimum_length):
        if (item.token, item.start) not in seen:
            seen.add((item.token, item.start))
            definitions.append(item)
    definitions.sort(key=lambda item: item.start)
    return definitions


def _extract_extended_definitions(
    sanitized: str, minimum_length: int
) -> List[Definition]:
    """Extended forms, restricted to tokens that are not everyday labels."""

    found: List[Definition] = []
    for match in _extended_definition_pattern(minimum_length).finditer(sanitized):
        token = match.group("token")
        if _is_known_bare_token(token):
            continue
        raw_meaning = next(
            (
                value
                for value in (
                    match.group("colon"),
                    match.group("arrow"),
                    match.group("phrase"),
                )
                if value is not None
            ),
            "",
        )
        meaning = _clean_inline_meaning(raw_meaning).strip(" \t\r\n`*_:.,;–—-")
        if meaning:
            found.append(Definition(token, meaning, match.start(), match.end()))
    for match in _table_row_pattern(minimum_length).finditer(sanitized):
        token = match.group("token")
        if _is_known_bare_token(token):
            continue
        meaning = match.group("cell").strip(" \t\r\n`*_:.,;–—-")
        if meaning:
            found.append(Definition(token, meaning, match.start(), match.end()))
    return found


def _extract_strict_definitions(
    sanitized: str, minimum_length: int
) -> List[Definition]:
    definitions: List[Definition] = []
    for match in _definition_pattern(minimum_length).finditer(sanitized):
        raw_meaning = next(
            (
                value
                for value in (
                    match.group("paren"),
                    match.group("equals"),
                    match.group("la"),
                    match.group("dash"),
                )
                if value is not None
            ),
            "",
        )
        meaning = (
            raw_meaning.strip()
            if match.group("paren") is not None
            else _clean_inline_meaning(raw_meaning)
        )
        meaning = meaning.strip(" \t\r\n`*_:.,;–—-")
        if meaning:
            definitions.append(
                Definition(match.group("token"), meaning, match.start(), match.end())
            )
    return definitions


def _read_glossary(path: pathlib.Path) -> List[Tuple[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []

    entries: List[Tuple[str, str]] = []
    for raw_line in lines:
        line = _COMMENT_SUFFIX.sub("", raw_line).strip()
        match = _GLOSSARY_LINE.fullmatch(line)
        if match:
            entries.append((match.group(1), match.group(2).strip()))
    return entries


def _project_root(payload: Mapping[str, object]) -> Optional[pathlib.Path]:
    raw = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if not raw:
        value = get_field(payload, "cwd")
        raw = value.strip() if isinstance(value, str) else ""
    if not raw:
        return None
    try:
        root = pathlib.Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return root if root.is_dir() else None


def _safe_project_glossary(root: Optional[pathlib.Path]) -> Optional[pathlib.Path]:
    if root is None:
        return None
    candidate = root / ".claude" / "glossary.txt"
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _load_glossaries(project_root: Optional[pathlib.Path]) -> GlossaryData:
    entries: Dict[str, Tuple[str, str]] = {}
    conflicts = set()

    home_path = pathlib.Path.home() / ".claude" / "glossary.txt"
    for token, meaning in _read_glossary(home_path):
        key = normalize_text(token)
        normalized_meaning = normalize_text(meaning)
        previous = entries.get(key)
        if previous is not None and normalize_text(previous[1]) != normalized_meaning:
            conflicts.add(token)
            continue
        entries.setdefault(key, (token, meaning))

    project_path = _safe_project_glossary(project_root)
    if project_path is not None:
        try:
            same_as_home = project_path == home_path.resolve(strict=True)
        except (OSError, RuntimeError):
            same_as_home = False
        if not same_as_home:
            for token, meaning in _read_glossary(project_path):
                key = normalize_text(token)
                normalized_meaning = normalize_text(meaning)
                previous = entries.get(key)
                if previous is not None:
                    if normalize_text(previous[1]) != normalized_meaning:
                        conflicts.add(token)
                    continue
                entries[key] = (token, meaning)

    return GlossaryData(entries, tuple(sorted(conflicts, key=normalize_text)))


def _last_message(payload: Mapping[str, object]) -> str:
    value = get_field(payload, "last_assistant_message")
    if isinstance(value, str):
        return value if value.strip() else ""
    if isinstance(value, Mapping):
        content = value.get("content")
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            parts = []
            for block in content:
                if isinstance(block, Mapping):
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
            return "\n".join(parts)
    return ""


def _nearby_text(text: str, definition: Definition) -> str:
    line_start = text.rfind("\n", 0, definition.start)
    previous_start = text.rfind("\n", 0, max(0, line_start))
    start = 0 if previous_start < 0 else previous_start + 1

    line_end = text.find("\n", definition.end)
    if line_end < 0:
        return text[start:]
    next_end = text.find("\n", line_end + 1)
    end = len(text) if next_end < 0 else next_end
    return text[start:end]


def _contains_normalized_phrase(line: str, phrase: str) -> bool:
    normalized_line = normalize_text(line)
    normalized_phrase = normalize_text(phrase)
    if not normalized_phrase:
        return False
    pattern = re.compile(r"(?<!\w)" + re.escape(normalized_phrase) + r"(?!\w)")
    return pattern.search(normalized_line) is not None


def _has_valid_citation(
    text: str,
    definition: Definition,
    project_root: Optional[pathlib.Path],
) -> bool:
    if project_root is None:
        return False
    for citation, _start, _end in _citations(_nearby_text(text, definition)):
        try:
            resolved = resolve_project_citation(citation, project_root)
        except (OSError, RuntimeError, ValueError):
            continue
        if _contains_normalized_phrase(
            resolved.text, definition.token
        ) and _contains_normalized_phrase(resolved.text, definition.meaning):
            return True
    return False


def _repeat_scope(payload: Mapping[str, object]) -> str:
    parts = []
    for field in ("session_id", "prompt_id", "hook_event_name", "agent_type", "cwd"):
        value = get_field(payload, field)
        parts.append(value if isinstance(value, str) else "")
    return hash_value("\x00".join(parts), domain="gloss_repeat")


def _repeat_counter_path(scope: str) -> pathlib.Path:
    return default_state_root() / "gloss-repeat" / "{}.json".format(scope)


def _bump_repeat_count(scope: str) -> int:
    """Return how many times this exact scope has been blocked, including now.

    Any storage problem returns 0, which the caller reads as "cannot count", so
    the hook falls back to the previous permissive behaviour instead of looping
    forever.
    """

    path = _repeat_counter_path(scope)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = int(json.loads(path.read_text(encoding="utf-8"))["count"])
        except (OSError, ValueError, KeyError, TypeError):
            current = 0
        current += 1
        path.write_text(json.dumps({"count": current}), encoding="utf-8")
        return current
    except Exception:
        return 0


def _clear_repeat_count(scope: str) -> None:
    try:
        _repeat_counter_path(scope).unlink()
    except Exception:
        pass


def _log(event: str, fields: Optional[Mapping[str, object]] = None) -> None:
    try:
        secure_log(event, fields)
    except Exception:
        # Logging is diagnostic and must never change a hook decision. A narrow
        # except tuple would let an unexpected error escape to ``main`` and exit
        # non-zero, which the runtime reads as "do not block" - the exact
        # fail-open this gate exists to prevent.
        pass


def _blocked_message(violations: Sequence[Violation]) -> str:
    details = []
    for violation in violations[:8]:
        if violation.reason == "glossary_conflict":
            details.append(
                "  - {}: glossary project mâu thuẫn với glossary home/entry trước."
                .format(violation.token)
            )
        elif violation.reason == "glossary_mismatch":
            details.append(
                '  - {} → "{}": không khớp chính xác nghĩa đã duyệt trong glossary.'
                .format(violation.token, violation.meaning)
            )
        else:
            details.append(
                '  - {} → "{}": thiếu citation local path:line hợp lệ chứa đúng token và nghĩa.'
                .format(violation.token, violation.meaning)
            )
    return (
        "BLOCKED bởi gloss-gate: phát hiện định nghĩa token chưa được chứng minh.\n"
        + "\n".join(details)
        + "\nGiữ nguyên token và dùng [CHƯA RÕ: <token>], hoặc trích glossary/citation "
        "path:line nằm trong project. Không suy nghĩa từ chữ cái đầu."
    )


def main() -> int:
    configure_stdio()
    mode = _mode()
    if mode == "off":
        return 0

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, TypeError, ValueError):
        _log("gloss_gate_fail_open", {"reason": "invalid_json"})
        return 0
    if not isinstance(payload, Mapping):
        _log("gloss_gate_fail_open", {"reason": "invalid_payload"})
        return 0
    repeating = _truthy(get_field(payload, "stop_hook_active"))

    text = _last_message(payload)
    if not text.strip():
        _log("gloss_gate_fail_open", {"reason": "missing_last_message"})
        return 0

    minimum_length = safe_env_int(
        "GLOSS_MIN_LEN", DEFAULT_MIN_TOKEN_LENGTH, 2, MAX_TOKEN_LENGTH
    )
    project_root = _project_root(payload)
    glossary = _load_glossaries(project_root)
    violations: List[Violation] = [
        Violation(token, "", "glossary_conflict") for token in glossary.conflicts
    ]

    for definition in _extract_definitions(text, minimum_length):
        key = normalize_text(definition.token)
        approved = glossary.entries.get(key)
        if approved is not None:
            if normalize_text(definition.meaning) != normalize_text(approved[1]):
                violations.append(
                    Violation(
                        definition.token,
                        definition.meaning,
                        "glossary_mismatch",
                    )
                )
            continue
        if not _has_valid_citation(text, definition, project_root):
            violations.append(
                Violation(definition.token, definition.meaning, "missing_citation")
            )

    scope = _repeat_scope(payload)
    if not violations:
        _clear_repeat_count(scope)
        return 0

    kinds = sorted({violation.reason for violation in violations})
    _log(
        "gloss_gate_violation",
        {"count": len(violations), "mode": mode, "types": kinds},
    )
    if mode == "warn":
        return 0

    if repeating:
        cap = safe_env_int("GLOSS_REPEAT_CAP", DEFAULT_REPEAT_CAP, 1, 20)
        attempts = _bump_repeat_count(scope)
        if attempts == 0 or attempts >= cap:
            # Give up blocking so the conversation cannot deadlock, but never
            # do it silently: the claim below was NOT verified.
            _log(
                "gloss_gate_repeat_cap",
                {"attempts": attempts, "cap": cap, "count": len(violations)},
            )
            print(
                "gloss-gate: chạm attempt cap sau {} lần chặn liên tiếp. "
                "Cho qua để không treo hội thoại, NHƯNG định nghĩa dưới đây "
                "CHƯA được xác minh bằng glossary hoặc citation:\n{}".format(
                    attempts if attempts else cap, _blocked_message(violations)
                ),
                file=sys.stderr,
            )
            _clear_repeat_count(scope)
            return 0

    print(_blocked_message(violations), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
