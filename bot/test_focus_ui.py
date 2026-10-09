#!/usr/bin/env python3
"""Чистый focus-UI: меню длительностей, активная карточка, expiry-действия.

Без Telegram, сети и git: временный vault, временная SQLite, обычный dict как
`context.user_data`. Проверяются форма callback_data, локализация времени и
идемпотентность действий.
"""

from __future__ import annotations

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import focus
import focus_ui
import runtime_store
import task_metadata
import task_ui as ui

UTC = datetime.timezone.utc
ALMATY = datetime.timezone(datetime.timedelta(hours=5), "Asia/Almaty")
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def iso(moment: datetime.datetime) -> str:
    return moment.astimezone(UTC).isoformat()


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
            "---\n" f"# {title}\n\nТело\n", encoding="utf-8")
        return path

    def token_for(self, path: Path) -> str:
        return ui.issue_token(self.state, str(path))

    def entries(self, name: str = "Задача.md"):
        return task_metadata.read_task(self.root, name).time_entries

    def callbacks(self, view) -> list:
        return [data for row in view.rows for _label, data in row]

    def labels(self, view) -> list:
        return [label for row in view.rows for label, _data in row]


class DoneStopsActiveTests(UIBase):
    def test_stop_for_done_stops_matching_task(self) -> None:
        path = self.task()
        focus.start_focus(self.root, "Задача.md", 25, self.store, now=NOW)
        stopped = focus_ui.stop_for_done(self.root, self.store, str(path))
        self.assertTrue(stopped)
        self.assertNotEqual(self.entries()[0].end_time, "")
        self.assertIsNone(self.store.get_active_focus())

    def test_stop_for_done_ignores_other_task(self) -> None:
        self.task()
        other = self.task("Другая.md")
        focus.start_focus(self.root, "Задача.md", 25, self.store, now=NOW)
        self.assertFalse(
            focus_ui.stop_for_done(self.root, self.store, str(other)))
        self.assertIsNotNone(self.store.get_active_focus())
        self.assertEqual(self.entries()[0].end_time, "")


class DurationMenuTests(UIBase):
    def test_menu_bounds_and_callbacks(self) -> None:
        token = self.token_for(self.task())
        action = focus_ui.begin_duration_menu(self.state, self.root, token, now=NOW)
        labels = self.labels(action.view)
        for _minutes, label in focus_ui.DURATION_LABELS:
            self.assertIn(label, labels)
        for data in self.callbacks(action.view):
            self.assertLessEqual(len(data.encode("utf-8")), 64, data)
            self.assertTrue(data.startswith("q:"), data)
            self.assertNotIn("/", data)
            self.assertNotIn("Задача", data)
        self.assertFalse(action.sync_required)

    def test_same_menu_token_starts_one_session(self) -> None:
        token = self.token_for(self.task())
        menu = focus_ui.begin_duration_menu(self.state, self.root, token, now=NOW)
        start = next(data for data in self.callbacks(menu.view)
                     if data.startswith("q:s:"))
        menu_token = start.split(":")[2]
        first = focus_ui.start_from_menu(self.state, self.root, self.store,
                                         menu_token, "25", now=NOW)
        second = focus_ui.start_from_menu(self.state, self.root, self.store,
                                          menu_token, "25", now=at(minutes=1))
        self.assertTrue(first.sync_required)
        self.assertFalse(second.sync_required)
        self.assertEqual(len(self.entries()), 1)

    def test_reopening_menu_allows_new_block(self) -> None:
        path = self.task()
        token = self.token_for(path)
        first_menu = focus_ui.begin_duration_menu(self.state, self.root, token, now=NOW)
        first_token = next(d for d in self.callbacks(first_menu.view)
                           if d.startswith("q:s:")).split(":")[2]
        focus_ui.start_from_menu(self.state, self.root, self.store,
                                 first_token, "25", now=NOW)
        session = self.store.get_active_focus().session_id
        focus_ui.stop_active(self.state, self.root, self.store, session,
                             now=at(minutes=5))
        second_menu = focus_ui.begin_duration_menu(self.state, self.root, token,
                                                   now=at(minutes=6))
        second_token = next(d for d in self.callbacks(second_menu.view)
                            if d.startswith("q:s:")).split(":")[2]
        self.assertNotEqual(first_token, second_token)
        focus_ui.start_from_menu(self.state, self.root, self.store,
                                 second_token, "25", now=at(minutes=6))
        self.assertEqual(len(self.entries()), 2)

    def test_stale_menu_token_fails_closed(self) -> None:
        action = focus_ui.start_from_menu(self.state, self.root, self.store,
                                          "deadbeef", "25", now=NOW)
        self.assertTrue(action.view.stale)
        self.assertIn("Задачи", action.view.text)


