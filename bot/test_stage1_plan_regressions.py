#!/usr/bin/env python3
"""Регрессии плана недели: подмена карточки, неверный план, будущее закрытие (4, 5, 9)."""

from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import status
import weekly_plan

TASK = """---
title: "{title}"
status: {status}
priority: normal
scheduled: {scheduled}
created: {created}
{extra}tags:
  - task
---

# {title}
"""


def write_card(vault: Path, title: str, status: str = "open", created: str = "2026-10-01T10:00",
               scheduled: str = "2026-10-01", extra: str = "", name: str = "") -> Path:
    tasks = vault / "TaskNotes" / "Tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    path = tasks / f"{name or title}.md"
    path.write_text(TASK.format(title=title, status=status, created=created,
                                scheduled=scheduled, extra=extra), encoding="utf-8")
    return path


def plan_text(week_start: str, week_end: str, tasks: str, timezone: str = "Asia/Almaty",
              frozen: str = "2026-10-05T09:00:00+05:00", type_: str = "weekly_plan") -> str:
    return (
        "---\n"
        f"type: {type_}\n"
        f"timezone: {timezone}\n"
        f"week_start: {week_start}\n"
        f"week_end: {week_end}\n"
        f"frozen: {frozen}\n"
        "tasks:\n"
        f"{tasks}"
        "---\n\n# План\n"
    )


class PlanIdentityTests(unittest.TestCase):
    """Пункт 4: обязательство принадлежит конкретной карточке."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.today = weekly_plan.today_local()

    def test_replacement_card_does_not_satisfy_commitment(self) -> None:
        original = write_card(self.vault, "первое", status="open", created="2026-10-01T10:00")
        weekly_plan.save_plan(self.vault, ["первое"])

        original.unlink()
        # новая карточка с тем же названием, но другой карточкой
        write_card(self.vault, "первое", status="done", created="2026-10-06T09:00",
                   extra=f"completedDate: {self.today.isoformat()}\n")

        result = weekly_plan.metric(self.vault, self.today)
        self.assertEqual(result["done"], 0, "чужую одноимённую карточку не засчитываем")
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["percent"], 0)

    def test_rename_with_same_identity_is_tracked(self) -> None:
        card = write_card(self.vault, "первое", status="done", created="2026-10-01T10:00")
        weekly_plan.save_plan(self.vault, ["первое"])
        card.rename(card.with_name("первое переименованное.md"))

        result = weekly_plan.metric(self.vault, self.today)
        self.assertEqual(result["missing"], 0, "переименование не должно ломать план")
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["percent"], 100)

    def test_same_title_twins_are_reported_as_ambiguous(self) -> None:
        write_card(self.vault, "первое", status="open", created="2026-10-01T10:00")
        plan_path = weekly_plan.plan_path(self.vault)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        start, end = weekly_plan.week_bounds(self.today)
        # в плане записано только название — идентичность не зафиксирована
        plan_path.write_text(plan_text(start, end, '  - "первое"\n'), encoding="utf-8")

        write_card(self.vault, "первое", status="done", created="2026-10-06T09:00",
                   name="первое копия")
        result = weekly_plan.metric(self.vault, self.today)
        self.assertFalse(result["rows"][0]["state"] == "done",
                         "неоднозначность не должна засчитываться как выполнение")

    def test_plan_records_stable_identity_at_freeze(self) -> None:
        card = write_card(self.vault, "первое", status="open", created="2026-10-01T10:00")
        weekly_plan.save_plan(self.vault, ["первое"])
        plan, reason = weekly_plan.load_plan(self.vault)
        self.assertEqual(reason, "")
        entry = plan["tasks"][0]
        self.assertIsInstance(entry, dict, "freeze должен сохранить идентичность карточки")
        self.assertEqual(entry.get("stem"), card.stem)
        self.assertEqual(entry.get("created"), "2026-10-01T10:00")

    def test_unresolvable_commitment_keeps_denominator(self) -> None:
        """Свободное обязательство остаётся в знаменателе, но не засчитывается.

        Контракт изменён в третьем проходе: без карточки и без ID связь не
        подтверждена, поэтому состояние `unverified`, а не «выполнено».
        """
        write_card(self.vault, "первое", status="done", created="2026-10-01T10:00")
        weekly_plan.save_plan(self.vault, ["первое", "несуществующее"])
        result = weekly_plan.metric(self.vault, self.today)
        self.assertEqual(result["total"], 2, "обязательство остаётся в знаменателе")
        self.assertEqual(result["unverified"], 1)
        self.assertEqual(result["done"], 1, "карточка с ID засчитана")
        self.assertEqual(result["percent"], 50)


class PlanValidationTests(unittest.TestCase):
    """Пункт 5: неверный план не выдаётся за зафиксированный."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.today = weekly_plan.today_local()
        self.start, self.end = weekly_plan.week_bounds(self.today)
        self.path = weekly_plan.plan_path(self.vault)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_plan(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def assert_not_fixed(self, expected_fragment: str = "") -> dict:
        result = weekly_plan.metric(self.vault, self.today)
        self.assertFalse(result["fixed"], "некорректный план нельзя считать зафиксированным")
        self.assertIsNone(result["percent"])
        self.assertTrue(result["reason"], "нужна понятная причина")
        if expected_fragment:
            self.assertIn(expected_fragment, result["reason"])
        return result

    def test_wrong_timezone(self) -> None:
        self.write_plan(plan_text(self.start, self.end, '  - "первое"\n', timezone="UTC"))
        self.assert_not_fixed("Asia/Almaty")

    def test_bad_frozen_value(self) -> None:
        self.write_plan(plan_text(self.start, self.end, '  - "первое"\n', frozen="nope"))
        self.assert_not_fixed("фиксац")

    def test_wrong_type(self) -> None:
        self.write_plan(plan_text(self.start, self.end, '  - "первое"\n', type_="task"))
        self.assert_not_fixed("type")

    def test_tasks_as_string(self) -> None:
        self.write_plan(plan_text(self.start, self.end, "  - первое\n").replace(
            "tasks:\n  - первое\n", "tasks: первое\n"))
        self.assert_not_fixed("списк")

    def test_inconsistent_bounds(self) -> None:
        # week_end не воскресенье той же недели
        self.write_plan(plan_text(self.start, "2030-01-01", '  - "первое"\n'))
        self.assert_not_fixed()

    def test_valid_plan_is_fixed(self) -> None:
        write_card(self.vault, "первое", status="open")
        weekly_plan.save_plan(self.vault, ["первое"])
        result = weekly_plan.metric(self.vault, self.today)
        self.assertTrue(result["fixed"])
        self.assertEqual(result["total"], 1)

    def test_invalid_plan_is_not_silently_rewritten(self) -> None:
        self.write_plan(plan_text(self.start, self.end, '  - "первое"\n', timezone="UTC"))
        before = self.path.read_text(encoding="utf-8")
        weekly_plan.metric(self.vault, self.today)
        status._metrics(self.vault)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before,
                         "read-only расчёт не переписывает план")

    def test_legacy_plan_without_identity_still_loads(self) -> None:
        """Старый формат читается, но связь не подтверждена и выполнение не засчитывается.

        Контракт изменён в третьем проходе по решению Codex: идентичность — только
        `duskTaskId`, записанный при ручном freeze. План без ID остаётся планом
        (обязательство в знаменателе), но done по нему не начисляется.
        """
        write_card(self.vault, "первое", status="done", created="2026-10-01T10:00")
        self.write_plan(plan_text(self.start, self.end, '  - "первое"\n'))

        result = weekly_plan.metric(self.vault, self.today)
        self.assertTrue(result["fixed"], "структурно корректный старый план остаётся планом")
        self.assertEqual(result["done"], 0, "без ID выполнение не засчитываем")
        self.assertEqual(result["total"], 1, "обязательство остаётся в знаменателе")
        self.assertEqual(result["rows"][0]["state"], weekly_plan.UNVERIFIED)
        self.assertTrue(result["rows"][0]["note"], "связь без идентичности помечается явно")

    def test_entry_without_title_is_rejected(self) -> None:
        self.write_plan(plan_text(self.start, self.end,
                                  '  - {"stem": "первое", "created": "2026-10-01T10:00"}\n'))
        self.assert_not_fixed("названия")


