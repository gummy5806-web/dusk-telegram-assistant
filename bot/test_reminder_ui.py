#!/usr/bin/env python3
"""Чистый reminder-UI: меню, custom-ввод, карточка, уведомление, действия."""

from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path

import reminder_ui
import reminders
import runtime_store
import task_metadata
import task_ui as ui

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def at(**delta) -> datetime.datetime:
    return NOW + datetime.timedelta(**delta)


class UIBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        self.store = runtime_store.RuntimeStore(
            self.root / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)
        self.state: dict = {}

    def task(self, name: str = "Задача.md", status: str = "next") -> Path:
        title = name[:-3] if name.endswith(".md") else name
        path = self.tasks / (title + ".md")
        path.write_text(
            "---\n" f'title: "{title}"\nstatus: {status}\n'
            "---\n" f"# {title}\n", encoding="utf-8")
        return path

    def token_for(self, path: Path) -> str:
        return ui.issue_token(self.state, str(path))

    def callbacks(self, view) -> list:
        return [data for row in view.rows for _label, data in row]

    def labels(self, view) -> list:
        return [label for row in view.rows for label, _data in row]

    def create_reminder(self, path, when, minutes=0):
        return reminders.create_reminder(self.root, path, when, self.store,
                                         now=NOW)


class MenuTests(UIBase):
    def test_menu_callbacks_bounded_and_opaque(self) -> None:
        token = self.token_for(self.task())
        action = reminder_ui.begin_menu(self.state, self.root, token)
        labels = self.labels(action.view)
        for expected in ("15 мин", "1 час", "3 часа", "Завтра 09:00",
                         "Другое время", "⬅️ Назад"):
            self.assertIn(expected, labels)
        for data in self.callbacks(action.view):
            self.assertLessEqual(len(data.encode("utf-8")), 64, data)
            self.assertTrue(data.startswith("r:"), data)
            self.assertNotIn("/", data)
            self.assertNotIn("Задача", data)

    def test_schedule_preset_creates_and_is_idempotent(self) -> None:
        path = self.task()
        menu = reminder_ui.begin_menu(self.state, self.root,
                                      self.token_for(path))
        start = next(data for data in self.callbacks(menu.view)
                     if data.endswith(":15"))
        menu_token = start.split(":")[2]
        first = reminder_ui.schedule_preset(self.state, self.root, self.store,
                                            menu_token, "15", now=NOW)
        self.assertTrue(first.sync_required)
        second = reminder_ui.schedule_preset(self.state, self.root, self.store,
                                             menu_token, "15", now=at(minutes=1))
        self.assertFalse(second.sync_required)
        entries = task_metadata.read_task(self.root, "Задача.md").reminders
        self.assertEqual(len([e for e in entries if e.is_dusk()]), 1)

    def test_custom_flow(self) -> None:
        path = self.task()
        menu = reminder_ui.begin_menu(self.state, self.root,
                                      self.token_for(path))
        other = next(data for data in self.callbacks(menu.view)
                     if data.startswith("r:o:"))
        reminder_ui.other_time(self.state, other.split(":")[2])
        self.assertTrue(reminder_ui.custom_pending(self.state))
        result = reminder_ui.submit_custom(self.state, self.root, self.store,
                                           "через 2 часа", now=NOW)
        self.assertTrue(result.sync_required)
        self.assertFalse(reminder_ui.custom_pending(self.state))
        entries = task_metadata.read_task(self.root, "Задача.md").reminders
        self.assertEqual(len([e for e in entries if e.is_dusk()]), 1)

    def test_custom_bad_time_keeps_mode(self) -> None:
        path = self.task()
        menu = reminder_ui.begin_menu(self.state, self.root,
                                      self.token_for(path))
        other = next(data for data in self.callbacks(menu.view)
                     if data.startswith("r:o:"))
        reminder_ui.other_time(self.state, other.split(":")[2])
        result = reminder_ui.submit_custom(self.state, self.root, self.store,
                                           "потом как-нибудь", now=NOW)
        self.assertIn("⚠️", result.view.text)
        self.assertTrue(reminder_ui.custom_pending(self.state))
        self.assertEqual(task_metadata.read_task(
            self.root, "Задача.md").reminders, ())