class ActiveCardTests(UIBase):
    def start(self, minutes: int = 25, moment=None):
        path = self.task()
        started = focus.start_focus(self.root, "Задача.md", minutes,
                                    self.store, now=moment or NOW)
        return path, self.token_for(path), started

    def test_card_shows_local_time_and_ceil_remaining(self) -> None:
        _path, token, _started = self.start(25)
        view = focus_ui.card_focus(self.state, self.root, self.store, token,
                                   now=at(minutes=5, seconds=30))
        self.assertIn("до 17:25", view.text)
        self.assertIn("осталось 20 мин", view.text)
        labels = self.labels(view)
        self.assertIn("⏹ Закончить", labels)
        self.assertIn("➕15", labels)

    def test_card_without_active_shows_focus_button(self) -> None:
        token = self.token_for(self.task())
        view = focus_ui.card_focus(self.state, self.root, self.store, token, now=NOW)
        self.assertEqual([data for _l, data in view.rows[0]], [f"q:d:{token}"])

    def test_timezone_env_override(self) -> None:
        _path, token, _started = self.start(25)
        with patch.dict(os.environ, {"DUSK_TIMEZONE": "Europe/Berlin"}):
            view = focus_ui.card_focus(self.state, self.root, self.store, token,
                                       now=NOW)
        self.assertIn("до 14:25", view.text)


class ExtendTests(UIBase):
    def test_double_tap_once_then_fresh_token_again(self) -> None:
        path = self.task()
        focus.start_focus(self.root, "Задача.md", 25, self.store, now=NOW)
        token = self.token_for(path)
        view = focus_ui.card_focus(self.state, self.root, self.store, token,
                                   now=at(minutes=1))
        ext = next(d for d in self.callbacks(view) if d.startswith("q:e:"))
        ext_token = ext.split(":")[2]
        first = focus_ui.extend_from_token(self.state, self.root, self.store,
                                           ext_token, now=at(minutes=1))
        second = focus_ui.extend_from_token(self.state, self.root, self.store,
                                            ext_token, now=at(minutes=2))
        self.assertTrue(first.sync_required)
        self.assertFalse(second.sync_required)
        self.assertEqual(self.entries()[0].planned_minutes, 40)

        view2 = focus_ui.card_focus(self.state, self.root, self.store, token,
                                    now=at(minutes=3))
        ext2 = next(d for d in self.callbacks(view2) if d.startswith("q:e:"))
        self.assertNotEqual(ext, ext2)
        focus_ui.extend_from_token(self.state, self.root, self.store,
                                   ext2.split(":")[2], now=at(minutes=3))
        self.assertEqual(self.entries()[0].planned_minutes, 55)


class StopTests(UIBase):
    def test_stop_idempotent(self) -> None:
        path = self.task()
        focus.start_focus(self.root, "Задача.md", 25, self.store, now=NOW)
        view = focus_ui.card_focus(self.state, self.root, self.store,
                                   self.token_for(path), now=at(minutes=1))
        session = next(d for d in self.callbacks(view)
                       if d.startswith("q:x:")).split(":")[2]
        first = focus_ui.stop_active(self.state, self.root, self.store, session,
                                     now=at(minutes=5))
        self.assertTrue(first.sync_required)
        before = (self.tasks / "Задача.md").read_text(encoding="utf-8")
        second = focus_ui.stop_active(self.state, self.root, self.store, session,
                                      now=at(minutes=6))
        self.assertFalse(second.sync_required)
        self.assertEqual((self.tasks / "Задача.md").read_text(encoding="utf-8"),
                         before)


class StaleTests(UIBase):
    def test_foreign_tokens_fail_closed(self) -> None:
        actions = [
            focus_ui.stop_active(self.state, self.root, self.store, "deadbeef", now=NOW),
            focus_ui.extend_from_token(self.state, self.root, self.store, "deadbeef", now=NOW),
            focus_ui.done_from_expiry(self.state, self.root, self.store, "deadbeef", now=NOW),
        ]
        for action in actions:
            self.assertTrue(action.view.stale)
            self.assertIn("Задачи", action.view.text)


