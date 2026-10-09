#!/usr/bin/env python3
"""Ядро напоминаний: create/snooze/cancel/reconcile/collect_due.

Временный vault и временная SQLite, без Telegram/git/сети.
"""

from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dispatch
import reminders
import runtime_store
import task_metadata

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def at(**delta) -> datetime.datetime:
    return NOW + datetime.timedelta(**delta)


class ReminderBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        self.store = runtime_store.RuntimeStore(
            self.root / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)

    def task(self, name: str = "Задача.md", status: str = "next") -> Path:
        title = name[:-3] if name.endswith(".md") else name
        path = self.tasks / (title + ".md")
        path.write_text(
            "---\n" f'title: "{title}"\nstatus: {status}\n'
            "---\n" f"# {title}\n", encoding="utf-8")
        return path

    def add_reminders(self, name: str, entries) -> None:
        task_metadata.write_task(self.root, name, reminders=entries)

    def stored(self, name: str = "Задача.md"):
        return task_metadata.read_task(self.root, name).reminders

    def dusk(self, reminder_id: str, when: datetime.datetime):
        return task_metadata.ReminderEntry({
            "id": reminder_id, "type": "absolute",
            "description": "Dusk reminder",
            "absoluteTime": when.isoformat()})

    def native(self):
        return task_metadata.ReminderEntry({
            "id": "native-1", "type": "relative", "relatedTo": "due",
            "offset": "-PT1H"})


class CreateTests(ReminderBase):
    def test_create_writes_markdown_and_projection(self) -> None:
        self.task()
        result = reminders.create_reminder(self.root, "Задача.md",
                                           at(minutes=60), self.store, now=NOW)
        self.assertTrue(result.sync_required)
        entries = self.stored()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].is_dusk())
        self.assertEqual(entries[0].type, "absolute")
        record = self.store.get_reminder(entries[0].id)
        self.assertIsNotNone(record)
        self.assertEqual(record.state, runtime_store.REMINDER_ACTIVE)
        self.assertEqual(record.generation, 1)

    def test_create_rejects_past_and_naive(self) -> None:
        self.task()
        with self.assertRaises(reminders.ReminderError):
            reminders.create_reminder(self.root, "Задача.md", at(minutes=-1),
                                      self.store, now=NOW)
        with self.assertRaises(reminders.ReminderError):
            reminders.create_reminder(self.root, "Задача.md", at(minutes=0),
                                      self.store, now=NOW)
        with self.assertRaises(reminders.ReminderError):
            reminders.create_reminder(
                self.root, "Задача.md",
                datetime.datetime(2026, 10, 10, 9, 0), self.store, now=NOW)
        self.assertEqual(self.stored(), ())

    def test_create_rejects_done_task(self) -> None:
        self.task(status="done")
        with self.assertRaises(reminders.ReminderError):
            reminders.create_reminder(self.root, "Задача.md", at(minutes=60),
                                      self.store, now=NOW)
        self.assertEqual(self.stored(), ())

    def test_create_idempotent_by_key(self) -> None:
        self.task()
        first = reminders.create_reminder(self.root, "Задача.md",
                                          at(minutes=60), self.store,
                                          idempotency_key="k", now=NOW)
        second = reminders.create_reminder(self.root, "Задача.md",
                                           at(minutes=60), self.store,
                                           idempotency_key="k", now=at(minutes=1))
        self.assertTrue(second.reused)
        self.assertEqual(first.reminder.reminder_id, second.reminder.reminder_id)
        self.assertEqual(len(self.stored()), 1)

    def test_create_recovers_after_db_failure(self) -> None:
        self.task()
        with patch.object(self.store, "upsert_reminder",
                          side_effect=runtime_store.RuntimeStoreError("boom")):
            with self.assertRaises(runtime_store.RuntimeStoreError):
                reminders.create_reminder(self.root, "Задача.md",
                                          at(minutes=60), self.store,
                                          idempotency_key="k1", now=NOW)
        entries = self.stored()
        self.assertEqual(len(entries), 1)
        self.assertIsNone(self.store.get_reminder(entries[0].id))
        result = reminders.create_reminder(self.root, "Задача.md",
                                           at(minutes=60), self.store,
                                           idempotency_key="k1", now=at(minutes=1))
        self.assertTrue(result.reused)
        self.assertEqual(len(self.stored()), 1)
        self.assertIsNotNone(self.store.get_reminder(entries[0].id))

    def test_markdown_failure_does_not_touch_sqlite(self) -> None:
        path = self.task()
        before = path.read_text(encoding="utf-8")
        with patch("task_metadata._atomic_write",
                   side_effect=task_metadata.TaskMetadataError("boom")):
            with self.assertRaises(reminders.ReminderError):
                reminders.create_reminder(self.root, "Задача.md",
                                          at(minutes=60), self.store, now=NOW)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.store.pending_reminder_events(), ())

    def test_reminder_id_validation(self) -> None:
        self.assertTrue(reminders.is_valid_reminder_id("dusk_" + "a" * 42))
        for bad in ("", "dusk", "dusk_", "dusk_bad:id", "dusk_сессия",
                    "dusk_" + "a" * 44, "native-1"):
            with self.subTest(bad=bad):
                self.assertFalse(reminders.is_valid_reminder_id(bad))


