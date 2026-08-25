"""Regression tests for the shared v2 state/security primitives."""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import pathlib
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from hooks import _shared as shared


def contract_document(
    *,
    build: str = "python3 -m compileall -q src",
    lint: str = "python3 tools/lint.py",
    test: str = "python3 -m unittest -v",
    lint_cwd: str = ".",
) -> dict:
    return {
        "version": 1,
        "steps": {
            "build": {"command": build, "cwd": "."},
            "typecheck": None,
            "lint": {"command": lint, "cwd": lint_cwd},
            "test": {"command": test, "cwd": "."},
        },
        "n_a_reasons": {
            "typecheck": "N/A: project does not use a static type checker"
        },
    }


class TemporaryStateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.other_project = self.base / "other-project"
        self.other_project.mkdir()
        self.state_root = self.base / "state"
        self.store = shared.StateStore(self.state_root, busy_timeout_ms=250)
        self.contract = shared.validate_verification_contract(
            contract_document(), self.project
        )

    def record(
        self,
        step: str,
        tool_use_id: str,
        *,
        project: pathlib.Path | None = None,
        contract: shared.VerificationContract | None = None,
        agent: str = "agent-A",
        outcome: str = shared.SUCCESS_OUTCOME,
        command: str | None = None,
        cwd: pathlib.Path | None = None,
    ) -> shared.VerificationResult:
        selected = contract or self.contract
        spec = selected.step(step)
        assert spec is not None
        return self.store.record_verification(
            project_id=project or self.project,
            session_id="session-A",
            prompt_id="prompt-A",
            agent_id=agent,
            step=step,
            tool_use_id=tool_use_id,
            command=spec.command if command is None else command,
            cwd=str(spec.cwd if cwd is None else cwd),
            outcome=outcome,
            contract=selected,
        )

    def full_chain(
        self,
        *,
        suffix: str = "one",
        contract: shared.VerificationContract | None = None,
    ) -> dict[str, str]:
        receipts = {}
        selected = contract or self.contract
        for step in selected.required_steps:
            result = self.record(step, "{}-{}".format(suffix, step), contract=selected)
            self.assertIsNotNone(result.receipt)
            receipts[step] = result.receipt or ""
        return receipts

    def validate(
        self,
        receipts: dict[str, str],
        *,
        project: pathlib.Path | None = None,
        contract: shared.VerificationContract | None = None,
        agent: str = "agent-A",
    ) -> shared.ReceiptValidation:
        return self.store.validate_receipts(
            project_id=project or self.project,
            session_id="session-A",
            prompt_id="prompt-A",
            agent_id=agent,
            receipts=receipts,
            contract=contract or self.contract,
        )