class ExpiryActionTests(UIBase):
    def expired(self):
        path = self.task()
        started = focus.start_focus(self.root, "Задача.md", 15, self.store, now=NOW)
        focus.expire_due(self.root, self.store, now=at(minutes=20))
        return path, started.session.session_id

    def test_expiry_buttons(self) -> None:
        rows = focus_ui.expiry_buttons("abc123")
        labels = [label for row in rows for label, _data in row]
        self.assertIn("✅ Задача готова", labels)
        self.assertIn("➕ Ещё 15", labels)
        self.assertIn("⏹ Закончил блок", labels)
        for row in rows:
            for _label, data in row:
                self.assertLessEqual(len(data.encode("utf-8")), 64)
                self.assertTrue(data.startswith("q:"))

    def test_done_from_expiry_sets_done_once(self) -> None:
        _path, session = self.expired()
        first = focus_ui.done_from_expiry(self.state, self.root, self.store,
                                          session, now=at(minutes=21))
        self.assertEqual(task_metadata.read_task(self.root, "Задача.md").status,
                         "done")
        self.assertTrue(first.sync_required)
        again = focus_ui.done_from_expiry(self.state, self.root, self.store,
                                          session, now=at(minutes=22))
        self.assertFalse(again.sync_required)

    def test_extend_from_expiry_starts_one_new_block(self) -> None:
        _path, session = self.expired()
        data = f"q:E:{session}"
        first = focus_ui.extend_from_expiry(self.state, self.root, self.store,
                                            session, data, now=at(minutes=21))
        self.assertTrue(first.sync_required)
        self.assertEqual(self.store.get_active_focus().planned_minutes, 15)
        second = focus_ui.extend_from_expiry(self.state, self.root, self.store,
                                             session, data, now=at(minutes=22))
        self.assertFalse(second.sync_required)
        self.assertEqual(self.store.get_active_focus().planned_minutes, 15)
        self.assertEqual(len(self.entries()), 2)

    def test_dismiss_expiry_does_not_write(self) -> None:
        _path, session = self.expired()
        before = (self.tasks / "Задача.md").read_text(encoding="utf-8")
        action = focus_ui.dismiss_expiry(self.state, self.root, self.store,
                                         session, now=at(minutes=21))
        self.assertFalse(action.sync_required)
        self.assertEqual((self.tasks / "Задача.md").read_text(encoding="utf-8"),
                         before)


class CallbackSafetyTests(UIBase):
    def test_expiry_buttons_reject_unsafe_session_id(self) -> None:
        for bad in ("bad:id", "a" * 49, "сессия", "", "has space"):
            with self.subTest(bad=bad):
                self.assertEqual(focus_ui.expiry_buttons(bad), ())

    def test_expiry_buttons_max_bytes_with_max_id(self) -> None:
        rows = list(focus_ui.expiry_buttons("a" * 48))
        self.assertTrue(rows)
        for row in rows:
            for _label, data in row:
                self.assertLessEqual(len(data.encode("utf-8")), 64, data)
                self.assertTrue(data.startswith("q:"))

    def test_active_view_omits_invalid_session_callback(self) -> None:
        path = self.task()
        self.store.activate_focus(runtime_store.FocusRecord(
            session_id="bad:id", task_path=str(path.relative_to(self.root)),
            status=runtime_store.FOCUS_ACTIVE, started_at=iso(NOW),
            planned_end=iso(at(minutes=25)), planned_minutes=25), now=NOW)
        view = focus_ui.card_focus(self.state, self.root, self.store,
                                   self.token_for(path), now=NOW)
        self.assertNotIn("⏹ Закончить", self.labels(view))
        for data in self.callbacks(view):
            self.assertLessEqual(len(data.encode("utf-8")), 64, data)
            self.assertNotIn("bad:id", data)

    def test_active_view_max_bytes_with_max_id(self) -> None:
        path = self.task()
        self.store.activate_focus(runtime_store.FocusRecord(
            session_id="a" * 48, task_path=str(path.relative_to(self.root)),
            status=runtime_store.FOCUS_ACTIVE, started_at=iso(NOW),
            planned_end=iso(at(minutes=25)), planned_minutes=25), now=NOW)
        view = focus_ui.card_focus(self.state, self.root, self.store,
                                   self.token_for(path), now=NOW)
        self.assertIn("⏹ Закончить", self.labels(view))
        for data in self.callbacks(view):
            self.assertLessEqual(len(data.encode("utf-8")), 64, data)


