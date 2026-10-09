#!/usr/bin/env python3
"""Runtime transport: typed reminder intent is handled before search/capture."""

from __future__ import annotations

import ast
import asyncio
import datetime
import tempfile
import types
import unittest
from pathlib import Path

import assistant_router
import personal_search
import reminder_nlp
import reminder_ui
import runtime_store
import task_metadata

BOT = Path(__file__).resolve().parent
TELEGRAM = BOT / "telegram_bot.py"
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def load_function(path: Path, name: str, namespace: dict):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            exec(compile(module, str(path), "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"нет функции {name}")


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class FakeUpdate:
    def __init__(self, update_id=1):
        self.update_id = update_id
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, state=None):
        self.user_data = state if state is not None else {}


class FakeUI:
    MODE_TASK = "task"
    MODE_NOTE = "note"
    MODE_VIDEO = "video"
    MODE_SEARCH = "search"
    SCREEN_MAIN = "main"
    SCREEN_CANCEL = "cancel"

    def thought_edit_pending(self, state):
        return False

    def mode(self, state):
        return ""

    def cancel(self, state):
        return None

    def begin(self, state, name):
        return "prompt"

    def keyboard_for(self, result):
        return "main"

    def mutation_sync(self, result, sync):
        return False


class Router:
    KIND_TASK = assistant_router.KIND_TASK
    KIND_PERSONAL_SEARCH = assistant_router.KIND_PERSONAL_SEARCH
    KIND_KNOWLEDGE_SEARCH = assistant_router.KIND_KNOWLEDGE_SEARCH

    def __init__(self):
        self.calls = []

    def route(self, text):
        self.calls.append(text)
        return assistant_router.Route(
            assistant_router.KIND_PERSONAL_SEARCH, subject=text, personal=True)


class TransportBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        self.store = runtime_store.RuntimeStore(
            self.root / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)
        self.router = Router()
        self.appended = []
        self.synced = []

        namespace = {
            "re": __import__("re"),
            "asyncio": asyncio,
            "ui": FakeUI(),
            "assistant_router": self.router,
            "personal_search": personal_search,
            "ask": types.SimpleNamespace(answer=lambda query: "RESEARCH"),
            "_dispatch": types.SimpleNamespace(
                shared_prefix=lambda text: False,
                dispatch_detailed=lambda text: types.SimpleNamespace(
                    text="LEGACY", mutated=False, private=False, kind="")),
            "market": types.SimpleNamespace(parse_command=lambda text: None),
            "append_to_inbox": lambda text, kind="thought", private=False: (
                self.appended.append(text) or "line"),
            "_related_hint": lambda text: "",
            "reminder_nlp": reminder_nlp,
            "reminder_ui": reminder_ui,
            "_vault": lambda: self.root,
            "_runtime_store": lambda: self.store,
            "_now": lambda: NOW,
            "_sync_reminder": lambda: self.synced.append("reminder"),
            "_wake_reminder": lambda context: None,
            "_inline": lambda view: None,
            "MAIN_KEYBOARD": None,
            "_synced": lambda message: None,
            "_keyboard": lambda screen: None,
            "_sync_task": lambda: None,
            "_sync_note": lambda: None,
            "_sync_video": lambda: None,
            "logger": types.SimpleNamespace(
                error=lambda *a, **k: None, warning=lambda *a, **k: None),
        }
        for name in ("_maybe_menu_reminder", "_maybe_reminder_custom", "_maybe_reminder_intent",
                     "handle_content"):
            load_function(TELEGRAM, name, namespace)
        self.namespace = namespace

    def task(self, name: str = "Задача.md") -> Path:
        title = name[:-3] if name.endswith(".md") else name
        path = self.tasks / (title + ".md")
        path.write_text(
            "---\n" f'title: "{title}"\nstatus: next\n'
            "---\n" f"# {title}\n", encoding="utf-8")
        return path

    async def run_content(self, text, update_id=1, origin="text", state=None):
        update = FakeUpdate(update_id)
        context = FakeContext(state)
        await self.namespace["handle_content"](update, context, text,
                                               origin=origin)
        return update.message.replies


class IntentRuntimeTests(TransportBase):
    async def test_past_intent_is_error_not_search(self) -> None:
        replies = await self.run_content("напомни сегодня в 9 отправить отчёт")
        self.assertTrue(replies[0][0].startswith("⚠️"), replies)
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.appended, [])

    async def test_huge_intent_is_error_not_capture(self) -> None:
        replies = await self.run_content(
            "напомни через 999999999999999999999 дней отчёт")
        self.assertTrue(replies[0][0].startswith("⚠️"), replies)
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.appended, [])

    async def test_missing_subject_is_error(self) -> None:
        replies = await self.run_content("напомни завтра в 9")
        self.assertIn("Что напомнить", replies[0][0])
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.appended, [])

    async def test_no_time_falls_through_to_search(self) -> None:
        replies = await self.run_content("напомни мою мысль про X")
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual(self.appended, [])
        self.assertIn("Dusk", replies[0][0])

    async def test_valid_intent_creates_task_and_reminder(self) -> None:
        replies = await self.run_content("напомни завтра в 9 отправить отчёт")
        self.assertIn("⏰ Напомню", replies[0][0])
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.appended, [])
        tasks = list(self.tasks.glob("*.md"))
        self.assertEqual(len(tasks), 1)
        self.assertIn("отправить отчёт", tasks[0].name)
        snapshot = task_metadata.read_task(self.root, tasks[0].name)
        self.assertEqual(len([e for e in snapshot.reminders if e.is_dusk()]), 1)

    async def test_voice_origin_shares_path(self) -> None:
        replies = await self.run_content("напомни завтра в 9 отправить отчёт",
                                         origin="voice")
        self.assertIn("⏰ Напомню", replies[0][0])
        self.assertEqual(len(list(self.tasks.glob("*.md"))), 1)

    async def test_duplicate_update_is_idempotent(self) -> None:
        await self.run_content("напомни завтра в 9 отправить отчёт", update_id=7)
        await self.run_content("напомни завтра в 9 отправить отчёт", update_id=7)
        tasks = list(self.tasks.glob("*.md"))
        self.assertEqual(len(tasks), 1)
        snapshot = task_metadata.read_task(self.root, tasks[0].name)
        self.assertEqual(len([e for e in snapshot.reminders if e.is_dusk()]), 1)


class CustomRuntimeTests(TransportBase):
    async def test_custom_creates_reminder(self) -> None:
        path = self.task()
        state = {reminder_ui.INPUT_KEY: {"path": str(path), "request": "req1"}}
        replies = await self.run_content("через 2 часа", state=state, update_id=3)
        self.assertIn("🔔 Напоминание", replies[0][0])
        self.assertFalse(reminder_ui.custom_pending(state))
        snapshot = task_metadata.read_task(self.root, "Задача.md")
        self.assertEqual(len([e for e in snapshot.reminders if e.is_dusk()]), 1)

    async def test_custom_bad_time_keeps_mode(self) -> None:
        path = self.task()
        state = {reminder_ui.INPUT_KEY: {"path": str(path), "request": "req1"}}
        replies = await self.run_content("потом как-нибудь", state=state)
        self.assertTrue(replies[0][0].startswith("⚠️"))
        self.assertTrue(reminder_ui.custom_pending(state))
        self.assertEqual(task_metadata.read_task(
            self.root, "Задача.md").reminders, ())


if __name__ == "__main__":
    unittest.main()