class SnoozeTests(ReminderBase):
    def test_snooze_bumps_generation(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=60), self.store, now=NOW)
        rid = created.reminder.reminder_id
        result = reminders.snooze_reminder(self.root, rid, at(minutes=120),
                                           self.store, now=at(minutes=1))
        self.assertTrue(result.sync_required)
        self.assertEqual(self.store.get_reminder(rid).generation, 2)
        entry = [item for item in self.stored() if item.id == rid][0]
        self.assertEqual(entry.absolute_time, at(minutes=120).isoformat())

    def test_snooze_same_time_is_idempotent(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=60), self.store, now=NOW)
        rid = created.reminder.reminder_id
        result = reminders.snooze_reminder(self.root, rid, at(minutes=60),
                                           self.store, now=at(minutes=1))
        self.assertTrue(result.reused)
        self.assertEqual(self.store.get_reminder(rid).generation, 1)

    def test_snooze_recovers_after_db_failure(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=60), self.store, now=NOW)
        rid = created.reminder.reminder_id
        with patch.object(self.store, "snooze_reminder",
                          side_effect=runtime_store.RuntimeStoreError("boom")):
            with self.assertRaises(runtime_store.RuntimeStoreError):
                reminders.snooze_reminder(self.root, rid, at(minutes=120),
                                          self.store, idempotency_key="s1",
                                          now=at(minutes=1))
        entry = [item for item in self.stored() if item.id == rid][0]
        self.assertEqual(entry.absolute_time, at(minutes=120).isoformat())
        result = reminders.snooze_reminder(self.root, rid, at(minutes=120),
                                           self.store, idempotency_key="s1",
                                           now=at(minutes=2))
        self.assertTrue(result.reused)
        self.assertEqual(self.store.get_reminder(rid).generation, 2)

    def test_snooze_rejects_past(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=60), self.store, now=NOW)
        with self.assertRaises(reminders.ReminderError):
            reminders.snooze_reminder(self.root,
                                      created.reminder.reminder_id,
                                      at(minutes=-1), self.store, now=NOW)


class CancelTests(ReminderBase):
    def test_cancel_removes_exact_dusk_only(self) -> None:
        self.task()
        self.add_reminders("Задача.md", [
            self.native(),
            self.dusk("dusk_a", at(minutes=60)),
            self.dusk("dusk_b", at(minutes=90)),
        ])
        reminders.reconcile(self.root, self.store, now=NOW)
        result = reminders.cancel_reminder(self.root, "dusk_a", self.store,
                                           now=at(minutes=1))
        self.assertTrue(result.sync_required)
        self.assertEqual({item.id for item in self.stored()},
                         {"native-1", "dusk_b"})
        self.assertEqual(self.store.get_reminder("dusk_a").state,
                         runtime_store.REMINDER_CANCELLED)

    def test_cancel_unknown_fails(self) -> None:
        self.task()
        with self.assertRaises(reminders.ReminderError):
            reminders.cancel_reminder(self.root, "dusk_missing", self.store,
                                      now=NOW)


