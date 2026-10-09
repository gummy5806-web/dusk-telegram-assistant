#!/usr/bin/env python3
"""Focus state machine: старт/продление/стоп/истечение/восстановление.

Изолированно: временный vault, временная SQLite, никакой сети, Telegram и
живого валта. Долгосрочная истина — Markdown; SQLite хранит только операционное
состояние (активная сессия, идемпотентность, доставка уведомлений).
"""

from __future__ import annotations

import datetime
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import focus
import runtime_store
import task_metadata

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def at(**delta) -> datetime.datetime:
    return T0 + datetime.timedelta(**delta)


def iso(moment: datetime.datetime) -> str:
    return moment.astimezone(UTC).isoformat()


class FocusBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        self.store = runtime_store.RuntimeStore(self.root / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)

    def create_task(self, name: str, status: str = "next") -> Path:
        title = name[:-3] if name.endswith(".md") else name
        path = self.tasks / (title + ".md")
        path.write_text(
            "---\n"
            f'title: "{title}"\n'
            f"status: {status}\n"
            "priority: normal\n"
            f'nextStep: "{title}"\n'
            "created: 2026-10-01T09:00\n"
            "scheduled: 2026-10-01\n"
            "tags:\n  - task\n"
            'source: "пакет дня"\n'
            "---\n"
            f"# {title}\n\nТело\n",
            encoding="utf-8")
        return path

    def add_entries(self, name: str, entries, scalars=None) -> None:
        task_metadata.write_task(self.root, name, time_entries=entries,
                                 scalars=scalars or {})

    def entries(self, name: str):
        return task_metadata.read_task(self.root, name).time_entries

    def entry(self, session: str, start: datetime.datetime, minutes: int,
              open_: bool = True) -> task_metadata.TimeEntry:
        data = {
            "startTime": iso(start),
            "plannedEnd": iso(start + datetime.timedelta(minutes=minutes)),
            "plannedMinutes": minutes,
            "duskSessionId": session,
            "description": f"Dusk focus · {minutes} мин",
            "source": task_metadata.DUSK_SOURCE,
        }
        if not open_:
            data["endTime"] = iso(start + datetime.timedelta(minutes=minutes))
        return task_metadata.TimeEntry(data)


# ── старт ───────────────────────────────────────────────────────────────────

