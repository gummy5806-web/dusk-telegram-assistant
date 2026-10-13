#!/usr/bin/env python3
"""Dusk-first ассистент: один content-путь для текста и голоса, кнопки, доки.

Telegram-пакет на машине разработки не установлен, поэтому тонкий слой
проверяется статически (AST), а общий content-путь — рантаймом с внедрёнными
зависимостями (тот же приём, что у `_synced` в test_telegram_ux_pass4).
"""

from __future__ import annotations

import ast
import tempfile
import types
import unittest
from pathlib import Path

import assistant_router
import personal_search
import task_ui

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


def load_async_function(path: Path, name: str, namespace: dict):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            exec(compile(module, str(path), "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"нет async-функции {name}")


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class FakeUpdate:
    def __init__(self):
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, state=None):
        self.user_data = state if state is not None else {}


class FakeUI:
    """Константы из task_ui + записывающие submit-функции."""

    MODE_TASK = task_ui.MODE_TASK
    MODE_NOTE = task_ui.MODE_NOTE
    MODE_VIDEO = task_ui.MODE_VIDEO
    MODE_SEARCH = task_ui.MODE_SEARCH
    SCREEN_MAIN = task_ui.SCREEN_MAIN
    SCREEN_CANCEL = task_ui.SCREEN_CANCEL

    def thought_edit_pending(self, state):
        return False

    def __init__(self):
        self.calls = []

    def mode(self, state):
        return state.get("_mode", "")

    def cancel(self, state):
        state.pop("_mode", None)

    def begin(self, state, name):
        state["_mode"] = name
        return "prompt"

    def keyboard_for(self, result):
        return self.SCREEN_CANCEL if not result.ok else self.SCREEN_MAIN

    def _ok(self, label, text):
        return types.SimpleNamespace(ok=True, text=text, synced=True, path="")

    def submit_task(self, state, text, vault=None, sync=None):
        self.calls.append(("task", text))
        state.pop("_mode", None)
        return self._ok("task", "TASK:" + text)

    def submit_note(self, state, text, sync=None):
        self.calls.append(("note", text))
        state.pop("_mode", None)
        return self._ok("note", "NOTE:" + text)

    def submit_video(self, state, text, sync=None):
        self.calls.append(("video", text))
        state.pop("_mode", None)
        return self._ok("video", "VIDEO:" + text)

    def mutation_sync(self, result, sync):
        return False

    def render_list(self, *args, **kwargs):
        return types.SimpleNamespace(text="list", rows=[])


class ContentHarness:
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.ui = FakeUI()
        self.appended = []
        self.synced = []
        self.ask_calls = []

        research = tempfile.TemporaryDirectory()
        self.addCleanup(research.cleanup)

        def append(text, kind="thought", private=False):
            self.appended.append((text, kind, private))
            return "line"

        def synced(message):
            self.synced.append(message)

        async def reply_task(update, context, result):
            await update.message.reply_text(result.text, reply_markup="MAIN_KB")

        async def reply_saved(update, context, text, path="", line=""):
            await update.message.reply_text(text, reply_markup="MAIN_KB")

        self.namespace = {
            "re": __import__("re"),
            "asyncio": __import__("asyncio"),
            "ui": self.ui,
            "thought_digest": __import__("thought_digest"),
            "assistant_router": assistant_router,
            "personal_search": personal_search,
            "resource_ui": __import__("resource_ui"),
            "queue_store": __import__("queue_store"),
            "_inline": lambda view: "MAIN_KB",
            "_semantic_query_button": lambda state, query: ("Поиск по смыслу", "ss:test"),
            "ask": types.SimpleNamespace(
                answer=lambda query: (self.ask_calls.append(query) or "RESEARCH")),
            "_dispatch": types.SimpleNamespace(
                shared_prefix=lambda text: False,
                dispatch_detailed=lambda text: types.SimpleNamespace(
                    text="LEGACY", mutated=False, private=False, kind="")),
            "market": types.SimpleNamespace(parse_command=lambda text: None),
            "append_to_inbox": append,
            "_related_hint": lambda text: "",
            "_maybe_menu_reminder": self._no_reminder,
            "_reply_task_submission": reply_task,
            "_reply_saved_thought": reply_saved,
            "_maybe_reminder_custom": self._no_reminder,
            "_maybe_reminder_intent": self._no_reminder,
            "_vault": lambda: self.vault,
            "_synced": synced,
            "_keyboard": lambda screen: "KB:" + str(screen),
            "_sync_task": lambda: synced("task"),
            "_sync_note": lambda: synced("note"),
            "_sync_video": lambda: synced("video"),
            "MAIN_KEYBOARD": "MAIN_KB",
            "logger": types.SimpleNamespace(
                error=lambda *a, **k: None, warning=lambda *a, **k: None),
        }
        self.namespace["handle_content"] = load_async_function(
            TELEGRAM, "handle_content", self.namespace)

    async def run_content(self, text, mode="", origin="text"):
        update = FakeUpdate()
        context = FakeContext({"_mode": mode} if mode else {})
        await self.namespace["handle_content"](update, context, text, origin=origin)
        return update.message.replies

    async def _no_reminder(self, update, context, text):
        return False


class SharedPathTests(unittest.TestCase):
    def test_text_and_voice_share_one_content_path(self) -> None:
        for name in ("handle_text", "handle_voice"):
            body = function_source(TELEGRAM, name)
            with self.subTest(name=name):
                self.assertIn("handle_content(", body,
                              f"{name} обязан звать общий content-путь")

    def test_voice_does_not_capture_directly(self) -> None:
        body = function_source(TELEGRAM, "handle_voice")
        self.assertNotIn("append_to_inbox", body,
                         "голос только расшифровывает и делегирует")
        self.assertIn("speech_to_text.transcribe_file", body)
        self.assertNotIn("DeepgramClient", body)
        self.assertIn("transcript", body)
        self.assertIn("origin=", body)


class ModeTests(ContentHarness, unittest.IsolatedAsyncioTestCase):
    async def test_pending_search_mode_runs_personal_search(self) -> None:
        (self.vault / "ZETA" / "FLEETING").mkdir(parents=True)
        (self.vault / "ZETA" / "FLEETING" / "00 Инбокс.md").write_text(
            "# 📥 Inbox\n\n## 💭 Мысли и идеи\n\n"
            "- **10:00** продумал аудит воронки\n\n"
            "## ✅ Задачи\n\n## 🔗 Ссылки и материалы\n",
            encoding="utf-8")
        replies = await self.run_content("аудит", mode=task_ui.MODE_SEARCH,
                                         origin="voice")
        self.assertTrue(replies)
        self.assertIn("Из твоего Dusk", replies[0][0])
        self.assertEqual(self.ask_calls, [], "кнопка «Найти» ищет в Dusk, не в Research")

    async def test_pending_task_mode_accepts_voice_transcript(self) -> None:
        await self.run_content("купить кабель", mode=task_ui.MODE_TASK,
                               origin="voice")
        self.assertEqual(self.ui.calls, [("task", "купить кабель")])

    async def test_pending_thought_mode_accepts_voice_transcript(self) -> None:
        await self.run_content("мысль про оффер", mode=task_ui.MODE_NOTE,
                               origin="voice")
        self.assertEqual(self.ui.calls, [("note", "мысль про оффер")])


class FreeRoutingTests(ContentHarness, unittest.IsolatedAsyncioTestCase):
    async def test_personal_recall_never_calls_research(self) -> None:
        (self.vault / "ZETA" / "FLEETING").mkdir(parents=True)
        (self.vault / "ZETA" / "FLEETING" / "00 Инбокс.md").write_text(
            "# 📥 Inbox\n\n## 💭 Мысли и идеи\n\n"
            "- **10:00** думал про оффер\n\n"
            "## ✅ Задачи\n\n## 🔗 Ссылки и материалы\n",
            encoding="utf-8")
        replies = await self.run_content("что я думал про оффер")
        self.assertEqual(self.ask_calls, [], "личный запрос не идёт в Research Brain")
        self.assertIn("Из твоего Dusk", replies[0][0])

    async def test_knowledge_query_returns_both_sections(self) -> None:
        (self.vault / "ZETA" / "FLEETING").mkdir(parents=True)
        (self.vault / "ZETA" / "FLEETING" / "00 Инбокс.md").write_text(
            "# 📥 Inbox\n\n## 💭 Мысли и идеи\n\n"
            "- **10:00** оффер из воронки\n\n"
            "## ✅ Задачи\n\n## 🔗 Ссылки и материалы\n",
            encoding="utf-8")
        replies = await self.run_content("что Хормози говорил про оффер")
        text = replies[0][0]
        self.assertEqual(len(self.ask_calls), 1)
        self.assertIn("Из твоего Dusk", text)
        self.assertIn("Из агентских конспектов", text)
        self.assertLess(text.index("Из твоего Dusk"),
                        text.index("Из агентских конспектов"))

    async def test_ambiguous_text_captures_to_inbox_once(self) -> None:
        replies = await self.run_content("сегодня хорошая погода")
        self.assertEqual(self.appended, [("сегодня хорошая погода", "thought", False)])
        self.assertTrue(replies)

    async def test_natural_task_creates_task(self) -> None:
        await self.run_content("создай задачу купить кабель")
        self.assertEqual(self.ui.calls, [("task", "купить кабель")])


class DocumentedUxTests(unittest.TestCase):
    def test_main_buttons_match_dusk_goals(self) -> None:
        self.assertEqual(
            list(task_ui.MAIN_BUTTONS),
            ["💭 Мысль", "🧠 Мысли", "➕ Задача",
             "📋 Задачи", "🎓 Материал", "📊 Статус"],
        )
        self.assertNotIn("🩺 Здоровье", task_ui.MAIN_BUTTONS,
                         "health не занимает главную клавиатуру")

    def test_search_mode_exists(self) -> None:
        state: dict = {}
        task_ui.begin(state, task_ui.MODE_SEARCH)
        self.assertEqual(task_ui.mode(state), task_ui.MODE_SEARCH)
        task_ui.cancel(state)
        self.assertEqual(task_ui.mode(state), "")

    def test_start_has_no_colon_grammar(self) -> None:
        body = function_source(TELEGRAM, "start")
        for token in ("задача:", "мысль:", "найди:", "?как", "спроси:"):
            with self.subTest(token=token):
                self.assertNotIn(token, body)

    def test_docs_do_not_teach_colon_grammar(self) -> None:
        for relative in ("Пульт.md", "Board/Начать здесь.md", "bot/INTERFACE.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            compact = " ".join(text.split())
            with self.subTest(relative=relative):
                self.assertNotIn("задача:", compact)
                self.assertNotIn("мысль:", compact)
                self.assertNotIn("найди:", compact)
                self.assertNotIn("день:", compact)

    def test_docs_explain_research_is_secondary_and_reminders_not_ready(self) -> None:
        for relative in ("Пульт.md", "Board/Начать здесь.md", "bot/INTERFACE.md"):
            text = (ROOT / relative).read_text(encoding="utf-8").lower()
            with self.subTest(relative=relative):
                self.assertIn("research brain", text)
                self.assertIn("dusk", text)

    def test_callback_data_stays_short(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        vault = Path(directory.name)
        tasks = vault / "TaskNotes" / "Tasks"
        tasks.mkdir(parents=True)
        (tasks / "Проверить воронку.md").write_text(
            '---\ntitle: "Проверить воронку"\nstatus: next\n---\n\n# Проверить воронку\n',
            encoding="utf-8")
        state: dict = {}
        view = task_ui.render_list(state, vault)
        for row in view.rows:
            for _label, data in row:
                self.assertLessEqual(len(data.encode("utf-8")), 64, data)


if __name__ == "__main__":
    unittest.main()