class FutureCompletionTests(unittest.TestCase):
    """Пункт 9: будущее закрытие не считается выполненным."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.today = datetime.date(2026, 10, 6)

    def test_closed_week_excludes_future_completion(self) -> None:
        write_card(self.vault, "закрыта в будущем", status="done",
                   extra="completedDate: 2026-10-10\n")
        with mock.patch.object(weekly_plan, "today_local", return_value=self.today):
            metrics = status._metrics(self.vault)
        self.assertEqual(metrics["closed_week"], 0)

    def test_closed_week_counts_completion_up_to_today(self) -> None:
        write_card(self.vault, "закрыта сегодня", status="done",
                   extra="completedDate: 2026-10-06\n")
        write_card(self.vault, "закрыта завтра", status="done",
                   extra="completedDate: 2026-10-07\n")
        with mock.patch.object(weekly_plan, "today_local", return_value=self.today):
            metrics = status._metrics(self.vault)
        self.assertEqual(metrics["closed_week"], 1)

    def test_plan_does_not_count_future_completion(self) -> None:
        write_card(self.vault, "первое", status="done", extra="completedDate: 2026-10-10\n")
        with mock.patch.object(weekly_plan, "today_local", return_value=self.today):
            weekly_plan.save_plan(self.vault, ["первое"], day=self.today)
            result = weekly_plan.metric(self.vault, self.today)
        self.assertEqual(result["done"], 0, "будущее закрытие не считается выполнением")
        self.assertEqual(result["percent"], 0)

    def test_unknown_date_is_not_replaced_by_today(self) -> None:
        write_card(self.vault, "первое", status="done")  # без completedDate
        with mock.patch.object(weekly_plan, "today_local", return_value=self.today):
            weekly_plan.save_plan(self.vault, ["первое"], day=self.today)
            result = weekly_plan.metric(self.vault, self.today)
        self.assertEqual(result["done"], 1, "карточка без даты остаётся выполненной")


if __name__ == "__main__":
    unittest.main()
