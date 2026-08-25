#!/usr/bin/env python3
"""Shared, dependency-free primitives for agent-kit hooks.

The state store deliberately keeps only hashes of Claude runtime identifiers,
plans, commands, working directories, and receipts.  Hook entry points should
catch :class:`StateError` and block mutations/READY reports: an unavailable or
unsafe store must never be interpreted as approval.

This module targets Python 3.9+ and does not claim that ``sqlite3`` is present
in every Python distribution.  ``StateStore`` raises ``StateUnavailable`` with
an actionable message when the stdlib module or a safe state directory is not
available.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
import time
import unicodedata
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple

try:  # Some minimal Python builds omit the optional sqlite3 extension.
    import sqlite3 as _sqlite3
except Exception:  # pragma: no cover - exercised by replacing the module in tests.
    _sqlite3 = None


SCHEMA_VERSION = 2
DEFAULT_BUSY_TIMEOUT_MS = 250
MINIMUM_BUSY_TIMEOUT_MS = 50
MAXIMUM_BUSY_TIMEOUT_MS = 1_000
SUCCESS_OUTCOME = "runtime_success"
ALLOWED_OUTCOMES = frozenset(
    {SUCCESS_OUTCOME, "runtime_failure", "interrupted", "background"}
)
VERIFICATION_STEPS = ("build", "typecheck", "lint", "test")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CITATION = re.compile(r"^\s*`?(.+):([1-9][0-9]*)`?\s*$")
# A verification command must be one bare executable invocation so its exit
# code is the only thing that can decide pass/fail. Any shell operator lets a
# contract author neutralize the command (e.g. "pytest tests || true") while
# still passing schema validation, so every one of these is rejected outright.
_SHELL_METACHARACTERS = re.compile(r"[|&;<>`\n]|\$\(")
STOP_RETRY_CAP = 3

# Diagnostic persistence is deliberately allowlist-based. Unknown values keep
# their shape/key for debugging but never their raw scalar value.
_SAFE_METADATA_KEYS = frozenset(
    {
        "background",
        "count",
        "created",
        "epoch",
        "json",
        "object",
        "safe",
        "schema_version",
        "status",
        "step",
        "steps",
        "outcome",
        "hook_event_name",
        "event_name",
        "tool",
        "tool_name",
    }
)
_SAFE_ENUM_VALUES = frozenset(
    set(VERIFICATION_STEPS)
    | set(ALLOWED_OUTCOMES)
    | {
        "READY",
        "NOT_READY",
        "Bash",
        "PowerShell",
        "Edit",
        "Write",
        "NotebookEdit",
        "Monitor",
        "EnterWorktree",
        "ExitWorktree",
        "SessionStart",
        "SubagentStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStop",
        "Stop",
        "SessionEnd",
    }
)


class StateError(RuntimeError):
    """Base error that callers must treat as a fail-closed decision."""


class StateUnavailable(StateError):
    """Persistent state could not be initialized or accessed safely."""


class StateConflict(StateError):
    """A supposedly idempotent runtime event was reused with other facts."""


class InvalidStateInput(StateError):
    """A required runtime identifier or state field is absent/invalid."""


class InvalidVerificationContract(InvalidStateInput):
    """The machine-readable verification contract is not exact or safe."""


@dataclasses.dataclass(frozen=True)
class VerificationResult:
    created: bool
    receipt: Optional[str]
    epoch: int
    outcome: str
    conflict: bool = False
    replay: bool = False
    project_generation: int = 0
    session_generation: int = 0


@dataclasses.dataclass(frozen=True)
class GenerationTuple:
    project_generation: int
    session_generation: int
    epoch: int
    project_tainted: bool = False
    session_tainted: bool = False
    prompt_tainted: bool = False

    @property
    def tainted(self) -> bool:
        return self.project_tainted or self.session_tainted or self.prompt_tainted


@dataclasses.dataclass(frozen=True)
class MutationObservation:
    scope: str
    invalidated: bool
    conflict: bool
    replay: bool
    project_generation: int
    session_generation: int
    epoch: int
    background_tainted: bool


@dataclasses.dataclass(frozen=True)
class VerificationRecord:
    step: str
    tool_use_hash: str
    command_hash: str
    cwd_hash: str
    outcome: str
    epoch: int
    recorded_at: float
    project_generation: int = 0
    session_generation: int = 0


@dataclasses.dataclass(frozen=True)
class ReceiptValidation:
    valid: bool
    errors: Tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class VerificationStepSpec:
    """One active step from a validated verification contract."""

    name: str
    command: str
    declared_cwd: str
    cwd: pathlib.Path


@dataclasses.dataclass(frozen=True)
class VerificationContract:
    """Exact contract validated without retaining it in persistent state."""

    active_steps: Tuple[VerificationStepSpec, ...]
    n_a_reasons: Mapping[str, str]
    fingerprint: str
    project_root: Optional[pathlib.Path]

    @property
    def required_steps(self) -> Tuple[str, ...]:
        return tuple(step.name for step in self.active_steps)

    def step(self, name: str) -> Optional[VerificationStepSpec]:
        return next((item for item in self.active_steps if item.name == name), None)

    def match(self, command: str, cwd: pathlib.Path) -> Optional[VerificationStepSpec]:
        candidate = pathlib.Path(cwd)
        matches = tuple(
            step
            for step in self.active_steps
            if step.command == command and step.cwd == candidate
        )
        if len(matches) > 1:  # Defensive: validation rejects duplicate pairs.
            raise InvalidVerificationContract(
                "verification contract has an ambiguous command/cwd pair"
            )
        return matches[0] if matches else None


@dataclasses.dataclass(frozen=True)
class Citation:
    path: pathlib.Path
    line: int
    text: str


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str], label: str) -> None:
    actual = set(value)
    wanted = set(expected)
    if actual != wanted:
        missing = sorted(wanted - actual)
        extra = sorted(actual - wanted)
        details = []
        if missing:
            details.append("missing {}".format(", ".join(missing)))
        if extra:
            details.append("unknown {}".format(", ".join(extra)))
        raise InvalidVerificationContract(
            "{} fields invalid: {}".format(label, "; ".join(details))
        )


def _verification_cwd(
    raw_cwd: Any,
    step: str,
    project_root: Optional[pathlib.Path],
) -> pathlib.Path:
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        raise InvalidVerificationContract(
            "step {} cwd must be a non-empty string".format(step)
        )
    if "\0" in raw_cwd:
        raise InvalidVerificationContract("step {} cwd contains a NUL byte".format(step))
    posix_path = pathlib.Path(raw_cwd)
    windows_path = pathlib.PureWindowsPath(raw_cwd)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
    ):
        raise InvalidVerificationContract(
            "step {} cwd must be project-relative".format(step)
        )
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise InvalidVerificationContract(
            "step {} cwd must not contain parent traversal".format(step)
        )
    if project_root is None:
        return posix_path
    try:
        resolved = (project_root / posix_path).resolve(strict=True)
        resolved.relative_to(project_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InvalidVerificationContract(
            "step {} cwd escapes or cannot resolve inside project".format(step)
        ) from exc
    if not resolved.is_dir():
        raise InvalidVerificationContract(
            "step {} cwd is not a directory".format(step)
        )
    return resolved


def validate_verification_contract(
    document: Any,
    project_dir: Optional[pathlib.Path] = None,
) -> VerificationContract:
    """Purely validate the exact v1 contract and return normalized, hashed facts.

    When ``project_dir`` is supplied, every active cwd is resolved strictly
    inside that existing directory. Without it, the function performs only
    schema and lexical path validation, which lets static tooling reuse the
    same rules without opening files or initializing state.
    """

    if not isinstance(document, dict):
        raise InvalidVerificationContract("verification contract root must be an object")
    _exact_keys(document, ("version", "steps", "n_a_reasons"), "contract")
    if type(document["version"]) is not int or document["version"] != 1:
        raise InvalidVerificationContract("verification contract version must be integer 1")
    steps = document["steps"]
    reasons = document["n_a_reasons"]
    if not isinstance(steps, dict):
        raise InvalidVerificationContract("contract steps must be an object")
    if not isinstance(reasons, dict):
        raise InvalidVerificationContract("contract n_a_reasons must be an object")
    _exact_keys(steps, VERIFICATION_STEPS, "steps")
    if any(not isinstance(key, str) for key in reasons):
        raise InvalidVerificationContract("n_a_reasons keys must be strings")

    root: Optional[pathlib.Path] = None
    if project_dir is not None:
        try:
            root = pathlib.Path(project_dir).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidVerificationContract("project directory cannot be resolved") from exc
        if not root.is_dir():
            raise InvalidVerificationContract("project directory is not a directory")

    active = []
    null_steps = []
    seen_pairs = set()
    canonical_steps: Dict[str, Any] = {}
    for name in VERIFICATION_STEPS:
        declaration = steps[name]
        if declaration is None:
            null_steps.append(name)
            canonical_steps[name] = None
            continue
        if not isinstance(declaration, dict):
            raise InvalidVerificationContract(
                "step {} must be an object or null".format(name)
            )
        _exact_keys(declaration, ("command", "cwd"), "step {}".format(name))
        command = declaration["command"]
        raw_cwd = declaration["cwd"]
        if not isinstance(command, str) or not command.strip():
            raise InvalidVerificationContract(
                "step {} command must be a non-empty string".format(name)
            )
        if _SHELL_METACHARACTERS.search(command):
            raise InvalidVerificationContract(
                "step {} command must be a bare command with no shell "
                "metacharacters (|, &, ;, <, >, `, $(, newline); wrap-and-swallow "
                "patterns like 'cmd || true' make the exit code meaningless"
                .format(name)
            )
        cwd = _verification_cwd(raw_cwd, name, root)
        pair = (command, str(cwd))
        if pair in seen_pairs:
            raise InvalidVerificationContract(
                "verification command/cwd pairs must be unique"
            )
        seen_pairs.add(pair)
        active.append(VerificationStepSpec(name, command, raw_cwd, cwd))
        canonical_steps[name] = {"command": command, "cwd": raw_cwd}

    if not active:
        raise InvalidVerificationContract(
            "verification contract must declare at least one active step; "
            "declaring build/typecheck/lint/test all null disables every "
            "verification requirement, which VERIFICATION.template.md forbids"
        )

    if set(reasons) != set(null_steps):
        raise InvalidVerificationContract(
            "n_a_reasons must contain exactly the null verification steps"
        )
    normalized_reasons: Dict[str, str] = {}
    for name in null_steps:
        reason = reasons[name]
        if (
            not isinstance(reason, str)
            or not reason.startswith("N/A:")
            or not reason[len("N/A:") :].strip()
        ):
            raise InvalidVerificationContract(
                "N/A reason for {} must use 'N/A: <non-empty explanation>'".format(name)
            )
        normalized_reasons[name] = reason

    canonical = {
        "version": 1,
        "steps": canonical_steps,
        "n_a_reasons": normalized_reasons,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return VerificationContract(
        tuple(active),
        normalized_reasons,
        hash_value(encoded, "verification_contract"),
        root,
    )


def safe_env_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    """Read an integer environment setting, falling back and clamping safely."""

    if minimum > maximum:
        raise ValueError("minimum must not exceed maximum")
    source = os.environ if environ is None else environ
    raw = source.get(name)
    try:
        value = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError, OverflowError):
        value = int(default)
    return max(minimum, min(maximum, value))


def normalize_text(value: str) -> str:
    """Normalize for exact human-text comparison without removing accents."""

    normalized = unicodedata.normalize("NFC", str(value)).casefold()
    return " ".join(normalized.split())


def _snake_to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def get_field(payload: Mapping[str, Any], name: str, default: Any = None) -> Any:
    """Get a top-level hook field accepting both snake_case and camelCase."""

    if not isinstance(payload, Mapping):
        return default
    candidates = (name, _snake_to_camel(name), _camel_to_snake(name))
    for candidate in candidates:
        if candidate in payload:
            return payload[candidate]
    return default


def hash_value(value: Any, domain: str = "value") -> str:
    """Return a domain-separated SHA-256 digest; never persist ``value`` itself."""

    raw = str(value).encode("utf-8", errors="strict")
    prefix = str(domain).encode("utf-8", errors="strict")
    return hashlib.sha256(prefix + b"\0" + raw).hexdigest()


def _fact_hash(value: Any, domain: str) -> str:
    """Hash a JSON fact deterministically; reject ambiguous/non-JSON values."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidStateInput("event fact must be deterministic JSON") from exc
    return hash_value(encoded, domain)


