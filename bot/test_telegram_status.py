#!/usr/bin/env python3
"""Telegram `📊 Статус` собирает личную сводку на момент нажатия (без модели/сети)."""

from __future__ import annotations

import ast
import datetime
import tempfile
import types
import unittest
from pathlib import Path

import personal_summary
import runtime_store

BOT = Path(__file__).resolve().parent
TELEGRAM = BOT / "telegram_bot.py"
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def function_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def load_function(path: Path, name: str, namespace: dict):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            exec(compile(module, str(path), "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"нет функции {name}")


class StatusTextTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "TaskNotes" / "Tasks").mkdir(parents=True)
        (self.root / "ZETA" / "FLEETING").mkdir(parents=True)
        (self.root / "ZETA" / "FLEETING_DAILY").mkdir(parents=True)
        self.store = runtime_store.RuntimeStore(
            self.root / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)
        self.function = load_function(TELEGRAM, "_personal_status_text", {
            "personal_summary": personal_summary,
            "_vault": lambda: self.root,
            "_runtime_store": lambda: self.store,
            "_now": lambda: NOW,
        })

    def make_task(self, name):
        (self.root / "TaskNotes" / "Tasks" / name).write_text(
            f'---\ntitle: "{name[:-3]}"\nstatus: next\n---\n# {name[:-3]}\n',
            encoding="utf-8")

    def test_returns_bounded_summary(self) -> None:
        self.make_task("Задача.md")
        text = self.function()
        self.assertIn("📊 Сводка", text)
        self.assertLessEqual(len(text), 4000)
        self.assertNotIn("Traceback", text)

    def test_survives_unavailable_store(self) -> None:
        self.make_task("Задача.md")
        namespace = {
            "personal_summary": personal_summary,
            "_vault": lambda: self.root,
            "_runtime_store": lambda: None,
            "_now": lambda: NOW,
        }
        text = load_function(TELEGRAM, "_personal_status_text", namespace)()
        self.assertIn("📊 Сводка", text)


class StatusWiringTests(unittest.TestCase):
    def test_show_status_offloads_and_uses_summary(self) -> None:
        body = function_source(TELEGRAM, "_show_status")
        self.assertIn("asyncio.to_thread", body)
        self.assertIn("_personal_status_text", body)
        self.assertNotIn("Статус.md", body)
        self.assertNotIn("llm", body)
        self.assertNotIn("ask(", body)

    def test_main_keyboard_preserved(self) -> None:
        body = function_source(TELEGRAM, "_show_status")
        self.assertIn("MAIN_KEYBOARD", body)


if __name__ == "__main__":
    unittest.main()
