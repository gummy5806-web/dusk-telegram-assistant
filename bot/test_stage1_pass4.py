#!/usr/bin/env python3
"""Регрессии четвёртого прохода: чужие заметки, типы дат, отображение связей.

Изоляция: временный валт, mock внешних границ. Никаких реальных вызовов.
"""

from __future__ import annotations

import datetime
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import learn
import status
import weekly_plan

from test_learn_queue import AREA, QueueTestCase, fake_distill

URL_FIRST = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_SECOND = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
ACTION = "сделать действие"

MANUAL_NOTE = """---
connections:
reference: {url}
tags:
  - literature_note
type: literature_note
created: 2026-10-01T10:00
---

# Мои наблюдения по видео

Я смотрел это сам и записал главное своими словами.
Второе наблюдение: работает только при регулярности.
"""


class ManualNoteSafetyTests(QueueTestCase):
    """Пункт 1: ручные заметки не перезаписываются."""

    def manual_note(self, url: str = URL_FIRST, name: str = "Мои наблюдения.md") -> Path:
        hub = self.vault / learn.HUB / AREA
        hub.mkdir(parents=True, exist_ok=True)
        path = hub / name
        path.write_text(MANUAL_NOTE.format(url=url), encoding="utf-8")
        return path

    def digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def run_queue(self, url: str = URL_FIRST, action: str = ACTION) -> str:
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=action)):
            return learn.process_queue()

    def owned_notes(self) -> list:
        return [p for p in self.notes()
                if frontmatter_get(p.read_text(encoding="utf-8"), "generator") == "dusk"]

    def test_manual_note_with_same_url_is_never_overwritten(self) -> None:
        manual = self.manual_note()
        before = self.digest(manual)
        self.write_queue(URL_FIRST)

        self.run_queue()

        self.assertEqual(self.digest(manual), before, "ручная заметка обязана остаться дословной")
        self.assertIn("Я смотрел это сам", manual.read_text(encoding="utf-8"))
        owned = self.owned_notes()
        self.assertEqual(len(owned), 1, "новый разбор публикуется отдельным файлом")
        self.assertTrue(learn._note_is_complete(owned[0].read_text(encoding="utf-8")))

    def test_manual_note_is_not_chosen_for_repair(self) -> None:
        manual = self.manual_note()
        found = learn._existing_note(self.vault, URL_FIRST, owned_only=True)
        self.assertIsNone(found, "чужая заметка не считается нашим черновиком")
        self.assertIsNotNone(learn._existing_note(self.vault, URL_FIRST, owned_only=False))
        self.assertTrue(manual.exists())

    def test_complete_generated_result_wins_over_manual(self) -> None:
        self.manual_note()
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            learn.ingest_result(URL_FIRST)
        generated = self.owned_notes()
        self.assertEqual(len(generated), 1)
        generated_path = generated[0]
        before = self.digest(generated_path)

        # повтор того же URL: находится завершённый наш результат, дубля нет
        with patch.object(learn, "fetch_subtitles", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_distill", side_effect=AssertionError("модель не нужна")):
            ok, message = learn.ingest_result(URL_FIRST)

        self.assertTrue(ok)
        self.assertEqual(self.digest(generated_path), before, "наша заметка не перезаписана")
        self.assertEqual(len(self.owned_notes()), 1, "дубль не создан")
        self.assertEqual(len(self.tasks()), 1, "вторая задача не создана")
        self.assertIn("повторно не дублирую", message)

    def test_legacy_partial_without_marker_is_preserved(self) -> None:
        hub = self.vault / learn.HUB / AREA
        hub.mkdir(parents=True, exist_ok=True)
        legacy = hub / "старый черновик.md"
        legacy.write_text(
            f"---\nconnections:\nreference: {URL_FIRST}\n"
            "tags:\n  - literature_note\ntype: literature_note\n---\n\n# Черновик\n",
            encoding="utf-8",
        )
        before = self.digest(legacy)
        self.write_queue(URL_FIRST)

        self.run_queue()

        self.assertEqual(self.digest(legacy), before, "неизвестный файл не наш — не трогаем")
        self.assertEqual(len(self.owned_notes()), 1)

    def test_owned_partial_keeps_its_text_and_result_goes_beside(self) -> None:
        """Контракт уточнён в пятом проходе: черновик сохраняется дословно.

        Построчное слияние убрано: оно теряло повторы и строки, совпавшие с
        тезисами. Теперь исходный файл не трогается вообще, а завершённый
        результат публикуется рядом.
        """
        hub = self.vault / learn.HUB / AREA
        hub.mkdir(parents=True, exist_ok=True)
        owned = hub / "наш черновик.md"
        owned.write_text(
            "---\nconnections:\n"
            f"reference: {URL_FIRST}\n"
            "generator: dusk\n"
            "tags:\n  - literature_note\ntype: literature_note\n---\n\n"
            "# Черновик\n\nМоё наблюдение: сработало на второй неделе.\n",
            encoding="utf-8",
        )
        before = owned.read_bytes()

        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            ok, _ = learn.ingest_result(URL_FIRST)

        self.assertTrue(ok)
        self.assertEqual(owned.read_bytes(), before, "черновик не изменяется")
        self.assertIn(b"\xd0\x9c\xd0\xbe\xd1\x91 \xd0\xbd\xd0\xb0\xd0\xb1\xd0\xbb\xd1\x8e\xd0\xb4\xd0\xb5\xd0\xbd\xd0\xb8\xd0\xb5",
                      owned.read_bytes(), "пользовательский текст на месте")
        complete = [p for p in self.notes()
                    if learn._note_is_complete(p.read_text(encoding="utf-8"))]
        self.assertEqual(len(complete), 1, "рядом ровно один завершённый результат")

    def test_repeat_ingest_does_not_duplicate_result_or_task(self) -> None:
        """Повторный разбор не создаёт второй результат, задачу и не правит черновик."""
        hub = self.vault / learn.HUB / AREA
        hub.mkdir(parents=True, exist_ok=True)
        owned = hub / "наш черновик.md"
        owned.write_text(
            "---\nconnections:\n"
            f"reference: {URL_FIRST}\n"
            "generator: dusk\n"
            "tags:\n  - literature_note\ntype: literature_note\n---\n\n"
            "# Черновик\n\nМоё наблюдение: сработало на второй неделе.\n",
            encoding="utf-8",
        )
        before = owned.read_bytes()

        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            learn.ingest_result(URL_FIRST)
        with patch.object(learn, "fetch_subtitles",
                          side_effect=AssertionError("модель не нужна на повторе")), \
                patch.object(learn, "_distill",
                             side_effect=AssertionError("модель не нужна на повторе")):
            ok, message = learn.ingest_result(URL_FIRST)

        self.assertTrue(ok)
        self.assertIn("повторно не дублирую", message)
        self.assertEqual(owned.read_bytes(), before, "черновик не тронут")
        complete = [p for p in self.notes()
                    if learn._note_is_complete(p.read_text(encoding="utf-8"))]
        self.assertEqual(len(complete), 1, "завершённый результат один")
        self.assertEqual(len(self.tasks()), 1, "задача одна")


    def test_name_collision_keeps_foreign_file(self) -> None:
        hub = self.vault / learn.HUB / AREA
        hub.mkdir(parents=True, exist_ok=True)
        foreign = hub / "Общее название.md"
        foreign.write_text(
            "---\nconnections:\nreference: " + URL_SECOND + "\n"
            "tags:\n  - literature_note\ntype: literature_note\n---\n\n# Чужой конспект\n",
            encoding="utf-8",
        )
        before = self.digest(foreign)
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(title="Общее название", action=ACTION)):
            learn.ingest_result(URL_FIRST)

        self.assertEqual(self.digest(foreign), before, "одноимённый чужой файл сохраняется")
        self.assertEqual(len(self.notes()), 2)