class ScanValidationTests(ReminderBase):
    def test_invalid_dusk_reminder_fails_closed(self) -> None:
        variants = [
            {"id": "dusk_bad:id", "type": "absolute",
             "absoluteTime": at(minutes=60).isoformat()},
            {"id": "dusk_" + "a" * 44, "type": "absolute",
             "absoluteTime": at(minutes=60).isoformat()},
            {"id": "dusk_rel", "type": "relative",
             "absoluteTime": at(minutes=60).isoformat()},
            {"id": "dusk_naive", "type": "absolute",
             "absoluteTime": "2026-10-10T09:00:00"},
            {"id": "dusk_missing", "type": "absolute"},
        ]
        for variant in variants:
            with self.subTest(variant=variant["id"]):
                for path in self.tasks.glob("*.md"):
                    path.unlink()
                self.task()
                self.add_reminders("Задача.md",
                                   [task_metadata.ReminderEntry(variant)])
                before = (self.tasks / "Задача.md").read_text(encoding="utf-8")
                with self.assertRaises(reminders.ReminderError):
                    reminders.reconcile(self.root, self.store, now=NOW)
                self.assertEqual(
                    (self.tasks / "Задача.md").read_text(encoding="utf-8"),
                    before)

    def test_duplicate_id_across_files_fails_closed(self) -> None:
        self.task("A.md")
        self.task("B.md")
        self.add_reminders("A.md", [self.dusk("dusk_dup", at(minutes=60))])
        self.add_reminders("B.md", [self.dusk("dusk_dup", at(minutes=90))])
        with self.assertRaises(reminders.ReminderError):
            reminders.reconcile(self.root, self.store, now=NOW)
        self.assertIsNone(self.store.get_reminder("dusk_dup"))


class ReconcileTests(ReminderBase):
    def test_restores_from_markdown_after_db_loss(self) -> None:
        self.task()
        self.add_reminders("Задача.md", [self.dusk("dusk_a", at(minutes=30))])
        fresh = runtime_store.RuntimeStore(self.root / "loss" / "runtime.sqlite3")
        self.addCleanup(fresh.close)
        result = reminders.reconcile(self.root, fresh, now=NOW)
        self.assertEqual(len(result.active), 1)
        self.assertEqual(fresh.get_reminder("dusk_a").state,
                         runtime_store.REMINDER_ACTIVE)
        events = reminders.collect_due(fresh, now=at(minutes=31))
        self.assertEqual(len(events), 1)
        self.assertEqual(len(fresh.pending_reminder_events()), 1)

    def test_preserves_delivered_for_unchanged_time(self) -> None:
        self.task()
        self.add_reminders("Задача.md", [self.dusk("dusk_a", at(minutes=30))])
        reminders.reconcile(self.root, self.store, now=NOW)
        [event] = reminders.collect_due(self.store, now=at(minutes=31))
        claimed = self.store.claim_reminder_event(now=at(minutes=31))
        self.assertTrue(self.store.mark_reminder_event_delivered(
            claimed.event_id, claimed.attempts, now=at(minutes=31)))
        self.assertEqual(self.store.get_reminder("dusk_a").state,
                         runtime_store.REMINDER_DELIVERED)
        reminders.reconcile(self.root, self.store, now=at(minutes=32))
        self.assertEqual(self.store.get_reminder("dusk_a").state,
                         runtime_store.REMINDER_DELIVERED)

    def test_cancels_missing_and_done(self) -> None:
        self.task("A.md")
        self.task("B.md")
        self.add_reminders("A.md", [self.dusk("dusk_a", at(minutes=60))])
        self.add_reminders("B.md", [self.dusk("dusk_b", at(minutes=60))])
        reminders.reconcile(self.root, self.store, now=NOW)
        self.add_reminders("A.md", [])
        dispatch.set_task_status(self.tasks / "B.md", "done", now=NOW,
                                 vault=self.root)
        reminders.reconcile(self.root, self.store, now=at(minutes=1))
        self.assertEqual(self.store.get_reminder("dusk_a").state,
                         runtime_store.REMINDER_CANCELLED)
        self.assertEqual(self.store.get_reminder("dusk_b").state,
                         runtime_store.REMINDER_CANCELLED)


