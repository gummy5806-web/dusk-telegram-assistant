#!/usr/bin/env python3
"""Статический контракт Telegram-слоя для напоминаний (без real API)."""

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
    def test_reminder_namespace_routed(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertIn('"r:"', body)
        self.assertIn("_handle_reminder_callback", body)

    def test_focus_and_thought_namespaces_unchanged(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertIn('"f:"', body)
        self.assertIn('"q:"', body)

    def test_callback_uses_safe_edit_only(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertIn("ui.safe_edit", body)
        self.assertNotIn("edit_message_text", body)

    def test_reminder_sync_message_exists(self) -> None:
        self.assertEqual(ui.SYNC_MESSAGES.get("reminder"), "telegram: напоминание")


class ContentContractTests(unittest.TestCase):
    def test_content_checks_custom_and_intent(self) -> None:
        body = function_source(TELEGRAM, "handle_content")
        self.assertIn("_maybe_reminder_custom", body)
        self.assertIn("_maybe_reminder_intent", body)

    def test_natural_intent_before_router(self) -> None:
        body = function_source(TELEGRAM, "handle_content")
        self.assertLess(body.index("_maybe_reminder_intent"),
                        body.index("route = assistant_router.route"))

    def test_main_buttons_clear_custom(self) -> None:
        body = function_source(TELEGRAM, "handle_text")
        self.assertIn("_clear_inputs", body)
        self.assertIn("reminder_ui.clear_custom", function_source(TELEGRAM, "_clear_inputs"))


class LifecycleContractTests(unittest.TestCase):
    def test_post_init_starts_reminder_scheduler(self) -> None:
        body = function_source(TELEGRAM, "post_init")
        self.assertIn("reminders.reconcile", body)
        self.assertIn("reminder_scheduler.ReminderScheduler", body)
        self.assertIn("reminder_wake", body)

    def test_post_shutdown_stops_reminder_scheduler(self) -> None:
        body = function_source(TELEGRAM, "post_shutdown")
        self.assertIn("reminder_task", body)
        self.assertIn("reminder_stop", body)

    def test_start_mentions_reminder_without_stale_line(self) -> None:
        body = function_source(TELEGRAM, "start")
        self.assertIn("🔔 Напомнить", body)
        self.assertNotIn("не реализованы", body)


class IsolationTests(unittest.TestCase):
    def test_pure_modules_do_not_import_telegram_or_git(self) -> None:
        for name in ("reminder_nlp.py", "reminder_ui.py",
                     "reminder_scheduler.py"):
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

    def test_card_shows_reminder_button_and_bounded_callbacks(self) -> None:
        view = ui.render_list(self.state, self.root)
        token = [data.split(":", 1)[1] for row in view.rows
                 for _label, data in row if data.startswith("t:")][0]
        card = ui.render_card(self.state, self.root, token)
        labels = [label for row in card.rows for label, _data in row]
        self.assertIn("🔔 Напомнить", labels)
        for _label, data in [pair for row in card.rows for pair in row]:
            self.assertLessEqual(len(data.encode("utf-8")), 64, data)


class PTBContractTests(unittest.TestCase):
    def test_import_and_build_with_lifecycle_hooks(self) -> None:
        try:
            from telegram.ext import Application
        except ImportError:
            self.skipTest("python-telegram-bot not installed")

        async def hook(application):
            return None

        app = (Application.builder().token("123456:TEST")
               .post_init(hook).post_shutdown(hook).build())
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()
