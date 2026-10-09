#!/usr/bin/env python3
"""Регрессии третьего прохода (пункты 1–5).

Каждый тест падал на отклонённом втором проходе. Изоляция: временный валт,
mock внешних границ, никаких реальных вызовов.
"""

from __future__ import annotations

import datetime
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import dispatch
import learn
import status
import weekly_plan

from test_learn_queue import AREA, QueueTestCase, fake_distill
from test_stage1_plan_regressions import plan_text

URL_FIRST = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_SECOND = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
ACTION = "сделать действие"


# ── пункт 1: готовность задачи ────────────────────────────────────────────

class TaskReadinessTests(QueueTestCase):
    def make_note(self, url: str = URL_FIRST, stem: str = "конспект",
                  area: str = AREA) -> Path:
        hub = self.vault / learn.HUB / area
        hub.mkdir(parents=True, exist_ok=True)
        path = hub / f"{stem}.md"
        # Маркер владения обязателен: с пятого прохода только `generator: dusk`
        # делает конспект нашим. Иначе ingest публикует новый результат рядом,
        # и проверки про связь задачи с конспектом относятся уже к нему.
        path.write_text(
            "---\nconnections:\n"
            f"reference: {url}\n"
            "generator: dusk\n"
            "tags:\n  - literature_note\ntype: literature_note\n"
            "created: 2026-10-06T10:00\n---\n\n"
            f"# {stem}\n\n## Что применить\n\n- [ ] {ACTION}\n\n"
            f"---\n_Источник: {url}_\n_Разбор._\n",
            encoding="utf-8",
        )
        return path

    def note_source(self, note: Path) -> str:
        return note.relative_to(self.vault).as_posix()

    def valid_task(self, note: Path, name: str = "", title: str = ACTION) -> str:
        return (
            "---\n"
            f'title: "{title}"\n'
            "status: open\n"
            "priority: normal\n"
            "created: 2026-10-06T10:05\n"
            "scheduled: 2026-10-06\n"
            "timeEstimate: 45\n"
            "tags:\n  - task\n"
            f'source: "{self.note_source(note)}"\n'
            "---\n\n"
            f"# {title}\n\n"
            f"Из видео: [[{note.stem}]]\n"
        )

    def valid_tasks(self) -> list:
        """Задачи, у которых есть валидная шапка, source и обратная ссылка."""
        result = []
        for path in self.tasks():
            text = path.read_text(encoding="utf-8")
            if learn._task_is_valid(self.vault, text):
                result.append(path)
        return result

    def run_queue(self) -> str:
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            return learn.process_queue()

    def test_partial_task_with_backlink_does_not_confirm_action(self) -> None:
        note = self.make_note()
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        partial = tasks / f"{ACTION}.md"
        partial.write_text(f"Из видео: [[{note.stem}]]\n", encoding="utf-8")
        self.write_queue(URL_FIRST)

        self.run_queue()

        valid = self.valid_tasks()
        self.assertEqual(len(valid), 1, "должна появиться полноценная задача")
        self.assertEqual(partial.read_text(encoding="utf-8"),
                         f"Из видео: [[{note.stem}]]\n", "обрывок не перезаписываем")
        self.assertEqual(self.queue_urls(), [], "ссылка уходит только после валидной задачи")

    def test_process_queue_keeps_url_when_action_cannot_be_created(self) -> None:
        self.make_note()
        self.write_queue(URL_FIRST)
        original = learn._publish

        def failing_publish(path, text, **kwargs):
            if str(path).startswith(str(self.vault / "TaskNotes")):
                raise OSError("диск полон")
            return original(path, text, **kwargs)

        with patch.object(learn, "_publish", side_effect=failing_publish):
            with self.assertRaises(OSError):
                self.run_queue()

        self.assertIn(URL_FIRST, self.queue_urls(), "без задачи ссылка остаётся в очереди")

    def test_foreign_task_with_random_backlink_is_not_ours(self) -> None:
        note = self.make_note()
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        foreign = tasks / "чужая.md"
        foreign.write_text(
            "---\ntitle: \"чужая задача\"\nstatus: done\ncreated: 2026-01-01T10:00\n"
            "tags:\n  - task\nsource: \"Другое/место.md\"\n---\n\n"
            f"# чужая задача\n\nИз видео: [[{note.stem}]]\n",
            encoding="utf-8",
        )
        self.write_queue(URL_FIRST)

        self.run_queue()

        valid = self.valid_tasks()
        self.assertEqual(len(valid), 1)
        self.assertNotEqual(valid[0].name, foreign.name)
        self.assertIn(self.note_source(note), valid[0].read_text(encoding="utf-8"))
        self.assertIn("чужая задача", foreign.read_text(encoding="utf-8"),
                      "чужой файл не трогаем")

    def test_task_with_open_frontmatter_is_not_valid(self) -> None:
        note = self.make_note()
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        broken = tasks / f"{ACTION}.md"
        broken.write_text(
            f'---\ntitle: "{ACTION}"\nstatus: open\n'
            f'source: "{self.note_source(note)}"\n\nИз видео: [[{note.stem}]]\n',
            encoding="utf-8",
        )
        self.write_queue(URL_FIRST)

        self.run_queue()

        self.assertEqual(len(self.valid_tasks()), 1, "недописанная шапка не считается задачей")

    def test_task_without_title_or_status_is_not_valid(self) -> None:
        note = self.make_note()
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / f"{ACTION}.md").write_text(
            f'---\nsource: "{self.note_source(note)}"\n---\n\nИз видео: [[{note.stem}]]\n',
            encoding="utf-8",
        )
        self.write_queue(URL_FIRST)
        self.run_queue()
        self.assertEqual(len(self.valid_tasks()), 1)

    def test_same_stem_in_another_area_does_not_satisfy(self) -> None:
        first = self.make_note(url=URL_FIRST, stem="конспект", area=AREA)
        second = self.make_note(url=URL_SECOND, stem="конспект", area="Другая область")
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / "первая задача.md").write_text(
            self.valid_task(first, name="первая задача"), encoding="utf-8")

        self.write_queue(URL_SECOND)
        self.run_queue()

        valid = self.valid_tasks()
        # задача первого конспекта остаётся, и появляется своя для второго
        self.assertEqual(len(valid), 2, "для второго конспекта нужна своя задача")
        sources = [learn.frontmatter.get(p.read_text(encoding="utf-8"), "source") for p in valid]
        self.assertIn(self.note_source(second), sources,
                      "новая задача ссылается на свой конспект, а не на одноимённый чужой")

    def test_repeat_after_repair_creates_no_duplicate(self) -> None:
        note = self.make_note()
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / f"{ACTION}.md").write_text(f"Из видео: [[{note.stem}]]\n", encoding="utf-8")

        self.write_queue(URL_FIRST)
        self.run_queue()
        self.write_queue(URL_FIRST)          # ссылка снова в очереди
        self.run_queue()

        self.assertEqual(len(self.valid_tasks()), 1, "повтор не создаёт вторую задачу")
        self.assertEqual(self.queue_urls(), [])


