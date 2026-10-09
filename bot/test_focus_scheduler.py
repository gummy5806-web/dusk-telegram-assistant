#!/usr/bin/env python3
"""Чистый focus-scheduler: bounded cycle, доставка, backoff, cancellation.

Без Telegram: `deliver` и `sync` внедряются. Временный vault и временная SQLite.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import tempfile
import unittest
from pathlib import Path

import focus
import focus_scheduler
import runtime_store
import task_metadata

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def iso(moment: datetime.datetime) -> str:
    return moment.astimezone(UTC).isoformat()


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
        logging.getLogger("focus_scheduler").setLevel(logging.CRITICAL)

    def task(self, name: str = "Задача.md") -> Path:
        title = name[:-3] if name.endswith(".md") else name
        path = self.tasks / (title + ".md")
        path.write_text(
            "---\n" f'title: "{title}"\nstatus: next\n'
            "---\n" f"# {title}\n", encoding="utf-8")
        return path

    def make_scheduler(self, deliver=None, sync=None, **kwargs):
        async def default(notification):
            self.sent.append(notification)
        if sync is None:
            sync = lambda message: self.synced.append(message)
        return focus_scheduler.FocusScheduler(
            vault=self.root, store=self.store,
            deliver=deliver or default, sync=sync, **kwargs)

    def labels(self, notification) -> list:
        return [label for row in notification.buttons for label, _data in row]


class CycleTests(SchedulerBase):
    async def test_cycle_expires_and_delivers_once(self) -> None:
        self.task()
        focus.start_focus(self.root, "Задача.md", 15, self.store, now=NOW)
        scheduler = self.make_scheduler()
        result = await scheduler.run_cycle(now=at(minutes=20))
        self.assertEqual(result.expired, 1)
        self.assertEqual(result.delivered, 1)
        self.assertTrue(result.synced)
        self.assertEqual(self.synced, ["telegram: фокус"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Время вышло", self.sent[0].text)
        self.assertIn("Задача", self.sent[0].text)
        self.assertIn("✅ Задача готова", self.labels(self.sent[0]))
        self.assertIn("➕ Ещё 15", self.labels(self.sent[0]))
        self.assertIn("⏹ Закончил блок", self.labels(self.sent[0]))
        self.assertEqual(self.store.get_event(
            self.store.pending_events()[0].event_id).status
            if self.store.pending_events() else "delivered", "delivered")
        again = await scheduler.run_cycle(now=at(minutes=25))
        self.assertEqual(again.delivered, 0)
        self.assertEqual(len(self.sent), 1)

    async def test_send_failure_backs_off_then_retries(self) -> None:
        calls = []

        async def failing(notification):
            calls.append(notification)
            raise RuntimeError("boom")

        self.task()
        focus.start_focus(self.root, "Задача.md", 15, self.store, now=NOW)
        scheduler = self.make_scheduler(deliver=failing)
        first = await scheduler.run_cycle(now=at(minutes=20))
        self.assertEqual(first.failed, 1)
        self.assertEqual(len(calls), 1)
        [event] = self.store.pending_events()
        self.assertEqual(event.status, "failed")
        second = await scheduler.run_cycle(now=at(minutes=20, seconds=5))
        self.assertEqual(second.failed, 0)
        self.assertEqual(len(calls), 1, "backoff ещё не истёк")
        third = await scheduler.run_cycle(now=at(minutes=20, seconds=31))
        self.assertEqual(len(calls), 2, "после backoff повтор доставки")

    async def test_cancelled_delivery_is_not_marked_failed(self) -> None:
        async def cancelled(notification):
            raise asyncio.CancelledError()

        self.task()
        focus.start_focus(self.root, "Задача.md", 15, self.store, now=NOW)
        scheduler = self.make_scheduler(deliver=cancelled)
        with self.assertRaises(asyncio.CancelledError):
            await scheduler.run_cycle(now=at(minutes=20))
        [event] = self.store.pending_events()
        self.assertEqual(event.status, "leased")
        self.assertEqual(event.attempts, 1)

    async def test_cycle_is_bounded(self) -> None:
        for index in range(3):
            self.store.enqueue_event(f"s{index}",
                                     runtime_store.EVENT_FOCUS_EXPIRED, now=NOW)
        scheduler = self.make_scheduler(max_events=2)
        result = await scheduler.run_cycle(now=NOW)
        self.assertEqual(result.delivered, 2)
        self.assertEqual(len(self.sent), 2)


class WaitTests(SchedulerBase):
    def test_idle_wait_is_clamped(self) -> None:
        scheduler = self.make_scheduler(max_idle=10.0)
        self.assertEqual(scheduler.next_wait(now=NOW), 10.0)

    def test_active_deadline_drives_wait(self) -> None:
        self.task()
        focus.start_focus(self.root, "Задача.md", 15, self.store, now=NOW)
        scheduler = self.make_scheduler(max_idle=30.0)
        self.assertAlmostEqual(
            scheduler.next_wait(now=at(minutes=14, seconds=55)), 5.0, delta=1.0)
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


class SyncResilienceTests(SchedulerBase):
    async def test_sync_error_does_not_block_delivery(self) -> None:
        self.task()
        focus.start_focus(self.root, "Задача.md", 15, self.store, now=NOW)

        def boom(message):
            raise RuntimeError("git")

        scheduler = self.make_scheduler(sync=boom)
        result = await scheduler.run_cycle(now=at(minutes=20))
        self.assertEqual(result.delivered, 1)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(result.synced)
        self.assertIn("RuntimeError", result.errors)
        self.assertEqual(self.store.pending_events(), ())

    async def test_delivery_precedes_sync(self) -> None:
        order = []
        self.task()
        focus.start_focus(self.root, "Задача.md", 15, self.store, now=NOW)

        async def deliver(notification):
            order.append("deliver")

        def sync(message):
            order.append("sync")

        scheduler = self.make_scheduler(deliver=deliver, sync=sync)
        result = await scheduler.run_cycle(now=at(minutes=20))
        self.assertEqual(order, ["deliver", "sync"])
        self.assertTrue(result.synced)


class StartupReconcileTests(SchedulerBase):
    async def test_reconcile_then_cycle_delivers_pending(self) -> None:
        self.task()
        past = task_metadata.TimeEntry({
            "startTime": iso(at(minutes=-60)),
            "plannedEnd": iso(at(minutes=-30)),
            "plannedMinutes": 30,
            "duskSessionId": "past1",
        })
        task_metadata.write_task(self.root, "Задача.md", time_entries=[past])
        reconciled = focus.reconcile(self.root, self.store, now=NOW)
        self.assertEqual(len(reconciled.expired), 1)
        scheduler = self.make_scheduler()
        result = await scheduler.run_cycle(now=at(minutes=5))
        self.assertEqual(result.delivered, 1)
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