class CardTests(UIBase):
    def test_nearest_future_reminder_line(self) -> None:
        path = self.task()
        self.create_reminder("Задача.md", at(minutes=90))
        view = reminder_ui.card_reminder(self.state, self.root,
                                         self.token_for(path), now=NOW)
        self.assertIn("🔔 Напоминание:", view.text)
        self.assertIn("🔔 Напомнить", self.labels(view))
        for data in self.callbacks(view):
            self.assertLessEqual(len(data.encode("utf-8")), 64)

    def test_past_reminder_not_shown(self) -> None:
        path = self.task()
        self.create_reminder("Задача.md", at(minutes=30))
        view = reminder_ui.card_reminder(self.state, self.root,
                                         self.token_for(path),
                                         now=at(minutes=31))
        self.assertEqual(view.text, "")
        self.assertIn("🔔 Напомнить", self.labels(view))


class NotificationTests(UIBase):
    def claimed(self):
        self.task()
        created = self.create_reminder("Задача.md", at(minutes=30))
        self.store.collect_due(now=at(minutes=31))
        event = self.store.claim_reminder_event(now=at(minutes=31))
        reminder = reminders.find_reminder(self.root,
                                           created.reminder.reminder_id)
        return created.reminder.reminder_id, event, reminder

    def test_buttons_use_event_id_and_are_bounded(self) -> None:
        _rid, event, reminder = self.claimed()
        notification = reminder_ui.notification(
            reminder, event.event_id, "Задача", now=at(minutes=31))
        self.assertIn("⏰ Напоминание: Задача", notification.text)
        self.assertEqual(notification.event_id, event.event_id)
        labels = [label for row in notification.buttons for label, _ in row]
        for expected in ("✅ Готово", "+15 мин", "+1 час", "Убрать"):
            self.assertIn(expected, labels)
        data = [value for row in notification.buttons for _label, value in row]
        self.assertIn(f"r:g:{event.event_id}", data)
        for value in data:
            self.assertLessEqual(len(value.encode("utf-8")), 64, value)

    def test_snooze_then_old_callback_is_stale(self) -> None:
        rid, event, _reminder = self.claimed()
        first = reminder_ui.snooze_action(self.root, self.store,
                                          event.event_id, 15, now=at(minutes=31))
        self.assertTrue(first.sync_required)
        stale = reminder_ui.snooze_action(self.root, self.store,
                                          event.event_id, 15, now=at(minutes=32))
        self.assertTrue(stale.view.stale)
        moved = reminders.find_reminder(self.root, rid)
        self.assertEqual(moved.absolute_time[:16],
                         at(minutes=45).isoformat()[:16])

    def test_remove_then_old_done_cannot_close_task(self) -> None:
        rid, event, _reminder = self.claimed()
        removed = reminder_ui.remove_action(self.root, self.store,
                                            event.event_id, now=at(minutes=31))
        self.assertTrue(removed.sync_required)
        done = reminder_ui.done_action(self.root, self.store, event.event_id,
                                       now=at(minutes=32))
        self.assertTrue(done.view.stale)
        self.assertEqual(task_metadata.read_task(self.root, "Задача.md").status,
                         "next")
        self.assertEqual(self.store.get_reminder(rid).state,
                         runtime_store.REMINDER_CANCELLED)

    def test_done_normal_then_stale(self) -> None:
        rid, event, _reminder = self.claimed()
        done = reminder_ui.done_action(self.root, self.store, event.event_id,
                                       now=at(minutes=31))
        self.assertTrue(done.sync_required)
        self.assertEqual(task_metadata.read_task(self.root, "Задача.md").status,
                         "done")
        self.assertEqual(self.store.get_reminder(rid).state,
                         runtime_store.REMINDER_CANCELLED)
        again = reminder_ui.done_action(self.root, self.store, event.event_id,
                                        now=at(minutes=32))
        self.assertTrue(again.view.stale)

    def test_max_event_id_callback_length(self) -> None:
        data = [value for row in reminder_ui.reminder_buttons(2 ** 63 - 1)
                for _label, value in row]
        self.assertTrue(data)
        for value in data:
            self.assertLessEqual(len(value.encode("utf-8")), 64, value)
        self.assertEqual(reminder_ui.reminder_buttons(0), ())
        self.assertEqual(reminder_ui.reminder_buttons("bad"), ())
        self.assertEqual(reminder_ui.reminder_buttons(2 ** 63), ())

    def test_stale_actions_fail_closed(self) -> None:
        actions = [
            reminder_ui.done_action(self.root, self.store, 999999, now=NOW),
            reminder_ui.remove_action(self.root, self.store, 999999, now=NOW),
            reminder_ui.snooze_action(self.root, self.store, 999999, 15, now=NOW),
            reminder_ui.done_action(self.root, self.store, "bad", now=NOW),
        ]
        for action in actions:
            self.assertTrue(action.view.stale)