class HelperAndContractTests(unittest.TestCase):
    def test_safe_env_int_invalid_default_and_clamps(self) -> None:
        cases = (
            ({}, 5),
            ({"LIMIT": "not-an-int"}, 5),
            ({"LIMIT": "-99"}, 1),
            ({"LIMIT": "999"}, 10),
            ({"LIMIT": " 7 "}, 7),
        )
        for environ, expected in cases:
            with self.subTest(environ=environ):
                self.assertEqual(
                    shared.safe_env_int("LIMIT", 5, 1, 10, environ), expected
                )

    def test_normalize_text_and_payload_adapter(self) -> None:
        self.assertEqual(shared.normalize_text("CA\u0301C   THU\u031B\u0301"), "các thứ")
        payload = {"sessionId": "s", "prompt_id": "p", "toolName": "Bash"}
        self.assertEqual(shared.get_field(payload, "session_id"), "s")
        self.assertEqual(shared.get_field(payload, "promptId"), "p")
        self.assertEqual(shared.get_field(payload, "tool_name"), "Bash")

    def test_default_state_root_uses_plugin_data_or_private_home(self) -> None:
        plugin = shared.default_state_root({"CLAUDE_PLUGIN_DATA": "/private/plugin"})
        self.assertEqual(
            plugin,
            pathlib.Path("/private/plugin").expanduser().absolute() / "agent-kit",
        )
        self.assertEqual(
            shared.default_state_root({}), pathlib.Path.home() / ".claude" / "agent-kit"
        )

    def test_contract_validator_returns_order_and_stable_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first = shared.validate_verification_contract(contract_document(), root)
            second = shared.validate_verification_contract(contract_document(), root)
        self.assertEqual(first.required_steps, ("build", "lint", "test"))
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)

    def test_contract_validator_rejects_bool_version_and_non_exact_fields(self) -> None:
        invalid = contract_document()
        invalid["version"] = True
        with self.assertRaisesRegex(shared.InvalidVerificationContract, "integer 1"):
            shared.validate_verification_contract(invalid)
        invalid = contract_document()
        invalid["extra"] = True
        with self.assertRaisesRegex(shared.InvalidVerificationContract, "unknown extra"):
            shared.validate_verification_contract(invalid)

    def test_contract_validator_rejects_leading_na_whitespace_and_empty_reason(self) -> None:
        for reason in (" N/A: disabled", "N/A:", "n/a: disabled"):
            invalid = contract_document()
            invalid["n_a_reasons"]["typecheck"] = reason
            with self.subTest(reason=reason), self.assertRaises(
                shared.InvalidVerificationContract
            ):
                shared.validate_verification_contract(invalid)

    def test_contract_validator_rejects_unsafe_cwds_and_duplicate_pairs(self) -> None:
        for cwd in ("../outside", "/absolute", "C:\\outside", "\\\\host\\share"):
            invalid = contract_document(lint_cwd=cwd)
            with self.subTest(cwd=cwd), self.assertRaises(
                shared.InvalidVerificationContract
            ):
                shared.validate_verification_contract(invalid)
        duplicate = contract_document(lint="python3 -m compileall -q src")
        with self.assertRaisesRegex(shared.InvalidVerificationContract, "must be unique"):
            shared.validate_verification_contract(duplicate)

    def test_contract_validator_without_project_has_no_filesystem_side_effect(self) -> None:
        document = contract_document(lint_cwd="directory-that-does-not-exist")
        result = shared.validate_verification_contract(document)
        self.assertEqual(result.step("lint").cwd, pathlib.Path("directory-that-does-not-exist"))
        self.assertIsNone(result.project_root)

    def test_contract_validator_rejects_all_four_steps_null(self) -> None:
        # Loi 1: a contract that declares every step N/A must be refused, not
        # silently accepted as "nothing to verify".
        document = {
            "version": 1,
            "steps": {
                "build": None,
                "typecheck": None,
                "lint": None,
                "test": None,
            },
            "n_a_reasons": {
                "build": "N/A: disabled",
                "typecheck": "N/A: disabled",
                "lint": "N/A: disabled",
                "test": "N/A: disabled",
            },
        }
        with self.assertRaisesRegex(
            shared.InvalidVerificationContract, "at least one active step"
        ):
            shared.validate_verification_contract(document)

    def test_contract_validator_rejects_shell_metacharacters_in_command(self) -> None:
        # Loi 2: a command that can neutralize its own exit code (e.g. via
        # "|| true") must be refused even though it is a non-empty string.
        unsafe_commands = (
            "pytest tests || true",
            "pytest tests && rm -rf /",
            "pytest tests; echo done",
            "pytest tests | tee out.log",
            "pytest tests > out.log",
            "pytest tests < input.txt",
            "echo `whoami`",
            "echo $(whoami)",
            "pytest tests\necho done",
        )
        for unsafe in unsafe_commands:
            with self.subTest(command=unsafe):
                invalid = contract_document(test=unsafe)
                with self.assertRaises(shared.InvalidVerificationContract):
                    shared.validate_verification_contract(invalid)

    def test_contract_validator_accepts_quoted_bare_commands(self) -> None:
        # Single/double quotes are not shell metacharacters on their own and
        # must stay accepted (this matches the repo's own real contract).
        safe = contract_document(
            test="python3 -m unittest discover -s tests -p 'test_*.py' -v"
        )
        contract = shared.validate_verification_contract(safe)
        self.assertEqual(contract.required_steps, ("build", "lint", "test"))

    def test_resolve_project_citation_accepts_local_file_and_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            project = base / "project"
            project.mkdir()
            source = project / "docs.txt"
            source.write_text("first\nTOKEN = Nghĩa chuẩn\n", encoding="utf-8")
            outside = base / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            citation = shared.resolve_project_citation("`docs.txt:2`", project)
            self.assertEqual((citation.path, citation.line), (source.resolve(), 2))
            for value in ("../outside.txt:1", str(outside) + ":1", "docs.txt:9", "bad"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    shared.resolve_project_citation(value, project)


class PlanAndMigrationTests(TemporaryStateTestCase):
    @staticmethod
    def create_v1_database(root: pathlib.Path) -> None:
        root.mkdir(mode=0o700, parents=True)
        path = root / "state.sqlite3"
        with contextlib.closing(sqlite3.connect(str(path))) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE plan_approvals (
                    session_hash TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                    plan_hash TEXT NOT NULL, approved_at REAL NOT NULL,
                    PRIMARY KEY (session_hash, prompt_hash)
                );
                CREATE TABLE mutation_epochs (
                    session_hash TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                    agent_hash TEXT NOT NULL, epoch INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (session_hash, prompt_hash, agent_hash)
                );
                CREATE TABLE mutation_events (
                    session_hash TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                    agent_hash TEXT NOT NULL, tool_use_hash TEXT NOT NULL,
                    epoch INTEGER NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY (session_hash, prompt_hash, agent_hash, tool_use_hash)
                );
                CREATE TABLE verification_records (
                    session_hash TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                    agent_hash TEXT NOT NULL, step TEXT NOT NULL,
                    tool_use_hash TEXT NOT NULL, command_hash TEXT NOT NULL,
                    cwd_hash TEXT NOT NULL, outcome TEXT NOT NULL,
                    receipt_hash TEXT, epoch INTEGER NOT NULL, recorded_at REAL NOT NULL,
                    PRIMARY KEY (session_hash, prompt_hash, agent_hash, step, tool_use_hash)
                );
                PRAGMA user_version = 1;
                """
            )
            connection.execute("INSERT INTO meta VALUES('schema_version', '1')")
            connection.execute(
                "INSERT INTO plan_approvals VALUES (?, ?, ?, ?)",
                (
                    shared.hash_value("legacy-session", "session"),
                    shared.hash_value("legacy-prompt", "prompt"),
                    shared.hash_value("legacy-plan", "plan"),
                    1.0,
                ),
            )
            connection.execute(
                "INSERT INTO mutation_epochs VALUES (?, ?, ?, 4, 1.0)",
                (
                    shared.hash_value("legacy-session", "session"),
                    shared.hash_value("legacy-prompt", "prompt"),
                    shared.hash_value("legacy-agent", "agent"),
                ),
            )
            connection.execute(
                "INSERT INTO verification_records VALUES (?, ?, ?, 'build', ?, ?, ?, ?, ?, 4, 1.0)",
                (
                    shared.hash_value("legacy-session", "session"),
                    shared.hash_value("legacy-prompt", "prompt"),
                    shared.hash_value("legacy-agent", "agent"),
                    shared.hash_value("legacy-tool", "tool_use"),
                    shared.hash_value("legacy-command", "command"),
                    shared.hash_value("legacy-cwd", "cwd"),
                    shared.SUCCESS_OUTCOME,
                    shared.hash_value("legacy-receipt", "receipt"),
                ),
            )

    def test_plan_approval_scope_and_cleanup(self) -> None:
        plan = "## Plan\n1. Change\n2. Verify\n3. Report\n\n## DoD\nTests pass"
        self.store.approve_plan("session", "one", plan)
        self.store.approve_plan("session", "two", "other")
        self.assertTrue(self.store.check_plan("session", "one", plan))
        self.store.cleanup_prompt("session", "one")
        self.assertFalse(self.store.check_plan("session", "one"))
        self.assertTrue(self.store.check_plan("session", "two"))
        self.store.cleanup_session("session")
        self.assertFalse(self.store.check_plan("session", "two"))

    def test_v1_migration_keeps_plans_and_invalidates_live_evidence(self) -> None:
        legacy_root = self.base / "legacy"
        self.create_v1_database(legacy_root)
        migrated = shared.StateStore(legacy_root)
        self.assertTrue(migrated.check_plan("legacy-session", "legacy-prompt", "legacy-plan"))
        with contextlib.closing(sqlite3.connect(str(migrated.db_path))) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM verification_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mutation_epochs").fetchone()[0], 0)
        one_step = {
            "version": 1,
            "steps": {
                "build": {"command": "legacy-command", "cwd": "."},
                "typecheck": None,
                "lint": None,
                "test": None,
            },
            "n_a_reasons": {
                "typecheck": "N/A: disabled",
                "lint": "N/A: disabled",
                "test": "N/A: disabled",
            },
        }
        validation = migrated.validate_receipts(
            self.project,
            "legacy-session",
            "legacy-prompt",
            "legacy-agent",
            {"build": "legacy-receipt"},
            shared.validate_verification_contract(one_step, self.project),
        )
        self.assertFalse(validation.valid)

    def test_failed_migration_rolls_back_and_reopen_v2_is_idempotent(self) -> None:
        legacy_root = self.base / "rollback"
        self.create_v1_database(legacy_root)
        with mock.patch.object(shared.StateStore, "_create_v2_schema", side_effect=RuntimeError("boom")):
            with self.assertRaises(shared.StateUnavailable):
                shared.StateStore(legacy_root)
        with contextlib.closing(sqlite3.connect(str(legacy_root / "state.sqlite3"))) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM plan_approvals").fetchone()[0], 1)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("verification_records", tables)
        first = shared.StateStore(legacy_root)
        second = shared.StateStore(legacy_root)
        self.assertEqual(
            (first.busy_timeout_ms, second.busy_timeout_ms),
            (shared.DEFAULT_BUSY_TIMEOUT_MS, shared.DEFAULT_BUSY_TIMEOUT_MS),
        )
        self.assertTrue(second.check_plan("legacy-session", "legacy-prompt", "legacy-plan"))

    def test_future_schema_fails_closed(self) -> None:
        root = self.base / "future"
        root.mkdir()
        with contextlib.closing(sqlite3.connect(str(root / "state.sqlite3"))) as connection, connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(shared.StateUnavailable, "unsupported state schema"):
            shared.StateStore(root)


class VerificationStateTests(TemporaryStateTestCase):
    def test_ordered_chain_rejects_out_of_order_then_accepts_fresh_sequence(self) -> None:
        self.assertIsNone(self.record("test", "early-test").receipt)
        build = self.record("build", "build")
        self.assertIsNone(self.record("test", "skipped-lint").receipt)
        lint = self.record("lint", "lint")
        test = self.record("test", "test")
        receipts = {"build": build.receipt or "", "lint": lint.receipt or "", "test": test.receipt or ""}
        self.assertTrue(self.validate(receipts).valid)

    def test_rerun_step_invalidates_itself_and_every_downstream_step(self) -> None:
        receipts = self.full_chain()
        lint = self.record("lint", "rerun-lint")
        self.assertIsNotNone(lint.receipt)
        self.assertFalse(self.validate(receipts).valid)
        partial = dict(receipts, lint=lint.receipt or "")
        self.assertFalse(self.validate(partial).valid)
        test = self.record("test", "rerun-test")
        self.assertTrue(self.validate(dict(partial, test=test.receipt or "")).valid)

    def test_exact_failure_and_interruption_invalidate_whole_chain(self) -> None:
        for outcome in ("runtime_failure", "interrupted"):
            with self.subTest(outcome=outcome):
                receipts = self.full_chain(suffix=outcome)
                epoch_before = self.store.current_mutation_epoch(self.project, "session-A", "prompt-A")
                failed = self.record("build", outcome + "-event", outcome=outcome)
                self.assertIsNone(failed.receipt)
                self.assertEqual(failed.epoch, epoch_before + 1)
                self.assertFalse(self.validate(receipts).valid)

    def test_background_taints_prompt_even_after_new_full_chain(self) -> None:
        self.assertIsNone(self.record("build", "background-build", outcome="background").receipt)
        validation = self.validate(self.full_chain(suffix="after-background"))
        self.assertFalse(validation.valid)
        self.assertIn("prompt is tainted by background verification", validation.errors)

    def test_cross_actor_mutation_uses_project_epoch_and_stales_owner_chain(self) -> None:
        receipts = self.full_chain()
        epoch = self.store.bump_mutation_epoch(
            self.project,
            "session-A",
            "prompt-A",
            "agent-B",
            "edit-B",
            {"tool": "Edit", "path_hash": shared.hash_value("file.py", "path")},
        )
        self.assertEqual(epoch, 1)
        self.assertFalse(self.validate(receipts).valid)

    def test_failed_verification_from_other_agent_keeps_owner_live_rows(self) -> None:
        # Loi 6 point 1: record_verification's non-success branch used to wipe
        # verification_live for the WHOLE project/session/prompt regardless of
        # which agent's attempt failed. The DELETE must be scoped to the
        # reporting agent, matching the agent_hash column on the event row
        # inserted right below it, so agent B's failed attempt does not erase
        # agent A's already-recorded live rows at the storage layer.
        self.full_chain(suffix="owner")
        owner_hash = shared.hash_value("agent-A", "agent")
        other_hash = shared.hash_value("agent-B", "agent")
        with contextlib.closing(sqlite3.connect(str(self.store.db_path))) as connection, connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM verification_live WHERE agent_hash = ?",
                (owner_hash,),
            ).fetchone()[0]
        self.assertEqual(before, 3)

        self.record("build", "other-agent-fail", agent="agent-B", outcome="runtime_failure")

        with contextlib.closing(sqlite3.connect(str(self.store.db_path))) as connection, connection:
            after_owner = connection.execute(
                "SELECT COUNT(*) FROM verification_live WHERE agent_hash = ?",
                (owner_hash,),
            ).fetchone()[0]
            after_other = connection.execute(
                "SELECT COUNT(*) FROM verification_live WHERE agent_hash = ?",
                (other_hash,),
            ).fetchone()[0]
        self.assertEqual(after_owner, 3, "agent A's live rows must survive agent B's failure")
        self.assertEqual(after_other, 0)

    def test_mutation_idempotency_and_fact_collision(self) -> None:
        arguments = (
            self.project,
            "session-A",
            "prompt-A",
            "agent-A",
            "same-tool",
            {"tool": "Edit", "result": "success"},
        )
        self.assertEqual(self.store.bump_mutation_epoch(*arguments), 1)
        self.assertEqual(self.store.bump_mutation_epoch(*arguments), 1)
        with self.assertRaises(shared.StateConflict):
            self.store.bump_mutation_epoch(
                self.project, "session-A", "prompt-A", "agent-A", "same-tool", {"tool": "Write"}
            )
        with self.assertRaises(shared.StateConflict):
            self.store.bump_mutation_epoch(
                self.project, "session-A", "prompt-A", "agent-B", "same-tool", {"tool": "Edit", "result": "success"}
            )

    def test_projects_and_owner_agents_are_isolated(self) -> None:
        receipts = self.full_chain()
        other_contract = shared.validate_verification_contract(contract_document(), self.other_project)
        self.assertFalse(self.validate(receipts, project=self.other_project, contract=other_contract).valid)
        self.assertFalse(self.validate(receipts, agent="agent-B").valid)
        self.store.bump_mutation_epoch(
            self.other_project, "session-A", "prompt-A", "agent-B", "other-edit", {"tool": "Edit"}
        )
        self.assertTrue(self.validate(receipts).valid)

    def test_current_contract_fingerprint_command_and_cwd_are_required(self) -> None:
        receipts = self.full_chain()
        changed_command = shared.validate_verification_contract(
            contract_document(build="python3 different-build.py"), self.project
        )
        self.assertFalse(self.validate(receipts, contract=changed_command).valid)
        child = self.project / "child"
        child.mkdir()
        changed_cwd = shared.validate_verification_contract(contract_document(lint_cwd="child"), self.project)
        self.assertFalse(self.validate(receipts, contract=changed_cwd).valid)
        with self.assertRaisesRegex(shared.InvalidStateInput, "command does not match"):
            self.record("build", "wrong-command", command="python3 fake.py")

    def test_verification_tool_id_is_idempotent_and_fact_collision_blocks(self) -> None:
        first = self.record("build", "same-verification")
        duplicate = self.record("build", "same-verification")
        self.assertTrue(first.created)
        self.assertFalse(duplicate.created)
        self.assertIsNone(duplicate.receipt)
        with self.assertRaises(shared.StateConflict):
            self.record("build", "same-verification", outcome="runtime_failure")

    def test_database_contains_only_hashes_of_runtime_and_contract_values(self) -> None:
        plan = "raw plan text that must not persist"
        self.store.approve_plan("raw-session-id", "raw-prompt-id", plan)
        build = self.contract.step("build")
        assert build is not None
        result = self.store.record_verification(
            self.project,
            "raw-session-id",
            "raw-prompt-id",
            "raw-agent-id",
            "build",
            "raw-tool-use-id",
            build.command,
            str(self.project),
            shared.SUCCESS_OUTCOME,
            self.contract,
        )
        raw_values = (
            "raw-session-id",
            "raw-prompt-id",
            "raw-agent-id",
            "raw-tool-use-id",
            build.command,
            str(self.project),
            plan,
            result.receipt or "",
        )
        database_bytes = b""
        for suffix in ("", "-wal", "-shm"):
            path = pathlib.Path(str(self.store.db_path) + suffix)
            if path.exists():
                database_bytes += path.read_bytes()
        for value in raw_values:
            with self.subTest(value=value):
                self.assertNotIn(value.encode("utf-8"), database_bytes)


class StateSecurityAndConcurrencyTests(TemporaryStateTestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_state_database_log_and_dump_have_private_modes_and_no_raw_secrets(self) -> None:
        secrets_to_hide = (
            "query-secret", "body-secret", "data-secret", "url-secret",
            "nested-secret", "list-secret", "unknown-secret", "looks-safe-but-is-not",
        )
        payload = {
            "query": secrets_to_hide[0],
            "body": secrets_to_hide[1],
            "data": secrets_to_hide[2],
            "url": secrets_to_hide[3],
            "nested": {"value": secrets_to_hide[4]},
            "items": [secrets_to_hide[5]],
            "unknown": secrets_to_hide[6],
            "safe": secrets_to_hide[7],
            "hook_event_name": "PostToolUse",
            "step": "build",
            "receipt_hash": shared.hash_value("receipt", "receipt"),
        }
        log = shared.secure_log("hook.result", payload, root=self.state_root)
        dump = shared.secure_dump(payload, root=self.state_root)
        combined = log.read_text(encoding="utf-8") + dump.read_text(encoding="utf-8")
        for secret in secrets_to_hide:
            self.assertNotIn(secret, combined)
        self.assertIn("PostToolUse", combined)
        self.assertIn("build", combined)
        self.assertNotIn("hook.result", combined)
        self.assertEqual(stat.S_IMODE(self.state_root.stat().st_mode), 0o700)
        for path in (self.store.db_path, log, dump):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_log_and_dump_are_capped_and_rotated(self) -> None:
        log = None
        for index in range(20):
            log = shared.secure_log(
                "event-{}".format(index), {"unknown": "x" * 300, "count": index},
                root=self.state_root, max_bytes=1_024,
            )
        assert log is not None
        self.assertLessEqual(log.stat().st_size, 1_024)
        self.assertTrue(pathlib.Path(str(log) + ".1").exists())
        dump = shared.secure_dump({"unknown": "first"}, root=self.state_root)
        shared.secure_dump({"unknown": "second"}, root=self.state_root)
        self.assertTrue(pathlib.Path(str(dump) + ".1").exists())

    def test_symlink_state_root_and_database_are_refused(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unsupported")
        real = self.base / "real"
        real.mkdir()
        root_link = self.base / "root-link"
        try:
            root_link.symlink_to(real, target_is_directory=True)
        except OSError as exc:
            self.skipTest("cannot create symlink: {}".format(exc))
        with self.assertRaises(shared.StateUnavailable):
            shared.StateStore(root_link)

    def test_busy_timeout_is_clamped_and_lock_contention_is_bounded(self) -> None:
        self.assertEqual(
            shared.StateStore(self.base / "low", -1).busy_timeout_ms,
            shared.MINIMUM_BUSY_TIMEOUT_MS,
        )
        self.assertEqual(
            shared.StateStore(self.base / "high", 99_999).busy_timeout_ms,
            shared.MAXIMUM_BUSY_TIMEOUT_MS,
        )
        locker = sqlite3.connect(str(self.store.db_path), isolation_level=None)
        self.addCleanup(locker.close)
        locker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with self.assertRaises(shared.StateUnavailable):
            self.store.bump_mutation_epoch(
                self.project, "locked-session", "locked-prompt", "actor", "tool", {"tool": "Edit"}
            )
        elapsed = time.monotonic() - started
        locker.execute("ROLLBACK")
        self.assertLess(elapsed, 1.5)

    def test_threaded_cross_actor_mutations_are_atomic(self) -> None:
        errors = []

        def worker(worker_id: int) -> None:
            try:
                local = shared.StateStore(self.state_root, busy_timeout_ms=1_000)
                for index in range(5):
                    local.bump_mutation_epoch(
                        self.project,
                        "thread-session",
                        "thread-prompt",
                        "actor-{}".format(worker_id),
                        "thread-{}-{}".format(worker_id, index),
                        {"tool": "Edit", "index": index},
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(
            self.store.current_mutation_epoch(self.project, "thread-session", "thread-prompt"), 20
        )
        with contextlib.closing(sqlite3.connect(str(self.store.db_path))) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_concurrent_processes_never_lose_a_log_record(self) -> None:
        """Appends from separate processes must all survive.

        POSIX O_APPEND resolves the end offset inside write(); the Windows CRT
        emulates it with lseek(END) + write, so unsynchronised writers can pick
        the same offset and one record overwrites the other. This drove an
        intermittent CI failure where 32 concurrent hooks produced 31 lines.
        """

        writers = 8
        records = 40
        writer_source = self.base / "log_writer.py"
        writer_source.write_text(
            "\n".join(
                (
                    "import pathlib, sys",
                    "sys.path.insert(0, sys.argv[1])",
                    "from hooks import _shared",
                    "worker = int(sys.argv[3])",
                    "for index in range(int(sys.argv[4])):",
                    "    _shared.secure_log(",
                    "        'concurrent-append',",
                    "        {'epoch': worker, 'count': index},",
                    "        root=pathlib.Path(sys.argv[2]),",
                    "        max_bytes=4 * 1024 * 1024,",
                    "    )",
                    "",
                )
            ),
            encoding="utf-8",
        )
        repo_root = str(pathlib.Path(shared.__file__).resolve().parent.parent)

        def spawn(worker: int) -> int:
            return subprocess.run(
                [
                    sys.executable,
                    str(writer_source),
                    repo_root,
                    str(self.state_root),
                    str(worker),
                    str(records),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            ).returncode

        with concurrent.futures.ThreadPoolExecutor(max_workers=writers) as pool:
            self.assertEqual(list(pool.map(spawn, range(writers))), [0] * writers)

        log = self.state_root / "agent-kit.log"
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), writers * records)
        written = {
            (entry["fields"]["epoch"], entry["fields"]["count"])
            for entry in (json.loads(line) for line in lines)
        }
        self.assertEqual(
            written,
            {(worker, index) for worker in range(writers) for index in range(records)},
        )

    def test_missing_sqlite_fails_closed_with_actionable_error(self) -> None:
        with mock.patch.object(shared, "_sqlite3", None):
            with self.assertRaisesRegex(shared.StateUnavailable, "block mutations and READY reports"):
                shared.StateStore(self.base / "no-sqlite")


if __name__ == "__main__":
    unittest.main()
