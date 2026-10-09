#!/usr/bin/env python3
"""Недельный учёт: обе границы периода, сохранённый план, честные пропуски.

Изолированно: временный валт, даты задаются явно, время берётся из Asia/Almaty.
"""

from __future__ import annotations

import datetime
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import status
import weekly_plan

TASK = """---
title: "{title}"
status: {status}
priority: normal
scheduled: {scheduled}
{extra}tags:
  - task
---

# {title}
"""


def write_task(vault: Path, title: str, status: str = "open",
               scheduled: str = "", extra: str = "") -> Path:
    tasks_dir = vault / "TaskNotes" / "Tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / f"{title}.md"
    path.write_text(TASK.format(title=title, status=status,
                                scheduled=scheduled or "2026-01-01", extra=extra), encoding="utf-8")
    return path


class WeekBoundsTests(unittest.TestCase):
    def test_monday_to_sunday(self) -> None:
        # 7 октября 2026 — среда
        self.assertEqual(weekly_plan.week_bounds(datetime.date(2026, 10, 7)),
                         ("2026-10-05", "2026-10-11"))
        # понедельник и воскресенье принадлежат той же неделе
        self.assertEqual(weekly_plan.week_bounds(datetime.date(2026, 10, 5)),
                         ("2026-10-05", "2026-10-11"))
        self.assertEqual(weekly_plan.week_bounds(datetime.date(2026, 10, 11)),
                         ("2026-10-05", "2026-10-11"))
        self.assertEqual(weekly_plan.week_bounds(datetime.date(2026, 10, 12)),
                         ("2026-10-12", "2026-10-18"))

    def test_timezone_is_almaty(self) -> None:
        # 20:00 UTC — в Алматы уже следующие сутки
        moment = datetime.datetime(2026, 10, 5, 20, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(weekly_plan.today_local(moment).isoformat(), "2026-10-06")
        # 18:00 UTC — ещё тот же день
        moment = datetime.datetime(2026, 10, 5, 18, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(weekly_plan.today_local(moment).isoformat(), "2026-10-05")


class PlanStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)

    def test_without_plan_metric_is_not_fixed_and_creates_nothing(self) -> None:
        result = weekly_plan.metric(self.vault)
        self.assertFalse(result["fixed"])
        self.assertEqual(result["reason"], "план не зафиксирован")
        self.assertIsNone(result["percent"], "процент не выдумываем")
        self.assertFalse(weekly_plan.plan_path(self.vault).exists(),
                         "read-only расчёт не создаёт план")

    def test_plan_of_another_week_is_not_used(self) -> None:
        today = weekly_plan.today_local()
        old_day = today - datetime.timedelta(days=21)
        write_task(self.vault, "старое обязательство", status="done")
        weekly_plan.save_plan(self.vault, ["старое обязательство"], day=old_day)

        result = weekly_plan.metric(self.vault, today)
        self.assertFalse(result["fixed"])
        self.assertIn("другой недели", result["reason"])

    def test_empty_plan_file_is_ignored(self) -> None:
        path = weekly_plan.plan_path(self.vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nweek_start: 2026-10-05\nweek_end: 2026-10-11\ntasks:\n---\n",
                        encoding="utf-8")
        # API уточнён во втором проходе: load_plan отдаёт (план | None, причина),
        # чтобы метрика объясняла отказ, а не показывала ложный процент.
        plan, reason = weekly_plan.load_plan(self.vault)
        self.assertIsNone(plan)
        self.assertTrue(reason, "у отказа должна быть понятная причина")
        self.assertFalse(weekly_plan.metric(self.vault)["fixed"])

    def test_percent_counts_only_saved_commitments(self) -> None:
        today = weekly_plan.today_local()
        write_task(self.vault, "первое", status="done")
        write_task(self.vault, "второе", status="open")
        write_task(self.vault, "третье", status="open")
        # карточка, которой нет в плане, процент не поднимает
        write_task(self.vault, "случайная", status="done")

        weekly_plan.save_plan(self.vault, ["первое", "второе", "третье"])
        result = weekly_plan.metric(self.vault, today)
        self.assertTrue(result["fixed"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["percent"], 33)

    def test_rescheduling_does_not_change_percent(self) -> None:
        today = weekly_plan.today_local()
        task = write_task(self.vault, "первое", status="done", scheduled=today.isoformat())
        write_task(self.vault, "второе", status="open")
        weekly_plan.save_plan(self.vault, ["первое", "второе"])

        before = weekly_plan.metric(self.vault, today)["percent"]
        # Переносим карточку на другой месяц. ID, записанный при freeze,
        # остаётся в карточке — идентичность не зависит от даты.
        task_id = weekly_plan.load_plan(self.vault)[0]["tasks"][0]["id"]
        task.write_text(TASK.format(title="первое", status="done",
                                    scheduled="2030-01-01",
                                    extra=f"duskTaskId: {task_id}\n"), encoding="utf-8")
        after = weekly_plan.metric(self.vault, today)["percent"]
        self.assertEqual(before, after, "перенос даты карточки не меняет план")

    def test_deleted_card_stays_in_denominator(self) -> None:
        today = weekly_plan.today_local()
        write_task(self.vault, "первое", status="done")
        removed = write_task(self.vault, "второе", status="open")
        weekly_plan.save_plan(self.vault, ["первое", "второе"])

        removed.unlink()
        result = weekly_plan.metric(self.vault, today)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["percent"], 50, "удаление карточки не улучшает процент до 100")

    def test_renamed_file_with_same_title_stays_tracked(self) -> None:
        """Переименовали файл, заголовок прежний — обязательство находится."""
        today = weekly_plan.today_local()
        task = write_task(self.vault, "первое", status="done")
        weekly_plan.save_plan(self.vault, ["первое"])
        task.rename(task.with_name("первое переименованное.md"))

        result = weekly_plan.metric(self.vault, today)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(result["percent"], 100)

    def test_renamed_card_with_new_title_is_missing(self) -> None:
        """Сменились и имя файла, и заголовок — карточка честно считается пропавшей."""
        today = weekly_plan.today_local()
        task = write_task(self.vault, "первое", status="done")
        weekly_plan.save_plan(self.vault, ["первое"])
        task.write_text(TASK.format(title="совсем другое", status="done",
                                    scheduled="2026-01-01", extra=""), encoding="utf-8")
        task.rename(task.with_name("совсем другое.md"))

        result = weekly_plan.metric(self.vault, today)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["percent"], 0, "пропавшее обязательство не улучшает процент")


class StatusMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.today = weekly_plan.today_local()
        self.week_start, self.week_end = weekly_plan.week_bounds(self.today)

    def test_execution_is_none_without_plan(self) -> None:
        metrics = status._metrics(self.vault)
        self.assertIsNone(metrics["execution"], "без плана не показываем 0%")
        self.assertFalse(metrics["plan"]["fixed"])
        self.assertFalse(weekly_plan.plan_path(self.vault).exists())

    def test_execution_comes_from_saved_plan(self) -> None:
        write_task(self.vault, "первое", status="done")
        write_task(self.vault, "второе", status="open")
        weekly_plan.save_plan(self.vault, ["первое", "второе"])
        metrics = status._metrics(self.vault)
        self.assertEqual(metrics["execution"], 50)
        self.assertEqual((metrics["week_start"], metrics["week_end"]),
                         (self.week_start, self.week_end))

    def test_future_task_does_not_enter_current_period(self) -> None:
        write_task(self.vault, "задача из 2030", status="open", scheduled="2030-01-01")
        metrics = status._metrics(self.vault)
        self.assertEqual(metrics["closed_week"], 0)
        self.assertNotIn("задача из 2030", [r["scheduled"] for r in metrics["aging"]])

    def test_future_completion_date_is_not_counted(self) -> None:
        write_task(self.vault, "закрыта в будущем", status="done",
                   scheduled=self.today.isoformat(),
                   extra="completedDate: 2030-01-01\n")
        metrics = status._metrics(self.vault)
        self.assertEqual(metrics["closed_week"], 0)

    def test_closed_week_respects_both_bounds(self) -> None:
        write_task(self.vault, "на этой неделе", status="done",
                   scheduled=self.week_start, extra=f"completedDate: {self.week_start}\n")
        last_week = (self.today - datetime.timedelta(days=7)).isoformat()
        write_task(self.vault, "на прошлой", status="done",
                   scheduled=last_week, extra=f"completedDate: {last_week}\n")
        metrics = status._metrics(self.vault)
        self.assertEqual(metrics["closed_week"], 1)

    def test_done_without_date_is_not_guessed_into_the_week(self) -> None:
        """Старая карточка со статусом done, но без даты, в неделю не засчитывается.

        Историческое время для неё не выдумывается — это честнее, чем
        подставить сегодняшнюю дату и улучшить отчёт.
        """
        write_task(self.vault, "старая без даты", status="done", scheduled="2026-01-01")
        metrics = status._metrics(self.vault)
        self.assertEqual(metrics["closed_week"], 0)


class StatusPageTests(unittest.TestCase):
    """Страница собирается целиком: метки честные, ничего не выдумывается."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)

        for patcher in (
            mock.patch.object(status, "_ask_model",
                              lambda m: {"rocks": ["цель"], "today": [], "cut": []}),
            mock.patch.object(status, "_market_section", lambda vault: []),
            mock.patch.object(status.someday, "count", lambda vault=None: 0),
            mock.patch.object(status.someday, "with_triggers", lambda vault=None: []),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def render(self) -> str:
        with mock.patch.dict("os.environ", {"VAULT_PATH": str(self.vault)}):
            return status.build_status()

    def test_page_without_plan_says_not_fixed(self) -> None:
        page = self.render()
        self.assertIn("не зафиксирован", page)
        self.assertIn("weekly_plan.py freeze", page, "способ фиксации должен быть виден на странице")
        self.assertIn("Строк в инбоксе добавлено (git)", page)
        self.assertNotIn("Выгружено мыслей", page, "старая вводящая в заблуждение подпись убрана")
        self.assertIn("пн–вс", page)
        self.assertTrue((self.vault / "Статус.md").exists())
        self.assertFalse(weekly_plan.plan_path(self.vault).exists(), "страница не создаёт план")

    def test_page_with_plan_shows_percent(self) -> None:
        write_task(self.vault, "первое", status="done")
        write_task(self.vault, "второе", status="open")
        weekly_plan.save_plan(self.vault, ["первое", "второе"])
        page = self.render()
        self.assertIn("50%", page)
        self.assertNotIn("не зафиксирован", page)


if __name__ == "__main__":
    unittest.main()
