#!/usr/bin/env python3
"""Регрессии ревью-прохода 3.

Проверяются рантаймом: реальная мутация для video/note, приватный маршрут без
tracked sync, сохранение режима при сбое, тихие except в callback и источник
задачи, созданной кнопкой.
"""

from __future__ import annotations

import ast
import datetime
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dispatch
import learn
import task_ui

BOT = Path(__file__).resolve().parent
NOW = datetime.datetime(
    2026, 10, 7, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5)))
VIDEO = "https://youtu.be/abcdefghijk"


def function_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


class VaultBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.tasks = self.vault / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        (self.vault / "ZETA" / "FLEETING").mkdir(parents=True)
        self.state: dict = {}

        for name, value in (
            ("VAULT", self.vault),
            ("INBOX", self.vault / "ZETA" / "FLEETING" / "00 Инбокс.md"),
            ("PRIVATE", self.vault / "Private" / "00 Private.md"),
        ):
            patcher = patch.object(dispatch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(dispatch, "_write_via_obsidian", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def recorder(self):
        calls = []

        def sync(*args):
            calls.append(args[0] if args else None)

        return calls, sync


# ── P1.1: video sync только на реальной вставке ─────────────────────────────

class VideoMutationTests(VaultBase):
    def test_duplicate_video_is_success_without_sync(self) -> None:
        calls, sync = self.recorder()
        with patch.object(learn, "enqueue", return_value="уже в очереди"):
            result = task_ui.submit_video(self.state, VIDEO, sync=sync)
        self.assertTrue(result.ok, "повтор должен быть понятным успехом")
        self.assertFalse(result.synced)
        self.assertEqual(calls, [], "дубль не запускает tracked sync")

    def test_new_video_syncs_once(self) -> None:
        calls, sync = self.recorder()
        with patch.object(learn, "enqueue", return_value="добавлено"):
            result = task_ui.submit_video(self.state, VIDEO, sync=sync)
        self.assertTrue(result.ok)
        self.assertTrue(result.synced)
        self.assertEqual(len(calls), 1)

    def test_save_video_result_reports_structured_flag(self) -> None:
        with patch.object(learn, "enqueue", return_value="уже в очереди"):
            duplicate = dispatch.save_video_result(VIDEO)
        with patch.object(learn, "enqueue", return_value="добавлено"):
            fresh = dispatch.save_video_result(VIDEO)
        self.assertFalse(duplicate.mutated)
        self.assertTrue(fresh.mutated)
        self.assertEqual(fresh.kind, "video")


# ── P1.2/P1.3: note sync и private ──────────────────────────────────────────

class NoteMutationTests(VaultBase):
    def test_private_note_is_written_but_not_synced(self) -> None:
        calls, sync = self.recorder()
        result = task_ui.submit_note(self.state, "записаться к врачу", sync=sync)
        self.assertTrue(result.ok)
        self.assertFalse(result.synced, "личное не уходит в tracked sync")
        self.assertEqual(calls, [])
        private = self.vault / "Private" / "00 Private.md"
        self.assertTrue(private.exists(), "приватное всё же записано")
        self.assertIn("врачу", private.read_text(encoding="utf-8"))

    def test_public_note_syncs_once(self) -> None:
        calls, sync = self.recorder()
        result = task_ui.submit_note(self.state, "связать задачи и заметки", sync=sync)
        self.assertTrue(result.ok)
        self.assertTrue(result.synced)
        self.assertEqual(len(calls), 1)

    def test_save_note_result_reports_private_structured(self) -> None:
        private = dispatch.save_note_result("записаться к врачу")
        public = dispatch.save_note_result("обычная мысль про интерфейс")
        self.assertTrue(private.private)
        self.assertFalse(public.private)
        self.assertTrue(public.mutated)
        self.assertEqual(public.kind, "note")


# ── P1: режим выживает при сбое ─────────────────────────────────────────────

class ModeSurvivalTests(VaultBase):
    def test_failed_task_write_keeps_mode_and_does_not_sync(self) -> None:
        task_ui.begin(self.state, task_ui.MODE_TASK)
        calls, sync = self.recorder()
        with patch.object(dispatch, "create_task_exact", return_value=(None, False)):
            result = task_ui.submit_task(
                self.state, "Задача", vault=self.vault, sync=sync, now=NOW)
        self.assertFalse(result.ok)
        self.assertEqual(task_ui.mode(self.state), task_ui.MODE_TASK,
                         "сбой записи оставляет режим для повтора")
        self.assertEqual(calls, [])

    def test_duplicate_task_clears_mode(self) -> None:
        task_ui.begin(self.state, task_ui.MODE_TASK)
        task_ui.submit_task(self.state, "Повтор", vault=self.vault, now=NOW)
        task_ui.begin(self.state, task_ui.MODE_TASK)
        calls, sync = self.recorder()
        result = task_ui.submit_task(
            self.state, "Повтор", vault=self.vault, sync=sync, now=NOW)
        self.assertTrue(result.ok)
        self.assertFalse(result.synced)
        self.assertEqual(task_ui.mode(self.state), "")

    def test_note_exception_keeps_mode(self) -> None:
        task_ui.begin(self.state, task_ui.MODE_NOTE)
        calls, sync = self.recorder()

        def boom(_text):
            raise OSError("disk")

        result = task_ui.submit_note(self.state, "текст", save=boom, sync=sync)
        self.assertFalse(result.ok)
        self.assertEqual(task_ui.mode(self.state), task_ui.MODE_NOTE)
        self.assertEqual(calls, [])

    def test_video_exception_keeps_mode(self) -> None:
        task_ui.begin(self.state, task_ui.MODE_VIDEO)
        calls, sync = self.recorder()

        def boom(_url):
            raise OSError("queue")

        result = task_ui.submit_video(self.state, VIDEO, enqueue=boom, sync=sync)
        self.assertFalse(result.ok)
        self.assertEqual(task_ui.mode(self.state), task_ui.MODE_VIDEO)
        self.assertEqual(calls, [])


# ── P1: callback не глотает ошибки молча ────────────────────────────────────

class CallbackErrorHandlingTests(unittest.TestCase):
    def test_no_silent_except_pass_in_callback(self) -> None:
        body = function_source(BOT / "telegram_bot.py", "handle_callback")
        self.assertTrue(body)
        compact = re.sub(r"\s+", " ", body)
        self.assertNotIn("except Exception: pass", compact,
                         "тихий except Exception: pass запрещён")
        self.assertNotIn("except Exception:\n", body)
        self.assertIn("logger.error", body)
        self.assertIn("logger.warning", body)

    def test_unauthorized_callback_still_acknowledges(self) -> None:
        body = function_source(BOT / "telegram_bot.py", "handle_callback")
        self.assertIn("query.answer", body)
        self.assertIn("auth_check", body)


# ── P2: источник задачи ─────────────────────────────────────────────────────

class TaskSourceTests(VaultBase):
    def test_button_task_has_telegram_source(self) -> None:
        calls, sync = self.recorder()
        result = task_ui.submit_task(
            self.state, "Кнопочная задача", vault=self.vault, sync=sync, now=NOW)
        self.assertTrue(result.ok)
        [path] = list(self.tasks.glob("*.md"))
        body = path.read_text(encoding="utf-8")
        self.assertIn('source: "Telegram"', body)
        self.assertNotIn("пакет дня", body)

    def test_exact_create_default_source_is_compatible(self) -> None:
        path, created = dispatch.create_task_exact("Пакетная задача", now=NOW)
        self.assertTrue(created)
        self.assertIn('source: "пакет дня"', path.read_text(encoding="utf-8"))

    def test_daily_pack_default_source_is_compatible(self) -> None:
        dispatch._daily_tasks("Первая\nВторая")
        for path in self.tasks.glob("*.md"):
            self.assertIn('source: "пакет дня"', path.read_text(encoding="utf-8"))

    def test_source_is_json_quoted(self) -> None:
        path, created = dispatch.create_task_exact(
            "Источник", now=NOW, source='с "кавычками"')
        self.assertTrue(created)
        self.assertIn('source: "с \\"кавычками\\""', path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