class CollectDueTests(ReminderBase):
    def test_collect_due_transitions_once(self) -> None:
        self.task()
        self.add_reminders("Задача.md", [self.dusk("dusk_a", at(minutes=30))])
        reminders.reconcile(self.root, self.store, now=NOW)
        self.assertEqual(reminders.collect_due(self.store, now=at(minutes=29)), ())
        first = reminders.collect_due(self.store, now=at(minutes=31))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].state, runtime_store.EVENT_PENDING)
        self.assertEqual(self.store.get_reminder("dusk_a").state,
                         runtime_store.REMINDER_DUE)
        self.assertEqual(reminders.collect_due(self.store, now=at(minutes=32)), ())
        self.assertEqual(len(self.store.pending_reminder_events()), 1)


class GlobalScanFailClosedTests(ReminderBase):
    def broken(self, name: str = "Broken.md") -> Path:
        self.task(name)
        self.add_reminders(name, [task_metadata.ReminderEntry({
            "id": "dusk_bad:id", "type": "absolute",
            "absoluteTime": at(minutes=60).isoformat()})])
        return self.tasks / name

    def test_create_blocked_by_malformed_other_card(self) -> None:
        target = self.task("Target.md")
        self.broken()
        before = target.read_text(encoding="utf-8")
        with self.assertRaises(reminders.ReminderError):
            reminders.create_reminder(self.root, "Target.md", at(minutes=120),
                                      self.store, now=NOW)
        self.assertEqual(target.read_text(encoding="utf-8"), before)
        self.assertEqual(self.store.pending_reminder_events(), ())

    def test_idempotent_replay_blocked_by_malformed_other_card(self) -> None:
        target = self.task("Target.md")
        reminders.create_reminder(self.root, "Target.md", at(minutes=120),
                                  self.store, idempotency_key="k", now=NOW)
        self.broken()
        before = target.read_text(encoding="utf-8")
        with self.assertRaises(reminders.ReminderError):
            reminders.create_reminder(self.root, "Target.md", at(minutes=120),
                                      self.store, idempotency_key="k",
                                      now=at(minutes=1))
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_snooze_and_cancel_blocked_by_malformed_other_card(self) -> None:
        self.task("Target.md")
        created = reminders.create_reminder(self.root, "Target.md",
                                            at(minutes=60), self.store, now=NOW)
        self.broken()
        rid = created.reminder.reminder_id
        with self.assertRaises(reminders.ReminderError):
            reminders.snooze_reminder(self.root, rid, at(minutes=120),
                                      self.store, now=at(minutes=1))
        with self.assertRaises(reminders.ReminderError):
            reminders.cancel_reminder(self.root, rid, self.store,
                                      now=at(minutes=1))

    def test_duplicate_ids_block_create_before_bytes_change(self) -> None:
        target = self.task("Target.md")
        self.task("A.md")
        self.task("B.md")
        self.add_reminders("A.md", [self.dusk("dusk_dup", at(minutes=60))])
        self.add_reminders("B.md", [self.dusk("dusk_dup", at(minutes=90))])
        before = target.read_text(encoding="utf-8")
        with self.assertRaises(reminders.ReminderError):
            reminders.create_reminder(self.root, "Target.md", at(minutes=120),
                                      self.store, now=NOW)
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_symlink_child_blocks_create(self) -> None:
        target = self.task("Target.md")
        outside = self.root / "outside.md"
        outside.write_text("---\ntitle: x\nreminders: []\n---\n",
                           encoding="utf-8")
        (self.tasks / "Link.md").symlink_to(outside)
        before = target.read_text(encoding="utf-8")
        with self.assertRaises(reminders.ReminderError):
            reminders.create_reminder(self.root, "Target.md", at(minutes=120),
                                      self.store, now=NOW)
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_generated_id_is_globally_unique(self) -> None:
        target = self.task("Target.md")
        self.add_reminders("Target.md", [self.dusk("dusk_collide", at(minutes=30))])
        with patch.object(reminders, "_new_reminder_id",
                          side_effect=["dusk_collide", "dusk_unique1"]):
            result = reminders.create_reminder(self.root, "Target.md",
                                               at(minutes=120), self.store,
                                               now=NOW)
        self.assertEqual(result.reminder.reminder_id, "dusk_unique1")
        self.assertEqual(len(self.stored("Target.md")), 2)