def default_state_root(environ: Optional[Mapping[str, str]] = None) -> pathlib.Path:
    """Return plugin-owned state root without using a shared temporary folder."""

    source = os.environ if environ is None else environ
    plugin_data = source.get("CLAUDE_PLUGIN_DATA", "").strip()
    if plugin_data:
        return pathlib.Path(plugin_data).expanduser().absolute() / "agent-kit"
    return pathlib.Path.home() / ".claude" / "agent-kit"


def _owner_is_current(info: os.stat_result) -> bool:
    getuid = getattr(os, "getuid", None)
    st_uid = getattr(info, "st_uid", None)
    return getuid is None or st_uid is None or st_uid == getuid()


def _assert_safe_existing(path: pathlib.Path, kind: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise StateUnavailable("cannot inspect {} {}: {}".format(kind, path, exc)) from exc
    if stat.S_ISLNK(info.st_mode):
        raise StateUnavailable("refusing symlink for {}: {}".format(kind, path))
    if not _owner_is_current(info):
        raise StateUnavailable("refusing {} owned by another user: {}".format(kind, path))
    return info


def _ensure_private_dir(path: pathlib.Path) -> pathlib.Path:
    """Create/check a user-owned, non-symlink directory and force mode 0700."""

    path = path.expanduser().absolute()
    try:
        _assert_safe_existing(path, "state directory")
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:  # A concurrent hook won the mkdir race.
            pass
        except OSError as exc:
            raise StateUnavailable("cannot create state directory {}: {}".format(path, exc)) from exc
    info = _assert_safe_existing(path, "state directory")
    if not stat.S_ISDIR(info.st_mode):
        raise StateUnavailable("state path is not a directory: {}".format(path))
    if os.name == "posix":
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise StateUnavailable("cannot secure state directory {}: {}".format(path, exc)) from exc
    return path


def _secure_regular_file(path: pathlib.Path, mode: int = 0o600) -> None:
    """Reject unsafe existing files and apply a private POSIX mode."""

    try:
        info = _assert_safe_existing(path, "state file")
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise StateUnavailable("state path is not a regular file: {}".format(path))
    if os.name == "posix":
        try:
            path.chmod(mode)
        except OSError as exc:
            raise StateUnavailable("cannot secure state file {}: {}".format(path, exc)) from exc


def _required(value: Any, label: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise InvalidStateInput("missing required {}".format(label))
    return text


class StateStore:
    """SQLite-backed state machine shared by plan and verification hooks."""

    def __init__(
        self,
        root: Optional[pathlib.Path] = None,
        busy_timeout_ms: Optional[int] = None,
    ) -> None:
        if _sqlite3 is None:
            raise StateUnavailable(
                "Python sqlite3 is unavailable; block mutations and READY reports"
            )
        self.root = _ensure_private_dir(default_state_root() if root is None else pathlib.Path(root))
        self.db_path = self.root / "state.sqlite3"
        _secure_regular_file(self.db_path)
        if busy_timeout_ms is None:
            busy_timeout_ms = safe_env_int(
                "AGENT_KIT_SQLITE_BUSY_TIMEOUT_MS",
                DEFAULT_BUSY_TIMEOUT_MS,
                MINIMUM_BUSY_TIMEOUT_MS,
                MAXIMUM_BUSY_TIMEOUT_MS,
            )
        self.busy_timeout_ms = max(
            MINIMUM_BUSY_TIMEOUT_MS,
            min(MAXIMUM_BUSY_TIMEOUT_MS, int(busy_timeout_ms)),
        )
        try:
            self._initialize()
        except StateError:
            raise
        except Exception as exc:
            raise StateUnavailable("cannot initialize SQLite state: {}".format(exc)) from exc

    def _connect(self):
        assert _sqlite3 is not None
        _secure_regular_file(self.db_path)
        connection = None
        try:
            connection = _sqlite3.connect(
                str(self.db_path),
                timeout=self.busy_timeout_ms / 1000.0,
                isolation_level=None,
            )
            connection.row_factory = _sqlite3.Row
            connection.execute("PRAGMA busy_timeout = {}".format(self.busy_timeout_ms))
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            _secure_regular_file(self.db_path)
            for suffix in ("-wal", "-shm"):
                _secure_regular_file(pathlib.Path(str(self.db_path) + suffix))
            return connection
        except StateError:
            if connection is not None:
                connection.close()
            raise
        except Exception as exc:
            if connection is not None:
                connection.close()
            raise StateUnavailable("cannot open SQLite state: {}".format(exc)) from exc

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except StateError:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise StateUnavailable("SQLite state transaction failed: {}".format(exc)) from exc
        finally:
            connection.close()

    @contextlib.contextmanager
    def _reader(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            yield connection
        except StateError:
            raise
        except Exception as exc:
            raise StateUnavailable("SQLite state read failed: {}".format(exc)) from exc
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._transaction() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION or current < 0:
                raise StateUnavailable(
                    "unsupported state schema {}; expected {}".format(current, SCHEMA_VERSION)
                )
            if current == 0:
                self._create_v2_schema(connection)
            elif current == 1:
                self._migrate_v1_to_v2(connection)
            else:
                self._assert_v2_schema(connection)
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute("PRAGMA user_version = {}".format(SCHEMA_VERSION))
            self._assert_v2_schema(connection)
        self._secure_sqlite_artifacts()

    @staticmethod
    def _create_v2_schema(connection: Any) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS plan_approvals (
                session_hash TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                approved_at REAL NOT NULL,
                PRIMARY KEY (session_hash, prompt_hash)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS mutation_epochs (
                project_hash TEXT NOT NULL,
                session_hash TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                epoch INTEGER NOT NULL CHECK (epoch >= 0),
                background_tainted INTEGER NOT NULL DEFAULT 0
                    CHECK (background_tainted IN (0, 1)),
                updated_at REAL NOT NULL,
                PRIMARY KEY (project_hash, session_hash, prompt_hash)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS mutation_events (
                project_hash TEXT NOT NULL,
                session_hash TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                actor_hash TEXT NOT NULL,
                tool_use_hash TEXT NOT NULL,
                fact_hash TEXT NOT NULL,
                epoch INTEGER NOT NULL CHECK (epoch >= 1),
                created_at REAL NOT NULL,
                PRIMARY KEY (
                    project_hash, session_hash, prompt_hash, tool_use_hash
                )
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS verification_events (
                project_hash TEXT NOT NULL,
                session_hash TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                agent_hash TEXT NOT NULL,
                step TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 0),
                tool_use_hash TEXT NOT NULL,
                contract_hash TEXT NOT NULL,
                command_hash TEXT NOT NULL,
                cwd_hash TEXT NOT NULL,
                outcome TEXT NOT NULL,
                receipt_hash TEXT,
                epoch INTEGER NOT NULL CHECK (epoch >= 0),
                recorded_at REAL NOT NULL,
                PRIMARY KEY (
                    project_hash, session_hash, prompt_hash, agent_hash, tool_use_hash
                )
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS verification_live (
                project_hash TEXT NOT NULL,
                session_hash TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                agent_hash TEXT NOT NULL,
                step TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 0),
                tool_use_hash TEXT NOT NULL,
                contract_hash TEXT NOT NULL,
                command_hash TEXT NOT NULL,
                cwd_hash TEXT NOT NULL,
                receipt_hash TEXT NOT NULL,
                epoch INTEGER NOT NULL CHECK (epoch >= 0),
                recorded_at REAL NOT NULL,
                PRIMARY KEY (project_hash, session_hash, prompt_hash, agent_hash, step)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS verification_event_receipt_idx
                ON verification_events (
                    project_hash, session_hash, prompt_hash, agent_hash,
                    step, receipt_hash
                )
            """,
            """
            CREATE INDEX IF NOT EXISTS verification_live_scope_idx
                ON verification_live (
                    project_hash, session_hash, prompt_hash, agent_hash,
                    contract_hash, epoch, position
                )
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @classmethod
    def _migrate_v1_to_v2(cls, connection: Any) -> None:
        """Invalidate all v1 live evidence while preserving plan approvals."""

        # DDL is transactional in SQLite. The caller already owns BEGIN
        # IMMEDIATE, so any failure rolls every DROP/CREATE back atomically.
        for legacy in (
            "verification_records",
            "verification_events",
            "verification_live",
            "mutation_events",
            "mutation_epochs",
        ):
            connection.execute("DROP TABLE IF EXISTS {}".format(legacy))
        cls._create_v2_schema(connection)

    @staticmethod
    def _assert_v2_schema(connection: Any) -> None:
        expected = {
            "meta": {"key", "value"},
            "plan_approvals": {
                "session_hash",
                "prompt_hash",
                "plan_hash",
                "approved_at",
            },
            "mutation_epochs": {
                "project_hash",
                "session_hash",
                "prompt_hash",
                "epoch",
                "background_tainted",
                "updated_at",
            },
            "mutation_events": {
                "project_hash",
                "session_hash",
                "prompt_hash",
                "actor_hash",
                "tool_use_hash",
                "fact_hash",
                "epoch",
                "created_at",
            },
            "verification_events": {
                "project_hash",
                "session_hash",
                "prompt_hash",
                "agent_hash",
                "step",
                "position",
                "tool_use_hash",
                "contract_hash",
                "command_hash",
                "cwd_hash",
                "outcome",
                "receipt_hash",
                "epoch",
                "recorded_at",
            },
            "verification_live": {
                "project_hash",
                "session_hash",
                "prompt_hash",
                "agent_hash",
                "step",
                "position",
                "tool_use_hash",
                "contract_hash",
                "command_hash",
                "cwd_hash",
                "receipt_hash",
                "epoch",
                "recorded_at",
            },
        }
        for table, columns in expected.items():
            rows = connection.execute("PRAGMA table_info({})".format(table)).fetchall()
            actual = {str(row[1]) for row in rows}
            if actual != columns:
                raise StateUnavailable("state schema table {} is invalid".format(table))

    def _secure_sqlite_artifacts(self) -> None:
        _secure_regular_file(self.db_path)
        for suffix in ("-wal", "-shm"):
            _secure_regular_file(pathlib.Path(str(self.db_path) + suffix))

    @staticmethod
    def _scope(session_id: Any, prompt_id: Any) -> Tuple[str, str]:
        session = _required(session_id, "session_id")
        prompt = _required(prompt_id, "prompt_id")
        return hash_value(session, "session"), hash_value(prompt, "prompt")

    @staticmethod
    def _project(project_id: Any) -> str:
        raw = _required(project_id, "project_id")
        try:
            project = pathlib.Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidStateInput("project_id cannot be resolved") from exc
        if not project.is_dir():
            raise InvalidStateInput("project_id is not a directory")
        canonical = os.path.normcase(str(project))
        return hash_value(canonical, "project")

    @staticmethod
    def _agent(agent_id: Any) -> str:
        return hash_value(_required(agent_id, "agent_id"), "agent")

    def approve_plan(self, session_id: Any, prompt_id: Any, plan: Any) -> str:
        session_hash, prompt_hash = self._scope(session_id, prompt_id)
        plan_text = _required(plan, "plan")
        plan_hash = hash_value(plan_text, "plan")
        now = time.time()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO plan_approvals(
                    session_hash, prompt_hash, plan_hash, approved_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(session_hash, prompt_hash) DO UPDATE SET
                    plan_hash = excluded.plan_hash,
                    approved_at = excluded.approved_at
                """,
                (session_hash, prompt_hash, plan_hash, now),
            )
        return plan_hash

    def check_plan(
        self,
        session_id: Any,
        prompt_id: Any,
        plan: Optional[Any] = None,
    ) -> bool:
        session_hash, prompt_hash = self._scope(session_id, prompt_id)
        with self._reader() as connection:
            row = connection.execute(
                """
                SELECT plan_hash FROM plan_approvals
                WHERE session_hash = ? AND prompt_hash = ?
                """,
                (session_hash, prompt_hash),
            ).fetchone()
        if row is None:
            return False
        return plan is None or row["plan_hash"] == hash_value(_required(plan, "plan"), "plan")

    def cleanup_prompt(self, session_id: Any, prompt_id: Any) -> None:
        session_hash, prompt_hash = self._scope(session_id, prompt_id)
        with self._transaction() as connection:
            for table in (
                "plan_approvals",
                "mutation_epochs",
                "mutation_events",
                "verification_events",
                "verification_live",
            ):
                connection.execute(
                    "DELETE FROM {} WHERE session_hash = ? AND prompt_hash = ?".format(table),
                    (session_hash, prompt_hash),
                )

    def cleanup_session(self, session_id: Any) -> None:
        session_hash = hash_value(_required(session_id, "session_id"), "session")
        with self._transaction() as connection:
            for table in (
                "plan_approvals",
                "mutation_epochs",
                "mutation_events",
                "verification_events",
                "verification_live",
            ):
                connection.execute(
                    "DELETE FROM {} WHERE session_hash = ?".format(table),
                    (session_hash,),
                )

    def _current_epoch_hashes(
        self,
        connection: Any,
        project_hash: str,
        session_hash: str,
        prompt_hash: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT epoch FROM mutation_epochs
            WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
            """,
            (project_hash, session_hash, prompt_hash),
        ).fetchone()
        return int(row["epoch"]) if row is not None else 0

    def _background_tainted_hashes(
        self,
        connection: Any,
        project_hash: str,
        session_hash: str,
        prompt_hash: str,
    ) -> bool:
        row = connection.execute(
            """
            SELECT background_tainted FROM mutation_epochs
            WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
            """,
            (project_hash, session_hash, prompt_hash),
        ).fetchone()
        return bool(row["background_tainted"]) if row is not None else False

    def current_mutation_epoch(
        self,
        project_id: Any,
        session_id: Any,
        prompt_id: Any,
    ) -> int:
        project_hash = self._project(project_id)
        session_hash, prompt_hash = self._scope(session_id, prompt_id)
        with self._reader() as connection:
            return self._current_epoch_hashes(
                connection, project_hash, session_hash, prompt_hash
            )

    def bump_mutation_epoch(
        self,
        project_id: Any,
        session_id: Any,
        prompt_id: Any,
        actor_id: Any,
        tool_use_id: Any,
        mutation_fact: Any,
        background: bool = False,
    ) -> int:
        """Advance one project epoch for a mutation from any actor.

        Idempotency is keyed by runtime tool id plus a deterministic fact hash.
        Reusing an id with a different actor or fact is a fail-closed conflict.
        """

        project_hash = self._project(project_id)
        session_hash, prompt_hash = self._scope(session_id, prompt_id)
        actor_hash = self._agent(actor_id)
        tool_hash = hash_value(_required(tool_use_id, "tool_use_id"), "tool_use")
        background_flag = bool(background)
        fact_hash = _fact_hash(
            {"background": background_flag, "fact": mutation_fact},
            "mutation_fact",
        )
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT actor_hash, fact_hash, epoch FROM mutation_events
                WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
                  AND tool_use_hash = ?
                """,
                (project_hash, session_hash, prompt_hash, tool_hash),
            ).fetchone()
            if existing is not None:
                if (
                    existing["actor_hash"] != actor_hash
                    or existing["fact_hash"] != fact_hash
                ):
                    raise StateConflict(
                        "tool_use_id was reused with different mutation facts"
                    )
                return int(existing["epoch"])
            epoch = self._current_epoch_hashes(
                connection, project_hash, session_hash, prompt_hash
            ) + 1
            now = time.time()
            connection.execute(
                """
                INSERT INTO mutation_epochs(
                    project_hash, session_hash, prompt_hash, epoch,
                    background_tainted, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_hash, session_hash, prompt_hash) DO UPDATE SET
                    epoch = excluded.epoch,
                    background_tainted = CASE
                        WHEN mutation_epochs.background_tainted = 1
                          OR excluded.background_tainted = 1 THEN 1 ELSE 0 END,
                    updated_at = excluded.updated_at
                """,
                (
                    project_hash,
                    session_hash,
                    prompt_hash,
                    epoch,
                    1 if background_flag else 0,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO mutation_events(
                    project_hash, session_hash, prompt_hash, actor_hash,
                    tool_use_hash, fact_hash, epoch, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_hash,
                    session_hash,
                    prompt_hash,
                    actor_hash,
                    tool_hash,
                    fact_hash,
                    epoch,
                    now,
                ),
            )
            connection.execute(
                """
                DELETE FROM verification_live
                WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
                """,
                (project_hash, session_hash, prompt_hash),
            )
            return epoch

    def record_verification(
        self,
        project_id: Any,
        session_id: Any,
        prompt_id: Any,
        agent_id: Any,
        step: Any,
        tool_use_id: Any,
        command: Any,
        cwd: Any,
        outcome: str,
        contract: VerificationContract,
    ) -> VerificationResult:
        """Record an exact step and advance the live chain only in order."""

        if not isinstance(contract, VerificationContract) or contract.project_root is None:
            raise InvalidStateInput(
                "record_verification requires a project-bound validated contract"
            )
        project_hash = self._project(project_id)
        if project_hash != self._project(contract.project_root):
            raise InvalidStateInput("project_id does not match verification contract")
        session_hash, prompt_hash = self._scope(session_id, prompt_id)
        agent_hash = self._agent(agent_id)
        step_text = _required(step, "verification step")
        expected_step = contract.step(step_text)
        if expected_step is None:
            raise InvalidStateInput("step is not active in verification contract")
        position = contract.required_steps.index(step_text)
        tool_hash = hash_value(_required(tool_use_id, "tool_use_id"), "tool_use")
        if not isinstance(command, str) or not command:
            raise InvalidStateInput("missing required command")
        if command != expected_step.command:
            raise InvalidStateInput("command does not match verification contract")
        raw_cwd = _required(cwd, "cwd")
        candidate = pathlib.Path(raw_cwd).expanduser()
        if not candidate.is_absolute():
            candidate = contract.project_root / candidate
        try:
            actual_cwd = candidate.resolve(strict=True)
            actual_cwd.relative_to(contract.project_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise InvalidStateInput("cwd escapes or cannot resolve inside project") from exc
        if not actual_cwd.is_dir() or actual_cwd != expected_step.cwd:
            raise InvalidStateInput("cwd does not match verification contract")
        command_hash = hash_value(command, "command")
        cwd_hash = hash_value(str(actual_cwd), "cwd")
        outcome_text = _required(outcome, "outcome")
        if outcome_text not in ALLOWED_OUTCOMES:
            raise InvalidStateInput("unsupported verification outcome: {}".format(outcome_text))

        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT step, position, contract_hash, command_hash, cwd_hash,
                       outcome, epoch
                FROM verification_events
                WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
                  AND agent_hash = ? AND tool_use_hash = ?
                """,
                (
                    project_hash,
                    session_hash,
                    prompt_hash,
                    agent_hash,
                    tool_hash,
                ),
            ).fetchone()
            if existing is not None:
                expected = (
                    step_text,
                    position,
                    contract.fingerprint,
                    command_hash,
                    cwd_hash,
                    outcome_text,
                )
                actual = (
                    existing["step"],
                    int(existing["position"]),
                    existing["contract_hash"],
                    existing["command_hash"],
                    existing["cwd_hash"],
                    existing["outcome"],
                )
                if actual != expected:
                    raise StateConflict(
                        "tool_use_id was reused with different verification facts"
                    )
                return VerificationResult(
                    False, None, int(existing["epoch"]), outcome_text
                )

            epoch = self._current_epoch_hashes(
                connection, project_hash, session_hash, prompt_hash
            )
            now = time.time()
            if outcome_text != SUCCESS_OUTCOME:
                epoch += 1
                background = 1 if outcome_text == "background" else 0
                connection.execute(
                    """
                    INSERT INTO mutation_epochs(
                        project_hash, session_hash, prompt_hash, epoch,
                        background_tainted, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_hash, session_hash, prompt_hash) DO UPDATE SET
                        epoch = excluded.epoch,
                        background_tainted = CASE
                            WHEN mutation_epochs.background_tainted = 1
                              OR excluded.background_tainted = 1 THEN 1 ELSE 0 END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        project_hash,
                        session_hash,
                        prompt_hash,
                        epoch,
                        background,
                        now,
                    ),
                )
                # Scoped to the reporting agent only: a failed/interrupted/
                # background verification from agent B must not erase agent
                # A's already-recorded live chain for the same project/
                # session/prompt (matches the agent_hash column on the event
                # row inserted immediately below).
                connection.execute(
                    """
                    DELETE FROM verification_live
                    WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
                      AND agent_hash = ?
                    """,
                    (project_hash, session_hash, prompt_hash, agent_hash),
                )
                connection.execute(
                    """
                    INSERT INTO verification_events(
                        project_hash, session_hash, prompt_hash, agent_hash,
                        step, position, tool_use_hash, contract_hash,
                        command_hash, cwd_hash, outcome, receipt_hash, epoch,
                        recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        project_hash,
                        session_hash,
                        prompt_hash,
                        agent_hash,
                        step_text,
                        position,
                        tool_hash,
                        contract.fingerprint,
                        command_hash,
                        cwd_hash,
                        outcome_text,
                        epoch,
                        now,
                    ),
                )
                return VerificationResult(True, None, epoch, outcome_text)

            # A different contract/epoch is never mixed into a live chain.
            connection.execute(
                """
                DELETE FROM verification_live
                WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
                  AND agent_hash = ? AND (contract_hash != ? OR epoch != ?)
                """,
                (
                    project_hash,
                    session_hash,
                    prompt_hash,
                    agent_hash,
                    contract.fingerprint,
                    epoch,
                ),
            )
            # Re-running step i invalidates i and every downstream live result.
            connection.execute(
                """
                DELETE FROM verification_live
                WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
                  AND agent_hash = ? AND position >= ?
                """,
                (project_hash, session_hash, prompt_hash, agent_hash, position),
            )
            upstream = connection.execute(
                """
                SELECT position FROM verification_live
                WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
                  AND agent_hash = ? AND contract_hash = ? AND epoch = ?
                  AND position < ?
                ORDER BY position
                """,
                (
                    project_hash,
                    session_hash,
                    prompt_hash,
                    agent_hash,
                    contract.fingerprint,
                    epoch,
                    position,
                ),
            ).fetchall()
            ordered = tuple(int(row["position"]) for row in upstream) == tuple(
                range(position)
            )
            receipt = secrets.token_urlsafe(32) if ordered else None
            receipt_hash = hash_value(receipt, "receipt") if receipt is not None else None
            connection.execute(
                """
                INSERT INTO verification_events(
                    project_hash, session_hash, prompt_hash, agent_hash,
                    step, position, tool_use_hash, contract_hash,
                    command_hash, cwd_hash, outcome, receipt_hash, epoch,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_hash,
                    session_hash,
                    prompt_hash,
                    agent_hash,
                    step_text,
                    position,
                    tool_hash,
                    contract.fingerprint,
                    command_hash,
                    cwd_hash,
                    outcome_text,
                    receipt_hash,
                    epoch,
                    now,
                ),
            )
            if receipt_hash is not None:
                connection.execute(
                    """
                    INSERT INTO verification_live(
                        project_hash, session_hash, prompt_hash, agent_hash,
                        step, position, tool_use_hash, contract_hash,
                        command_hash, cwd_hash, receipt_hash, epoch, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_hash,
                        session_hash,
                        prompt_hash,
                        agent_hash,
                        step_text,
                        position,
                        tool_hash,
                        contract.fingerprint,
                        command_hash,
                        cwd_hash,
                        receipt_hash,
                        epoch,
                        now,
                    ),
                )
        return VerificationResult(True, receipt, epoch, outcome_text)

    def get_verification(
        self,
        project_id: Any,
        session_id: Any,
        prompt_id: Any,
        agent_id: Any,
        step: Optional[str] = None,
    ) -> Tuple[VerificationRecord, ...]:
        project_hash = self._project(project_id)
        session_hash, prompt_hash = self._scope(session_id, prompt_id)
        agent_hash = self._agent(agent_id)
        query = """
            SELECT step, tool_use_hash, command_hash, cwd_hash,
                   outcome, epoch, recorded_at
            FROM verification_events
            WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
              AND agent_hash = ?
        """
        params: Tuple[Any, ...] = (
            project_hash,
            session_hash,
            prompt_hash,
            agent_hash,
        )
        if step is not None:
            query += " AND step = ?"
            params += (_required(step, "verification step"),)
        query += " ORDER BY recorded_at, step, tool_use_hash"
        with self._reader() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            VerificationRecord(
                row["step"],
                row["tool_use_hash"],
                row["command_hash"],
                row["cwd_hash"],
                row["outcome"],
                int(row["epoch"]),
                float(row["recorded_at"]),
            )
            for row in rows
        )

    def validate_receipts(
        self,
        project_id: Any,
        session_id: Any,
        prompt_id: Any,
        agent_id: Any,
        receipts: Mapping[str, str],
        contract: VerificationContract,
    ) -> ReceiptValidation:
        """Validate the complete current ordered chain against today's contract."""

        if not isinstance(contract, VerificationContract) or contract.project_root is None:
            return ReceiptValidation(
                False, ("validation requires a project-bound verification contract",)
            )
        try:
            project_hash = self._project(project_id)
            if project_hash != self._project(contract.project_root):
                return ReceiptValidation(
                    False, ("project_id does not match verification contract",)
                )
        except StateError as exc:
            return ReceiptValidation(False, (str(exc),))
        session_hash, prompt_hash = self._scope(session_id, prompt_id)
        agent_hash = self._agent(agent_id)
        if not isinstance(receipts, Mapping):
            return ReceiptValidation(False, ("receipts must be an object",))
        normalized: Dict[str, str] = {}
        for raw_step, raw_receipt in receipts.items():
            try:
                normalized[_required(raw_step, "verification step")] = _required(
                    raw_receipt, "receipt"
                )
            except InvalidStateInput as exc:
                return ReceiptValidation(False, (str(exc),))
        required = contract.required_steps
        errors = []
        extras = sorted(set(normalized) - set(required))
        for step in extras:
            errors.append("unexpected receipt for step {}".format(step))
        for step in required:
            if step not in normalized:
                errors.append("missing receipt for step {}".format(step))

        with self._reader() as connection:
            epoch = self._current_epoch_hashes(
                connection, project_hash, session_hash, prompt_hash
            )
            if self._background_tainted_hashes(
                connection, project_hash, session_hash, prompt_hash
            ):
                errors.append("prompt is tainted by background verification")
            rows = connection.execute(
                """
                SELECT step, position, command_hash, cwd_hash, receipt_hash
                FROM verification_live
                WHERE project_hash = ? AND session_hash = ? AND prompt_hash = ?
                  AND agent_hash = ? AND contract_hash = ? AND epoch = ?
                ORDER BY position
                """,
                (
                    project_hash,
                    session_hash,
                    prompt_hash,
                    agent_hash,
                    contract.fingerprint,
                    epoch,
                ),
            ).fetchall()
            live = {str(row["step"]): row for row in rows}
            if tuple(str(row["step"]) for row in rows) != required:
                errors.append("live verification chain is incomplete or out of order")
            for position, expected in enumerate(contract.active_steps):
                receipt = normalized.get(expected.name)
                row = live.get(expected.name)
                if receipt is None:
                    continue
                if row is None:
                    errors.append(
                        "invalid or stale receipt for step {}".format(expected.name)
                    )
                    continue
                expected_command = hash_value(expected.command, "command")
                expected_cwd = hash_value(str(expected.cwd), "cwd")
                if (
                    int(row["position"]) != position
                    or row["command_hash"] != expected_command
                    or row["cwd_hash"] != expected_cwd
                ):
                    errors.append(
                        "contract mismatch for step {}".format(expected.name)
                    )
                    continue
                if row["receipt_hash"] != hash_value(receipt, "receipt"):
                    errors.append("invalid receipt for step {}".format(expected.name))
        return ReceiptValidation(not errors, tuple(errors))


def _redact(value: Any, key: str = "", depth: int = 0) -> Any:
    """Return only fixed metadata/enums/hashes; redact every unknown scalar."""

    if depth > 8:
        return "[TRUNCATED]"
    hash_field = bool(key) and key.casefold().endswith("_hash")
    if key and key not in _SAFE_METADATA_KEYS and not hash_field:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        redacted: Dict[str, Any] = {}
        for index, (item_key, item_value) in enumerate(list(value.items())[:100]):
            item_text = str(item_key)
            known = item_text in _SAFE_METADATA_KEYS or item_text.casefold().endswith(
                "_hash"
            )
            stored_key = item_text[:128] if known else "redacted_{}".format(index)
            redacted[stored_key] = _redact(
                item_value,
                item_text if known else "",
                depth + 1,
            ) if known else "[REDACTED]"
        return redacted
    if isinstance(value, (list, tuple)):
        if key != "steps":
            return "[REDACTED]"
        return [
            item if isinstance(item, str) and item in VERIFICATION_STEPS else "[REDACTED]"
            for item in value[: len(VERIFICATION_STEPS)]
        ]
    if isinstance(value, str):
        if hash_field and _SHA256.fullmatch(value):
            return value
        if key in _SAFE_METADATA_KEYS and value in _SAFE_ENUM_VALUES:
            return value
        return "[REDACTED]"
    if value is None:
        return None if key in _SAFE_METADATA_KEYS else "[REDACTED]"
    if isinstance(value, bool):
        return value if key in _SAFE_METADATA_KEYS else "[REDACTED]"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if key in {"count", "epoch", "schema_version"}:
            return value
        if key in {"created", "json", "object", "safe", "background"}:
            return bool(value)
        return "[REDACTED]"
    return "[REDACTED]"


def _rotate(path: pathlib.Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _secure_regular_file(path)
    rotated = pathlib.Path(str(path) + ".1")
    if rotated.exists() or rotated.is_symlink():
        _secure_regular_file(rotated)
        try:
            rotated.unlink()
        except OSError as exc:
            raise StateUnavailable("cannot rotate state file {}: {}".format(path, exc)) from exc
    try:
        path.replace(rotated)
        if os.name == "posix":
            rotated.chmod(0o600)
    except OSError as exc:
        raise StateUnavailable("cannot rotate state file {}: {}".format(path, exc)) from exc


def _write_private(path: pathlib.Path, data: bytes, append: bool = False) -> None:
    _secure_regular_file(path)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags | nofollow, 0o600)
        with os.fdopen(descriptor, "ab" if append else "wb") as handle:
            handle.write(data)
        _secure_regular_file(path)
    except StateError:
        raise
    except OSError as exc:
        raise StateUnavailable("cannot write private state file {}: {}".format(path, exc)) from exc


def secure_log(
    event: str,
    fields: Optional[Mapping[str, Any]] = None,
    root: Optional[pathlib.Path] = None,
    max_bytes: Optional[int] = None,
) -> pathlib.Path:
    """Append one capped, redacted JSON event to the private plugin log."""

    state_root = _ensure_private_dir(default_state_root() if root is None else pathlib.Path(root))
    path = state_root / "agent-kit.log"
    cap = max_bytes or safe_env_int(
        "AGENT_KIT_LOG_MAX_BYTES", 256 * 1024, 4 * 1024, 4 * 1024 * 1024
    )
    record = {
        "event_hash": hash_value(str(event), "log_event"),
        "time": round(time.time(), 3),
        "fields": _redact(dict(fields or {})),
    }
    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > cap:
        encoded = (json.dumps(
            {
                "event_hash": record["event_hash"],
                "time": record["time"],
                "fields": "[TRUNCATED]",
                "record_sha256": hashlib.sha256(encoded).hexdigest(),
            },
            sort_keys=True,
        ) + "\n").encode("utf-8")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    except OSError as exc:
        raise StateUnavailable("cannot inspect log {}: {}".format(path, exc)) from exc
    if size + len(encoded) > cap:
        _rotate(path)
    _write_private(path, encoded, append=True)
    return path


def secure_dump(
    payload: Any,
    label: str = "hook-payload",
    root: Optional[pathlib.Path] = None,
    max_bytes: Optional[int] = None,
) -> pathlib.Path:
    """Write a redacted, capped diagnostic snapshot; retain at most one backup."""

    state_root = _ensure_private_dir(default_state_root() if root is None else pathlib.Path(root))
    # Labels can be influenced by callers, so the filename stores only a hash.
    label_hash = hash_value(str(label), "dump_label")[:16]
    path = state_root / ("dump-" + label_hash + ".json")
    cap = max_bytes or safe_env_int(
        "AGENT_KIT_DUMP_MAX_BYTES", 256 * 1024, 4 * 1024, 4 * 1024 * 1024
    )
    encoded = (json.dumps(_redact(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > cap:
        encoded = (json.dumps(
            {
                "truncated": True,
                "payload_sha256": hashlib.sha256(encoded).hexdigest(),
                "redacted_size": len(encoded),
            },
            sort_keys=True,
            indent=2,
        ) + "\n").encode("utf-8")
    if path.exists() or path.is_symlink():
        _rotate(path)
    _write_private(path, encoded)
    return path


def bump_stop_retry_attempts(
    project_id: Any,
    session_id: Any,
    prompt_id: Any,
    agent_id: Any,
    root: Optional[pathlib.Path] = None,
) -> int:
    """Increment and return a best-effort retry counter for one stop identity.

    Guards a Stop/SubagentStop retry loop (``stop_hook_active``) against an
    unbounded number of blocks for the exact same claim: callers cap on the
    returned count and let a retried claim through, loudly, once the cap is
    exceeded. Backed by a private counter file rather than the SQLite schema
    on purpose, so this can never perturb SCHEMA_VERSION or the v2 table
    shapes. The read-then-write is not linearizable across concurrent
    processes; treat the count as an approximate bound that exists only to
    avoid deadlocking a hook, never as a security boundary.
    """

    project = _required(project_id, "project_id")
    try:
        resolved_project = pathlib.Path(project).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidStateInput("project_id cannot be resolved") from exc
    if not resolved_project.is_dir():
        raise InvalidStateInput("project_id is not a directory")
    canonical_project = os.path.normcase(str(resolved_project))
    identity_hash = _fact_hash(
        {
            "agent": _required(agent_id, "agent_id"),
            "project": canonical_project,
            "prompt": _required(prompt_id, "prompt_id"),
            "session": _required(session_id, "session_id"),
        },
        "stop_retry_identity",
    )
    state_root = _ensure_private_dir(default_state_root() if root is None else pathlib.Path(root))
    path = state_root / "stop-retry-{}.count".format(identity_hash[:40])
    _secure_regular_file(path)
    try:
        current = int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        current = 0
    except (OSError, ValueError):
        current = 0
    updated = current + 1
    _write_private(path, "{}\n".format(updated).encode("utf-8"))
    return updated


def resolve_project_citation(citation: str, project_root: pathlib.Path) -> Citation:
    """Resolve a local ``path:line`` citation without allowing project escape."""

    match = _CITATION.match(str(citation))
    if match is None:
        raise ValueError("citation must use path:line")
    raw_path, raw_line = match.groups()
    relative = pathlib.Path(raw_path)
    if relative.is_absolute():
        raise ValueError("absolute citation paths are not allowed")
    try:
        root = pathlib.Path(project_root).expanduser().resolve(strict=True)
        candidate = (root / relative).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("citation escapes or cannot be resolved") from exc
    if not candidate.is_file():
        raise ValueError("citation target is not a regular file")
    line_number = int(raw_line)
    try:
        with candidate.open("r", encoding="utf-8", errors="replace") as handle:
            for index, text in enumerate(handle, 1):
                if index == line_number:
                    return Citation(candidate, line_number, text.rstrip("\r\n"))
    except OSError as exc:
        raise ValueError("citation target cannot be read") from exc
    raise ValueError("citation line is out of range")


__all__ = [
    "ALLOWED_OUTCOMES",
    "STOP_RETRY_CAP",
    "Citation",
    "InvalidStateInput",
    "InvalidVerificationContract",
    "MAXIMUM_BUSY_TIMEOUT_MS",
    "ReceiptValidation",
    "SCHEMA_VERSION",
    "SUCCESS_OUTCOME",
    "StateConflict",
    "StateError",
    "StateStore",
    "StateUnavailable",
    "VerificationRecord",
    "VerificationResult",
    "VerificationContract",
    "VerificationStepSpec",
    "VERIFICATION_STEPS",
    "bump_stop_retry_attempts",
    "default_state_root",
    "get_field",
    "hash_value",
    "normalize_text",
    "resolve_project_citation",
    "safe_env_int",
    "secure_dump",
    "secure_log",
    "validate_verification_contract",
]
