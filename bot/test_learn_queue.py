#!/usr/bin/env python3
"""Очередь обучения: ссылка не теряется, повтор не дублирует, гонки нет.

Все проверки изолированы: временный валт, подменённые субтитры и модель,
никаких сетевых вызовов и никаких записей в живой валт.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import learn

AREA = "Продуктивность - навык"
HUB_AREA = Path(learn.HUB) / AREA


def make_vault(root: Path) -> Path:
    (root / HUB_AREA).mkdir(parents=True, exist_ok=True)
    (root / "TaskNotes" / "Tasks").mkdir(parents=True, exist_ok=True)
    (root / "Учеба").mkdir(parents=True, exist_ok=True)
    return root


def fake_distill(title: str = "Разбор видео", action: str = "сделать одно действие"):
    """Модель подменена: возвращает предсказуемый разбор."""

    def _distill(meta, transcript, areas):
        return {
            "title": title,
            "area": AREA,
            "thesis": "главная мысль видео",
            "points": ["тезис один", "тезис два"],
            "action": action,
            "why": "потому что это двигает проект",
            "quote": "дословная цитата",
        }

    return _distill


class QueueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.vault = make_vault(Path(self._dir.name))
        self.queue = self.vault / learn.QUEUE

        patcher = patch.object(learn, "_vault", lambda: self.vault)
        patcher.start()
        self.addCleanup(patcher.stop)

        # git не трогаем вовсе: тест не должен коммитить
        patcher = patch.object(learn.vault_ops, "git_push", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_queue(self, *urls: str) -> None:
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.queue.write_text("# Очередь\n" + "".join(f"- {u}\n" for u in urls), encoding="utf-8")

    def queue_urls(self) -> list:
        if not self.queue.exists():
            return []
        return [l.strip()[2:].strip() for l in self.queue.read_text(encoding="utf-8").splitlines()
                if l.strip().startswith("- http")]

    def notes(self) -> list:
        return sorted((self.vault / learn.HUB).rglob("*.md"))

    def tasks(self) -> list:
        return sorted((self.vault / "TaskNotes" / "Tasks").glob("*.md"))


class FailedIngestTests(QueueTestCase):
    def test_failed_url_stays_in_queue(self) -> None:
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        self.write_queue(url)
        with patch.object(learn, "fetch_subtitles", return_value=(None, "субтитров нет")):
            report = learn.process_queue()

        self.assertIn(url, self.queue_urls(), "неудачная ссылка обязана остаться")
        self.assertIn("Не вышло: 1", report)
        self.assertIn("оставил в очереди", report)
        self.assertEqual(self.notes(), [])

    def test_model_error_keeps_url(self) -> None:
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        self.write_queue(url)
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", return_value={"error": "модель молчит"}):
            learn.process_queue()

        self.assertIn(url, self.queue_urls())
        self.assertEqual(self.notes(), [])

    def test_successful_url_is_removed(self) -> None:
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        self.write_queue(url)
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill()):
            report = learn.process_queue()

        self.assertEqual(self.queue_urls(), [])
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("Разобрано видео: 1", report)

    def test_url_added_during_processing_is_not_lost(self) -> None:
        first = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        second = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
        self.write_queue(first)

        def fetch(url, timeout=180):
            # второй писатель дописывает ссылку, пока идёт разбор первой
            with self.queue.open("a", encoding="utf-8") as handle:
                handle.write(f"- {second}\n")
            return "текст транскрипта", "заголовок"

        with patch.object(learn, "fetch_subtitles", side_effect=fetch), \
                patch.object(learn, "_distill", fake_distill()):
            learn.process_queue()

        remaining = self.queue_urls()
        self.assertEqual(remaining, [second], "ссылка, добавленная во время разбора, должна остаться")


class IdempotencyTests(QueueTestCase):
    def test_repeat_link_does_not_duplicate(self) -> None:
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill()):
            first_ok, _ = learn.ingest_result(url)
            second_ok, message = learn.ingest_result(url)

        self.assertTrue(first_ok and second_ok)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(len(self.tasks()), 1)
        self.assertIn("повторно не дублирую", message)

    def test_same_link_in_different_forms_is_one_entry(self) -> None:
        self.assertEqual(learn.enqueue("https://www.youtube.com/watch?v=aaaaaaaaaaa"), "в очереди")
        self.assertEqual(learn.enqueue("https://youtu.be/aaaaaaaaaaa?t=42"), "уже в очереди")
        self.assertEqual(len(self.queue_urls()), 1)

    def test_crash_after_note_before_task_is_completed_on_retry(self) -> None:
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill()), \
                patch.object(learn, "_create_task", side_effect=RuntimeError("обрыв записи")):
            with self.assertRaises(RuntimeError):
                learn.ingest_result(url)

        self.assertEqual(len(self.notes()), 1, "конспект создан до сбоя")
        self.assertEqual(len(self.tasks()), 0)

        # повтор не должен ни звать модель, ни дублировать конспект
        with patch.object(learn, "fetch_subtitles", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_distill", side_effect=AssertionError("модель не нужна")):
            ok, message = learn.ingest_result(url)

        self.assertTrue(ok)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(len(self.tasks()), 1)
        self.assertIn("Задача досоздана", message)

    def test_different_videos_with_same_title_do_not_overwrite(self) -> None:
        first = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        second = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill(title="Одинаковый заголовок")):
            learn.ingest_result(first)
            learn.ingest_result(second)

        notes = self.notes()
        self.assertEqual(len(notes), 2, "второй конспект не должен затирать первый")
        bodies = [p.read_text(encoding="utf-8") for p in notes]
        joined = "\n".join(bodies)
        self.assertIn(first, joined)
        self.assertIn(second, joined)
        self.assertTrue(any("(2)" in p.stem for p in notes), [p.stem for p in notes])

    def test_save_failure_keeps_queue_untouched(self) -> None:
        """Если запись очереди падает, ни одна ссылка не теряется."""
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        self.write_queue(url)
        with patch.object(learn, "fetch_subtitles", return_value=("текст", "заголовок")), \
                patch.object(learn, "_distill", fake_distill()), \
                patch.object(learn.queue_store, "atomic_write", side_effect=OSError("диск полон")):
            with self.assertRaises(OSError):
                learn.process_queue()

        self.assertEqual(self.queue_urls(), [url])


WORKER = textwrap.dedent(
    '''
    """Разовый разбор очереди для проверки гонки двух процессов."""
    import os
    import sys
    import time
    from pathlib import Path

    sys.path.insert(0, os.environ["BOT_DIR"])
    import learn

    vault = Path(os.environ["VAULT_PATH"])
    markers = Path(os.environ["MARKERS"])
    log = markers / "ingested.log"

    learn._vault = lambda: vault


    def fetch(url, timeout=180):
        # O_APPEND: короткая запись одной строкой не перемешивается с чужой
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(url + chr(10))
        time.sleep(0.05)
        return "текст транскрипта", "заголовок"


    def distill(meta, transcript, areas):
        return {
            "title": "Конспект " + areas[0] if areas else "Конспект",
            "area": areas[0] if areas else "",
            "thesis": "мысль",
            "points": ["тезис"],
            "action": "действие " + str(len(transcript)),
            "why": "зачем",
            "quote": "цитата",
        }


    learn.fetch_subtitles = fetch
    learn._distill = distill
    learn.vault_ops.git_push = lambda *a, **k: None

    (markers / ("ready-" + str(os.getpid()))).write_text("1", encoding="utf-8")
    while not (markers / "go").exists():
        time.sleep(0.02)
    learn.process_queue()
    '''
)


class ConcurrencyTests(QueueTestCase):
    def test_two_processes_do_not_repeat_work(self) -> None:
        urls = [f"https://www.youtube.com/watch?v=aaaaaaaaa{i}" for i in range(3)]
        self.write_queue(*urls)

        markers = self.vault / "markers"
        markers.mkdir(parents=True, exist_ok=True)
        worker = Path(self._dir.name) / "worker.py"
        worker.write_text(WORKER, encoding="utf-8")

        import os

        env = dict(os.environ)
        env["BOT_DIR"] = str(Path(learn.__file__).parent)
        env["VAULT_PATH"] = str(self.vault)
        env["MARKERS"] = str(markers)
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        procs = [subprocess.Popen([sys.executable, str(worker)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if len(list(markers.glob("ready-*"))) >= 2:
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(len(list(markers.glob("ready-*"))), 2, "воркеры не стартовали")
            (markers / "go").write_text("1", encoding="utf-8")

            for proc in procs:
                _, stderr = proc.communicate(timeout=90)
                self.assertEqual(proc.returncode, 0, stderr.decode("utf-8", "ignore"))
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()

        log = (markers / "ingested.log").read_text(encoding="utf-8").split()
        self.assertEqual(sorted(log), sorted(urls), "каждая ссылка должна быть разобрана ровно один раз")
        self.assertEqual(self.queue_urls(), [], "очередь должна опустеть")
        self.assertEqual(len(self.notes()), len(urls))


if __name__ == "__main__":
    unittest.main()
