#!/usr/bin/env python3
"""Регрессии ревью-прохода 4: безопасный edit, порядок sync→push, доки, TZ."""

from __future__ import annotations

import ast
import datetime
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import dispatch
import task_ui
import weekly_plan

BOT = Path(__file__).resolve().parent
ROOT = BOT.parent
TZ = datetime.timezone(datetime.timedelta(hours=5), "Asia/Almaty")


def function_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def load_function(path: Path, name: str, namespace: dict):
    """Компилирует одну функцию из файла без импорта самого модуля."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            exec(compile(module, str(path), "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"нет функции {name}")


# ── F1: benign «Message is not modified» ────────────────────────────────────

class BadRequest(Exception):
    """Имя совпадает с telegram.error.BadRequest (PTB локально не установлен)."""


class FakeQuery:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def edit_message_text(self, text, reply_markup=None):
        self.calls.append((text, reply_markup))
        if self.error is not None:
            raise self.error


class SafeEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_modified_is_benign(self) -> None:
        query = FakeQuery(BadRequest(
            "Message is not modified: specified new message content and reply "
            "markup are exactly the same"))
        await task_ui.safe_edit(query, "карточка", "kb")  # не бросает
        self.assertEqual(len(query.calls), 1)

    async def test_other_bad_request_is_reraised(self) -> None:
        query = FakeQuery(BadRequest("chat not found"))
        with self.assertRaises(BadRequest):
            await task_ui.safe_edit(query, "карточка")

    async def test_other_exception_is_reraised(self) -> None:
        query = FakeQuery(RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            await task_ui.safe_edit(query, "карточка")

    async def test_normal_edit_forwards_markup(self) -> None:
        query = FakeQuery()
        ok = await task_ui.safe_edit(query, "текст", "kb")
        self.assertTrue(ok)
        self.assertEqual(query.calls, [("текст", "kb")])

    def test_callback_uses_safe_edit_only(self) -> None:
        body = function_source(BOT / "telegram_bot.py", "handle_callback")
        self.assertIn("ui.safe_edit", body)
        self.assertNotIn("query.edit_message_text", body)


# ── F2: sync перед push ─────────────────────────────────────────────────────

class SyncOrderTests(unittest.TestCase):
    def make(self, *, sync_error=None):
        calls = []

        def git_sync():
            calls.append("sync")
            if sync_error is not None:
                raise sync_error

        def git_push(message):
            calls.append(("push", message))

        errors = []
        namespace = {
            "git_sync": git_sync,
            "git_push": git_push,
            "logger": types.SimpleNamespace(
                error=lambda *args, **kwargs: errors.append(args)),
        }
        function = load_function(BOT / "telegram_bot.py", "_synced", namespace)
        return function, calls, errors

    def test_sync_runs_before_push(self) -> None:
        function, calls, _errors = self.make()
        function("telegram: задача создана")
        self.assertEqual(calls, ["sync", ("push", "telegram: задача создана")],
                         "git_sync должен идти до git_push")

    def test_neutral_message_has_no_user_text(self) -> None:
        for kind, message in task_ui.SYNC_MESSAGES.items():
            with self.subTest(kind=kind):
                function, calls, _errors = self.make()
                function(message)
                self.assertEqual(calls, ["sync", ("push", message)])
                self.assertNotIn("{", message)
                self.assertNotIn("http", message)

    def test_sync_failure_does_not_push_and_does_not_raise(self) -> None:
        function, calls, errors = self.make(sync_error=OSError("network"))
        function("telegram: заметка")
        self.assertEqual(calls, ["sync"], "push не вызывается при сбое sync")
        self.assertTrue(errors, "сбой логируется")
        self.assertEqual(errors[0][1], "OSError", "логируется только тип")

    def test_push_failure_is_logged_not_raised(self) -> None:
        calls = []

        def git_sync():
            calls.append("sync")

        def git_push(message):
            raise RuntimeError("boom")

        errors = []
        namespace = {
            "git_sync": git_sync,
            "git_push": git_push,
            "logger": types.SimpleNamespace(
                error=lambda *args, **kwargs: errors.append(args)),
        }
        function = load_function(BOT / "telegram_bot.py", "_synced", namespace)
        function("telegram: видео в очередь")
        self.assertEqual(calls, ["sync"])
        self.assertEqual(errors[0][1], "RuntimeError")


# ── F3: доки про nextStep ───────────────────────────────────────────────────

class NextStepDocsTests(unittest.TestCase):
    def compact(self, relative: str) -> str:
        return " ".join((ROOT / relative).read_text(encoding="utf-8").split())

    def test_pult_states_default_first_step(self) -> None:
        text = self.compact("Пульт.md").replace("*", "")
        self.assertIn("Первый шаг", text)
        self.assertIn("назван", text)
        self.assertNotIn("Оценку времени, шаги и смысловые связи "
                         "детерминированный путь не создаёт автоматически", text)

    def test_board_states_default_first_step(self) -> None:
        text = self.compact("Board/Начать здесь.md")
        self.assertIn("Первый шаг", text)
        self.assertNotIn("Оценку времени и шаги система сама не проставляет", text)


# ── F4: owner time независимо от TZ процесса ────────────────────────────────

class VaultBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.tasks = self.vault / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        (self.vault / "ZETA" / "FLEETING").mkdir(parents=True)
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

    def body(self) -> str:
        return next(self.tasks.glob("*.md")).read_text(encoding="utf-8")


class PackOwnerTimeTests(VaultBase):
    def test_pack_moment_is_taken_from_owner_time(self) -> None:
        owner = datetime.datetime(2026, 10, 6, 1, 0, tzinfo=TZ)
        seen = []

        def fake_to_almaty(moment=None):
            seen.append(moment)
            return owner

        with patch.object(weekly_plan, "to_almaty", fake_to_almaty):
            dispatch._daily_tasks("Задача")

        self.assertIn(None, seen,
                      "пакетный момент берётся у owner-time, а не из datetime.now")
        body = self.body()
        self.assertIn("scheduled: 2026-10-06", body)
        self.assertIn("created: 2026-10-06T01:00", body)

    def test_button_and_prefix_paths_share_owner_day(self) -> None:
        owner = datetime.datetime(2026, 10, 6, 9, 30, tzinfo=TZ)
        with patch.object(weekly_plan, "to_almaty", lambda moment=None: owner):
            dispatch._daily_tasks("Пакетная")
        package = [p for p in self.tasks.glob("*.md") if p.stem == "Пакетная"][0]
        self.assertIn("scheduled: 2026-10-06",
                      package.read_text(encoding="utf-8"))

    def test_process_tz_does_not_change_owner_day(self) -> None:
        old_tz = os.environ.get("TZ")

        def restore():
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            if hasattr(time, "tzset"):
                time.tzset()

        self.addCleanup(restore)
        if hasattr(time, "tzset"):
            os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14, далеко от Алматы
            time.tzset()

        dispatch._daily_tasks("Пакетная")
        expected = weekly_plan.today_local().isoformat()
        body = self.body()
        self.assertIn(f"scheduled: {expected}", body)
        self.assertIn(f"created: {expected}T", body)


if __name__ == "__main__":
    unittest.main()
