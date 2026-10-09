#!/usr/bin/env python3
"""Регрессии второго прохода: потеря результата и ложный успех (пункты 1, 2, 3, 6, 7, 8).

Тесты написаны до исправлений: каждый падает на отклонённом патче и проходит после.
Изоляция: временный валт, mock внешних границ, никаких сетевых вызовов.
"""

from __future__ import annotations

import datetime
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import dispatch
import learn
import queue_store
import weekly_plan

from test_learn_queue import AREA, QueueTestCase, fake_distill

URL_FIRST = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
URL_SECOND = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
ACTION = "сделать одно действие"


class PartialNoteTests(QueueTestCase):
    """Пункт 1: незавершённый конспект не подтверждает ссылку."""

    def make_partial_note(self, url: str, name: str = "частичный конспект.md") -> Path:
        """Недописанный НАШ конспект: есть маркер владения, нет футера.

        Контракт уточнён в четвёртом проходе: починке подлежат только файлы,
        созданные Dusk (маркер `generator: dusk` или наш футер). Чужая заметка с
        тем же `reference` сохраняется как есть — это отдельный тест в
        `test_stage1_pass4.py`.
        """
        hub = self.vault / learn.HUB / AREA
        hub.mkdir(parents=True, exist_ok=True)
        path = hub / name
        path.write_text(
            "---\nconnections:\n"
            f"reference: {url}\n"
            "generator: dusk\n"
            "tags:\n  - literature_note\n"
            "type: literature_note\n",
            encoding="utf-8",
        )
        return path

    def test_partial_note_does_not_confirm_url(self) -> None:
        self.make_partial_note(URL_FIRST)
        self.write_queue(URL_FIRST)

        with patch.object(learn, "fetch_subtitles", return_value=(None, "нет субтитров")):
            report = learn.process_queue()

        self.assertIn(URL_FIRST, self.queue_urls(),
                      "частичный файл с reference не должен подтверждать ссылку")
        self.assertIn("Не вышло", report)

    def test_partial_note_is_preserved_and_result_published_beside(self) -> None:
        """Контракт уточнён в пятом проходе.

        Существующий файл не изменяется вообще: он сохраняется дословно, а
        завершённый результат публикуется рядом под свободным именем. Так
        исходный текст (включая повторы и пустые строки) не теряется.
        """
        partial = self.make_partial_note(URL_FIRST)
        before = partial.read_bytes()

        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            ok, _ = learn.ingest_result(URL_FIRST)

        self.assertTrue(ok)
        self.assertEqual(partial.read_bytes(), before, "черновик остаётся дословным")
        complete = [p for p in self.notes()
                    if learn._note_is_complete(p.read_text(encoding="utf-8"))]
        self.assertEqual(len(complete), 1, "рядом появился один завершённый результат")
        self.assertNotEqual(complete[0].resolve(), partial.resolve())
        self.assertEqual(len(self.tasks()), 1, "задача обязана появиться")

    def test_note_without_body_is_not_a_result(self) -> None:
        """Файл только с шапкой не считается конспектом."""
        hub = self.vault / learn.HUB / AREA
        hub.mkdir(parents=True, exist_ok=True)
        (hub / "только шапка.md").write_text(
            f"---\nreference: {URL_FIRST}\ngenerator: dusk\n---\n", encoding="utf-8")

        draft = hub / "только шапка.md"
        before = draft.read_bytes()
        self.assertFalse(learn._note_is_complete(draft.read_text(encoding="utf-8")))

        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            learn.ingest_result(URL_FIRST)

        self.assertEqual(draft.read_bytes(), before, "черновик не переписывается")
        complete = [p for p in self.notes()
                    if learn._note_is_complete(p.read_text(encoding="utf-8"))]
        self.assertEqual(len(complete), 1, "завершённый результат публикуется рядом")