class StartTests(FocusBase):
    def test_start_creates_entry_and_active_session(self) -> None:
        self.create_task("Задача.md")
        result = focus.start_focus(self.root, "Задача.md", 25, self.store,
                                   idempotency_key="k1", now=T0)
        self.assertTrue(result.ok)
        self.assertFalse(result.reused)
        session = result.session
        self.assertTrue(session.session_id)
        self.assertEqual(session.planned_minutes, 25)
        self.assertEqual(session.status, runtime_store.FOCUS_ACTIVE)

        entries = self.entries("Задача.md")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.start_time, iso(T0))
        self.assertEqual(entry.planned_end, iso(at(minutes=25)))
        self.assertEqual(entry.planned_minutes, 25)
        self.assertEqual(entry.dusk_session_id, session.session_id)
        self.assertEqual(entry.source, task_metadata.DUSK_SOURCE)
        self.assertFalse(entry.end_time)

        snapshot = task_metadata.read_task(self.root, "Задача.md")
        self.assertEqual(snapshot.status, "in-progress")
        self.assertEqual(snapshot.date_modified, iso(T0))

        active = self.store.get_active_focus()
        self.assertIsNotNone(active)
        self.assertEqual(active.session_id, session.session_id)

    def test_start_rejects_invalid_duration(self) -> None:
        self.create_task("Задача.md")
        for bad in (0, 10, 30, 200, "abc", None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(focus.FocusError):
                    focus.start_focus(self.root, "Задача.md", bad, self.store,
                                      now=T0)

    def test_start_rejects_missing_task(self) -> None:
        with self.assertRaises(focus.FocusError):
            focus.start_focus(self.root, "Нет.md", 25, self.store, now=T0)

    def test_start_rejects_escape(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("---\ntitle: x\nstatus: next\n---\n# x\n",
                           encoding="utf-8")
        with self.assertRaises(focus.FocusError):
            focus.start_focus(self.root, "../outside.md", 25, self.store, now=T0)

    def test_start_rejects_malformed_task(self) -> None:
        path = self.tasks / "Битая.md"
        path.write_text("---\ntitle: [\n---\nтело\n", encoding="utf-8")
        with self.assertRaises(focus.FocusError):
            focus.start_focus(self.root, "Битая.md", 25, self.store, now=T0)
        self.assertIsNone(self.store.get_active_focus())

    def test_start_rejects_done_task(self) -> None:
        self.create_task("Готово.md", status="done")
        with self.assertRaises(focus.FocusError):
            focus.start_focus(self.root, "Готово.md", 25, self.store, now=T0)
        self.assertIsNone(self.store.get_active_focus())

    def test_start_rejects_symlink_task(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("---\ntitle: x\nstatus: next\n---\n# x\n",
                           encoding="utf-8")
        (self.tasks / "Ссылка.md").symlink_to(outside)
        with self.assertRaises(focus.FocusError):
            focus.start_focus(self.root, "Ссылка.md", 25, self.store, now=T0)

    def test_start_closes_prior_dusk_entry_and_supersedes_session(self) -> None:
        self.create_task("Задача.md")
        first = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        second = focus.start_focus(self.root, "Задача.md", 45, self.store,
                                   now=at(minutes=5))
        self.assertNotEqual(first.session.session_id, second.session.session_id)
        entries = self.entries("Задача.md")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].end_time, iso(at(minutes=5)))
        self.assertFalse(entries[1].end_time)
        self.assertEqual(
            self.store.get_focus(first.session.session_id).status,
            runtime_store.FOCUS_SUPERSEDED)
        self.assertEqual(self.store.get_active_focus().session_id,
                         second.session.session_id)

    def test_start_fails_on_native_open_entry_without_write(self) -> None:
        self.create_task("Задача.md")
        native = task_metadata.TimeEntry({
            "startTime": iso(T0), "description": "native timer"})
        self.add_entries("Задача.md", [native])
        before = (self.tasks / "Задача.md").read_text(encoding="utf-8")
        with self.assertRaises(focus.FocusError):
            focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        self.assertEqual((self.tasks / "Задача.md").read_text(encoding="utf-8"),
                         before)
        self.assertIsNone(self.store.get_active_focus())

    def test_start_is_idempotent_by_key(self) -> None:
        self.create_task("Задача.md")
        first = focus.start_focus(self.root, "Задача.md", 25, self.store,
                                  idempotency_key="same", now=T0)
        second = focus.start_focus(self.root, "Задача.md", 25, self.store,
                                   idempotency_key="same", now=at(minutes=1))
        self.assertTrue(second.reused)
        self.assertEqual(first.session.session_id, second.session.session_id)
        self.assertEqual(len(self.entries("Задача.md")), 1)

    def test_start_second_task_supersedes_first(self) -> None:
        self.create_task("А.md")
        self.create_task("Б.md")
        first = focus.start_focus(self.root, "А.md", 25, self.store, now=T0)
        second = focus.start_focus(self.root, "Б.md", 25, self.store,
                                   now=at(minutes=3))
        self.assertEqual(self.store.get_active_focus().session_id,
                         second.session.session_id)
        self.assertEqual(self.store.get_focus(first.session.session_id).status,
                         runtime_store.FOCUS_SUPERSEDED)
        self.assertEqual(self.entries("А.md")[0].end_time, iso(at(minutes=3)))
        self.assertFalse(self.entries("Б.md")[0].end_time)


# ── продление ───────────────────────────────────────────────────────────────

class ExtendTests(FocusBase):
    def test_extend_adds_exactly_15_minutes(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        result = focus.extend_focus(self.root, started.session.session_id,
                                    self.store, idempotency_key="e1",
                                    now=at(minutes=5))
        self.assertTrue(result.ok)
        self.assertEqual(result.session.planned_minutes, 40)
        self.assertEqual(result.session.planned_end, iso(at(minutes=40)))
        entry = self.entries("Задача.md")[0]
        self.assertEqual(entry.planned_minutes, 40)
        self.assertEqual(entry.planned_end, iso(at(minutes=40)))
        self.assertEqual(self.store.get_active_focus().planned_minutes, 40)

    def test_extend_idempotent_key_does_not_double(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        focus.extend_focus(self.root, started.session.session_id, self.store,
                           idempotency_key="e1", now=at(minutes=5))
        again = focus.extend_focus(self.root, started.session.session_id,
                                   self.store, idempotency_key="e1",
                                   now=at(minutes=6))
        self.assertTrue(again.reused)
        self.assertEqual(self.entries("Задача.md")[0].planned_minutes, 40)

    def test_extend_requires_active_token(self) -> None:
        self.create_task("Задача.md")
        with self.assertRaises(focus.FocusError):
            focus.extend_focus(self.root, "нет-такого", self.store, now=T0)


# ── стоп ────────────────────────────────────────────────────────────────────

class StopTests(FocusBase):
    def test_stop_closes_entry_and_session(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        result = focus.stop_focus(self.root, started.session.session_id,
                                  self.store, now=at(minutes=10))
        self.assertTrue(result.ok)
        self.assertEqual(result.session.status, runtime_store.FOCUS_STOPPED)
        self.assertEqual(result.session.end_reason, runtime_store.REASON_MANUAL)
        entry = self.entries("Задача.md")[0]
        self.assertEqual(entry.end_time, iso(at(minutes=10)))
        self.assertIsNone(self.store.get_active_focus())

    def test_stop_repeat_is_idempotent_no_rewrite(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        focus.stop_focus(self.root, started.session.session_id, self.store,
                         now=at(minutes=10))
        text_before = (self.tasks / "Задача.md").read_text(encoding="utf-8")
        again = focus.stop_focus(self.root, started.session.session_id,
                                 self.store, now=at(minutes=20))
        self.assertTrue(again.ok)
        self.assertEqual(again.session.end_reason, runtime_store.REASON_MANUAL)
        self.assertEqual((self.tasks / "Задача.md").read_text(encoding="utf-8"),
                         text_before)
        self.assertEqual(self.store.get_focus(started.session.session_id).ended_at,
                         iso(at(minutes=10)))


# ── истечение ───────────────────────────────────────────────────────────────

class ExpireTests(FocusBase):
    def test_expire_closes_at_planned_deadline_not_wake_time(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 15, self.store, now=T0)
        result = focus.expire_due(self.root, self.store, now=at(minutes=20))
        self.assertEqual(len(result.expired), 1)
        entry = self.entries("Задача.md")[0]
        self.assertEqual(entry.end_time, iso(at(minutes=15)))
        record = self.store.get_focus(started.session.session_id)
        self.assertEqual(record.status, runtime_store.FOCUS_EXPIRED)
        self.assertEqual(record.ended_at, iso(at(minutes=15)))
        self.assertEqual(record.end_reason, runtime_store.REASON_EXPIRED)

    def test_expire_emits_single_pending_event(self) -> None:
        self.create_task("Задача.md")
        focus.start_focus(self.root, "Задача.md", 15, self.store, now=T0)
        first = focus.expire_due(self.root, self.store, now=at(minutes=20))
        self.assertEqual(len(first.events), 1)
        self.assertEqual(len(self.store.pending_events()), 1)
        second = focus.expire_due(self.root, self.store, now=at(minutes=25))
        self.assertEqual(second.expired, ())
        self.assertEqual(len(self.store.pending_events()), 1)

    def test_expire_never_marks_task_done(self) -> None:
        self.create_task("Задача.md")
        focus.start_focus(self.root, "Задача.md", 15, self.store, now=T0)
        focus.expire_due(self.root, self.store, now=at(minutes=20))
        self.assertEqual(task_metadata.read_task(self.root, "Задача.md").status,
                         "in-progress")


# ── восстановление после рестарта ───────────────────────────────────────────

class ReconcileTests(FocusBase):
    def test_reconcile_closes_past_entry_and_notifies(self) -> None:
        self.create_task("Задача.md")
        self.add_entries("Задача.md",
                         [self.entry("past1", at(minutes=-60), 30, open_=True)])
        result = focus.reconcile(self.root, self.store, now=T0)
        self.assertEqual(len(result.expired), 1)
        entry = self.entries("Задача.md")[0]
        self.assertEqual(entry.end_time, iso(at(minutes=-30)))
        record = self.store.get_focus("past1")
        self.assertEqual(record.status, runtime_store.FOCUS_EXPIRED)
        self.assertEqual(len(self.store.pending_events()), 1)
        self.assertIsNone(self.store.get_active_focus())

    def test_reconcile_restores_single_future_active(self) -> None:
        self.create_task("Задача.md")
        self.add_entries("Задача.md",
                         [self.entry("fut1", at(minutes=-5), 25, open_=True)])
        result = focus.reconcile(self.root, self.store, now=T0)
        self.assertIsNotNone(result.active)
        self.assertEqual(result.active.session_id, "fut1")
        self.assertEqual(self.store.get_active_focus().session_id, "fut1")

    def test_reconcile_keeps_newest_and_closes_older(self) -> None:
        self.create_task("А.md")
        self.create_task("Б.md")
        self.add_entries("А.md", [self.entry("old", at(minutes=-10), 50, open_=True)])
        self.add_entries("Б.md", [self.entry("new", at(minutes=-1), 50, open_=True)])
        result = focus.reconcile(self.root, self.store, now=T0)
        self.assertEqual(result.active.session_id, "new")
        self.assertEqual(self.entries("А.md")[0].end_time, iso(at(minutes=-1)))
        self.assertEqual(self.store.get_focus("old").status,
                         runtime_store.FOCUS_SUPERSEDED)
        self.assertEqual(self.store.get_active_focus().session_id, "new")

    def test_reconcile_orphans_db_active_missing_in_markdown(self) -> None:
        record = runtime_store.FocusRecord(
            session_id="ghost", task_path="TaskNotes/Tasks/Нет.md",
            status=runtime_store.FOCUS_ACTIVE, started_at=iso(at(minutes=-5)),
            planned_end=iso(at(minutes=20)), planned_minutes=25)
        self.store.activate_focus(record, now=T0)
        result = focus.reconcile(self.root, self.store, now=T0)
        self.assertEqual(self.store.get_focus("ghost").status,
                         runtime_store.FOCUS_ORPHANED)
        self.assertEqual(result.orphaned, ("ghost",))
        self.assertIsNone(self.store.get_active_focus())


# ── время ───────────────────────────────────────────────────────────────────

class ExpireRecoveryTests(FocusBase):
    def test_expire_recovers_without_active_row(self) -> None:
        self.create_task("Задача.md")
        self.add_entries("Задача.md",
                         [self.entry("past1", at(minutes=-60), 30, open_=True)])
        self.assertIsNone(self.store.get_active_focus())
        result = focus.expire_due(self.root, self.store, now=T0)
        self.assertEqual(len(result.expired), 1)
        self.assertEqual(self.entries("Задача.md")[0].end_time, iso(at(minutes=-30)))
        self.assertEqual(len(self.store.pending_events()), 1)
        self.assertEqual(self.store.get_focus("past1").status,
                         runtime_store.FOCUS_EXPIRED)
        self.assertEqual(focus.expire_due(self.root, self.store, now=T0).expired, ())
        self.assertEqual(len(self.store.pending_events()), 1)

    def test_expire_is_idempotent_when_markdown_close_retried(self) -> None:
        self.create_task("Задача.md")
        focus.start_focus(self.root, "Задача.md", 15, self.store, now=T0)
        with patch.object(focus, "_close_entry",
                          side_effect=focus.FocusError("boom")):
            result = focus.expire_due(self.root, self.store, now=at(minutes=20))
        self.assertEqual(len(result.expired), 1)
        self.assertEqual(len(self.store.pending_events()), 1)
        # Markdown всё ещё открыт; повторное истечение закрывает и не дублирует событие.
        self.assertFalse(self.entries("Задача.md")[0].end_time)
        focus.expire_due(self.root, self.store, now=at(minutes=25))
        self.assertEqual(self.entries("Задача.md")[0].end_time, iso(at(minutes=15)))
        self.assertEqual(len(self.store.pending_events()), 1)


class StartOrderingTests(FocusBase):
    def test_target_write_failure_leaves_old_focus(self) -> None:
        self.create_task("А.md")
        self.create_task("Б.md")
        first = focus.start_focus(self.root, "А.md", 25, self.store, now=T0)
        before_b = (self.tasks / "Б.md").read_text(encoding="utf-8")
        real_write = focus._write_task

        def flaky(vault, task, **kwargs):
            if Path(str(task)).name == "Б.md":
                raise focus.FocusError("boom")
            return real_write(vault, task, **kwargs)

        with patch.object(focus, "_write_task", side_effect=flaky):
            with self.assertRaises(focus.FocusError):
                focus.start_focus(self.root, "Б.md", 25, self.store,
                                  now=at(minutes=5))
        self.assertEqual((self.tasks / "Б.md").read_text(encoding="utf-8"), before_b)
        self.assertFalse(self.entries("А.md")[0].end_time,
                         "сбой цели не должен закрывать прошлый фокус")
        self.assertEqual(self.store.get_active_focus().session_id,
                         first.session.session_id)


class CallbackIdempotencyTests(FocusBase):
    def test_start_recovers_after_db_failure(self) -> None:
        self.create_task("Задача.md")
        with patch.object(self.store, "activate_focus_with_claim",
                          side_effect=runtime_store.RuntimeStoreError("boom")):
            with self.assertRaises(runtime_store.RuntimeStoreError):
                focus.start_focus(self.root, "Задача.md", 25, self.store,
                                  idempotency_key="k1", now=T0)
        entries = self.entries("Задача.md")
        self.assertEqual(len(entries), 1)
        session_id = entries[0].dusk_session_id
        self.assertEqual(entries[0].as_dict().get("duskRequest"),
                         focus._request_hash("start", "k1"))
        result = focus.start_focus(self.root, "Задача.md", 25, self.store,
                                   idempotency_key="k1", now=at(minutes=1))
        self.assertTrue(result.reused)
        self.assertEqual(result.session.session_id, session_id)
        self.assertEqual(len(self.entries("Задача.md")), 1)
        self.assertEqual(self.store.get_active_focus().session_id, session_id)

    def test_extend_recovers_after_db_failure(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        with patch.object(self.store, "update_focus_with_claim",
                          side_effect=runtime_store.RuntimeStoreError("boom")):
            with self.assertRaises(runtime_store.RuntimeStoreError):
                focus.extend_focus(self.root, started.session.session_id,
                                   self.store, idempotency_key="e1",
                                   now=at(minutes=5))
        entry = self.entries("Задача.md")[0]
        self.assertEqual(entry.planned_minutes, 40)
        self.assertEqual(entry.as_dict().get("duskExtend"),
                         focus._request_hash("extend", "e1"))
        result = focus.extend_focus(self.root, started.session.session_id,
                                    self.store, idempotency_key="e1",
                                    now=at(minutes=6))
        self.assertTrue(result.reused)
        self.assertEqual(self.entries("Задача.md")[0].planned_minutes, 40)
        self.assertEqual(
            self.store.get_focus(started.session.session_id).planned_minutes, 40)

    def test_no_raw_text_persisted(self) -> None:
        self.create_task("Задача.md")
        focus.start_focus(self.root, "Задача.md", 25, self.store,
                          idempotency_key="секретный текст", now=T0)
        raw = (self.tasks / "Задача.md").read_text(encoding="utf-8")
        self.assertNotIn("секретный", raw)


class ExtendDriftTests(FocusBase):
    def test_extend_rejects_past_deadline_without_mutation(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 15, self.store, now=T0)
        with self.assertRaises(focus.FocusError):
            focus.extend_focus(self.root, started.session.session_id, self.store,
                               now=at(minutes=20))
        self.assertEqual(self.entries("Задача.md")[0].planned_minutes, 15)
        self.assertEqual(
            self.store.get_focus(started.session.session_id).planned_minutes, 15)


class NormalizeAndBoundTests(FocusBase):
    def test_reconcile_normalizes_mixed_offset(self) -> None:
        self.create_task("Задача.md")
        entry = task_metadata.TimeEntry({
            "startTime": "2026-10-09T17:00:00+05:00",
            "plannedEnd": "2026-10-09T17:25:00+05:00",
            "plannedMinutes": 25, "duskSessionId": "mix1"})
        self.add_entries("Задача.md", [entry])
        focus.reconcile(self.root, self.store, now=T0)
        record = self.store.get_focus("mix1")
        self.assertEqual(record.started_at, "2026-10-09T12:00:00+00:00")
        self.assertEqual(record.planned_end, "2026-10-09T12:25:00+00:00")

    def test_scan_overflow_fails_closed(self) -> None:
        self.create_task("А.md")
        self.create_task("Б.md")
        with patch.object(focus, "SCAN_LIMIT", 1):
            with self.assertRaises(focus.FocusError):
                focus.reconcile(self.root, self.store, now=T0)


class ConcurrencyTests(FocusBase):
    def test_simultaneous_starts_leave_one_active(self) -> None:
        self.create_task("А.md")
        self.create_task("Б.md")
        barrier = threading.Barrier(2)
        errors = []

        def run(name, moment):
            try:
                barrier.wait(timeout=5)
                focus.start_focus(self.root, name, 25, self.store, now=moment)
            except Exception as error:  # тест собирает ошибки фоновых потоков
                errors.append(error)

        threads = [threading.Thread(target=run, args=("А.md", T0)),
                   threading.Thread(target=run, args=("Б.md", at(minutes=1)))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        active = self.store.get_active_focus()
        self.assertIsNotNone(active)
        open_entries = [entry for entry in
                        list(self.entries("А.md")) + list(self.entries("Б.md"))
                        if not entry.end_time]
        self.assertEqual(len(open_entries), 1)
        self.assertEqual(open_entries[0].dusk_session_id, active.session_id)


class MalformedCardInvariantTests(FocusBase):
    """Повреждённая карточка с Dusk-маркером не должна молча пропускаться."""

    def broken_dusk(self) -> Path:
        path = self.tasks / "Broken.md"
        path.write_text(
            "---\n"
            'title: "Broken"\nstatus: next\n'
            "timeEntries:\n"
            "  - startTime: '2026-10-09T10:00:00+00:00'\n"
            "    plannedEnd: '2026-10-09T10:30:00+00:00'\n"
            "    plannedMinutes: 30\n"
            "    duskSessionId: old\n"
            "    loop: &a\n      self: *a\n"
            "---\n# Broken\n",
            encoding="utf-8")
        return path

    def unrelated_malformed(self) -> Path:
        path = self.tasks / "Noise.md"
        path.write_text(
            "---\n"
            'title: "Noise"\nstatus: next\n'
            "timeEntries:\n"
            "  - loop: &a\n      self: *a\n"
            "---\n# Noise\n",
            encoding="utf-8")
        return path

    def test_broken_dusk_card_blocks_start(self) -> None:
        broken = self.broken_dusk()
        self.create_task("Новая.md")
        target = self.tasks / "Новая.md"
        before_target = target.read_text(encoding="utf-8")
        before_broken = broken.read_text(encoding="utf-8")
        with self.assertRaises(focus.FocusError):
            focus.start_focus(self.root, "Новая.md", 25, self.store, now=T0)
        self.assertEqual(target.read_text(encoding="utf-8"), before_target)
        self.assertEqual(broken.read_text(encoding="utf-8"), before_broken)
        self.assertIsNone(self.store.get_active_focus())

    def test_broken_dusk_card_blocks_reconcile(self) -> None:
        broken = self.broken_dusk()
        before_broken = broken.read_text(encoding="utf-8")
        with self.assertRaises(focus.FocusError):
            focus.reconcile(self.root, self.store, now=T0)
        self.assertEqual(broken.read_text(encoding="utf-8"), before_broken)
        self.assertIsNone(self.store.get_active_focus())

    def test_unrelated_malformed_card_does_not_block_start(self) -> None:
        self.unrelated_malformed()
        self.create_task("Новая.md")
        result = focus.start_focus(self.root, "Новая.md", 25, self.store, now=T0)
        self.assertTrue(result.ok)
        self.assertEqual(self.store.get_active_focus().session_id,
                         result.session.session_id)


class InvalidDuskEntryTests(FocusBase):
    """Открытая Dusk-like запись с невалидными полями не должна молча пропускаться."""

    BASE_FIELDS = (
        ("startTime", "'2026-10-09T10:00:00+00:00'"),
        ("plannedEnd", "'2026-10-09T10:30:00+00:00'"),
        ("plannedMinutes", "25"),
        ("duskSessionId", "old"),
    )

    def reset_tasks(self) -> None:
        for path in sorted(self.tasks.glob("*.md")):
            path.unlink()

    def fresh_store(self) -> "runtime_store.RuntimeStore":
        store = runtime_store.RuntimeStore(
            self.root / "state" / f"runtime-{uuid.uuid4().hex}.sqlite3")
        self.addCleanup(store.close)
        return store

    def entry_block(self, **overrides) -> str:
        fields = list(self.BASE_FIELDS)
        for key, value in overrides.items():
            fields = [(k, v) for k, v in fields if k != key]
            if value is not None:
                fields.append((key, value))
        lines = []
        for index, (key, value) in enumerate(fields):
            prefix = "  - " if index == 0 else "    "
            lines.append(f"{prefix}{key}: {value}")
        return "\n".join(lines) + "\n"

    def write_card(self, name: str, entry_block: str) -> Path:
        path = self.tasks / name
        path.write_text(
            "---\n"
            'title: "X"\nstatus: next\n'
            "timeEntries:\n" + entry_block + "---\n# X\n",
            encoding="utf-8")
        return path

    def variants(self):
        return [
            ("empty-session-id", self.entry_block(duskSessionId="''")),
            ("broken-start", self.entry_block(startTime="nope")),
            ("naive-start",
             self.entry_block(startTime="'2026-10-09T10:00:00'")),
            ("broken-planned-end", self.entry_block(plannedEnd="also-nope")),
            ("both-broken", self.entry_block(startTime="nope",
                                             plannedEnd="also-nope")),
            ("naive-planned-end",
             self.entry_block(plannedEnd="'2026-10-09T10:30:00'")),
            ("end-before-start",
             self.entry_block(plannedEnd="'2026-10-09T09:00:00+00:00'")),
            ("missing-start", self.entry_block(startTime=None)),
            ("missing-planned-end", self.entry_block(plannedEnd=None)),
            ("source-only-open",
             "  - startTime: '2026-10-09T10:00:00+00:00'\n"
             "    source: 'Dusk Telegram'\n"),
            ("colon-session-id", self.entry_block(duskSessionId="'bad:id'")),
            ("long-session-id", self.entry_block(duskSessionId="a" * 49)),
            ("unicode-session-id", self.entry_block(duskSessionId="'сессия'")),
        ]

    def test_invalid_open_dusk_blocks_all_transitions(self) -> None:
        for label, block in self.variants():
            with self.subTest(label=label):
                self.reset_tasks()
                store = self.fresh_store()
                broken = self.write_card("Broken.md", block)
                self.create_task("Новая.md")
                target = self.tasks / "Новая.md"
                before_target = target.read_text(encoding="utf-8")
                before_broken = broken.read_text(encoding="utf-8")

                with self.assertRaises(focus.FocusError):
                    focus.start_focus(self.root, "Новая.md", 25, store, now=T0)
                with self.assertRaises(focus.FocusError):
                    focus.reconcile(self.root, store, now=T0)
                with self.assertRaises(focus.FocusError):
                    focus.expire_due(self.root, store, now=T0)

                self.assertEqual(target.read_text(encoding="utf-8"),
                                 before_target)
                self.assertEqual(broken.read_text(encoding="utf-8"),
                                 before_broken)
                self.assertIsNone(store.get_active_focus())

    def test_duplicate_open_session_id_fails_closed(self) -> None:
        first = self.write_card("A.md", self.entry_block(duskSessionId="dup"))
        second = self.write_card("B.md", self.entry_block(duskSessionId="dup"))
        self.create_task("Новая.md")
        target = self.tasks / "Новая.md"
        before = {path: path.read_text(encoding="utf-8")
                  for path in (target, first, second)}
        with self.assertRaises(focus.FocusError):
            focus.start_focus(self.root, "Новая.md", 25, self.store, now=T0)
        with self.assertRaises(focus.FocusError):
            focus.reconcile(self.root, self.store, now=T0)
        with self.assertRaises(focus.FocusError):
            focus.expire_due(self.root, self.store, now=T0)
        for path, content in before.items():
            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIsNone(self.store.get_active_focus())

    def test_closed_broken_history_does_not_block_start(self) -> None:
        block = self.entry_block(plannedEnd="nope")
        block += "    endTime: '2026-10-09T10:30:00+00:00'\n"
        self.write_card("History.md", block)
        self.create_task("Новая.md")
        result = focus.start_focus(self.root, "Новая.md", 25, self.store, now=T0)
        self.assertTrue(result.ok)

    def test_native_open_entry_on_other_card_does_not_block(self) -> None:
        self.create_task("Native.md")
        self.add_entries("Native.md", [task_metadata.TimeEntry(
            {"startTime": iso(T0), "description": "native session"})])
        self.create_task("Новая.md")
        result = focus.start_focus(self.root, "Новая.md", 25, self.store, now=T0)
        self.assertTrue(result.ok)


class SyncSignalTests(FocusBase):
    """`sync_required` — правда только при реальной Markdown-мутации."""

    def start(self, name="Задача.md", key="", moment=None):
        self.create_task(name) if not (self.tasks / name).exists() else None
        return focus.start_focus(self.root, name, 25, self.store,
                                 idempotency_key=key, now=moment or T0)

    def test_fresh_start_requires_sync(self) -> None:
        self.create_task("Задача.md")
        result = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        self.assertTrue(result.sync_required)

    def test_reused_start_does_not_require_sync(self) -> None:
        self.create_task("Задача.md")
        focus.start_focus(self.root, "Задача.md", 25, self.store,
                          idempotency_key="k", now=T0)
        again = focus.start_focus(self.root, "Задача.md", 25, self.store,
                                  idempotency_key="k", now=at(minutes=1))
        self.assertTrue(again.reused)
        self.assertFalse(again.sync_required)

    def test_recovered_start_requires_sync(self) -> None:
        self.create_task("Задача.md")
        with patch.object(self.store, "activate_focus_with_claim",
                          side_effect=runtime_store.RuntimeStoreError("boom")):
            with self.assertRaises(runtime_store.RuntimeStoreError):
                focus.start_focus(self.root, "Задача.md", 25, self.store,
                                  idempotency_key="k1", now=T0)
        result = focus.start_focus(self.root, "Задача.md", 25, self.store,
                                   idempotency_key="k1", now=at(minutes=1))
        self.assertTrue(result.reused)
        self.assertTrue(result.sync_required)

    def test_fresh_extend_requires_sync(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        result = focus.extend_focus(self.root, started.session.session_id,
                                    self.store, now=at(minutes=1))
        self.assertTrue(result.sync_required)

    def test_reused_extend_does_not_require_sync(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        focus.extend_focus(self.root, started.session.session_id, self.store,
                           idempotency_key="e", now=at(minutes=1))
        again = focus.extend_focus(self.root, started.session.session_id,
                                   self.store, idempotency_key="e",
                                   now=at(minutes=2))
        self.assertTrue(again.reused)
        self.assertFalse(again.sync_required)

    def test_fresh_stop_requires_sync(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        result = focus.stop_focus(self.root, started.session.session_id,
                                  self.store, now=at(minutes=5))
        self.assertTrue(result.sync_required)

    def test_repeat_stop_does_not_require_sync(self) -> None:
        self.create_task("Задача.md")
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=T0)
        focus.stop_focus(self.root, started.session.session_id, self.store,
                         now=at(minutes=5))
        again = focus.stop_focus(self.root, started.session.session_id,
                                 self.store, now=at(minutes=6))
        self.assertTrue(again.reused)
        self.assertFalse(again.sync_required)

    def test_expire_requires_sync_only_when_markdown_changed(self) -> None:
        self.create_task("Задача.md")
        focus.start_focus(self.root, "Задача.md", 15, self.store, now=T0)
        first = focus.expire_due(self.root, self.store, now=at(minutes=20))
        self.assertTrue(first.sync_required)
        second = focus.expire_due(self.root, self.store, now=at(minutes=25))
        self.assertFalse(second.sync_required)

    def test_reconcile_db_only_repair_does_not_require_sync(self) -> None:
        record = runtime_store.FocusRecord(
            session_id="ghost", task_path="TaskNotes/Tasks/Нет.md",
            status=runtime_store.FOCUS_ACTIVE, started_at=iso(at(minutes=-5)),
            planned_end=iso(at(minutes=20)), planned_minutes=25)
        self.store.activate_focus(record, now=T0)
        result = focus.reconcile(self.root, self.store, now=T0)
        self.assertEqual(result.orphaned, ("ghost",))
        self.assertFalse(result.sync_required)

    def test_reconcile_past_entry_requires_sync(self) -> None:
        self.create_task("Задача.md")
        self.add_entries("Задача.md",
                         [self.entry("past1", at(minutes=-60), 30, open_=True)])
        result = focus.reconcile(self.root, self.store, now=T0)
        self.assertTrue(result.sync_required)


class SessionIdValidationTests(unittest.TestCase):
    def test_runtime_generated_id_is_valid(self) -> None:
        self.assertTrue(focus.is_valid_session_id(focus._new_session_id()))
        self.assertTrue(focus.is_valid_session_id("abc-123_X"))
        self.assertTrue(focus.is_valid_session_id("a" * 48))

    def test_invalid_ids_fail_closed(self) -> None:
        for bad in ("", None, "a" * 49, "bad:id", "сессия", "has space",
                    "new\nline", "dot.dot", "/slash"):
            with self.subTest(bad=bad):
                self.assertFalse(focus.is_valid_session_id(bad))


class TimeBoundaryTests(FocusBase):
    def test_naive_now_rejected(self) -> None:
        self.create_task("Задача.md")
        naive = datetime.datetime(2026, 10, 9, 12, 0)
        with self.assertRaises(focus.FocusError):
            focus.start_focus(self.root, "Задача.md", 25, self.store, now=naive)

    def test_invalid_planned_timestamp_rejected_on_extend(self) -> None:
        self.create_task("Задача.md")
        broken = task_metadata.TimeEntry({
            "startTime": iso(T0), "plannedEnd": "не-дата",
            "duskSessionId": "bad1", "plannedMinutes": 25})
        self.add_entries("Задача.md", [broken])
        record = runtime_store.FocusRecord(
            session_id="bad1", task_path="TaskNotes/Tasks/Задача.md",
            status=runtime_store.FOCUS_ACTIVE, started_at=iso(T0),
            planned_end=iso(at(minutes=25)), planned_minutes=25)
        self.store.activate_focus(record, now=T0)
        with self.assertRaises(focus.FocusError):
            focus.extend_focus(self.root, "bad1", self.store, now=T0)


if __name__ == "__main__":
    unittest.main()
