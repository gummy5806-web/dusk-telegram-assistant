#!/usr/bin/env python3
"""Операционное состояние Dusk: путь, права, WAL, cooldown, bounded-схема.

SQLite хранит только идентичность и cooldown. Тексты заметок в базу не
попадают: это проверяется отдельно по колонкам схемы.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_store


def moment(*args) -> datetime.datetime:
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


class DefaultPathTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        path = runtime_store.default_db_path(
            {"DUSK_RUNTIME_DB": "/tmp/dusk-custom/runtime.sqlite3"})
        self.assertEqual(path, Path("/tmp/dusk-custom/runtime.sqlite3"))

    def test_xdg_state_home_is_used(self) -> None:
        path = runtime_store.default_db_path({"XDG_STATE_HOME": "/tmp/state"})
        self.assertEqual(path, Path("/tmp/state/dusk/runtime.sqlite3"))

    def test_home_state_is_fallback(self) -> None:
        path = runtime_store.default_db_path({})
        self.assertEqual(path.name, "runtime.sqlite3")
        self.assertEqual(path.parent.name, "dusk")
        self.assertEqual(path.parent.parent.name, "state")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.db = self.dir / "state" / "runtime.sqlite3"
        self.store = runtime_store.RuntimeStore(self.db)
        self.addCleanup(self.store.close)

    def test_creates_parent_directory(self) -> None:
        self.assertTrue(self.db.exists())
        self.assertTrue(self.db.parent.is_dir())

    def test_database_mode_is_0600(self) -> None:
        self.assertEqual(oct(self.db.stat().st_mode & 0o777), "0o600")

    def test_wal_and_busy_timeout(self) -> None:
        with self.store.connection() as conn:
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(journal.lower(), "wal")
        self.assertGreaterEqual(busy, 1000)

    def test_connection_context_manager_closes(self) -> None:
        with self.store.connection() as conn:
            conn.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_hide_roundtrip_and_expiry(self) -> None:
        now = moment(2026, 10, 9, 12, 0)
        until = self.store.hide("ZETA/x.md#abc", now=now)
        self.assertEqual(until, "2026-10-16T12:00:00+00:00")
        self.assertTrue(self.store.is_hidden("ZETA/x.md#abc", now=now))
        self.assertIn("ZETA/x.md#abc", self.store.hidden(now=now))
        self.assertFalse(self.store.is_hidden(
            "ZETA/x.md#abc", now=moment(2026, 10, 17, 12, 0)))
        self.assertFalse(self.store.is_hidden("другое#1", now=now))

    def test_surface_records_last_shown(self) -> None:
        now = moment(2026, 10, 9, 12, 0)
        self.assertEqual(self.store.last_surface("a.md#1"), "")
        self.store.record_surface("a.md#1", now=now)
        self.assertEqual(self.store.last_surface("a.md#1"),
                         "2026-10-09T12:00:00+00:00")
        self.assertEqual(self.store.surfaced(),
                         {"a.md#1": "2026-10-09T12:00:00+00:00"})
        self.store.record_surface("a.md#1", now=moment(2026, 10, 10, 12, 0))
        self.assertEqual(self.store.last_surface("a.md#1"),
                         "2026-10-10T12:00:00+00:00")

    def test_related_cooldown_is_once_per_24h(self) -> None:
        now = moment(2026, 10, 9, 12, 0)
        self.assertTrue(self.store.claim_related(now=now))
        self.assertFalse(self.store.claim_related(now=moment(2026, 10, 9, 23, 0)))
        self.assertTrue(self.store.claim_related(now=moment(2026, 10, 10, 13, 0)))
        self.assertNotEqual(self.store.last_related(), "")

    def test_schema_stores_no_note_bodies(self) -> None:
        with self.store.connection() as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("hidden", tables)
        self.assertIn("surfaced", tables)
        with self.store.connection() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(hidden)")}
        self.assertEqual(columns, {"identity", "hidden_until", "updated_at"})
        for column in columns:
            self.assertNotIn("body", column)
            self.assertNotIn("text", column)
            self.assertNotIn("content", column)

    def test_newer_schema_fails_closed(self) -> None:
        db = self.dir / "newer" / "runtime.sqlite3"
        store = runtime_store.RuntimeStore(db)
        store.close()
        connection = sqlite3.connect(db)
        try:
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(runtime_store.RuntimeStoreError):
            runtime_store.RuntimeStore(db)

    def test_unwritable_parent_is_explicit_error(self) -> None:
        blocker = self.dir / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(runtime_store.RuntimeStoreError):
            runtime_store.RuntimeStore(blocker / "runtime.sqlite3")


class ReadBoundaryTests(unittest.TestCase):
    """Ошибка чтения SQLite обязана доходить как RuntimeStoreError, не sqlite3.Error."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = runtime_store.RuntimeStore(
            Path(directory.name) / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)
        # Ломаем именно read-путь: таблицы нет, SELECT падает OperationalError.
        with self.store.connection() as conn:
            for table in ("hidden", "surfaced", "meta"):
                conn.execute("DROP TABLE " + table)

    def test_is_hidden_read_error_is_runtime_error(self) -> None:
        with self.assertRaises(runtime_store.RuntimeStoreError) as ctx:
            self.store.is_hidden("x")
        self.assertIsInstance(ctx.exception.__cause__, sqlite3.OperationalError)

    def test_hidden_read_error_is_runtime_error(self) -> None:
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.hidden()

    def test_last_surface_read_error_is_runtime_error(self) -> None:
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.last_surface("x")

    def test_surfaced_read_error_is_runtime_error(self) -> None:
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.surfaced()

    def test_last_related_read_error_is_runtime_error(self) -> None:
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.last_related()

    def test_connect_error_is_runtime_error(self) -> None:
        with patch.object(runtime_store.sqlite3, "connect",
                          side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(runtime_store.RuntimeStoreError):
                self.store.last_surface("x")


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def test_schema_version_is_two_with_focus_tables(self) -> None:
        store = runtime_store.RuntimeStore(self.dir / "state" / "runtime.sqlite3")
        self.addCleanup(store.close)
        with store.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(version, runtime_store.SCHEMA_VERSION)
        self.assertGreaterEqual(version, 2)
        for table in ("hidden", "surfaced", "meta", "focus_sessions",
                      "idempotency", "focus_events"):
            self.assertIn(table, tables)

    def test_v1_to_v2_preserves_hidden_surfaced_meta(self) -> None:
        db = self.dir / "migrate" / "runtime.sqlite3"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        try:
            conn.executescript(
                "CREATE TABLE hidden (identity TEXT PRIMARY KEY, "
                "hidden_until TEXT NOT NULL, updated_at TEXT NOT NULL);"
                "CREATE TABLE surfaced (identity TEXT PRIMARY KEY, "
                "last_shown TEXT NOT NULL);"
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "INSERT INTO hidden VALUES ('a', '2999-01-01T00:00:00+00:00', "
                "'2026-01-01T00:00:00+00:00');"
                "INSERT INTO surfaced VALUES ('b', '2026-02-02T00:00:00+00:00');"
                "INSERT INTO meta VALUES ('related_shown', "
                "'2026-03-03T00:00:00+00:00');"
                "PRAGMA user_version = 1;")
            conn.commit()
        finally:
            conn.close()
        store = runtime_store.RuntimeStore(db)
        self.addCleanup(store.close)
        self.assertTrue(store.is_hidden("a", now=moment(2026, 10, 9)))
        self.assertEqual(store.last_surface("b"), "2026-02-02T00:00:00+00:00")
        self.assertEqual(store.last_related(), "2026-03-03T00:00:00+00:00")
        with store.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, runtime_store.SCHEMA_VERSION)

    def test_v2_to_v3_preserves_focus_and_adds_reminders(self) -> None:
        db = self.dir / "v2" / "runtime.sqlite3"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        try:
            conn.executescript(
                "CREATE TABLE focus_sessions (session_id TEXT PRIMARY KEY, "
                "task_path TEXT NOT NULL, status TEXT NOT NULL, "
                "started_at TEXT NOT NULL, planned_end TEXT NOT NULL, "
                "planned_minutes INTEGER NOT NULL, ended_at TEXT NOT NULL "
                "DEFAULT '', end_reason TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
                "INSERT INTO focus_sessions VALUES ('s1','p','stopped','a','b',25,"
                "'c','manual','d','e');"
                "PRAGMA user_version = 2;")
            conn.commit()
        finally:
            conn.close()
        store = runtime_store.RuntimeStore(db)
        self.addCleanup(store.close)
        self.assertEqual(store.get_focus("s1").status,
                         runtime_store.FOCUS_STOPPED)
        with store.connection() as c:
            version = c.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(version, runtime_store.SCHEMA_VERSION)
        for table in ("reminders", "reminder_events", "reminder_idempotency"):
            self.assertIn(table, tables)


class FocusStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = runtime_store.RuntimeStore(
            Path(directory.name) / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)

    def record(self, session: str, status: str = "active",
               minutes: int = 25, path: str = "TaskNotes/Tasks/Задача.md"):
        return runtime_store.FocusRecord(
            session_id=session, task_path=path, status=status,
            started_at="2026-10-09T12:00:00+00:00",
            planned_end="2026-10-09T12:25:00+00:00",
            planned_minutes=minutes)

    def test_active_lookup_and_token_unique(self) -> None:
        self.assertIsNone(self.store.get_active_focus())
        self.store.activate_focus(self.record("s1"), now=moment(2026, 10, 9, 12))
        active = self.store.get_active_focus()
        self.assertEqual(active.session_id, "s1")
        self.assertEqual(active.status, runtime_store.FOCUS_ACTIVE)
        self.assertEqual(self.store.get_focus("s1").planned_minutes, 25)
        self.assertIsNone(self.store.get_focus("нет"))

    def test_one_active_slot_enforced(self) -> None:
        self.store.activate_focus(self.record("s1"), now=moment(2026, 10, 9, 12))
        self.store.activate_focus(self.record("s2"), now=moment(2026, 10, 9, 12, 5))
        self.assertEqual(self.store.get_active_focus().session_id, "s2")
        self.assertEqual(self.store.get_focus("s1").status,
                         runtime_store.FOCUS_SUPERSEDED)
        with self.assertRaises(runtime_store.RuntimeStoreError):
            with self.store.connection() as conn:
                conn.execute(
                    "INSERT INTO focus_sessions(session_id, task_path, status, "
                    "started_at, planned_end, planned_minutes, ended_at, "
                    "end_reason, created_at, updated_at) "
                    "VALUES('s3','x','active','t','t',1,'','','t','t')")

    def test_finish_and_orphan_transitions(self) -> None:
        self.store.activate_focus(self.record("s1"), now=moment(2026, 10, 9, 12))
        finished = runtime_store.FocusRecord(
            session_id="s1", task_path="TaskNotes/Tasks/Задача.md",
            status=runtime_store.FOCUS_STOPPED,
            started_at="2026-10-09T12:00:00+00:00",
            planned_end="2026-10-09T12:25:00+00:00", planned_minutes=25,
            ended_at="2026-10-09T12:10:00+00:00",
            end_reason=runtime_store.REASON_MANUAL)
        self.store.upsert_focus(finished, now=moment(2026, 10, 9, 12, 10))
        self.assertIsNone(self.store.get_active_focus())
        self.assertEqual(self.store.get_focus("s1").status,
                         runtime_store.FOCUS_STOPPED)
        self.store.activate_focus(self.record("s1", status="active"),
                                  now=moment(2026, 10, 9, 13))
        orphan = runtime_store.FocusRecord(
            session_id="s1", task_path="TaskNotes/Tasks/Задача.md",
            status=runtime_store.FOCUS_ORPHANED,
            started_at="2026-10-09T12:00:00+00:00",
            planned_end="2026-10-09T12:25:00+00:00", planned_minutes=25)
        self.store.upsert_focus(orphan, now=moment(2026, 10, 9, 14))
        self.assertEqual(self.store.get_focus("s1").status,
                         runtime_store.FOCUS_ORPHANED)

    def test_idempotency_keeps_first_mapping(self) -> None:
        self.assertEqual(self.store.idempotency_session("k"), "")
        self.store.record_idempotency("k", "s1", now=moment(2026, 10, 9, 12))
        self.assertEqual(self.store.idempotency_session("k"), "s1")
        self.store.record_idempotency("k", "s2", now=moment(2026, 10, 9, 13))
        self.assertEqual(self.store.idempotency_session("k"), "s1")

    def test_activate_with_claim_is_one_transaction(self) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "CREATE TRIGGER block_idem BEFORE INSERT ON idempotency "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END")
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.activate_focus_with_claim(
                self.record("s1"), "k1", now=moment(2026, 10, 9, 12))
        self.assertIsNone(self.store.get_active_focus())
        self.assertEqual(self.store.idempotency_session("k1"), "")

    def test_event_enqueue_once_and_lease(self) -> None:
        key = self.store.enqueue_event("s1", runtime_store.EVENT_FOCUS_EXPIRED,
                                       now=moment(2026, 10, 9, 12))
        self.store.enqueue_event("s1", runtime_store.EVENT_FOCUS_EXPIRED,
                                 now=moment(2026, 10, 9, 12, 5))
        self.assertEqual(len(self.store.pending_events()), 1)
        first = self.store.claim_event(now=moment(2026, 10, 9, 12, 1),
                                       lease_seconds=60)
        self.assertEqual(first.session_id, "s1")
        self.assertEqual(first.status, "leased")
        self.assertEqual(first.attempts, 1)
        self.assertEqual(first.message_key, key)
        self.assertIsNone(self.store.claim_event(
            now=moment(2026, 10, 9, 12, 1, 30), lease_seconds=60))
        second = self.store.claim_event(now=moment(2026, 10, 9, 12, 2, 5),
                                        lease_seconds=60)
        self.assertEqual(second.event_id, first.event_id)
        self.assertEqual(second.attempts, 2)
        self.assertTrue(key)

    def test_event_failed_backoff_then_reclaim(self) -> None:
        self.store.enqueue_event("s1", runtime_store.EVENT_FOCUS_EXPIRED,
                                 now=moment(2026, 10, 9, 12))
        claimed = self.store.claim_event(now=moment(2026, 10, 9, 12),
                                         lease_seconds=1)
        self.assertTrue(self.store.mark_event_failed(
            claimed.event_id, claimed.attempts, now=moment(2026, 10, 9, 12)))
        failed = self.store.get_event(claimed.event_id)
        self.assertEqual(failed.status, "failed")
        self.assertIsNone(self.store.claim_event(
            now=moment(2026, 10, 9, 12), lease_seconds=60))
        later = self.store.claim_event(now=moment(2026, 10, 9, 12, 1),
                                       lease_seconds=60)
        self.assertEqual(later.event_id, claimed.event_id)
        self.assertEqual(later.attempts, 2)

    def test_event_delivered_is_terminal(self) -> None:
        self.store.enqueue_event("s1", runtime_store.EVENT_FOCUS_EXPIRED,
                                 now=moment(2026, 10, 9, 12))
        claimed = self.store.claim_event(now=moment(2026, 10, 9, 12),
                                         lease_seconds=60)
        self.assertTrue(self.store.mark_event_delivered(
            claimed.event_id, claimed.attempts, now=moment(2026, 10, 9, 12, 1)))
        self.assertEqual(self.store.pending_events(), ())
        self.assertEqual(self.store.get_event(claimed.event_id).status,
                         "delivered")
        # повторный enqueue не воскрешает доставленное
        self.store.enqueue_event("s1", runtime_store.EVENT_FOCUS_EXPIRED,
                                 now=moment(2026, 10, 9, 13))
        self.assertEqual(self.store.pending_events(), ())
        self.assertEqual(self.store.get_event(claimed.event_id).status,
                         "delivered")


