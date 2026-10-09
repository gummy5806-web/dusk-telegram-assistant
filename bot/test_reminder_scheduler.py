#!/usr/bin/env python3
"""Чистый reminder-scheduler: delivery, backoff, validate, cancellation."""

from __future__ import annotations

import asyncio
import datetime
import logging
import tempfile
import threading
import unittest
from pathlib import Path

import dispatch
import reminder_scheduler
import reminders
import runtime_store
import task_metadata

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def at(**delta) -> datetime.datetime:
    return NOW + datetime.timedelta(**delta)


class SchedulerBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        self.store = runtime_store.RuntimeStore(
            self.root / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)
        self.sent = []
        self.synced = []
        logging.getLogger("reminder_scheduler").setLevel(logging.CRITICAL)

    def task(self, name: str = "Задача.md", status: str = "next") -> Path:
        title = name[:-3] if name.endswith(".md") else name
        path = self.tasks / (title + ".md")
        path.write_text(
            "---\n" f'title: "{title}"\nstatus: {status}\n'
            "---\n" f"# {title}\n", encoding="utf-8")
        return path

    def seed_due(self, minutes: int = 30):
        self.task()
        return reminders.create_reminder(self.root, "Задача.md",
                                         at(minutes=minutes), self.store, now=NOW)

    def make_scheduler(self, deliver=None, sync=None, **kwargs):
        async def default(notification):
            self.sent.append(notification)
        if sync is None:
            sync = lambda message: self.synced.append(message)
        return reminder_scheduler.ReminderScheduler(
            vault=self.root, store=self.store, deliver=deliver or default,
            sync=sync, **kwargs)