class DoneFailClosedTests(UIBase):
    def active_db_only(self):
        """Активный focus только в SQLite: Markdown status остаётся `next`."""
        path = self.task()
        self.store.activate_focus(runtime_store.FocusRecord(
            session_id="s1", task_path=str(path.relative_to(self.root)),
            status=runtime_store.FOCUS_ACTIVE, started_at=iso(NOW),
            planned_end=iso(at(minutes=25)), planned_minutes=25), now=NOW)
        return path, self.token_for(path)

    def active_markdown(self):
        path = self.task()
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=NOW)
        return path, started.session.session_id, self.token_for(path)

    def test_stop_for_done_false_without_active(self) -> None:
        path = self.task()
        self.assertFalse(focus_ui.stop_for_done(self.root, self.store, str(path)))

    def test_stop_for_done_raises_on_store_error(self) -> None:
        path, _token = self.active_db_only()
        before = path.read_text(encoding="utf-8")
        with patch.object(self.store, "get_active_focus",
                          side_effect=runtime_store.RuntimeStoreError("boom")):
            with self.assertRaises(runtime_store.RuntimeStoreError):
                focus_ui.stop_for_done(self.root, self.store, str(path))
        self.assertEqual(task_metadata.read_task(self.root, "Задача.md").status,
                         "next")
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_status_done_aborts_on_store_error(self) -> None:
        path, token = self.active_db_only()
        before = path.read_text(encoding="utf-8")
        with patch.object(self.store, "get_active_focus",
                          side_effect=runtime_store.RuntimeStoreError("boom")):
            with self.assertRaises(runtime_store.RuntimeStoreError):
                focus_ui.status_with_focus(
                    self.state, self.root, self.store, token, "done", now=NOW)
        self.assertEqual(task_metadata.read_task(self.root, "Задача.md").status,
                         "next")
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertIsNotNone(self.store.get_active_focus())

    def test_status_done_aborts_on_focus_error(self) -> None:
        path, token = self.active_db_only()
        before = path.read_text(encoding="utf-8")
        with patch.object(focus, "stop_focus",
                          side_effect=focus.FocusError("boom")):
            with self.assertRaises(focus.FocusError):
                focus_ui.status_with_focus(
                    self.state, self.root, self.store, token, "done", now=NOW)
        self.assertEqual(task_metadata.read_task(self.root, "Задача.md").status,
                         "next")
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertIsNotNone(self.store.get_active_focus())

    def test_status_done_stops_then_sets_done(self) -> None:
        _path, _session, token = self.active_markdown()
        outcome = focus_ui.status_with_focus(
            self.state, self.root, self.store, token, "done", now=at(minutes=5))
        self.assertTrue(outcome.stopped)
        self.assertTrue(outcome.changed)
        self.assertTrue(outcome.sync_required)
        self.assertEqual(task_metadata.read_task(self.root, "Задача.md").status,
                         "done")
        self.assertIsNone(self.store.get_active_focus())

    def test_status_non_done_keeps_focus(self) -> None:
        _path, _session, token = self.active_markdown()
        outcome = focus_ui.status_with_focus(
            self.state, self.root, self.store, token, "next", now=at(minutes=1))
        self.assertFalse(outcome.stopped)
        self.assertEqual(self.entries()[0].end_time, "")
        self.assertIsNotNone(self.store.get_active_focus())

    def test_done_from_expiry_aborts_on_stop_error(self) -> None:
        self.task()
        started = focus.start_focus(self.root, "Задача.md", 15, self.store, now=NOW)
        focus.expire_due(self.root, self.store, now=at(minutes=20))
        before = task_metadata.read_task(self.root, "Задача.md").status
        with patch.object(self.store, "get_active_focus",
                          side_effect=runtime_store.RuntimeStoreError("boom")):
            action = focus_ui.done_from_expiry(
                self.state, self.root, self.store, started.session.session_id,
                now=at(minutes=21))
        self.assertFalse(action.sync_required)
        self.assertEqual(task_metadata.read_task(self.root, "Задача.md").status,
                         before)
        self.assertNotEqual(before, "done")

    def test_done_from_expiry_syncs_when_only_stop_changed(self) -> None:
        path = self.task()
        started = focus.start_focus(self.root, "Задача.md", 25, self.store, now=NOW)
        import dispatch
        dispatch.set_task_status(path, "done", now=NOW, vault=self.root)
        action = focus_ui.done_from_expiry(
            self.state, self.root, self.store, started.session.session_id,
            now=at(minutes=1))
        self.assertTrue(action.sync_required)
        self.assertNotEqual(self.entries()[0].end_time, "")
        self.assertIsNone(self.store.get_active_focus())


class RemainingMinutesTests(unittest.TestCase):
    def test_ceil_and_floor(self) -> None:
        deadline = iso(at(minutes=25))
        self.assertEqual(focus_ui.remaining_minutes(deadline, now=NOW), 25)
        self.assertEqual(
            focus_ui.remaining_minutes(deadline, now=at(minutes=24, seconds=1)), 1)
        self.assertEqual(
            focus_ui.remaining_minutes(deadline, now=at(minutes=25, seconds=1)), 0)
        self.assertEqual(
            focus_ui.remaining_minutes(deadline, now=at(minutes=40)), 0)


if __name__ == "__main__":
    unittest.main()