class CancelRecoveryTests(ReminderBase):
    def test_cancel_recovers_after_db_failure(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=60), self.store, now=NOW)
        rid = created.reminder.reminder_id
        with patch.object(self.store, "cancel_reminder",
                          side_effect=runtime_store.RuntimeStoreError("boom")):
            with self.assertRaises(runtime_store.RuntimeStoreError):
                reminders.cancel_reminder(self.root, rid, self.store,
                                          now=at(minutes=1))
        self.assertEqual(self.stored(), ())
        self.assertEqual(self.store.get_reminder(rid).state,
                         runtime_store.REMINDER_ACTIVE)
        retry = reminders.cancel_reminder(self.root, rid, self.store,
                                          now=at(minutes=2))
        self.assertTrue(retry.reused)
        self.assertTrue(retry.sync_required)
        self.assertEqual(self.store.get_reminder(rid).state,
                         runtime_store.REMINDER_CANCELLED)
        again = reminders.cancel_reminder(self.root, rid, self.store,
                                          now=at(minutes=3))
        self.assertTrue(again.reused)
        self.assertFalse(again.sync_required)

    def test_cancel_pending_event_is_cancelled(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=30), self.store, now=NOW)
        rid = created.reminder.reminder_id
        reminders.collect_due(self.store, now=at(minutes=31))
        claimed = self.store.claim_reminder_event(now=at(minutes=31))
        reminders.cancel_reminder(self.root, rid, self.store, now=at(minutes=32))
        self.assertFalse(self.store.mark_reminder_event_delivered(
            claimed.event_id, claimed.attempts, now=at(minutes=32)))
        self.assertEqual(self.store.get_reminder(rid).state,
                         runtime_store.REMINDER_CANCELLED)


class SnoozeOwnershipTests(ReminderBase):
    def test_key_belongs_to_other_reminder_rejected(self) -> None:
        self.task()
        first = reminders.create_reminder(self.root, "Задача.md",
                                          at(minutes=60), self.store, now=NOW)
        second = reminders.create_reminder(self.root, "Задача.md",
                                           at(minutes=90), self.store, now=NOW)
        reminders.snooze_reminder(self.root, first.reminder.reminder_id,
                                  at(minutes=120), self.store,
                                  idempotency_key="s", now=at(minutes=1))
        with self.assertRaises(reminders.ReminderError):
            reminders.snooze_reminder(self.root,
                                      second.reminder.reminder_id,
                                      at(minutes=150), self.store,
                                      idempotency_key="s", now=at(minutes=2))

    def test_ghost_snooze_rejected(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=60), self.store,
                                            idempotency_key="k", now=NOW)
        self.add_reminders("Задача.md", [])
        with self.assertRaises(reminders.ReminderError):
            reminders.snooze_reminder(self.root,
                                      created.reminder.reminder_id,
                                      at(minutes=120), self.store,
                                      idempotency_key="k", now=at(minutes=1))

    def test_ghost_create_rejected(self) -> None:
        self.task()
        reminders.create_reminder(self.root, "Задача.md", at(minutes=60),
                                  self.store, idempotency_key="k", now=NOW)
        self.add_reminders("Задача.md", [])
        with self.assertRaises(reminders.ReminderError):
            reminders.create_reminder(self.root, "Задача.md", at(minutes=60),
                                      self.store, idempotency_key="k",
                                      now=at(minutes=1))


