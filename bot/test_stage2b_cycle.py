#!/usr/bin/env python3
"""Этап 2Б: пара «конспект ↔ задача применения», наблюдение пользователя, обзор.

Всё на временных валтах: модель и сеть подменены, внешних вызовов нет.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import dispatch
import learn
import learning_cycle as lc

from test_learn_queue import AREA
from test_stage2a_ingest import SYNTHESIS, URL, Stage2ABase, fake_model, is_synthesis


class PairLinkTests(Stage2ABase):
    """Пункт 1: пара связана полными путями от корня валта."""

    def test_note_and_task_link_by_full_paths(self) -> None:
        ok, message = self.run_ingest()
        self.assertTrue(ok, message)

        task = self.tasks()[0]
        note = self.our_notes()[-1]
        note_rel = note.relative_to(self.vault).as_posix()
        task_rel = task.relative_to(self.vault).as_posix()

        self.assertEqual(learn.frontmatter.get(task.read_text(encoding="utf-8"), "source"),
                         note_rel, "задача ссылается на конспект полным путём")
        self.assertIn(f"[[{task_rel[:-3]}]]", note.read_text(encoding="utf-8"),
                      "конспект ссылается на задачу полным путём")

    def test_task_card_has_criterion_and_three_fields(self) -> None:
        self.run_ingest()
        task = self.tasks()[0].read_text(encoding="utf-8")

        self.assertIn(lc.CRITERION_HEADING, task)
        self.assertIn("гипотеза", task.lower(), "критерий помечен как гипотеза")
        for heading in (lc.TRIED_HEADING, lc.OBSERVED_HEADING, lc.CONCLUSION_HEADING):
            self.assertIn(heading, task)
        self.assertEqual(task.count(lc.NO_DATA), 3,
                         "три поля начинаются с «нет данных»")

    def test_model_criterion_is_used(self) -> None:
        def model(prompt):
            if is_synthesis(prompt):
                return json.dumps({**SYNTHESIS,
                                   "criterion": "Видно, что шаг выполнен: есть запись числа"},
                                  ensure_ascii=False)
            return json.dumps({"points": [{"text": "тезис",
                                           "segments": re.findall(r"\[(seg-\d+)", prompt)[:1]}],
                               "quotes": []})

        ok, message = self.run_ingest(model=model)

        self.assertTrue(ok, message)
        self.assertIn("Видно, что шаг выполнен", self.tasks()[0].read_text(encoding="utf-8"))

    def test_fallback_criterion_invents_nothing(self) -> None:
        self.run_ingest()
        task = self.tasks()[0].read_text(encoding="utf-8")
        criterion = lc.read_application(task)["criterion"].lower()

        self.assertTrue(criterion)
        for banned in ("продаж", "выручк", "лид", "конверси", "оффер", "канал"):
            self.assertNotIn(banned, criterion)
        self.assertIsNone(re.search(r"\d", criterion), "никаких выдуманных чисел")

    def test_unknown_context_is_not_invented_in_prompts(self) -> None:
        prompts = []

        def model(prompt):
            prompts.append(prompt)
            return fake_model()(prompt)

        self.run_ingest(model=model)

        joined = "\n".join(prompts)
        self.assertNotIn("ориентируйся на контент и монетизацию", joined)
        self.assertIn("неизвест", joined.lower())


class UserTextTests(Stage2ABase):
    """Пункты 2 и 4: текст пользователя неприкосновенен, done ничего не заполняет."""

    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(dispatch, "VAULT", self.vault)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fill(self, task: Path, tried="попробовал шаг", observed="стало видно X",
             conclusion="мой вывод Y") -> str:
        text = task.read_text(encoding="utf-8")
        for heading, value in ((lc.TRIED_HEADING, tried),
                               (lc.OBSERVED_HEADING, observed),
                               (lc.CONCLUSION_HEADING, conclusion)):
            text = text.replace(f"{heading}\n\n{lc.NO_DATA}", f"{heading}\n\n{value}", 1)
        task.write_text(text, encoding="utf-8")
        return text

    def body(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        return text.split("---", 2)[2]

    def test_done_does_not_fill_the_result(self) -> None:
        self.run_ingest()
        task = self.tasks()[0]

        dispatch.advance_task("готово", task.stem, now=datetime.datetime(2026, 10, 6, 12))

        text = task.read_text(encoding="utf-8")
        self.assertIn("status: done", text)
        self.assertEqual(lc.read_application(text)["observed"], lc.NO_DATA)
        self.assertEqual(lc.read_application(text)["conclusion"], lc.NO_DATA)

    def test_reopen_and_repeat_done_keep_user_text(self) -> None:
        self.run_ingest()
        task = self.tasks()[0]
        self.fill(task)
        before = self.body(task)

        for command in ("готово", "список", "готово", "готово"):
            dispatch.advance_task(command, task.stem, now=datetime.datetime(2026, 10, 6, 12))

        self.assertEqual(self.body(task), before, "тело карточки не меняется")
        application = lc.read_application(task.read_text(encoding="utf-8"))
        self.assertEqual(application["observed"], "стало видно X")
        self.assertEqual(application["conclusion"], "мой вывод Y")

    def test_negative_result_is_user_text_not_verdict(self) -> None:
        self.run_ingest()
        task = self.tasks()[0]
        self.fill(task, observed="ничего не изменилось", conclusion="гипотеза не подтвердилась")

        row = lc.application_rows(self.vault)['rows'][0]

        self.assertEqual(row["observed"], "ничего не изменилось")
        self.assertEqual(row["conclusion"], "гипотеза не подтвердилась")
        self.assertNotIn("эффектив", json.dumps(row, ensure_ascii=False).lower())

    def test_repeat_ingest_preserves_user_edits(self) -> None:
        self.run_ingest()
        task = self.tasks()[0]
        self.fill(task)
        before = task.read_bytes()

        self.write_queue(URL)
        with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            learn.process_queue()

        self.assertEqual(task.read_bytes(), before, "повтор не переписывает карточку")
        self.assertEqual(len(self.tasks()), 1, "вторая задача не создаётся")
        self.assertEqual(self.queue_urls(), [], "пара готова — очередь подтверждена")


class ReviewTests(unittest.TestCase):
    """Пункт 5: детерминированный обзор применения."""

    def setUp(self) -> None:
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.tasks_dir = self.vault / "TaskNotes" / "Tasks"
        self.tasks_dir.mkdir(parents=True)
        self.hub = self.vault / learn.HUB / AREA / f"{learn.NOTE_DIR_PREFIX} - авто конспект"
        self.hub.mkdir(parents=True)
        self.today = datetime.date(2026, 10, 6)          # вторник

    PAIR = "pairfixture0001"

    def note(self, name: str = "Разбор видео", pair: str = None) -> Path:
        path = self.hub / f"{name}.md"
        pair = pair or self.PAIR
        path.write_text(
            "---\nconnections:\n" + f"reference: {URL}\ngenerator: dusk\n"
            f"{lc.MARK}\n{lc.PAIR_FIELD}: {pair}\n"
            "type: literature_note\n---\n\n# " + name + "\n\n---\n"
            f"_Источник: {URL}_\n", encoding="utf-8")
        return path

    def card(self, name: str, note_rel: str, *, status="open", created="2026-10-06T10:00",
             completed="", observed=lc.NO_DATA, conclusion=lc.NO_DATA,
             criterion="Признак: видно результат шага", marker=True, pair=None) -> Path:
        path = self.tasks_dir / f"{name}.md"
        text = (
            "---\n"
            f'title: "{name}"\n'
            f"status: {status}\n"
            "priority: normal\n"
            f"created: {created}\n"
            "scheduled: 2026-10-06\n"
            + (f"{lc.MARK}\n" if marker else "")
            + (f"{lc.PAIR_FIELD}: {pair or self.PAIR}\n" if marker else "")
            + (f"completedDate: {completed}\n" if completed else "")
            + f'source: "{note_rel}"\n'
            "---\n\n"
            f"# {name}\n\n"
            f"{lc.CRITERION_HEADING}\n\n{criterion} — гипотеза\n\n"
            f"{lc.TRIED_HEADING}\n\n{lc.NO_DATA}\n\n"
            f"{lc.OBSERVED_HEADING}\n\n{observed}\n\n"
            f"{lc.CONCLUSION_HEADING}\n\n{conclusion}\n\n"
        )
        path.write_text(text, encoding="utf-8")
        return path

    def rows(self):
        return lc.application_rows(self.vault, today=self.today)

    def test_done_without_observation(self) -> None:
        note = self.note()
        self.card("Шаг", note.relative_to(self.vault).as_posix(),
                  status="done", completed="2026-10-06")

        review = self.rows()

        self.assertEqual(len(review["rows"]), 1)
        row = review["rows"][0]
        self.assertTrue(row["done"])
        self.assertFalse(row["has_observation"])
        self.assertEqual(row["observed_state"], lc.NO_DATA)

    def test_observation_without_conclusion(self) -> None:
        note = self.note()
        self.card("Шаг", note.relative_to(self.vault).as_posix(),
                  observed="стало видно X")

        row = self.rows()["rows"][0]

        self.assertTrue(row["has_observation"])
        self.assertEqual(row["conclusion"], lc.NO_DATA, "пустой вывод так и показывается")
        self.assertFalse(row["has_conclusion"])

    def test_positive_and_negative_are_user_text(self) -> None:
        note = self.note()
        self.card("Шаг", note.relative_to(self.vault).as_posix(),
                  observed="выросло", conclusion="сработало")

        row = self.rows()["rows"][0]

        self.assertEqual(row["observed"], "выросло")
        self.assertEqual(row["conclusion"], "сработало")

    def test_old_open_card_is_active_now(self) -> None:
        """Контракт обзора уточнён на проходе 2.

        Раньше обзор фильтровал карточки по дате создания, из-за чего открытая
        пара исчезала при смене недели, а закрытая на этой неделе старая карточка
        не попадала в завершения. Теперь: завершения — по строгому
        `completedDate` периода, открытые пары — как «активные сейчас».
        """
        note = self.note()
        self.card("Старый шаг", note.relative_to(self.vault).as_posix(),
                  created="2026-09-28T10:00")

        review = self.rows()

        self.assertEqual(review["counts"]["done"], 0, "старая пара не начисляет выполнение")
        self.assertEqual(len(review["rows"]), 1)
        self.assertTrue(review["rows"][0]["active"], "открытая пара показана как активная сейчас")
        self.assertEqual(review["period"]["start"], "2026-10-05")

    def test_old_completion_outside_period_is_not_counted(self) -> None:
        note = self.note()
        self.card("Закрытая давно", note.relative_to(self.vault).as_posix(),
                  status="done", created="2026-09-01T10:00", completed="2026-09-15")

        review = self.rows()

        self.assertEqual(review["rows"], [], "старое завершение вне периода не показываем")
        self.assertEqual(review["counts"]["done"], 0)

    def test_prior_created_current_completed_counts(self) -> None:
        """Регрессия: завершение считается по completedDate, а не по created."""
        note = self.note()
        self.card("Старая, но закрыта сейчас", note.relative_to(self.vault).as_posix(),
                  status="done", created="2026-09-29T10:00", completed="2026-10-06")

        review = self.rows()

        self.assertEqual(review["counts"]["done"], 1,
                         "карточка создана раньше, закрыта на этой неделе — выполнена")

    def test_future_completion_is_not_counted_done(self) -> None:
        note = self.note()
        self.card("Будущий", note.relative_to(self.vault).as_posix(),
                  status="done", completed="2026-10-20")

        row = self.rows()["rows"][0]

        self.assertFalse(row["done"], "закрытие будущей датой не считается выполнением")

    def test_deleted_note_is_unknown_not_substituted(self) -> None:
        other = self.note("Другой разбор")
        note = self.note("Исчезнувший")
        rel = note.relative_to(self.vault).as_posix()
        note.unlink()
        self.card("Шаг", rel)

        row = self.rows()["rows"][0]

        self.assertFalse(row["source_ok"], "чужой конспект не подставляется")
        self.assertIsNone(row["source_link"])
        self.assertNotIn(other.stem, json.dumps(row, ensure_ascii=False))

    def test_same_names_in_different_folders_use_full_paths(self) -> None:
        first = self.note("Разбор")
        second_dir = self.vault / learn.HUB / "Другая область" / f"{learn.NOTE_DIR_PREFIX} - авто конспект"
        second_dir.mkdir(parents=True)
        second = second_dir / "Разбор.md"
        second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
        self.card("Шаг", second.relative_to(self.vault).as_posix())

        row = self.rows()["rows"][0]

        self.assertTrue(row["source_ok"])
        self.assertIn("Другая область", row["source_link"])

    def test_unmarked_cards_are_not_in_the_review(self) -> None:
        note = self.note()
        self.card("Старая карточка", note.relative_to(self.vault).as_posix(), marker=False)

        self.assertEqual(self.rows()["rows"], [], "старые карточки не мигрируются")

    def test_no_cards_says_no_data(self) -> None:
        review = self.rows()
        self.assertEqual(review["rows"], [])
        self.assertIn("данн", review["summary"].lower())

    def test_counts_report_no_data_separately_from_zero(self) -> None:
        note = self.note()
        rel = note.relative_to(self.vault).as_posix()
        self.card("Выполнен", rel, status="done", completed="2026-10-06")
        self.card("С наблюдением", rel, observed="есть")
        self.card("Пустой", rel)

        counts = self.rows()["counts"]

        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["done"], 1)
        self.assertEqual(counts["observed"], 1)
        self.assertEqual(counts["no_data"], 2)


class StatusBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        (self.vault / "TaskNotes" / "Tasks").mkdir(parents=True)
        (self.vault / learn.HUB / AREA).mkdir(parents=True)
        for patcher in (
            patch.object(__import__("status"), "_ask_model", lambda m: {"rocks": [], "today": [], "cut": []}),
            patch.object(__import__("status"), "_market_section", lambda vault: []),
            patch.object(__import__("status").someday, "count", lambda vault=None: 0),
            patch.object(__import__("status").someday, "with_triggers", lambda vault=None: []),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def render(self) -> str:
        import status
        with patch.dict("os.environ", {"VAULT_PATH": str(self.vault)}):
            return status.build_status()

    def test_page_lists_applications_or_says_no_data(self) -> None:
        page = self.render()

        self.assertIn(lc.SECTION_TITLE, page)
        self.assertIn("данн", page.lower())

    def test_page_shows_done_and_observation_states(self) -> None:
        tasks = self.vault / "TaskNotes" / "Tasks"
        notes = self.vault / learn.HUB / AREA / f"{learn.NOTE_DIR_PREFIX} - авто конспект"
        notes.mkdir(parents=True, exist_ok=True)
        note = notes / "Разбор.md"
        note.write_text("---\nconnections:\n" + f"reference: {URL}\ngenerator: dusk\n"
                        f"{lc.MARK}\n{lc.PAIR_FIELD}: pairpage0001\n"
                        "---\n\n# Разбор\n", encoding="utf-8")
        rel = note.relative_to(self.vault).as_posix()
        today = datetime.date.today()
        (tasks / "Шаг.md").write_text(
            "---\n" + f'title: "Шаг"\nstatus: done\ncreated: {today}T09:00\n'
            f"completedDate: {today}\nsource: \"{rel}\"\n{lc.MARK}\n"
            f"{lc.PAIR_FIELD}: pairpage0001\n---\n\n# Шаг\n\n"
            f"{lc.OBSERVED_HEADING}\n\nстало видно X\n\n{lc.CONCLUSION_HEADING}\n\nмой вывод Y\n",
            encoding="utf-8")

        page = self.render()

        self.assertIn("выполнено", page.lower())
        self.assertIn("мой вывод Y", page, "вывод пользователя показывается как текст")
        self.assertIn("[[", page, "есть точные ссылки")


class CrashRetryTests(Stage2ABase):
    def test_crash_before_task_retry_builds_the_pair_once(self) -> None:
        source = self.source()
        with patch.object(learn, "obtain_source", return_value=(source, "Fixture")), \
                patch.object(learn, "_ask_model", side_effect=fake_model()), \
                patch.object(learn, "_create_task", side_effect=OSError("сбой задачи")):
            with self.assertRaises(OSError):
                learn.ingest_result(URL)
        self.assertEqual(len(self.tasks()), 0)

        ok, message = self.run_ingest()

        self.assertTrue(ok, message)
        self.assertEqual(len(self.our_notes()), 1)
        self.assertEqual(len(self.tasks()), 1)
        note_text = self.our_notes()[-1].read_text(encoding="utf-8")
        self.assertIn(self.tasks()[0].stem, note_text)

    def test_crash_before_note_block_retry_repairs_without_touching_user_text(self) -> None:
        source = self.source()
        with patch.object(learn, "obtain_source", return_value=(source, "Fixture")), \
                patch.object(learn, "_ask_model", side_effect=fake_model()), \
                patch.object(learn, "link_note_to_task", side_effect=RuntimeError("обрыв")):
            with self.assertRaises(RuntimeError):
                learn.ingest_result(URL)
        task = self.tasks()[0]
        text = task.read_text(encoding="utf-8").replace(
            f"{lc.OBSERVED_HEADING}\n\n{lc.NO_DATA}", f"{lc.OBSERVED_HEADING}\n\nмоё наблюдение")
        task.write_text(text, encoding="utf-8")
        before = task.read_bytes()
        note = self.our_notes()[-1]
        task_rel = task.relative_to(self.vault).as_posix()[:-3]
        self.assertNotIn(f"[[{task_rel}]]", note.read_text(encoding="utf-8"),
                         "обратной ссылки ещё нет")

        self.write_queue(URL)
        with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            learn.process_queue()

        self.assertIn(f"[[{task_rel}]]", note.read_text(encoding="utf-8"),
                      "повтор восстанавливает обратную ссылку")
        self.assertEqual(task.read_bytes(), before, "текст пользователя не тронут")
        self.assertEqual(self.queue_urls(), [])

    def test_make_task_false_creates_no_task_and_no_block(self) -> None:
        ok, _ = self.run_ingest(make_task=False)

        self.assertTrue(ok)
        self.assertEqual(self.tasks(), [])
        note = self.our_notes()[-1].read_text(encoding="utf-8")
        self.assertNotIn(lc.SECTION_TITLE, note)
        self.assertFalse(learn.frontmatter.get(note, "applyTask"))


class Pass2RegressionTests(Stage2ABase):
    """Регрессии прохода 2: идентичность пары, границы, критерий, обзор."""

    def pair_files(self):
        ok, message = self.run_ingest()
        self.assertTrue(ok, message)
        return self.our_notes()[-1], self.tasks()[0]

    def retry(self) -> None:
        self.write_queue(URL)
        with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            learn.process_queue()

    def test_renamed_task_updates_the_note_block(self) -> None:
        note, task = self.pair_files()
        renamed = task.with_name("Переименованная карточка.md")
        task.rename(renamed)
        text = renamed.read_text(encoding="utf-8").replace(
            f"{lc.OBSERVED_HEADING}\n\n{lc.NO_DATA}",
            f"{lc.OBSERVED_HEADING}\n\nмоё наблюдение")
        renamed.write_text(text, encoding="utf-8")

        self.retry()

        new_rel = renamed.relative_to(self.vault).as_posix()[:-3]
        self.assertIn(f"[[{new_rel}]]", note.read_text(encoding="utf-8"),
                      "блок связи указывает на новое имя карточки")
        self.assertIn("моё наблюдение", renamed.read_text(encoding="utf-8"))
        self.assertEqual(len(self.tasks()), 1, "вторая карточка не создаётся")
        self.assertEqual(self.queue_urls(), [])

    def test_renamed_note_keeps_the_pair_and_user_text(self) -> None:
        """Исправлено на проходе 3: rename чинит служебные ссылки, не трогая поля.

        Прежняя версия требовала неизменности всего файла карточки и тем самым
        закрепляла ошибку: после переименования конспекта `source` и полная
        обратная ссылка оставались на исчезнувший путь. Проверка пользовательских
        данных сохранена и усилена: текст наблюдения обязан остаться, а
        служебные ссылки — указывать на новое имя конспекта.
        """
        note, task = self.pair_files()
        text = task.read_text(encoding="utf-8").replace(
            f"{lc.OBSERVED_HEADING}\n\n{lc.NO_DATA}",
            f"{lc.OBSERVED_HEADING}\n\nнаблюдение пользователя")
        task.write_text(text, encoding="utf-8")
        renamed = note.with_name("Переименованный конспект.md")
        note.rename(renamed)

        self.retry()

        self.assertEqual(len(self.tasks()), 1, "переименование конспекта не создаёт вторую задачу")
        after = task.read_text(encoding="utf-8")
        self.assertIn("наблюдение пользователя", after, "поле пользователя не теряется")
        rel = renamed.relative_to(self.vault).as_posix()
        self.assertEqual(learn.frontmatter.get(after, "source"), rel,
                         "source чинится на новое имя конспекта")
        self.assertIn(f"[[{rel[:-3]}]]", after, "полная обратная ссылка восстановлена")
        self.assertEqual(self.queue_urls(), [], "пара найдена по идентичности")

    def test_duplicate_pair_id_is_refused(self) -> None:
        note, task = self.pair_files()
        task.with_name("Дубликат.md").write_bytes(task.read_bytes())

        ok, reason = learn.ensure_application_pair(self.vault, note, "шаг", True)

        self.assertFalse(ok, "две карточки с одной идентичностью — не пара")
        self.assertIn("несколько", reason)

    def test_replaced_source_is_not_confirmed(self) -> None:
        note, task = self.pair_files()
        note.write_text("# Ручная заметка\n", encoding="utf-8")
        created = learn.frontmatter.get(task.read_text(encoding="utf-8"), "created")

        row = lc.application_rows(self.vault, today=lc.weekly_plan.parse_task_date(created))["rows"][0]

        self.assertFalse(row["source_ok"], "тот же путь не доказывает источник")
        self.assertIsNone(row["source_link"])

    def test_deleted_counterpart_is_not_recreated(self) -> None:
        """Напарник удалён после готовой пары: замену не создаём и не подтверждаем."""
        note, task = self.pair_files()
        task.unlink()

        self.retry()

        self.assertEqual(self.tasks(), [], "замена удалённой карточки не создаётся")
        self.assertEqual(len(self.our_notes()), 1, "вторая заметка не публикуется")
        self.assertIn(URL, self.queue_urls(), "успех без напарника не подтверждается")

    def test_crash_before_task_still_completes_the_pair(self) -> None:
        """Прерванное первое создание отличается от удалённого напарника."""
        source = self.source()
        with patch.object(learn, "obtain_source", return_value=(source, "Fixture")), \
                patch.object(learn, "_ask_model", side_effect=fake_model()), \
                patch.object(learn, "_create_task", side_effect=OSError("сбой")):
            with self.assertRaises(OSError):
                learn.ingest_result(URL)
        self.assertEqual(self.tasks(), [], "задача не создана до сбоя")
        note = self.our_notes()[-1]
        self.assertNotIn(lc.NOTE_BLOCK_MARK, note.read_text(encoding="utf-8"),
                         "блока связи ещё нет — пара не завершалась")

        ok, message = self.run_ingest()

        self.assertTrue(ok, message)
        self.assertEqual(len(self.tasks()), 1, "пара достраивается на повторе")
        self.assertIn(lc.NOTE_BLOCK_MARK, note.read_text(encoding="utf-8"))
        self.assertEqual(self.queue_urls(), [])

    def test_symlinked_tasks_root_is_refused(self) -> None:
        import tempfile
        ok, message = self.run_ingest(make_task=False)
        self.assertTrue(ok, message)
        note = self.our_notes()[-1]
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            tasks = self.vault / "TaskNotes" / "Tasks"
            if tasks.exists():
                tasks.rmdir()
            tasks.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(external, tasks)

            with self.assertRaises((ValueError, OSError)):
                learn._create_task(self.vault, "шаг", note, datetime.datetime(2026, 10, 6, 12))

            self.assertEqual(list(external.iterdir()), [], "наружу ничего не записано")

    def test_symlinked_card_is_not_confirmed(self) -> None:
        import tempfile
        note, task = self.pair_files()
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / task.name
            external.write_bytes(task.read_bytes())
            task.unlink()
            os.symlink(external, task)
            before = note.read_bytes()

            ok, reason = learn.ensure_application_pair(self.vault, note, "шаг", True)

            self.assertFalse(ok, "внешняя карточка не подтверждает пару")
            self.assertEqual(note.read_bytes(), before)

    def test_linking_preserves_user_bytes(self) -> None:
        note, task = self.pair_files()
        original = note.read_bytes().split(lc.NOTE_BLOCK_MARK.encode(), 1)[0] + b"\n\n\n\n"
        note.write_bytes(original)

        learn.link_note_to_task(self.vault, note, task)

        self.assertTrue(note.read_bytes().startswith(original),
                        "добавление блока не срезает пользовательские байты")

    def test_injected_criterion_cannot_fill_observation(self) -> None:
        injected = "критерий\n\n" + lc.OBSERVED_HEADING + "\n\nвыдуманный результат"

        def model(prompt):
            if is_synthesis(prompt):
                return json.dumps({**SYNTHESIS, "criterion": injected}, ensure_ascii=False)
            ids = re.findall(r"\[(seg-\d+)", prompt)
            return json.dumps({"points": [{"text": "тезис", "segments": ids[:1]}], "quotes": []})

        ok, message = self.run_ingest(model=model)

        self.assertTrue(ok, message)
        application = lc.read_application(self.tasks()[0].read_text(encoding="utf-8"))
        self.assertFalse(application["has_observation"],
                         "критерий — предложение, а не фактическое наблюдение")
        self.assertEqual(application["observed"], lc.NO_DATA)

    def test_user_observation_with_markdown_and_pipe_keeps_meaning(self) -> None:
        ok, message = self.run_ingest()
        self.assertTrue(ok, message)
        task = self.tasks()[0]
        observed = "первая строка\n**важное** | вторая\n- пункт списка"
        text = task.read_text(encoding="utf-8").replace(
            f"{lc.OBSERVED_HEADING}\n\n{lc.NO_DATA}",
            f"{lc.OBSERVED_HEADING}\n\n{observed}")
        text = text.replace(f"{lc.CONCLUSION_HEADING}\n\n{lc.NO_DATA}",
                            f"{lc.CONCLUSION_HEADING}\n\nвывод пользователя")
        task.write_text(text, encoding="utf-8")
        created = learn.frontmatter.get(text, "created")
        today = lc.weekly_plan.parse_task_date(created)

        row = lc.application_rows(self.vault, today=today)["rows"][0]
        section = lc.render_section(self.vault, today=today)
        table = [line for line in section if line.startswith("| ")]

        self.assertIn("**важное**", row["observed"], "смысл сохранён")
        self.assertEqual(row["conclusion"], "вывод пользователя")
        self.assertTrue(all(len(re.split(r"(?<!\\)\|", line)) == 6 for line in table[1:]),
                        "вертикальная черта не ломает таблицу")
        self.assertTrue(any("\\|" in line for line in table), "черта экранирована")


class Pass3RegressionTests(Stage2ABase):
    """Регрессии прохода 3: границы блока, оба rename, дубликаты, containment."""

    def pair_files(self):
        ok, message = self.run_ingest()
        self.assertTrue(ok, message)
        return self.our_notes()[-1], self.tasks()[0]

    def retry(self) -> None:
        self.write_queue(URL)
        with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            learn.process_queue()

    def test_repair_preserves_prefix_and_suffix_around_block(self) -> None:
        note, task = self.pair_files()
        prefix = note.read_bytes().split(lc.NOTE_BLOCK_MARK.encode(), 1)[0]
        suffix = b"\r\nUSER TEXT AFTER BLOCK\r\n"
        note.write_bytes(note.read_bytes() + suffix)
        renamed = task.with_name("Renamed card.md")
        task.rename(renamed)

        self.retry()

        raw = note.read_bytes()
        self.assertTrue(raw.startswith(prefix), "prefix до блока сохраняется")
        self.assertTrue(raw.endswith(suffix), "suffix после блока сохраняется")
        self.assertIn(f"[[{renamed.relative_to(self.vault).as_posix()[:-3]}]]".encode(), raw,
                      "блок указывает на новое имя задачи")

    def test_crlf_note_prefix_survives_repair(self) -> None:
        note, task = self.pair_files()
        raw = note.read_bytes().replace(b"\n", b"\r\n")
        prefix = raw.split(lc.NOTE_BLOCK_MARK.encode(), 1)[0]
        suffix = b"\r\nUSER SUFFIX\r\n"
        note.write_bytes(raw + suffix)
        task.rename(task.with_name("Renamed CRLF.md"))

        self.retry()

        after = note.read_bytes()
        self.assertTrue(after.startswith(prefix), "CRLF-префикс не нормализуется")
        self.assertTrue(after.endswith(suffix), "CRLF-suffix после блока не теряется")

    def test_task_rename_repairs_both_directions(self) -> None:
        note, task = self.pair_files()
        renamed = task.with_name("Task renamed.md")
        task.rename(renamed)

        self.retry()

        note_rel = note.relative_to(self.vault).as_posix()
        task_rel = renamed.relative_to(self.vault).as_posix()
        task_text = renamed.read_text(encoding="utf-8")
        self.assertIn(f"[[{task_rel[:-3]}]]", note.read_text(encoding="utf-8"))
        self.assertEqual(learn.frontmatter.get(task_text, "source"), note_rel)
        self.assertIn(f"[[{note_rel[:-3]}]]", task_text)
        self.assertEqual([p.name for p in self.tasks()], [renamed.name])

    def test_note_rename_repairs_both_directions(self) -> None:
        note, task = self.pair_files()
        renamed = note.with_name("Note renamed.md")
        note.rename(renamed)

        self.retry()

        note_rel = renamed.relative_to(self.vault).as_posix()
        task_rel = task.relative_to(self.vault).as_posix()
        task_text = task.read_text(encoding="utf-8")
        self.assertEqual(learn.frontmatter.get(task_text, "source"), note_rel)
        self.assertIn(f"[[{note_rel[:-3]}]]", task_text)
        self.assertIn(f"[[{task_rel[:-3]}]]", renamed.read_text(encoding="utf-8"))
        self.assertEqual(len(self.tasks()), 1)

    def test_duplicate_owned_note_does_not_acknowledge(self) -> None:
        note, task = self.pair_files()
        note.with_name("Duplicate note.md").write_bytes(note.read_bytes())
        self.write_queue(URL)
        before = note.read_bytes()

        with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            learn.process_queue()

        self.assertIn(URL, self.queue_urls(), "неоднозначность не подтверждает очередь")
        self.assertEqual(note.read_bytes(), before, "дубликат не переписывается")
        self.assertEqual(len(self.tasks()), 1, "третья карточка не создаётся")

    def test_conflicting_source_is_refused_without_repair(self) -> None:
        note, task = self.pair_files()
        wrong = self.vault / "wrong.md"
        wrong.write_text("# ручная заметка\n", encoding="utf-8")
        changed, valid = learn.frontmatter.set_field(
            task.read_text(encoding="utf-8"), "source", '"wrong.md"')
        self.assertTrue(valid)
        task.write_text(changed, encoding="utf-8")
        before_note, before_task = note.read_bytes(), task.read_bytes()

        ok, reason = learn.ensure_application_pair(self.vault, note, "шаг", True)

        self.assertFalse(ok, "конфликтующий source — не подтверждённая пара")
        self.assertEqual(note.read_bytes(), before_note)
        self.assertEqual(task.read_bytes(), before_task, "конфликт не переписывается")
        self.assertEqual(len(self.tasks()), 1)

    def test_external_note_is_rejected_without_read(self) -> None:
        import tempfile
        note, task = self.pair_files()
        with tempfile.TemporaryDirectory() as directory:
            ext = Path(directory) / note.name
            ext.write_bytes(note.read_bytes())
            reads = []
            original = Path.read_bytes

            def watched(path, *args, **kwargs):
                if path == ext:
                    reads.append(path)
                return original(path, *args, **kwargs)

            with patch.object(Path, "read_bytes", watched):
                ok, reason = learn.ensure_application_pair(self.vault, ext, "шаг", True)
            self.assertFalse(ok)
            self.assertTrue(reason, "отказ обязан иметь причину")
            self.assertEqual(reads, [], "внешний конспект не читается")

    def test_external_task_symlink_is_rejected_without_read(self) -> None:
        import tempfile
        note, task = self.pair_files()
        with tempfile.TemporaryDirectory() as directory:
            ext = Path(directory) / task.name
            ext.write_bytes(task.read_bytes())
            task.unlink()
            task.symlink_to(ext)
            reads = []
            original = Path.read_bytes

            def watched(path, *args, **kwargs):
                if path == task:
                    reads.append(path)
                return original(path, *args, **kwargs)

            with patch.object(Path, "read_bytes", watched):
                ok, reason = learn.ensure_application_pair(self.vault, note, "шаг", True)
            self.assertFalse(ok)
            self.assertTrue(reason, "отказ обязан иметь причину")
            self.assertEqual(reads, [], "внешняя карточка не читается")

    def test_fenced_criterion_cannot_hide_observation(self) -> None:
        criterion = "```text\nпредложенный критерий\n```"

        def model(prompt):
            if is_synthesis(prompt):
                return json.dumps({**SYNTHESIS, "criterion": criterion}, ensure_ascii=False)
            ids = re.findall(r"\[(seg-\d+)", prompt)
            return json.dumps({"points": [{"text": "тезис", "segments": ids[:1]}],
                               "quotes": []})

        ok, message = self.run_ingest(model=model)

        self.assertTrue(ok, message)
        task = self.tasks()[0]
        self.assertNotIn("```", task.read_text(encoding="utf-8"),
                         "fence не попадает в карточку")
        text = task.read_text(encoding="utf-8").replace(
            f"{lc.OBSERVED_HEADING}\n\n{lc.NO_DATA}",
            f"{lc.OBSERVED_HEADING}\n\nпользовательское наблюдение")
        application = lc.read_application(text)
        self.assertTrue(application["has_observation"],
                        "разметка критерия не прячет поле наблюдения")
        self.assertEqual(application["observed"], "пользовательское наблюдение")

    def test_marked_pair_without_identity_is_refused(self) -> None:
        note, task = self.pair_files()
        text, valid = learn.frontmatter.set_field(
            note.read_text(encoding="utf-8"), lc.PAIR_FIELD, None)
        self.assertTrue(valid)
        note.write_text(text, encoding="utf-8")

        ok, reason = learn.ensure_application_pair(self.vault, note, "шаг", True)

        self.assertFalse(ok, "маркированная пара без ID не считается legacy")
        self.assertTrue(reason)
        self.assertEqual(len(self.tasks()), 1, "замена не создаётся")

    def test_has_note_block_checks_only_the_service_block(self) -> None:
        note, task = self.pair_files()
        task_rel = task.relative_to(self.vault).as_posix()
        text = note.read_text(encoding="utf-8")
        self.assertTrue(lc.has_note_block(text, task_rel))
        broken = text.replace(f"- Задача: [[{task_rel[:-3]}]]", "- Задача: [[другая]]")
        broken += f"\n[[{task_rel[:-3]}]]\n"       # ссылка вне блока не считается
        self.assertFalse(lc.has_note_block(broken, task_rel))

    def test_unknown_block_markup_refuses_without_write(self) -> None:
        note, task = self.pair_files()
        note.write_text(note.read_text(encoding="utf-8").replace(
            lc.SECTION_TITLE, "## другое", 1), encoding="utf-8")
        before = note.read_bytes()
        with self.assertRaises(ValueError):
            learn.link_note_to_task(self.vault, note, task)
        self.assertEqual(note.read_bytes(), before, "неизвестная разметка не перезаписывается")

    def test_two_markers_refuse_without_write(self) -> None:
        note, task = self.pair_files()
        note.write_text(note.read_text(encoding="utf-8") + f"\n{lc.NOTE_BLOCK_MARK}\n",
                        encoding="utf-8")
        before = note.read_bytes()
        with self.assertRaises(ValueError):
            learn.link_note_to_task(self.vault, note, task)
        self.assertEqual(note.read_bytes(), before)


class Pass4RegressionTests(Stage2ABase):
    """Регрессии прохода 4: точные полные пути и сохранность пользовательских ссылок."""

    def pair_files(self):
        ok, message = self.run_ingest()
        self.assertTrue(ok, message)
        return self.our_notes()[-1], self.tasks()[0]

    def retry(self) -> None:
        self.write_queue(URL)
        with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            learn.process_queue()

    def test_full_backlink_to_other_same_named_note_is_conflict(self) -> None:
        note, task = self.pair_files()
        other = self.vault / "Other" / note.name
        other.parent.mkdir()
        other.write_text("# Different note\n", encoding="utf-8")
        rel = note.relative_to(self.vault).as_posix()
        other_rel = other.relative_to(self.vault).as_posix()
        text = task.read_text(encoding="utf-8").replace(
            f"Из видео: [[{rel[:-3]}]]", f"Из видео: [[{other_rel[:-3]}]]")
        task.write_text(text, encoding="utf-8")
        before = task.read_bytes()

        ok, reason = learn.ensure_application_pair(self.vault, note, "шаг", True)

        self.assertFalse(ok, "совпадение basename — не точный полный backlink")
        self.assertTrue(reason)
        self.assertEqual(task.read_bytes(), before, "конфликт не переписывается")

    def test_repair_preserves_user_observation_link_like_text(self) -> None:
        note, task = self.pair_files()
        observed = ("User example: Из видео: [[My own linked note]]\n"
                    "_(имя конспекта: [[My own linked note]])_")
        text = task.read_text(encoding="utf-8").replace(
            f"{lc.OBSERVED_HEADING}\n\n{lc.NO_DATA}",
            f"{lc.OBSERVED_HEADING}\n\n{observed}")
        task.write_text(text, encoding="utf-8")
        before = lc.read_application(text)
        renamed = note.with_name("Renamed exact note.md")
        note.rename(renamed)

        self.retry()

        after_text = task.read_text(encoding="utf-8")
        self.assertEqual(lc.read_application(after_text)["observed"], before["observed"],
                         "текст пользователя с похожими ссылками не меняется")
        self.assertIn("[[My own linked note]]", after_text)
        self.assertEqual(learn.frontmatter.get(after_text, "source"),
                         renamed.relative_to(self.vault).as_posix(),
                         "служебный source всё равно чинится")
        self.assertEqual(len(self.tasks()), 1)

    def test_absolute_source_is_conflict_without_repair(self) -> None:
        note, task = self.pair_files()
        rel = note.relative_to(self.vault).as_posix()
        text, valid = learn.frontmatter.set_field(
            task.read_text(encoding="utf-8"), "source", "/outside-vault/Old note.md")
        self.assertTrue(valid)
        text = text.replace(f"Из видео: [[{rel[:-3]}]]",
                            "Из видео: [[/outside-vault/Old note]]")
        task.write_text(text, encoding="utf-8")
        before = task.read_bytes()

        ok, reason = learn.ensure_application_pair(self.vault, note, "шаг", True)

        self.assertFalse(ok, "абсолютный прежний source — не harmless rename")
        self.assertTrue(reason)
        self.assertEqual(task.read_bytes(), before)

    def test_traversal_source_is_conflict_without_repair(self) -> None:
        note, task = self.pair_files()
        rel = note.relative_to(self.vault).as_posix()
        text, valid = learn.frontmatter.set_field(
            task.read_text(encoding="utf-8"), "source", "../outside.md")
        self.assertTrue(valid)
        text = text.replace(f"Из видео: [[{rel[:-3]}]]", "Из видео: [[../outside]]")
        task.write_text(text, encoding="utf-8")
        before = task.read_bytes()

        ok, reason = learn.ensure_application_pair(self.vault, note, "шаг", True)

        self.assertFalse(ok, "traversal за валт — не rename")
        self.assertEqual(task.read_bytes(), before)

    def test_ambiguous_service_span_refuses_without_write(self) -> None:
        note, task = self.pair_files()
        text = task.read_text(encoding="utf-8").replace(
            "_(имя конспекта:", "_(другое имя:", 1)
        task.write_text(text, encoding="utf-8")
        before_note, before_task = note.read_bytes(), task.read_bytes()

        ok, reason = learn.ensure_application_pair(self.vault, note, "шаг", True)

        self.assertFalse(ok, "неоднозначный служебный span — отказ")
        self.assertTrue(reason)
        self.assertEqual(note.read_bytes(), before_note)
        self.assertEqual(task.read_bytes(), before_task)

    def test_legacy_action_task_is_not_marked_without_id(self) -> None:
        ok, message = self.run_ingest(make_task=False)
        self.assertTrue(ok, message)
        note = self.our_notes()[0]
        text = note.read_text(encoding="utf-8")
        for field in ("duskApply", lc.PAIR_FIELD):
            text, valid = learn.frontmatter.set_field(text, field, None)
            self.assertTrue(valid)
        note.write_text(text, encoding="utf-8")
        self.write_queue(URL)

        with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            learn.process_queue()

        tasks = self.tasks()
        self.assertEqual(len(tasks), 1)
        task_text = tasks[0].read_text(encoding="utf-8")
        self.assertNotIn("duskApply: 2b", task_text,
                         "legacy-задача не помечается 2Б без идентичности пары")
        self.assertEqual(learn.frontmatter.get(task_text, lc.PAIR_FIELD), "")
        self.assertEqual(self.queue_urls(), [], "legacy-пара подтверждена без маркера")

    def test_windows_drive_source_is_conflict_without_repair(self) -> None:
        note, task = self.pair_files()
        rel = note.relative_to(self.vault).as_posix()
        original = task.read_text(encoding="utf-8")
        for source, backlink in (("C:/outside/old.md", "C:/outside/old"),
                                 ("D:\\outside\\old.md", "D:\\outside\\old")):
            with self.subTest(source=source):
                text, valid = learn.frontmatter.set_field(original, "source", source)
                self.assertTrue(valid)
                text = text.replace(f"Из видео: [[{rel[:-3]}]]",
                                    f"Из видео: [[{backlink}]]")
                task.write_text(text, encoding="utf-8")
                before = task.read_bytes()

                ok, reason = learn.ensure_application_pair(self.vault, note, "шаг", True)

                self.assertFalse(ok, "drive-prefixed path — не vault-relative rename")
                self.assertTrue(reason)
                self.assertEqual(task.read_bytes(), before, "конфликт не переписывается")
        task.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
