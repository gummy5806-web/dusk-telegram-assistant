#!/usr/bin/env python3
"""Статический контракт тонкого Telegram-слоя для фокуса.

Пакет `telegram` на машине разработки не установлен, поэтому слой проверяется
AST/текстом, а чистые контракты — рантаймом. Никаких реальных API.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

import task_ui as ui

BOT = Path(__file__).resolve().parent
ROOT = BOT.parent
TELEGRAM = BOT / "telegram_bot.py"


def function_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


class CallbackContractTests(unittest.TestCase):
    def test_focus_namespace_routed(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertIn('"q:"', body)
        self.assertIn("_handle_focus_callback", body)

    def test_thought_namespace_unchanged(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertIn('"f:"', body)
        self.assertIn("_handle_thought_callback", body)

    def test_callback_uses_safe_edit_only(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertIn("ui.safe_edit", body)
        self.assertNotIn("edit_message_text", body)

    def test_status_done_stops_active_focus(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertIn("status_with_focus", body)
        helper = (BOT / "focus_ui.py").read_text(encoding="utf-8")
        self.assertIn("def status_with_focus", helper)
        self.assertIn("stop_for_done", helper)

    def test_focus_actions_wake_scheduler(self) -> None:
        src = TELEGRAM.read_text(encoding="utf-8")
        self.assertIn("focus_wake", src)
        self.assertIn("_wake_focus(context)", src)

    def test_sync_runs_off_event_loop(self) -> None:
        src = TELEGRAM.read_text(encoding="utf-8")
        self.assertIn("asyncio.to_thread(_sync_focus)", src)
        self.assertIn("asyncio.to_thread(_sync_status)", src)

    def test_status_uses_focus_helper(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertIn("status_with_focus", body)

    def test_focus_sync_message_exists(self) -> None:
        self.assertEqual(ui.SYNC_MESSAGES.get("focus"), "telegram: фокус")


class LifecycleContractTests(unittest.TestCase):
    def test_builder_registers_lifecycle_hooks(self) -> None:
        body = function_source(TELEGRAM, "main")
        self.assertIn("post_init", body)
        self.assertIn("post_shutdown", body)
        self.assertIn("Application.builder()", body)

    def test_scheduler_uses_raw_task_not_application_create_task(self) -> None:
        src = TELEGRAM.read_text(encoding="utf-8")
        self.assertIn("asyncio.create_task", src)
        self.assertNotIn("create_task_", src)

    def test_start_mentions_focus(self) -> None:
        body = function_source(TELEGRAM, "start")
        self.assertIn("Фокус", body)
        self.assertIn("Задачи", body)


class IsolationTests(unittest.TestCase):
    def test_pure_modules_do_not_import_telegram_or_git(self) -> None:
        for name in ("focus_ui.py", "focus_scheduler.py"):
            src = (BOT / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotIn("import telegram", src)
                self.assertNotIn("from telegram", src)
                self.assertNotIn("git_push", src)
                self.assertNotIn("git_sync", src)


class CardRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        (self.tasks / "Задача.md").write_text(
            '---\ntitle: "Задача"\nstatus: next\n---\n# Задача\n',
            encoding="utf-8")
        self.state: dict = {}

    def test_card_without_focus_shows_focus_button(self) -> None:
        view = ui.render_list(self.state, self.root)
        token = [data.split(":", 1)[1] for row in view.rows
                 for _label, data in row if data.startswith("t:")][0]
        card = ui.render_card(self.state, self.root, token)
        labels = [label for row in card.rows for label, _data in row]
        self.assertIn("⏱ Фокус", labels)
        for _label, data in [pair for row in card.rows for pair in row]:
            self.assertLessEqual(len(data.encode("utf-8")), 64)

    def test_card_accepts_focus_view(self) -> None:
        view = ui.render_list(self.state, self.root)
        token = [data.split(":", 1)[1] for row in view.rows
                 for _label, data in row if data.startswith("t:")][0]
        focus_view = ui.View("⏱ Фокус: 25 мин · до 17:25 · осталось 20 мин",
                             [[("⏹ Закончить", "q:x:abc"), ("➕15", "q:e:def")]])
        card = ui.render_card(self.state, self.root, token, focus=focus_view)
        self.assertIn("осталось 20 мин", card.text)
        labels = [label for row in card.rows for label, _data in row]
        self.assertIn("⏹ Закончить", labels)
        self.assertIn("➕15", labels)


if __name__ == "__main__":
    unittest.main()