class SafeDueTests(ReminderBase):
    def due_event(self, minutes: int = 30):
        self.task()
        reminders.create_reminder(self.root, "Задача.md", at(minutes=minutes),
                                  self.store, now=NOW)
        reminders.collect_due_safe(self.root, self.store, now=at(minutes=minutes + 1))
        return self.store.claim_reminder_event(now=at(minutes=minutes + 1))

    def test_validate_delivery_valid(self) -> None:
        event = self.due_event()
        check = reminders.validate_delivery(self.root, self.store, event,
                                            now=at(minutes=31))
        self.assertTrue(check.ok)
        self.assertEqual(check.reminder.reminder_id, event.reminder_id)

    def test_validate_after_done_is_no_delivery(self) -> None:
        event = self.due_event()
        dispatch.set_task_status(self.tasks / "Задача.md", "done",
                                 now=at(minutes=31), vault=self.root)
        check = reminders.validate_delivery(self.root, self.store, event,
                                            now=at(minutes=31))
        self.assertFalse(check.ok)
        self.assertEqual(check.reason, "missing_or_done")
        self.assertEqual(self.store.get_reminder(event.reminder_id).state,
                         runtime_store.REMINDER_CANCELLED)

    def test_validate_after_snooze_is_no_delivery(self) -> None:
        event = self.due_event()
        reminders.snooze_reminder(self.root, event.reminder_id,
                                  at(minutes=120), self.store,
                                  now=at(minutes=31))
        check = reminders.validate_delivery(self.root, self.store, event,
                                            now=at(minutes=31))
        self.assertFalse(check.ok)
        self.assertIn(check.reason, ("not_due", "moved"))

    def test_validate_after_delete_is_no_delivery(self) -> None:
        event = self.due_event()
        self.add_reminders("Задача.md", [])
        check = reminders.validate_delivery(self.root, self.store, event,
                                            now=at(minutes=31))
        self.assertFalse(check.ok)
        self.assertEqual(check.reason, "missing_or_done")

    def test_collect_due_safe_skips_done(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=30), self.store, now=NOW)
        dispatch.set_task_status(self.tasks / "Задача.md", "done",
                                 now=at(minutes=31), vault=self.root)
        events = reminders.collect_due_safe(self.root, self.store,
                                            now=at(minutes=31))
        self.assertEqual(events, ())
        self.assertEqual(self.store.get_reminder(created.reminder.reminder_id).state,
                         runtime_store.REMINDER_CANCELLED)


class DateModifiedTests(ReminderBase):
    def test_mutations_set_date_modified(self) -> None:
        self.task()
        reminders.create_reminder(self.root, "Задача.md", at(minutes=60),
                                  self.store, now=NOW)
        self.assertEqual(task_metadata.read_task(
            self.root, "Задача.md").date_modified, NOW.isoformat())
        rid = [r for r in self.stored() if r.is_dusk()][0].id
        reminders.snooze_reminder(self.root, rid, at(minutes=120), self.store,
                                  now=at(minutes=1))
        self.assertEqual(task_metadata.read_task(
            self.root, "Задача.md").date_modified, at(minutes=1).isoformat())
        reminders.cancel_reminder(self.root, rid, self.store, now=at(minutes=2))
        self.assertEqual(task_metadata.read_task(
            self.root, "Задача.md").date_modified, at(minutes=2).isoformat())

    def test_crlf_body_preserved(self) -> None:
        path = self.tasks / "Задача.md"
        body = "# Задача\r\n\r\nТело\r\n"
        path.write_text('---\ntitle: "Задача"\nstatus: next\n---\n' + body,
                        encoding="utf-8")
        reminders.create_reminder(self.root, "Задача.md", at(minutes=60),
                                  self.store, now=NOW)
        self.assertTrue(path.read_bytes().endswith(body.encode("utf-8")))