class CycleTests(SchedulerBase):
    async def test_cycle_delivers_and_marks_delivered(self) -> None:
        self.seed_due()
        scheduler = self.make_scheduler()
        result = await scheduler.run_cycle(now=at(minutes=31))
        self.assertEqual(result.due, 1)
        self.assertEqual(result.delivered, 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("⏰ Напоминание: Задача", self.sent[0].text)
        self.assertEqual(self.store.pending_reminder_events(), ())

    async def test_delivery_failure_backs_off(self) -> None:
        calls = []

        async def failing(notification):
            calls.append(notification)
            raise RuntimeError("boom")

        self.seed_due()
        scheduler = self.make_scheduler(deliver=failing)
        first = await scheduler.run_cycle(now=at(minutes=31))
        self.assertEqual(first.failed, 1)
        self.assertEqual(len(calls), 1)
        second = await scheduler.run_cycle(now=at(minutes=31, seconds=5))
        self.assertEqual(len(calls), 1, "backoff ещё не истёк")
        third = await scheduler.run_cycle(now=at(minutes=31, seconds=31))
        self.assertEqual(len(calls), 2)

    async def test_cancelled_delivery_not_failed(self) -> None:
        async def cancelled(notification):
            raise asyncio.CancelledError()

        self.seed_due()
        scheduler = self.make_scheduler(deliver=cancelled)
        with self.assertRaises(asyncio.CancelledError):
            await scheduler.run_cycle(now=at(minutes=31))
        [event] = self.store.pending_reminder_events()
        self.assertEqual(event.state, runtime_store.EVENT_LEASED)

    async def test_missing_after_claim_no_send(self) -> None:
        created = self.seed_due()
        self.store.collect_due(now=at(minutes=31))
        claimed = self.store.claim_reminder_event(now=at(minutes=31))
        task_metadata.write_task(self.root, "Задача.md", reminders=[])
        check = reminders.validate_delivery(self.root, self.store, claimed,
                                            now=at(minutes=31))
        self.assertFalse(check.ok)
        self.assertEqual(check.reason, "missing_or_done")
        scheduler = self.make_scheduler()
        await scheduler.run_cycle(now=at(minutes=31))
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(self.store.pending_reminder_events(), ())
        self.assertEqual(self.store.get_reminder(created.reminder.reminder_id).state,
                         runtime_store.REMINDER_CANCELLED)

    async def test_done_after_claim_no_send(self) -> None:
        self.seed_due()
        self.store.collect_due(now=at(minutes=31))
        claimed = self.store.claim_reminder_event(now=at(minutes=31))
        dispatch.set_task_status(self.tasks / "Задача.md", "done",
                                 now=at(minutes=31), vault=self.root)
        check = reminders.validate_delivery(self.root, self.store, claimed,
                                            now=at(minutes=31))
        self.assertFalse(check.ok)
        scheduler = self.make_scheduler()
        await scheduler.run_cycle(now=at(minutes=31))
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(self.store.pending_reminder_events(), ())


    async def test_notification_uses_event_id(self) -> None:
        self.seed_due()
        seen = []

        async def deliver(notification):
            seen.append(notification)

        scheduler = self.make_scheduler(deliver=deliver)
        await scheduler.run_cycle(now=at(minutes=31))
        self.assertEqual(len(seen), 1)
        data = [value for row in seen[0].buttons for _label, value in row]
        self.assertIn(f"r:g:{seen[0].event_id}", data)

    async def test_snooze_between_validate_and_mark(self) -> None:
        created = self.seed_due()
        rid = created.reminder.reminder_id

        async def deliver(notification):
            reminders.snooze_reminder(self.root, rid, at(minutes=120),
                                      self.store, now=at(minutes=31))

        scheduler = self.make_scheduler(deliver=deliver)
        result = await scheduler.run_cycle(now=at(minutes=31))
        self.assertEqual(result.delivered, 0)
        current = self.store.get_reminder(rid)
        self.assertEqual(current.state, runtime_store.REMINDER_ACTIVE)
        self.assertEqual(current.generation, 2)

    async def test_remove_between_validate_and_mark(self) -> None:
        created = self.seed_due()
        rid = created.reminder.reminder_id

        async def deliver(notification):
            reminders.cancel_reminder(self.root, rid, self.store,
                                      now=at(minutes=31))

        scheduler = self.make_scheduler(deliver=deliver)
        result = await scheduler.run_cycle(now=at(minutes=31))
        self.assertEqual(result.delivered, 0)
        self.assertEqual(self.store.get_reminder(rid).state,
                         runtime_store.REMINDER_CANCELLED)

    async def test_restart_reconcile_then_deliver(self) -> None:
        self.task()
        entry = task_metadata.ReminderEntry({
            "id": "dusk_restart", "type": "absolute",
            "absoluteTime": at(minutes=30).isoformat()})
        task_metadata.write_task(self.root, "Задача.md", reminders=[entry])
        fresh = runtime_store.RuntimeStore(
            self.root / "loss" / "runtime.sqlite3")
        self.addCleanup(fresh.close)
        sent = []

        async def deliver(notification):
            sent.append(notification)

        scheduler = reminder_scheduler.ReminderScheduler(
            vault=self.root, store=fresh, deliver=deliver,
            sync=lambda message: None, max_events=5)
        result = await scheduler.run_cycle(now=at(minutes=31))
        self.assertEqual(result.delivered, 1)
        self.assertEqual(len(sent), 1)


class WaitTests(SchedulerBase):
    def test_idle_wait_clamped(self) -> None:
        scheduler = self.make_scheduler(max_idle=10.0)
        self.assertEqual(scheduler.next_wait(now=NOW), 10.0)

    def test_active_deadline_drives_wait(self) -> None:
        self.seed_due(minutes=5)
        scheduler = self.make_scheduler(max_idle=30.0)
        self.assertAlmostEqual(
            scheduler.next_wait(now=at(minutes=4, seconds=55)), 5.0, delta=1.0)
        self.assertGreater(scheduler.next_wait(now=NOW), 0.0)

    async def test_run_loop_stops_cleanly(self) -> None:
        scheduler = self.make_scheduler(max_idle=1.0)
        stop = asyncio.Event()
        wake = asyncio.Event()
        task = asyncio.create_task(scheduler.run(stop, wake))
        await asyncio.sleep(0.05)
        scheduler.stop(stop, wake)
        await asyncio.wait_for(task, timeout=3)
        self.assertTrue(task.done())

    async def test_next_wait_runs_off_event_loop(self) -> None:
        scheduler = self.make_scheduler(max_idle=0.05)
        seen = []
        scheduler.next_wait = lambda now=None: (
            seen.append(threading.get_ident()) or 0.05)
        stop = asyncio.Event()
        wake = asyncio.Event()
        task = asyncio.create_task(scheduler.run(stop, wake))
        await asyncio.sleep(0.1)
        scheduler.stop(stop, wake)
        await asyncio.wait_for(task, timeout=3)
        self.assertTrue(seen)
        self.assertTrue(all(ident != threading.get_ident() for ident in seen))


if __name__ == "__main__":
    unittest.main()