class ManualMarkdownMoveTests(UIBase):
    def claimed(self):
        self.task()
        created = self.create_reminder("Задача.md", at(minutes=30))
        self.store.collect_due(now=at(minutes=31))
        event = self.store.claim_reminder_event(now=at(minutes=31))
        return created.reminder.reminder_id, event

    def move_markdown(self, rid, value):
        entries = [entry.with_absolute(value) if entry.id == rid else entry
                   for entry in task_metadata.read_task(
                       self.root, "Задача.md").reminders]
        task_metadata.write_task(self.root, "Задача.md", reminders=entries)

    def test_manual_time_change_stales_all_callbacks(self) -> None:
        rid, event = self.claimed()
        self.move_markdown(rid, at(minutes=120).isoformat())
        path = self.tasks / "Задача.md"
        before_bytes = path.read_bytes()
        before = self.store.get_reminder(rid)
        actions = [
            reminder_ui.snooze_action(self.root, self.store, event.event_id, 15,
                                      now=at(minutes=31)),
            reminder_ui.done_action(self.root, self.store, event.event_id,
                                    now=at(minutes=31)),
            reminder_ui.remove_action(self.root, self.store, event.event_id,
                                      now=at(minutes=31)),
        ]
        for action in actions:
            self.assertTrue(action.view.stale)
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(task_metadata.read_task(self.root, "Задача.md").status,
                         "next")
        after = self.store.get_reminder(rid)
        self.assertEqual(
            (after.state, after.generation, after.absolute_time),
            (before.state, before.generation, before.absolute_time))

    def test_malformed_markdown_time_stales(self) -> None:
        rid, event = self.claimed()
        self.move_markdown(rid, "not-a-time")
        path = self.tasks / "Задача.md"
        before_bytes = path.read_bytes()
        action = reminder_ui.remove_action(self.root, self.store,
                                           event.event_id, now=at(minutes=31))
        self.assertTrue(action.view.stale)
        self.assertEqual(path.read_bytes(), before_bytes)

    def test_same_instant_different_offset_is_valid(self) -> None:
        rid, event = self.claimed()
        local = at(minutes=30).astimezone(reminder_ui.timezone()).isoformat()
        self.move_markdown(rid, local)
        result = reminder_ui.snooze_action(self.root, self.store,
                                           event.event_id, 15,
                                           now=at(minutes=31))
        self.assertFalse(result.view.stale)
        self.assertTrue(result.sync_required)


class NaturalActionTests(UIBase):
    def test_creates_one_task_and_reminder(self) -> None:
        import reminder_nlp
        intent = reminder_nlp.parse_reminder_intent(
            "напомни завтра в 9 отправить отчёт", now=NOW)
        result = reminder_ui.natural_action(self.state, self.root, self.store,
                                            intent, idempotency_key="tg:1",
                                            now=NOW)
        self.assertIn("⏰ Напомню", result.view.text)
        self.assertTrue(result.sync_required)
        tasks = list(self.tasks.glob("*.md"))
        self.assertEqual(len(tasks), 1)
        snapshot = task_metadata.read_task(self.root, tasks[0].name)
        self.assertEqual(len([e for e in snapshot.reminders if e.is_dusk()]), 1)

    def test_replay_is_idempotent(self) -> None:
        import reminder_nlp
        intent = reminder_nlp.parse_reminder_intent(
            "напомни завтра в 9 отправить отчёт", now=NOW)
        reminder_ui.natural_action(self.state, self.root, self.store, intent,
                                   idempotency_key="tg:1", now=NOW)
        reminder_ui.natural_action(self.state, self.root, self.store, intent,
                                   idempotency_key="tg:1", now=NOW)
        tasks = list(self.tasks.glob("*.md"))
        self.assertEqual(len(tasks), 1)
        snapshot = task_metadata.read_task(self.root, tasks[0].name)
        self.assertEqual(len([e for e in snapshot.reminders if e.is_dusk()]), 1)


if __name__ == "__main__":
    unittest.main()