class TaskCollisionTests(QueueTestCase):
    """Пункт 2: занятое имя задачи не теряет действие."""

    def put_foreign_task(self, name: str) -> Path:
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        path = tasks / f"{name}.md"
        path.write_text(
            '---\ntitle: "чужая задача"\nstatus: open\npriority: normal\n---\n\n# чужая задача\n',
            encoding="utf-8",
        )
        return path

    def test_occupied_foreign_name_does_not_lose_action(self) -> None:
        self.put_foreign_task(ACTION)

        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            ok, message = learn.ingest_result(URL_FIRST)

        self.assertTrue(ok)
        tasks = self.tasks()
        self.assertEqual(len(tasks), 2, "действие должно получить свободный файл")
        linked = [t for t in tasks if "[[" in t.read_text(encoding="utf-8")]
        self.assertEqual(len(linked), 1, "ровно одна новая задача связана с конспектом")

    def test_two_videos_with_same_action_both_get_tasks(self) -> None:
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            first_ok, _ = learn.ingest_result(URL_FIRST)
            second_ok, _ = learn.ingest_result(URL_SECOND)

        self.assertTrue(first_ok and second_ok)
        notes = self.notes()
        self.assertEqual(len(notes), 2)
        tasks = self.tasks()
        self.assertEqual(len(tasks), 2, "оба видео должны получить своё действие")
        stems = [n.stem for n in notes]
        for task in tasks:
            body = task.read_text(encoding="utf-8")
            self.assertTrue(any(f"[[{stem}]]" in body for stem in stems),
                            f"задача не связана ни с одним конспектом: {task.name}")

    def test_partial_task_file_is_not_a_completed_action(self) -> None:
        """Обрыв записи задачи: файл есть, связи с конспектом нет."""
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / f"{ACTION}.md").write_text("---\n", encoding="utf-8")

        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            ok, _ = learn.ingest_result(URL_FIRST)

        self.assertTrue(ok)
        valid = [t for t in self.tasks() if "[[" in t.read_text(encoding="utf-8")]
        self.assertEqual(len(valid), 1, "должна появиться полноценная задача")

    def test_failed_task_write_keeps_url_for_retry(self) -> None:
        self.write_queue(URL_FIRST)
        original = learn._publish

        def failing_publish(path, text, **kwargs):
            if str(path).startswith(str(self.vault / "TaskNotes")):
                raise OSError("диск полон")
            return original(path, text, **kwargs)

        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)), \
                patch.object(learn, "_publish", side_effect=failing_publish):
            with self.assertRaises(OSError):
                learn.process_queue()

        self.assertIn(URL_FIRST, self.queue_urls(), "при сбое задачи ссылка остаётся в очереди")

    def test_action_required_but_task_missing_is_not_success(self) -> None:
        """Успех при обязательном действии — только с корректной задачей."""
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)), \
                patch.object(learn, "_create_task", return_value=""):
            ok, message = learn.ingest_result(URL_FIRST)

        self.assertFalse(ok, "без задачи действие не выполнено")
        self.assertIn("задач", message.lower())


class EnqueueNotBlockedTests(QueueTestCase):
    """Пункт 3: enqueue не ждёт окончания разбора."""

    def test_real_enqueue_returns_while_ingest_runs(self) -> None:
        self.write_queue(URL_FIRST)
        entered = threading.Event()
        release = threading.Event()
        report: dict = {}

        def slow_fetch(url, timeout=180):
            entered.set()
            release.wait(timeout=20)
            return "текст транскрипта", "заголовок"

        with patch.object(learn, "fetch_subtitles", side_effect=slow_fetch), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            worker = threading.Thread(
                target=lambda: report.setdefault("value", learn.process_queue()))
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=10), "разбор не начался")

                enqueued = threading.Event()

                def do_enqueue():
                    learn.enqueue(URL_SECOND)
                    enqueued.set()

                thread = threading.Thread(target=do_enqueue)
                thread.start()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(),
                                 "настоящий enqueue заблокирован на время ingest")
                self.assertTrue(enqueued.is_set())
                self.assertIn(URL_SECOND, self.queue.read_text(encoding="utf-8"))
            finally:
                release.set()
                worker.join(timeout=30)

        self.assertIn(URL_SECOND, self.queue_urls(),
                      "ссылка, добавленная во время разбора, должна остаться в очереди")

    def test_second_processor_does_not_take_the_same_work(self) -> None:
        """Второй разбор не стартует, пока идёт первый (без ожидания сети)."""
        self.write_queue(URL_FIRST)
        entered = threading.Event()
        release = threading.Event()
        reports: list = []

        def slow_fetch(url, timeout=180):
            entered.set()
            release.wait(timeout=20)
            return "текст", "заголовок"

        with patch.object(learn, "fetch_subtitles", side_effect=slow_fetch), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            first = threading.Thread(
                target=lambda: reports.append(learn.process_queue()))
            first.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                second = learn.process_queue()
                self.assertIn("идёт", second.lower(),
                              "второй разбор должен честно сказать, что уже идёт")
            finally:
                release.set()
                first.join(timeout=30)

    def test_enqueue_inside_ingest_does_not_deadlock(self) -> None:
        """Вложенный вызов из самого разбора не должен вешать процесс.

        Проверка идёт в отдельном потоке с таймаутом: если блокировка снова
        будет удерживаться на медленном шаге, тест упадёт, а не зависнет.
        """
        self.write_queue(URL_FIRST)
        outcome: dict = {}

        def fetch_with_enqueue(url, timeout=180):
            # второй писатель внутри медленного шага: тот же процесс, та же очередь
            learn.enqueue(URL_SECOND)
            return "текст", "заголовок"

        def run():
            outcome["report"] = learn.process_queue()

        with patch.object(learn, "fetch_subtitles", side_effect=fetch_with_enqueue), \
                patch.object(learn, "_distill", fake_distill(action=ACTION)):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive(), "вложенный enqueue привёл к взаимной блокировке")

        self.assertIn("Разобрано видео", outcome.get("report", ""))
        self.assertIn(URL_SECOND, self.queue_urls(),
                      "ссылка, добавленная во время разбора, должна остаться")
        self.assertNotIn(URL_FIRST, self.queue_urls())