class EventLeaseGenerationTests(unittest.TestCase):
    """Lease-owned события: старая попытка не перезаписывает новую."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = runtime_store.RuntimeStore(
            Path(directory.name) / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)
        self.store.enqueue_event("s1", runtime_store.EVENT_FOCUS_EXPIRED,
                                 now=moment(2026, 10, 9, 12))

    def test_exact_regression_expire_then_delivery_then_stale_failure(self) -> None:
        a = self.store.claim_event(now=moment(2026, 10, 9, 12), lease_seconds=10)
        b = self.store.claim_event(now=moment(2026, 10, 9, 12, 0, 11),
                                   lease_seconds=60)
        self.assertEqual((a.attempts, b.attempts), (1, 2))
        self.assertEqual(a.event_id, b.event_id)
        self.assertTrue(self.store.mark_event_delivered(
            b.event_id, b.attempts, now=moment(2026, 10, 9, 12, 0, 12)))
        self.assertFalse(self.store.mark_event_failed(
            a.event_id, a.attempts, now=moment(2026, 10, 9, 12, 0, 13)))
        self.assertEqual(self.store.get_event(a.event_id).status, "delivered")
        self.assertEqual(self.store.pending_events(), ())
        self.assertIsNone(self.store.claim_event(
            now=moment(2026, 10, 10, 12), lease_seconds=60))

    def test_stale_worker_cannot_overwrite_newer_lease(self) -> None:
        a = self.store.claim_event(now=moment(2026, 10, 9, 12), lease_seconds=10)
        b = self.store.claim_event(now=moment(2026, 10, 9, 12, 0, 11),
                                   lease_seconds=60)
        self.assertFalse(self.store.mark_event_failed(
            a.event_id, a.attempts, now=moment(2026, 10, 9, 12, 0, 12)))
        current = self.store.get_event(a.event_id)
        self.assertEqual(current.status, "leased")
        self.assertEqual(current.attempts, b.attempts)
        self.assertTrue(self.store.mark_event_failed(
            b.event_id, b.attempts, now=moment(2026, 10, 9, 12, 0, 13)))

    def test_delivered_cannot_be_reopened_by_stale_delivery(self) -> None:
        a = self.store.claim_event(now=moment(2026, 10, 9, 12), lease_seconds=10)
        b = self.store.claim_event(now=moment(2026, 10, 9, 12, 0, 11),
                                   lease_seconds=60)
        self.assertTrue(self.store.mark_event_delivered(
            b.event_id, b.attempts, now=moment(2026, 10, 9, 12, 0, 12)))
        self.assertFalse(self.store.mark_event_delivered(
            a.event_id, a.attempts, now=moment(2026, 10, 9, 12, 0, 13)))
        self.assertEqual(self.store.get_event(a.event_id).status, "delivered")


class FinishExpiredTests(unittest.TestCase):
    """Истечение и событие пишутся одной транзакцией."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = runtime_store.RuntimeStore(
            Path(directory.name) / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)

    def expired(self, session: str = "s1") -> runtime_store.FocusRecord:
        return runtime_store.FocusRecord(
            session_id=session, task_path="TaskNotes/Tasks/Задача.md",
            status=runtime_store.FOCUS_EXPIRED,
            started_at="2026-10-09T12:00:00+00:00",
            planned_end="2026-10-09T12:15:00+00:00", planned_minutes=15,
            ended_at="2026-10-09T12:15:00+00:00",
            end_reason=runtime_store.REASON_EXPIRED)

    def test_finish_expired_writes_record_and_event(self) -> None:
        key = self.store.finish_expired(self.expired(), now=moment(2026, 10, 9, 12, 20))
        self.assertEqual(self.store.get_focus("s1").status,
                         runtime_store.FOCUS_EXPIRED)
        self.assertEqual(len(self.store.pending_events()), 1)
        self.assertTrue(key)

    def test_finish_expired_rolls_back_atomically(self) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "CREATE TRIGGER block_event BEFORE INSERT ON focus_events "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END")
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.finish_expired(self.expired(), now=moment(2026, 10, 9, 12, 20))
        self.assertIsNone(self.store.get_focus("s1"))
        self.assertEqual(self.store.pending_events(), ())

    def test_finish_expired_retry_produces_one_event(self) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "CREATE TRIGGER block_event BEFORE INSERT ON focus_events "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END")
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.finish_expired(self.expired(), now=moment(2026, 10, 9, 12, 20))
        with self.store.connection() as conn:
            conn.execute("DROP TRIGGER block_event")
        self.store.finish_expired(self.expired(), now=moment(2026, 10, 9, 12, 20))
        self.store.finish_expired(self.expired(), now=moment(2026, 10, 9, 12, 21))
        self.assertEqual(len(self.store.pending_events()), 1)
        self.assertEqual(self.store.get_focus("s1").status,
                         runtime_store.FOCUS_EXPIRED)


class RuntimeTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = runtime_store.RuntimeStore(
            Path(directory.name) / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)

    def test_naive_now_rejected(self) -> None:
        naive = datetime.datetime(2026, 10, 9, 12, 0)
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.enqueue_event("s1", runtime_store.EVENT_FOCUS_EXPIRED,
                                     now=naive)
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.claim_event(now=naive)
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.record_surface("x", now=naive)

    def test_non_datetime_now_rejected(self) -> None:
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.enqueue_event("s1", runtime_store.EVENT_FOCUS_EXPIRED,
                                     now="2026-10-09T12:00:00+00:00")


class ReminderStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = runtime_store.RuntimeStore(
            Path(directory.name) / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)

    def record(self, reminder_id="dusk_a", when="2026-10-09T12:00:00+00:00"):
        return runtime_store.ReminderRecord(
            reminder_id=reminder_id, task_path="TaskNotes/Tasks/Задача.md",
            absolute_time=when, state=runtime_store.REMINDER_ACTIVE,
            generation=1)

    def test_upsert_get_and_no_note_text(self) -> None:
        self.store.upsert_reminder(self.record(), now=moment(2026, 10, 9, 11))
        record = self.store.get_reminder("dusk_a")
        self.assertEqual(record.state, runtime_store.REMINDER_ACTIVE)
        self.assertEqual(record.generation, 1)
        with self.store.connection() as conn:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(reminders)")}
        for column in columns:
            self.assertNotIn("title", column)
            self.assertNotIn("description", column)
            self.assertNotIn("text", column)

    def test_collect_due_is_atomic_unique_and_bounded(self) -> None:
        for index in range(3):
            self.store.upsert_reminder(self.record(
                f"dusk_{index}", when="2026-10-09T11:00:00+00:00"),
                now=moment(2026, 10, 9, 10))
        first = self.store.collect_due(now=moment(2026, 10, 9, 12), limit=2)
        self.assertEqual(len(first), 2)
        second = self.store.collect_due(now=moment(2026, 10, 9, 12), limit=2)
        self.assertEqual(len(second), 1)
        self.assertEqual(self.store.collect_due(
            now=moment(2026, 10, 9, 12), limit=2), ())
        for event in first + second:
            self.assertEqual(event.state, runtime_store.EVENT_PENDING)
            self.assertTrue(event.message_key.startswith("reminder_due:"))
        self.assertEqual(len(self.store.pending_reminder_events()), 3)

    def test_claim_lease_backoff_and_stale_generation(self) -> None:
        self.store.upsert_reminder(self.record(when="2026-10-09T11:00:00+00:00"),
                                   now=moment(2026, 10, 9, 10))
        self.store.collect_due(now=moment(2026, 10, 9, 12))
        first = self.store.claim_reminder_event(now=moment(2026, 10, 9, 12),
                                                lease_seconds=10)
        self.assertEqual(first.attempts, 1)
        self.assertIsNone(self.store.claim_reminder_event(
            now=moment(2026, 10, 9, 12, 0, 5), lease_seconds=10))
        second = self.store.claim_reminder_event(
            now=moment(2026, 10, 9, 12, 0, 11), lease_seconds=60)
        self.assertEqual(second.attempts, 2)
        self.assertTrue(self.store.mark_reminder_event_delivered(
            second.event_id, second.attempts, now=moment(2026, 10, 9, 12, 0, 12)))
        self.assertFalse(self.store.mark_reminder_event_failed(
            first.event_id, first.attempts, now=moment(2026, 10, 9, 12, 0, 13)))
        self.assertEqual(self.store.get_reminder("dusk_a").state,
                         runtime_store.REMINDER_DELIVERED)

    def test_failed_event_backoff_then_reclaim(self) -> None:
        self.store.upsert_reminder(self.record(when="2026-10-09T11:00:00+00:00"),
                                   now=moment(2026, 10, 9, 10))
        self.store.collect_due(now=moment(2026, 10, 9, 12))
        claimed = self.store.claim_reminder_event(
            now=moment(2026, 10, 9, 12), lease_seconds=1)
        self.assertTrue(self.store.mark_reminder_event_failed(
            claimed.event_id, claimed.attempts, now=moment(2026, 10, 9, 12)))
        self.assertIsNone(self.store.claim_reminder_event(
            now=moment(2026, 10, 9, 12), lease_seconds=60))
        later = self.store.claim_reminder_event(
            now=moment(2026, 10, 9, 12, 1), lease_seconds=60)
        self.assertEqual(later.event_id, claimed.event_id)
        self.assertEqual(later.attempts, 2)

    def test_reconcile_preserves_delivered_and_bumps_generation(self) -> None:
        self.store.upsert_reminder(self.record(when="2026-10-09T11:00:00+00:00"),
                                   now=moment(2026, 10, 9, 10))
        self.store.collect_due(now=moment(2026, 10, 9, 12))
        claimed = self.store.claim_reminder_event(now=moment(2026, 10, 9, 12))
        self.store.mark_reminder_event_delivered(
            claimed.event_id, claimed.attempts, now=moment(2026, 10, 9, 12))
        same = runtime_store.ReminderRecord(
            reminder_id="dusk_a", task_path="TaskNotes/Tasks/Задача.md",
            absolute_time="2026-10-09T11:00:00+00:00",
            state=runtime_store.REMINDER_ACTIVE, generation=1)
        self.store.reconcile_reminders([same], now=moment(2026, 10, 9, 12, 5))
        self.assertEqual(self.store.get_reminder("dusk_a").state,
                         runtime_store.REMINDER_DELIVERED)
        moved = runtime_store.ReminderRecord(
            reminder_id="dusk_a", task_path="TaskNotes/Tasks/Задача.md",
            absolute_time="2026-10-09T13:00:00+00:00",
            state=runtime_store.REMINDER_ACTIVE, generation=1)
        self.store.reconcile_reminders([moved], now=moment(2026, 10, 9, 12, 6))
        record = self.store.get_reminder("dusk_a")
        self.assertEqual(record.state, runtime_store.REMINDER_ACTIVE)
        self.assertEqual(record.generation, 2)

    def test_cancel_and_missing_cancel_unleased_events(self) -> None:
        self.store.upsert_reminder(self.record(when="2026-10-09T11:00:00+00:00"),
                                   now=moment(2026, 10, 9, 10))
        self.store.collect_due(now=moment(2026, 10, 9, 12))
        self.store.reconcile_reminders([], now=moment(2026, 10, 9, 12, 5))
        self.assertEqual(self.store.get_reminder("dusk_a").state,
                         runtime_store.REMINDER_CANCELLED)
        self.assertEqual(self.store.pending_reminder_events(), ())

    def test_reminder_idempotency(self) -> None:
        self.assertEqual(self.store.reminder_idempotency("k"), "")
        self.store.record_reminder_idempotency(
            "k", "dusk_a", now=moment(2026, 10, 9, 12))
        self.store.record_reminder_idempotency(
            "k", "dusk_b", now=moment(2026, 10, 9, 13))
        self.assertEqual(self.store.reminder_idempotency("k"), "dusk_a")


class ReminderLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = runtime_store.RuntimeStore(
            Path(directory.name) / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)

    def seed(self, count: int) -> None:
        for index in range(count):
            self.store.upsert_reminder(runtime_store.ReminderRecord(
                reminder_id=f"dusk_{index}",
                task_path="TaskNotes/Tasks/Задача.md",
                absolute_time="2026-10-09T11:00:00+00:00",
                state=runtime_store.REMINDER_ACTIVE, generation=1),
                now=moment(2026, 10, 9, 10))

    def test_zero_and_negative_limit_no_mutation(self) -> None:
        self.seed(1)
        self.assertEqual(self.store.collect_due(
            now=moment(2026, 10, 9, 12), limit=0), ())
        self.assertEqual(self.store.collect_due(
            now=moment(2026, 10, 9, 12), limit=-3), ())
        self.assertEqual(self.store.get_reminder("dusk_0").state,
                         runtime_store.REMINDER_ACTIVE)
        self.assertEqual(self.store.pending_reminder_events(), ())

    def test_huge_limit_is_capped(self) -> None:
        self.seed(runtime_store.REMINDER_MAX_BATCH + 3)
        events = self.store.collect_due(now=moment(2026, 10, 9, 12),
                                        limit=10 ** 9)
        self.assertEqual(len(events), runtime_store.REMINDER_MAX_BATCH)

    def test_invalid_limit_raises_runtime_error(self) -> None:
        for bad in ("5", 1.5, True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(runtime_store.RuntimeStoreError):
                    self.store.collect_due(now=moment(2026, 10, 9, 12),
                                           limit=bad)

    def test_delivery_after_snooze_stale_generation_is_noop(self) -> None:
        record = runtime_store.ReminderRecord(
            reminder_id="dusk_a", task_path="TaskNotes/Tasks/Задача.md",
            absolute_time="2026-10-09T11:00:00+00:00",
            state=runtime_store.REMINDER_ACTIVE, generation=1)
        self.store.upsert_reminder(record, now=moment(2026, 10, 9, 10))
        self.store.collect_due(now=moment(2026, 10, 9, 12))
        claimed = self.store.claim_reminder_event(now=moment(2026, 10, 9, 12))
        generation = self.store.snooze_reminder(
            "dusk_a", record.task_path, "2026-10-09T13:00:00+00:00",
            now=moment(2026, 10, 9, 12, 1))
        self.assertEqual(generation, 2)
        self.assertFalse(self.store.mark_reminder_event_delivered(
            claimed.event_id, claimed.attempts, now=moment(2026, 10, 9, 12, 2)))
        current = self.store.get_reminder("dusk_a")
        self.assertEqual(current.state, runtime_store.REMINDER_ACTIVE)
        self.assertEqual(current.generation, 2)


    def test_recover_preserves_state_and_conflict_fails_closed(self) -> None:
        delivered = runtime_store.ReminderRecord(
            reminder_id="dusk_a", task_path="TaskNotes/Tasks/Задача.md",
            absolute_time="2026-10-09T11:00:00+00:00",
            state=runtime_store.REMINDER_DELIVERED, generation=2)
        self.store.upsert_reminder(delivered, now=moment(2026, 10, 9, 10))
        recovered = self.store.recover_reminder(
            runtime_store.ReminderRecord(
                reminder_id="dusk_a", task_path="TaskNotes/Tasks/Задача.md",
                absolute_time="2026-10-09T11:00:00+00:00",
                state=runtime_store.REMINDER_ACTIVE, generation=1),
            "reminder:K", now=moment(2026, 10, 9, 12))
        self.assertEqual(recovered.state, runtime_store.REMINDER_DELIVERED)
        self.assertEqual(recovered.generation, 2)
        self.assertEqual(self.store.reminder_idempotency("reminder:K"),
                         "dusk_a")
        with self.assertRaises(runtime_store.RuntimeStoreError):
            self.store.recover_reminder(
                runtime_store.ReminderRecord(
                    reminder_id="dusk_b",
                    task_path="TaskNotes/Tasks/Задача.md",
                    absolute_time="2026-10-09T11:00:00+00:00",
                    state=runtime_store.REMINDER_ACTIVE, generation=1),
                "reminder:K", now=moment(2026, 10, 9, 12))
        self.assertIsNone(self.store.get_reminder("dusk_b"))


if __name__ == "__main__":
    unittest.main()
