#!/usr/bin/env python3
"""Детерминированная личная сводка: агрегаты, границы, изоляция ошибок."""

from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import inbox_core
import personal_summary
import runtime_store
import task_metadata
import thought_feed

ALMATY = ZoneInfo("Asia/Almaty")
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=ALMATY)   # пятница
TODAY = datetime.date(2026, 10, 9)
WEEK_START = datetime.date(2026, 10, 5)


def at(**delta):
    return NOW + datetime.timedelta(**delta)


class SummaryBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        (self.root / "ZETA" / "FLEETING").mkdir(parents=True)
        (self.root / "ZETA" / "FLEETING_DAILY").mkdir(parents=True)

    def make_task(self, name, status="next", due="", scheduled="",
                  completed="", entries=None, reminders_=None) -> Path:
        lines = ["---", f'title: "{name[:-3]}"', f"status: {status}"]
        if scheduled:
            lines.append(f"scheduled: {scheduled}")
        if due:
            lines.append(f"due: {due}")
        if completed:
            lines.append(f"completedDate: {completed}")
        lines += ["---", f"# {name[:-3]}", ""]
        path = self.tasks / name
        path.write_text("\n".join(lines), encoding="utf-8")
        if entries is not None or reminders_ is not None:
            task_metadata.write_task(self.root, name, time_entries=entries,
                                     reminders=reminders_)
        return path

    def focus_entry(self, sid, start, end=None, planned=None, minutes=None):
        data = {"startTime": start.isoformat(), "duskSessionId": sid,
                "description": "Dusk focus", "source": task_metadata.DUSK_SOURCE}
        if planned is not None:
            data["plannedEnd"] = planned.isoformat()
        if minutes is not None:
            data["plannedMinutes"] = minutes
        if end is not None:
            data["endTime"] = end.isoformat()
        return task_metadata.TimeEntry(data)

    def reminder_entry(self, rid, when):
        return task_metadata.ReminderEntry({
            "id": rid, "type": "absolute", "description": "Dusk reminder",
            "absoluteTime": when.isoformat()})

    def fleeting(self, name, days_ago):
        date = (TODAY - datetime.timedelta(days=days_ago)).isoformat()
        path = self.root / "ZETA" / "FLEETING_DAILY" / f"{date} — {name}.md"
        path.write_text(
            f'---\ntype: fleeting_note\ncreated: {date} 09:30\n---\n'
            f"# {name}\n\nтело\n", encoding="utf-8")
        return path


class EmptyTests(SummaryBase):
    def test_empty_vault_is_zero_and_clean(self) -> None:
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(summary.errors, ())
        self.assertEqual(summary.tasks.open, 0)
        self.assertEqual(summary.focus.today_sessions, 0)
        self.assertEqual(summary.reminders.active, 0)
        self.assertEqual(summary.inbox.total, 0)
        self.assertEqual(summary.knowledge.active, 0)
        text = personal_summary.render(summary)
        self.assertIn("📊 Сводка", text)
        self.assertLessEqual(len(text), personal_summary.MAX_RENDER_CHARS)