# ── пункт 2: устойчивая идентичность плана ────────────────────────────────

CARD = """---
title: "{title}"
status: {status}
priority: normal
scheduled: {scheduled}
created: {created}
{extra}tags:
  - task
---

# {title}

тело карточки
"""


class PlanIdTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.today = weekly_plan.today_local()

    def card(self, name: str, title: str = "", status: str = "open",
             created: str = "2026-10-01T10:00", extra: str = "") -> Path:
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        path = tasks / f"{name}.md"
        path.write_text(CARD.format(title=title or name, status=status,
                                    scheduled="2026-10-01", created=created,
                                    extra=extra), encoding="utf-8")
        return path

    def frontmatter(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        return text.split("---", 2)[1]

    def body(self, path: Path) -> str:
        return path.read_text(encoding="utf-8").split("---", 2)[2]

    def test_freeze_writes_stable_id_and_keeps_body(self) -> None:
        card = self.card("первое")
        before_body = self.body(card)

        weekly_plan.save_plan(self.vault, ["первое"])

        plan, reason = weekly_plan.load_plan(self.vault)
        entry = plan["tasks"][0]
        self.assertEqual(reason, "")
        self.assertTrue(entry.get("id"), "freeze обязан записать устойчивый ID")
        self.assertIn(f"duskTaskId: {entry['id']}", self.frontmatter(card))
        self.assertEqual(self.body(card), before_body, "тело карточки не меняется")

    def test_freeze_does_not_touch_other_cards(self) -> None:
        chosen = self.card("первое")
        other = self.card("второе")
        before = other.read_text(encoding="utf-8")

        weekly_plan.save_plan(self.vault, ["первое"])

        self.assertEqual(other.read_text(encoding="utf-8"), before,
                         "массовой миграции нет: другие карточки не меняются")
        self.assertNotIn("duskTaskId", self.frontmatter(other))
        self.assertIn("duskTaskId", self.frontmatter(chosen))

    def test_repeat_freeze_keeps_existing_id(self) -> None:
        card = self.card("первое")
        weekly_plan.save_plan(self.vault, ["первое"])
        first = weekly_plan.load_plan(self.vault)[0]["tasks"][0]["id"]
        weekly_plan.save_plan(self.vault, ["первое"])
        second = weekly_plan.load_plan(self.vault)[0]["tasks"][0]["id"]
        self.assertEqual(first, second, "повторная фиксация не меняет ID")
        self.assertEqual(self.frontmatter(card).count("duskTaskId"), 1)

    def test_replacement_card_is_not_counted(self) -> None:
        """Созданной заново одноимённой карточки обязательство не исполняет."""
        original = self.card("первое", created="")
        weekly_plan.save_plan(self.vault, ["первое"])
        original.unlink()

        self.card("первое", status="done", created="",
                  extra=f"completedDate: {self.today.isoformat()}\n")

        result = weekly_plan.metric(self.vault, self.today)
        self.assertEqual(result["done"], 0, "новую карточку не засчитываем")
        self.assertEqual(result["percent"], 0)

    def test_two_cards_with_same_created_are_distinguished(self) -> None:
        chosen = self.card("первое", created="2026-10-01T10:00")
        twin = self.card("первое копия", created="2026-10-01T10:00")
        weekly_plan.save_plan(self.vault, ["первое"])
        chosen.unlink()

        self.card("первое", status="done", created="2026-10-01T10:00",
                  extra=f"completedDate: {self.today.isoformat()}\n")
        result = weekly_plan.metric(self.vault, self.today)
        self.assertEqual(result["done"], 0, "одинаковый created не доказывает идентичность")
        self.assertTrue(twin.exists())

    def test_rename_of_file_and_title_is_still_tracked(self) -> None:
        card = self.card("первое", status="done", extra="completedDate: 2026-10-06\n")
        weekly_plan.save_plan(self.vault, ["первое"])
        task_id = weekly_plan.load_plan(self.vault)[0]["tasks"][0]["id"]
        # меняем и имя файла, и заголовок, но ID остаётся в карточке
        card.write_text(CARD.format(title="совсем другое", status="done",
                                    scheduled="2026-10-01", created="2026-10-01T10:00",
                                    extra=f"completedDate: 2026-10-06\nduskTaskId: {task_id}\n"),
                        encoding="utf-8")
        card.rename(card.with_name("совсем другое.md"))

        result = weekly_plan.metric(self.vault, self.today)
        self.assertEqual(result["done"], 1, "ID не зависит от переименования")
        self.assertEqual(result["missing"], 0)

    def test_duplicate_id_is_ambiguous_not_a_guess(self) -> None:
        card = self.card("первое")
        weekly_plan.save_plan(self.vault, ["первое"])
        task_id = weekly_plan.load_plan(self.vault)[0]["tasks"][0]["id"]
        self.card("второе", extra=f"duskTaskId: {task_id}\n")

        result = weekly_plan.metric(self.vault, self.today)
        self.assertEqual(result["done"], 0)
        self.assertEqual(result["rows"][0]["state"], weekly_plan.AMBIGUOUS)

    def test_legacy_plan_without_id_does_not_count_done(self) -> None:
        """Старый строковый план: связь неподтверждена, выполнение не засчитывается."""
        self.card("первое", status="done")
        start, end = weekly_plan.week_bounds(self.today)
        path = weekly_plan.plan_path(self.vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan_text(start, end, '  - "первое"\n'), encoding="utf-8")

        result = weekly_plan.metric(self.vault, self.today)
        self.assertTrue(result["fixed"], "структурно корректный старый план остаётся планом")
        self.assertEqual(result["done"], 0, "без ID выполнение не засчитываем")
        self.assertEqual(result["total"], 1, "обязательство остаётся в знаменателе")
        self.assertEqual(result["rows"][0]["state"], weekly_plan.UNVERIFIED)

    def test_freeze_with_damaged_frontmatter_records_unverified(self) -> None:
        """Шапку записать нельзя — план фиксируется без ID, выполнение не засчитывается."""
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        broken = tasks / "сломанная.md"
        broken.write_text("---\ntitle: сломанная\nstatus: done\n\nбез закрывающей шапки\n",
                          encoding="utf-8")
        before = broken.read_text(encoding="utf-8")

        weekly_plan.save_plan(self.vault, ["сломанная"])

        self.assertEqual(broken.read_text(encoding="utf-8"), before, "файл не меняем")
        result = weekly_plan.metric(self.vault, self.today)
        self.assertEqual(result["done"], 0)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["rows"][0]["state"], weekly_plan.UNVERIFIED)




