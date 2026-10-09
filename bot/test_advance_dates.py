#!/usr/bin/env python3
"""Закрытие задачи: дата завершения, повтор, переоткрытие, атомарность.

Изолированно: временный валт, никаких git-операций.
"""

from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dispatch
import status
import weekly_plan

TASK_TEMPLATE = """---
title: "проверить канбан"
status: {status}
priority: high
nextStep: "открыть доску"
created: 2026-10-01T10:00
scheduled: {scheduled}
{extra}tags:
  - task
---

# проверить канбан

тело задачи, которое нельзя потерять
"""


def task_text(status: str = "open", scheduled: str = "2026-10-01", extra: str = "") -> str:
    return TASK_TEMPLATE.format(status=status, scheduled=scheduled, extra=extra)


class AdvanceDateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.tasks_dir = self.vault / "TaskNotes" / "Tasks"
        self.tasks_dir.mkdir(parents=True)

        patcher = patch.object(dispatch, "VAULT", self.vault)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.task = self.tasks_dir / "проверить канбан.md"
        self.now = datetime.datetime(2026, 10, 6, 16, 4, 9, 123000,
                                     tzinfo=datetime.timezone(datetime.timedelta(hours=5)))
        self.later = self.now + datetime.timedelta(days=2)

    def write(self, text: str) -> None:
        self.task.write_text(text, encoding="utf-8")

    def read(self) -> str:
        return self.task.read_text(encoding="utf-8")

    def field(self, name: str) -> str:
        import re

        m = re.search(rf"^{name}:\s*(.*)$", self.read(), re.M)
        return m.group(1).strip() if m else ""

    def test_first_close_writes_date_and_modified(self) -> None:
        self.write(task_text(status="open"))
        dispatch.advance_task("готово", "канбан", now=self.now)

        self.assertEqual(self.field("status"), "done")
        self.assertEqual(self.field("completedDate"), "2026-10-06")
        self.assertEqual(self.field("dateModified"), "2026-10-06T16:04:09.123+05:00")
        body = self.read()
        self.assertIn("тело задачи, которое нельзя потерять", body)
        self.assertEqual(self.field("priority"), "high")
        self.assertEqual(self.field("nextStep"), '"открыть доску"')

    def test_repeat_close_keeps_original_date(self) -> None:
        self.write(task_text(status="open"))
        dispatch.advance_task("готово", "канбан", now=self.now)
        message = dispatch.advance_task("готово", "канбан", now=self.later)

        self.assertEqual(self.field("completedDate"), "2026-10-06", "дата не должна сдвигаться")
        self.assertIn("уже", message)

    def test_reopen_removes_completed_date(self) -> None:
        self.write(task_text(status="done", extra="completedDate: 2026-10-02\n"))
        dispatch.advance_task("список", "канбан", now=self.now)

        self.assertEqual(self.field("status"), "open")
        self.assertEqual(self.field("completedDate"), "", "переоткрытие убирает дату завершения")
        self.assertEqual(self.field("dateModified"), "2026-10-06T16:04:09.123+05:00")

    def test_close_after_reopen_gets_new_date(self) -> None:
        self.write(task_text(status="done", extra="completedDate: 2026-10-02\n"))
        dispatch.advance_task("список", "канбан", now=self.now)
        dispatch.advance_task("готово", "канбан", now=self.later)

        self.assertEqual(self.field("status"), "done")
        self.assertEqual(self.field("completedDate"), "2026-10-08")

    def test_old_done_card_without_date_does_not_invent_history(self) -> None:
        self.write(task_text(status="done"))
        message = dispatch.advance_task("готово", "канбан", now=self.now)

        self.assertEqual(self.field("completedDate"), "", "историческую дату не выдумываем")
        self.assertIn("уже", message)

        dispatch.advance_task("список", "канбан", now=self.now)
        self.assertEqual(self.field("completedDate"), "")
        dispatch.advance_task("готово", "канбан", now=self.now)
        self.assertEqual(self.field("completedDate"), "2026-10-06")

    def test_card_without_status_gets_status_and_date(self) -> None:
        plain = self.tasks_dir / "без статуса.md"
        plain.write_text("---\ntitle: \"без статуса\"\n---\n\n# без статуса\n", encoding="utf-8")
        dispatch.advance_task("готово", "без статуса", now=self.now)
        text = plain.read_text(encoding="utf-8")
        self.assertIn("status: done", text)
        self.assertIn("completedDate: 2026-10-06", text)
        self.assertIn("dateModified: ", text)

    def test_ambiguous_name_asks_for_clarification(self) -> None:
        self.write(task_text(status="open"))
        (self.tasks_dir / "проверить канбан дважды.md").write_text(
            task_text(status="open"), encoding="utf-8")
        message = dispatch.advance_task("готово", "проверить канбан", now=self.now)
        self.assertIn("уточни", message)
        self.assertEqual(self.field("status"), "open", "при неоднозначности ничего не меняем")


class MetricsReadClosedDateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.tasks_dir = self.vault / "TaskNotes" / "Tasks"
        self.tasks_dir.mkdir(parents=True)

    def test_ui_closed_card_counts_in_current_week(self) -> None:
        today = weekly_plan.today_local()
        (self.tasks_dir / "закрыта интерфейсом.md").write_text(
            task_text(status="done", scheduled=today.isoformat(),
                      extra=f"completedDate: {today.isoformat()}\n"),
            encoding="utf-8",
        )
        metrics = status._metrics(self.vault)
        self.assertEqual(metrics["closed_week"], 1)

    def test_stale_completion_is_not_counted(self) -> None:
        today = weekly_plan.today_local()
        old = (today - datetime.timedelta(days=21)).isoformat()
        (self.tasks_dir / "старая.md").write_text(
            task_text(status="done", scheduled=old, extra=f"completedDate: {old}\n"),
            encoding="utf-8",
        )
        metrics = status._metrics(self.vault)
        self.assertEqual(metrics["closed_week"], 0)


if __name__ == "__main__":
    unittest.main()