class InboxTests(SummaryBase):
    def test_total_counts_all_known_sections_and_thoughts_separately(self) -> None:
        path = self.root / "ZETA" / "FLEETING" / "00 Инбокс.md"
        path.write_text(inbox_core.DEFAULT_BODY, encoding="utf-8")
        inbox_core.append_line(path, "мысль", "thought", moment=NOW)
        inbox_core.append_line(path, "задача", "task", moment=NOW)
        inbox_core.append_line(path, "материал", "link", moment=NOW)

        summary = personal_summary.collect(self.root, store=None, now=NOW)

        self.assertEqual(summary.inbox.total, 3)
        self.assertEqual(summary.inbox.thoughts, 1)
        self.assertEqual(len(thought_feed.inbox_thoughts(self.root, limit=50)), 1)
        self.assertIn("необработанных 3, из них мыслей 1",
                      personal_summary.render(summary))

    def test_missing_is_zero_but_unreadable_is_unavailable(self) -> None:
        missing = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(missing.inbox.total, 0)

        path = self.root / "ZETA" / "FLEETING" / "00 Инбокс.md"
        path.write_bytes(b"\xff\xfe")
        broken = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertIsNone(broken.inbox)
        self.assertIn(personal_summary.SECTION_INBOX, broken.errors)

    def test_oversized_and_symlink_inbox_are_unavailable(self) -> None:
        path = self.root / "ZETA" / "FLEETING" / "00 Инбокс.md"
        path.write_bytes(b"x" * (thought_feed.MAX_FILE_BYTES + 1))
        oversized = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertIsNone(oversized.inbox)

        path.unlink()
        outside = self.root.parent / (self.root.name + "-outside-inbox.md")
        outside.write_text(inbox_core.DEFAULT_BODY, encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        path.symlink_to(outside)
        escaped = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertIsNone(escaped.inbox)


class TaskTests(SummaryBase):
    def test_open_overdue_closed_week(self) -> None:
        self.make_task("Открытая.md")
        self.make_task("Просроченная.md", due="2026-10-01")
        self.make_task("Закрытая.md", status="done",
                       completed="2026-10-07T10:00:00+05:00")
        self.make_task("Старая.md", status="done",
                       completed="2026-09-01T10:00:00+05:00")
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(summary.tasks.open, 2)
        self.assertEqual(summary.tasks.overdue, 1)
        self.assertEqual(summary.tasks.closed_week, 1)

    def test_future_completion_not_counted(self) -> None:
        self.make_task("Будущая.md", status="done",
                       completed="2026-10-20T10:00:00+05:00")
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(summary.tasks.closed_week, 0)


class FocusTests(SummaryBase):
    def test_today_week_and_active(self) -> None:
        self.make_task("Ф.md", entries=[
            self.focus_entry("dusk_a", at(hours=-2), end=at(hours=-1, minutes=-35),
                             minutes=25),
            self.focus_entry("dusk_b", at(minutes=-10), planned=at(minutes=15),
                             minutes=25),
        ])
        self.make_task("Прошлая.md", entries=[
            self.focus_entry("dusk_c",
                             datetime.datetime(2026, 10, 1, 10, 0, tzinfo=ALMATY),
                             end=datetime.datetime(2026, 10, 1, 10, 30, tzinfo=ALMATY),
                             minutes=30)])
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(summary.focus.today_sessions, 2)
        self.assertEqual(summary.focus.today_planned_minutes, 50)
        self.assertEqual(summary.focus.today_actual_minutes, 25)
        self.assertEqual(summary.focus.week_sessions, 2)
        self.assertEqual(summary.focus.week_actual_minutes, 25)
        self.assertEqual(summary.focus.active_task, "Ф")
        self.assertFalse(summary.focus.active_overdue)
        self.assertEqual(summary.focus.active_minutes_left, 15)

    def test_ignores_native_invalid_and_broken(self) -> None:
        native = task_metadata.TimeEntry({
            "startTime": at(hours=-1).isoformat(),
            "endTime": at(minutes=-30).isoformat()})
        self.make_task("Микс.md", entries=[
            native,
            self.focus_entry("bad:id", at(hours=-2), end=at(hours=-1)),
            self.focus_entry("dusk_x", at(hours=-3),
                             end=at(hours=-4), minutes=10),
        ])
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(summary.focus.today_sessions, 0)

    def test_week_boundary(self) -> None:
        self.make_task("Пн.md", entries=[self.focus_entry(
            "dusk_mon", datetime.datetime(2026, 10, 5, 9, 0, tzinfo=ALMATY),
            end=datetime.datetime(2026, 10, 5, 9, 20, tzinfo=ALMATY))])
        self.make_task("Вс.md", entries=[self.focus_entry(
            "dusk_sun", datetime.datetime(2026, 10, 4, 9, 0, tzinfo=ALMATY),
            end=datetime.datetime(2026, 10, 4, 9, 20, tzinfo=ALMATY))])
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(summary.focus.week_sessions, 1)

    def test_multiple_open_sessions_fail_closed(self) -> None:
        self.make_task("Одна.md", entries=[
            self.focus_entry("dusk_one", at(minutes=-10),
                             planned=at(minutes=15), minutes=25)])
        self.make_task("Две.md", entries=[
            self.focus_entry("dusk_two", at(minutes=-5),
                             planned=at(minutes=20), minutes=25)])

        summary = personal_summary.collect(self.root, store=None, now=NOW)

        self.assertIsNone(summary.focus)
        self.assertIn(personal_summary.SECTION_FOCUS, summary.errors)
        self.assertNotIn("Активный фокус", personal_summary.render(summary))

    def test_open_without_valid_deadline_fails_closed(self) -> None:
        self.make_task("Без дедлайна.md", entries=[
            self.focus_entry("dusk_missing", at(minutes=-5), minutes=25)])

        summary = personal_summary.collect(self.root, store=None, now=NOW)

        self.assertIsNone(summary.focus)
        self.assertIn(personal_summary.SECTION_FOCUS, summary.errors)

    def test_duplicate_open_session_id_fails_closed(self) -> None:
        for name in ("Одна.md", "Две.md"):
            self.make_task(name, entries=[
                self.focus_entry("dusk_same", at(minutes=-5),
                                 planned=at(minutes=20), minutes=25)])

        summary = personal_summary.collect(self.root, store=None, now=NOW)

        self.assertIsNone(summary.focus)

    def test_malformed_card_with_focus_marker_fails_closed(self) -> None:
        (self.tasks / "Бинарная.md").write_bytes(
            b"duskSessionId: dusk_bad\n\xff\xfe")

        summary = personal_summary.collect(self.root, store=None, now=NOW)

        self.assertIsNone(summary.focus)
        self.assertIn(personal_summary.SECTION_FOCUS, summary.errors)


class ReminderTests(SummaryBase):
    def test_active_and_next(self) -> None:
        self.make_task("Р.md", reminders_=[
            self.reminder_entry("dusk_r1", at(minutes=30)),
            self.reminder_entry("dusk_r2", at(minutes=10)),
            self.reminder_entry("dusk_r3", at(minutes=-10)),
            task_metadata.ReminderEntry({"id": "native-1",
                                         "type": "relative"}),
        ])
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(summary.reminders.active, 2)
        self.assertEqual(summary.reminders.next_title, "Р")
        self.assertTrue(summary.reminders.next_local)

    def test_done_task_reminders_ignored(self) -> None:
        self.make_task("Готово.md", status="done", reminders_=[
            self.reminder_entry("dusk_r", at(minutes=30))])
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(summary.reminders.active, 0)

    def test_malformed_dusk_reminder_fails_closed(self) -> None:
        self.make_task("Битое.md", reminders_=[task_metadata.ReminderEntry({
            "id": "dusk_bad", "type": "absolute",
            "description": "Dusk reminder"})])

        summary = personal_summary.collect(self.root, store=None, now=NOW)

        self.assertIsNone(summary.reminders)
        self.assertIn(personal_summary.SECTION_REMINDERS, summary.errors)

    def test_duplicate_reminder_id_fails_closed(self) -> None:
        reminder = self.reminder_entry("dusk_duplicate", at(minutes=30))
        self.make_task("Одна.md", reminders_=[reminder])
        self.make_task("Две.md", reminders_=[reminder])

        summary = personal_summary.collect(self.root, store=None, now=NOW)

        self.assertIsNone(summary.reminders)

    def test_malformed_card_with_reminder_marker_fails_closed(self) -> None:
        (self.tasks / "Бинарная.md").write_bytes(b"dusk_bad\n\xff\xfe")

        summary = personal_summary.collect(self.root, store=None, now=NOW)

        self.assertIsNone(summary.reminders)
        self.assertIn(personal_summary.SECTION_REMINDERS, summary.errors)


class KnowledgeTests(SummaryBase):
    def test_application_cycle(self) -> None:
        path = self.tasks / "Применение.md"
        path.write_text(
            '---\ntitle: "Применить"\nstatus: done\nduskApply: 2b\n'
            'completedDate: 2026-10-07T10:00:00+05:00\n---\n'
            "# Применить\n\n## Критерий наблюдения (гипотеза)\n\nпроверить\n\n"
            "## Что попробовал\n\nсделал\n\n## Что наблюдал\n\nстало лучше\n\n"
            "## Мой вывод\n\nработает\n", encoding="utf-8")
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(summary.knowledge.done_week, 1)
        self.assertEqual(summary.knowledge.observed, 1)
        self.assertTrue(summary.knowledge.chain)
        text = personal_summary.render(summary)
        self.assertIn("источник → применение → результат", text)


class IsolationTests(SummaryBase):
    def test_missing_store_does_not_break(self) -> None:
        self.make_task("Задача.md")
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertIsNotNone(summary.tasks)
        self.assertEqual(summary.errors, ())

    def test_corrupt_store_isolated(self) -> None:
        class BrokenStore:
            def is_hidden(self, *args, **kwargs):
                raise runtime_store.RuntimeStoreError("boom")

            def last_surface(self, *args, **kwargs):
                raise runtime_store.RuntimeStoreError("boom")

        self.fleeting("Старая", 30)
        self.make_task("Задача.md")
        summary = personal_summary.collect(self.root, store=BrokenStore(),
                                           now=NOW)
        self.assertEqual(summary.thoughts.forgotten_state, "unavailable")
        self.assertIsNotNone(summary.tasks)

    def test_missing_store_never_reveals_forgotten_title(self) -> None:
        self.fleeting("Скрытая", 30)

        summary = personal_summary.collect(self.root, store=None, now=NOW)

        self.assertEqual(summary.thoughts.forgotten_state, "unavailable")
        self.assertEqual(summary.thoughts.forgotten_title, "")
        self.assertNotIn("Скрытая", personal_summary.render(summary))

    def test_symlink_card_skipped(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("---\ntitle: x\nstatus: next\n---\n# x\n",
                           encoding="utf-8")
        (self.tasks / "Ссылка.md").symlink_to(outside)
        self.make_task("Норма.md")
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertEqual(summary.tasks.open, 1)

    def test_deterministic_render(self) -> None:
        self.make_task("Задача.md")
        self.fleeting("Недавняя", 1)
        first = personal_summary.render(
            personal_summary.collect(self.root, store=None, now=NOW))
        second = personal_summary.render(
            personal_summary.collect(self.root, store=None, now=NOW))
        self.assertEqual(first, second)

    def test_render_bounded(self) -> None:
        for index in range(120):
            self.make_task(f"Задача {index}.md")
        summary = personal_summary.collect(self.root, store=None, now=NOW)
        self.assertLessEqual(len(personal_summary.render(summary)),
                             personal_summary.MAX_RENDER_CHARS)

    def test_task_scan_over_limit_fails_closed(self) -> None:
        for index in range(personal_summary.SCAN_LIMIT + 1):
            (self.tasks / f"{index:04d}.md").write_text(
                f'---\ntitle: "{index}"\nstatus: next\n---\n# {index}\n',
                encoding="utf-8")

        summary = personal_summary.collect(self.root, store=None, now=NOW)

        self.assertIsNone(summary.tasks)
        self.assertIsNone(summary.focus)
        self.assertIsNone(summary.reminders)


class WeeklyReviewSectionTests(SummaryBase):
    def test_generated_text_has_personal_cycle(self) -> None:
        import weekly_review
        self.make_task("Задача.md")
        text = weekly_review.generated_text(self.root, day=TODAY)
        self.assertIn("## 🧭 Личный цикл (факты)", text)
        self.assertIn("• Задачи", text)
        self.assertIn("## ✅ Обязательства недели", text)
        self.assertIn("## 💰 Рынок", text)

    def test_user_zone_preserved(self) -> None:
        import weekly_review
        page = weekly_review.build_page(self.root, day=TODAY)
        self.assertIn(weekly_review.USER_START, page)
        self.assertIn(weekly_review.USER_END, page)
        self.assertIn("**Фокус:**", page)

    def test_review_without_runtime_store_does_not_reveal_old_thought(self) -> None:
        import weekly_review
        self.fleeting("Скрытая", 30)

        text = weekly_review.generated_text(self.root, day=TODAY)

        self.assertNotIn("Скрытая", text)
        self.assertIn("забытые — не удалось прочитать", text)


if __name__ == "__main__":
    unittest.main()