# ── пункты 3 и 4: дата закрытия ──────────────────────────────────────────

class StaleDateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.tasks = self.vault / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        self.card = self.tasks / "alpha.md"
        patcher = patch.object(dispatch, "VAULT", self.vault)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_close_replaces_stale_date(self) -> None:
        self.card.write_text(
            "---\ntitle: alpha\nstatus: open\ncompletedDate: 2026-09-01\n---\n# alpha\n",
            encoding="utf-8")
        dispatch.advance_task("готово", "alpha", now=datetime.datetime(2026, 10, 6, 12))
        self.assertEqual(dispatch._frontmatter_get(self.card.read_text(encoding="utf-8"),
                                                   "completedDate"), "2026-10-06")

    def test_quoted_done_is_already_done(self) -> None:
        for quoted in ('"done"', "'done'"):
            with self.subTest(status=quoted):
                self.card.write_text(
                    f"---\ntitle: alpha\nstatus: {quoted}\n---\n# alpha\n", encoding="utf-8")
                before = self.card.read_text(encoding="utf-8")
                dispatch.advance_task("готово", "alpha",
                                      now=datetime.datetime(2026, 10, 6, 12))
                self.assertEqual(self.card.read_text(encoding="utf-8"), before,
                                 "повторный done не выдумывает дату")

    def test_reopen_removes_date_only_from_frontmatter(self) -> None:
        self.card.write_text(
            "---\ntitle: alpha\nstatus: done\ncompletedDate: 2026-09-01\n---\n"
            "# alpha\n\ncompletedDate: пример в теле\n", encoding="utf-8")
        dispatch.advance_task("список", "alpha", now=datetime.datetime(2026, 10, 6, 12))
        text = self.card.read_text(encoding="utf-8")
        self.assertNotIn("completedDate: 2026-09-01", text.split("---", 2)[1])
        self.assertIn("completedDate: пример в теле", text)


class StrictDateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.tasks = self.vault / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)

    def card(self, completed: str, status: str = "done") -> None:
        (self.tasks / "alpha.md").write_text(
            f"---\ntitle: alpha\nstatus: {status}\ncompletedDate: {completed}\n---\n# alpha\n",
            encoding="utf-8")

    def closed(self, day: datetime.date) -> int:
        with mock.patch.object(weekly_plan, "today_local", return_value=day):
            return status._metrics(self.vault)["closed_week"]

    def test_utc_timestamp_is_converted_to_almaty(self) -> None:
        self.card("2026-10-04T20:00:00+00:00")   # понедельник 5 октября в Алматы
        self.assertEqual(self.closed(datetime.date(2026, 10, 5)), 1)
        self.assertEqual(self.closed(datetime.date(2026, 10, 4)), 0,
                         "на алматинское воскресенье это ещё не закрытие")

    def test_invalid_suffix_is_not_a_date(self) -> None:
        self.card("2026-10-06invalid")
        self.assertEqual(self.closed(datetime.date(2026, 10, 6)), 0)

    def test_date_only_is_still_accepted(self) -> None:
        self.card("2026-10-06")
        self.assertEqual(self.closed(datetime.date(2026, 10, 6)), 1)

    def test_naive_timestamp_is_local_almaty(self) -> None:
        self.card("2026-10-06T23:30:00")
        self.assertEqual(self.closed(datetime.date(2026, 10, 6)), 1)

    def test_strict_parser_refuses_garbage(self) -> None:
        for value in ("2026-10-06invalid", "2026-13-01", "не дата", "", "2026-10-06T99:00"):
            with self.subTest(value=value):
                self.assertIsNone(weekly_plan.parse_task_date(value))

    def test_strict_parser_accepts_date_and_timestamp(self) -> None:
        self.assertEqual(weekly_plan.parse_task_date("2026-10-06"),
                         datetime.date(2026, 10, 6))
        self.assertEqual(weekly_plan.parse_task_date("2026-10-04T20:00:00+00:00"),
                         datetime.date(2026, 10, 5))