class FrontmatterScopeTests(unittest.TestCase):
    """Пункт 6: правки только внутри первой шапки, тело дословно."""

    BODY_WITH_FIELDS = (
        "---\ntitle: alpha\nstatus: open\n---\n\n"
        "# alpha\n\n"
        "dateModified: пример поля в теле, сохранить дословно\n"
        "status: тоже просто текст\n\n"
        "```yaml\ncompletedDate: 1999-01-01\n```\n"
    )

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

    def body_of(self, text: str) -> str:
        return text.split("---", 2)[2]

    def test_body_is_untouched(self) -> None:
        self.card.write_text(self.BODY_WITH_FIELDS, encoding="utf-8")
        before = self.body_of(self.card.read_text(encoding="utf-8"))

        dispatch.advance_task("готово", "alpha",
                              now=datetime.datetime(2026, 10, 6, 12, tzinfo=datetime.timezone.utc))

        after = self.body_of(self.card.read_text(encoding="utf-8"))
        self.assertEqual(after, before, "тело заметки должно остаться дословным")

    def test_body_status_is_not_read_as_current_status(self) -> None:
        """Строка `status: done` в теле не делает карточку закрытой."""
        self.card.write_text(
            "---\ntitle: beta\nstatus: open\n---\n\n# beta\n\nstatus: done\n", encoding="utf-8")
        changed = self.tasks / "alpha.md"
        changed.write_text(self.card.read_text(encoding="utf-8"), encoding="utf-8")
        dispatch.advance_task("готово", "alpha",
                              now=datetime.datetime(2026, 10, 6, 12, tzinfo=datetime.timezone.utc))
        text = self.card.read_text(encoding="utf-8")
        head = text.split("---", 2)[1]
        self.assertIn("status: done", head)
        self.assertIn("completedDate:", head)

    def test_damaged_frontmatter_is_not_reported_as_success(self) -> None:
        self.card.write_text("# только тело без шапки\n", encoding="utf-8")
        message = dispatch.advance_task("готово", "alpha",
                                        now=datetime.datetime(2026, 10, 6, 12,
                                                              tzinfo=datetime.timezone.utc))
        self.assertNotIn("✅", message, "при повреждённой шапке успех не заявляем")
        self.assertEqual(self.card.read_text(encoding="utf-8"), "# только тело без шапки\n")


class CompletionStampTests(unittest.TestCase):
    """Пункты 7 и 8: пустое поле и часовой пояс Алматы."""

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

    def field(self, name: str) -> str:
        import re

        text = self.card.read_text(encoding="utf-8")
        head = text.split("---", 2)[1]
        m = re.search(rf"^{name}:\s*(.*)$", head, re.M)
        return m.group(1).strip() if m else ""

    def test_blank_completed_date_is_filled_on_first_close(self) -> None:
        self.card.write_text(
            "---\ntitle: alpha\nstatus: open\ncompletedDate:\n---\n\n# alpha\n", encoding="utf-8")
        dispatch.advance_task("готово", "alpha",
                              now=datetime.datetime(2026, 10, 6, 12,
                                                    tzinfo=datetime.timezone(datetime.timedelta(hours=5))))
        self.assertEqual(self.field("completedDate"), "2026-10-06")

    def test_utc_server_date_is_almaty_date(self) -> None:
        moment = datetime.datetime(2026, 10, 5, 20, 0, tzinfo=datetime.timezone.utc)
        day, stamp = dispatch._completion_stamp(moment)
        self.assertEqual(day, "2026-10-06", "20:00 UTC — это уже 6 октября в Алматы")
        self.assertTrue(stamp.startswith("2026-10-06T01:00:00"))

    def test_monday_boundary_in_almaty(self) -> None:
        # воскресенье 23:30 UTC → понедельник 04:30 в Алматы
        moment = datetime.datetime(2026, 10, 11, 23, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(dispatch._completion_stamp(moment)[0], "2026-10-12")

    def test_naive_now_is_treated_as_almaty_local(self) -> None:
        """Naive datetime — это местное время Алматы (документированная семантика)."""
        naive = datetime.datetime(2026, 10, 6, 9, 0)
        day, stamp = dispatch._completion_stamp(naive)
        self.assertEqual(day, "2026-10-06")
        self.assertIn("+05:00", stamp)
        self.assertEqual(weekly_plan.to_almaty(naive).utcoffset(),
                         weekly_plan.to_almaty(naive.replace(tzinfo=None)).utcoffset())


if __name__ == "__main__":
    unittest.main()