class RecoveryStateTests(ReminderBase):
    def test_create_recovery_preserves_delivered_generation(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=30), self.store,
                                            idempotency_key="K", now=NOW)
        rid = created.reminder.reminder_id
        reminders.snooze_reminder(self.root, rid, at(minutes=60), self.store,
                                  now=at(minutes=1))
        reminders.collect_due_safe(self.root, self.store, now=at(minutes=61))
        claimed = self.store.claim_reminder_event(now=at(minutes=61))
        self.assertTrue(self.store.mark_reminder_event_delivered(
            claimed.event_id, claimed.attempts, now=at(minutes=61)))
        before = self.store.get_reminder(rid)
        self.assertEqual(before.state, runtime_store.REMINDER_DELIVERED)
        self.assertEqual(before.generation, 2)
        with self.store.connection() as connection:
            connection.execute(
                "DELETE FROM reminder_idempotency WHERE key = ?",
                ("reminder:K",))
        replay = reminders.create_reminder(self.root, "Задача.md",
                                           at(minutes=120), self.store,
                                           idempotency_key="K", now=NOW)
        self.assertTrue(replay.reused)
        after = self.store.get_reminder(rid)
        self.assertEqual(after.state, runtime_store.REMINDER_DELIVERED)
        self.assertEqual(after.generation, 2)
        self.assertEqual(self.store.reminder_idempotency("reminder:K"), rid)
        self.assertEqual(self.store.pending_reminder_events(), ())

    def test_create_recovery_without_projection_creates_active_gen1(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=60), self.store,
                                            idempotency_key="K", now=NOW)
        rid = created.reminder.reminder_id
        with self.store.connection() as connection:
            connection.execute("DELETE FROM reminders WHERE reminder_id=?", (rid,))
            connection.execute(
                "DELETE FROM reminder_idempotency WHERE key=?", ("reminder:K",))
        replay = reminders.create_reminder(self.root, "Задача.md",
                                           at(minutes=120), self.store,
                                           idempotency_key="K", now=NOW)
        self.assertTrue(replay.reused)
        after = self.store.get_reminder(rid)
        self.assertEqual(after.state, runtime_store.REMINDER_ACTIVE)
        self.assertEqual(after.generation, 1)
        self.assertEqual(self.store.reminder_idempotency("reminder:K"), rid)

    def test_create_recovery_with_changed_markdown_time_bumps_generation(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=60), self.store,
                                            idempotency_key="K", now=NOW)
        rid = created.reminder.reminder_id
        entries = [entry.with_absolute(at(minutes=120).isoformat())
                   if entry.id == rid else entry for entry in self.stored()]
        self.add_reminders("Задача.md", entries)
        with self.store.connection() as connection:
            connection.execute(
                "DELETE FROM reminder_idempotency WHERE key=?", ("reminder:K",))
        replay = reminders.create_reminder(self.root, "Задача.md",
                                           at(minutes=150), self.store,
                                           idempotency_key="K", now=NOW)
        self.assertTrue(replay.reused)
        after = self.store.get_reminder(rid)
        self.assertEqual(after.state, runtime_store.REMINDER_ACTIVE)
        self.assertEqual(after.generation, 2)


class CancelledStaysCancelledTests(ReminderBase):
    def test_stale_same_stamp_markdown_does_not_resurrect(self) -> None:
        self.task()
        created = reminders.create_reminder(self.root, "Задача.md",
                                            at(minutes=30), self.store, now=NOW)
        rid = created.reminder.reminder_id
        path = self.tasks / "Задача.md"
        stale = path.read_bytes()
        reminders.cancel_reminder(self.root, rid, self.store, now=at(minutes=1))
        self.assertEqual(self.store.get_reminder(rid).state,
                         runtime_store.REMINDER_CANCELLED)
        path.write_bytes(stale)
        reminders.collect_due_safe(self.root, self.store, now=at(minutes=31))
        self.assertEqual(self.store.get_reminder(rid).state,
                         runtime_store.REMINDER_CANCELLED)
        self.assertEqual(self.store.pending_reminder_events(), ())


if __name__ == "__main__":
    unittest.main()