# ── пункт 5: метаданные только из шапки ──────────────────────────────────

class MetadataScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.tasks = self.vault / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)

    def test_body_example_does_not_count_as_completion(self) -> None:
        (self.tasks / "alpha.md").write_text(
            "---\ntitle: alpha\nstatus: done\n---\n# alpha\n\n"
            "Пример поля:\ncompletedDate: 2026-10-06\n", encoding="utf-8")
        with mock.patch.object(weekly_plan, "today_local",
                               return_value=datetime.date(2026, 10, 6)):
            self.assertEqual(status._metrics(self.vault)["closed_week"], 0)

    def test_body_fields_do_not_affect_status(self) -> None:
        (self.tasks / "alpha.md").write_text(
            "---\ntitle: alpha\nstatus: open\n---\n# alpha\n\n"
            "```yaml\nstatus: done\ncompletedDate: 2026-10-06\ndue: 2020-01-01\n```\n",
            encoding="utf-8")
        with mock.patch.object(weekly_plan, "today_local",
                               return_value=datetime.date(2026, 10, 6)):
            metrics = status._metrics(self.vault)
        self.assertEqual(metrics["closed_week"], 0)
        self.assertEqual(metrics["overdue"], 0)
        self.assertEqual(metrics["open_count"], 1)

    def test_body_fields_do_not_define_card_identity(self) -> None:
        """`title`/`created` из тела не становятся метаданными карточки."""
        (self.tasks / "alpha.md").write_text(
            "---\ntitle: \"настоящее имя\"\nstatus: open\n---\n# alpha\n\n"
            "title: подделка\ncreated: 2026-01-01T00:00\n", encoding="utf-8")
        cards = weekly_plan._all_cards(self.vault)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "настоящее имя")
        self.assertEqual(cards[0]["created"], "")

    def test_damaged_frontmatter_is_not_metadata(self) -> None:
        (self.tasks / "broken.md").write_text(
            "---\ntitle: broken\nstatus: done\n\ncompletedDate: 2026-10-06\n",
            encoding="utf-8")
        cards = weekly_plan._all_cards(self.vault)
        self.assertEqual(cards[0]["status"], "", "шапка без закрывающего --- не читается")
        self.assertEqual(cards[0]["completed"], "")

    def test_status_title_comes_from_frontmatter(self) -> None:
        (self.tasks / "file-name.md").write_text(
            "---\ntitle: \"правильный заголовок\"\nstatus: open\n---\n# file-name\n\n"
            "title: подделка в теле\n", encoding="utf-8")
        rows = status._tasks(self.vault)
        self.assertEqual(rows[0]["title"], "правильный заголовок")


if __name__ == "__main__":
    unittest.main()