class PlanDateTypeTests(unittest.TestCase):
    """Пункт 2: нестроковые типы дат отвергаются без исключения."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.path = weekly_plan.plan_path(self.vault)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.today = datetime.date(2026, 10, 6)

    def plan_text(self, start: str, end: str = "2026-10-11") -> str:
        return ("---\ntype: weekly_plan\ntimezone: Asia/Almaty\n"
                f"week_start: {start}\nweek_end: {end}\n"
                "frozen: 2026-10-06T12:00:00+05:00\ntasks:\n  - \"alpha\"\n---\n")

    def test_non_string_week_values_do_not_raise(self) -> None:
        for value in ('123', '["2026-10-05"]', '{"day":"2026-10-05"}', 'true', 'null'):
            with self.subTest(value=value):
                self.path.write_text(self.plan_text(value), encoding="utf-8")
                before = self.path.read_text(encoding="utf-8")
                result = weekly_plan.metric(self.vault, self.today)
                self.assertFalse(result["fixed"])
                self.assertIsNone(result["percent"])
                self.assertTrue(result["reason"], "нужна понятная причина отказа")
                self.assertEqual(self.path.read_text(encoding="utf-8"), before,
                                 "read-only расчёт не переписывает план")

    def test_non_string_week_end_does_not_raise(self) -> None:
        self.path.write_text(self.plan_text("2026-10-05", end="[1, 2]"), encoding="utf-8")
        result = weekly_plan.metric(self.vault, self.today)
        self.assertFalse(result["fixed"])
        self.assertTrue(result["reason"])

    def test_non_string_frozen_and_timezone_do_not_raise(self) -> None:
        self.path.write_text(
            "---\ntype: weekly_plan\ntimezone: 42\nweek_start: 2026-10-05\n"
            "week_end: 2026-10-11\nfrozen: true\ntasks:\n  - \"alpha\"\n---\n",
            encoding="utf-8")
        result = weekly_plan.metric(self.vault, self.today)
        self.assertFalse(result["fixed"])
        self.assertTrue(result["reason"])

    def test_parse_task_date_rejects_non_strings(self) -> None:
        for value in (123, True, False, ["2026-10-05"], {"day": "2026-10-05"}, None):
            with self.subTest(value=value):
                self.assertIsNone(weekly_plan.parse_task_date(value))

    def test_timestamp_cases_still_work(self) -> None:
        self.assertEqual(weekly_plan.parse_task_date("2026-10-04T20:00:00+00:00"),
                         datetime.date(2026, 10, 5))
        self.assertEqual(weekly_plan.parse_task_date("2026-10-06"), datetime.date(2026, 10, 6))
        self.assertEqual(weekly_plan.parse_task_date("2026-10-06T23:30:00"),
                         datetime.date(2026, 10, 6))


class StatusUnverifiedTests(unittest.TestCase):
    """Пункт 3: неподтверждённые связи видны на странице статуса."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True)
        for patcher in (
            mock.patch.object(status, "_ask_model",
                              lambda m: {"rocks": [], "today": [], "cut": []}),
            mock.patch.object(status, "_market_section", lambda vault: []),
            mock.patch.object(status.someday, "count", lambda vault=None: 0),
            mock.patch.object(status.someday, "with_triggers", lambda vault=None: []),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def card(self, name: str, status_: str = "done", extra: str = "") -> None:
        (self.vault / "TaskNotes" / "Tasks" / f"{name}.md").write_text(
            f"---\ntitle: {name}\nstatus: {status_}\ncreated: 2026-10-01T10:00\n{extra}"
            "---\n\n# " + name + "\n", encoding="utf-8")

    def legacy_plan(self) -> None:
        today = weekly_plan.today_local()
        start, end = weekly_plan.week_bounds(today)
        path = weekly_plan.plan_path(self.vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: weekly_plan\ntimezone: Asia/Almaty\n"
            f"week_start: {start}\nweek_end: {end}\n"
            "frozen: 2026-10-05T09:00:00+05:00\ntasks:\n  - \"первое\"\n---\n\n# План\n",
            encoding="utf-8")

    def render(self) -> str:
        with mock.patch.dict("os.environ", {"VAULT_PATH": str(self.vault)}):
            return status.build_status()

    def test_page_discloses_unverified_links(self) -> None:
        self.card("первое")
        self.legacy_plan()

        page = self.render()

        self.assertIn("не подтверждена", page.lower(),
                      "страница обязана сказать о неподтверждённых связях")
        row = [l for l in page.splitlines() if "Выполнение плана" in l]
        self.assertTrue(row, "строка плана должна остаться")
        self.assertIn("подтверждено", row[0].lower(),
                      "процент назван подтверждённым, раз есть неизвестные связи")
        self.assertNotIn("🔴", row[0], "непроверенный ноль не выдаём за доказанный провал")
        self.assertIn("в знаменателе", page.lower())

    def test_page_without_unverified_keeps_plain_row(self) -> None:
        card = self.vault / "TaskNotes" / "Tasks" / "первое.md"
        card.write_text("---\ntitle: первое\nstatus: open\ncreated: 2026-10-01T10:00\n"
                        "---\n\n# первое\n", encoding="utf-8")
        weekly_plan.save_plan(self.vault, ["первое"])

        page = self.render()

        self.assertNotIn("не подтверждена", page.lower())
        row = [l for l in page.splitlines() if "Выполнение плана" in l][0]
        self.assertIn("%", row)

    def test_page_without_plan_still_says_not_fixed(self) -> None:
        page = self.render()
        self.assertIn("не зафиксирован", page)


def frontmatter_get(text: str, name: str) -> str:
    return learn.frontmatter.get(text, name)


if __name__ == "__main__":
    unittest.main()
