#!/usr/bin/env python3
"""Кнопочный хаб мыслей в Telegram: короткие токены, задачи, cooldown, stale.

Telegram-пакет на машине разработки не установлен, поэтому чистые контракты
(`task_ui`) проверяются рантаймом, а тонкий слой (`telegram_bot`) — статически
(AST). Никакого git, сети и токена бота.
"""

from __future__ import annotations

import ast
import datetime
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import dispatch
import runtime_store
import task_ui
import thought_feed

BOT = Path(__file__).resolve().parent
ROOT = BOT.parent
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=datetime.timezone.utc)
DAY = datetime.timedelta(days=1)
TELEGRAM = BOT / "telegram_bot.py"


def function_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def callback_data(view) -> list:
    return [data for row in view.rows for _label, data in row]


def labels(view) -> list:
    return [label for row in view.rows for label, _data in row]


def load_async_function(path: Path, name: str, namespace: dict):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            exec(compile(module, str(path), "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"нет async-функции {name}")


class ThoughtBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.daily = self.root / "ZETA" / "FLEETING_DAILY"
        self.daily.mkdir(parents=True)
        (self.root / "ZETA" / "FLEETING").mkdir(parents=True)
        self.inbox = self.root / "ZETA" / "FLEETING" / "00 Инбокс.md"
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        self.state: dict = {}
        self.store = runtime_store.RuntimeStore(self.root / "state" / "runtime.sqlite3")
        self.addCleanup(self.store.close)

    def note(self, days_ago: int, title: str, body: str = "") -> Path:
        date = (NOW.date() - DAY * days_ago).isoformat()
        path = self.daily / f"{date} — {title}.md"
        path.write_text(
            "---\ntype: fleeting_note\n"
            f"created: {date} 09:30\n---\n\n# {title}\n\n{body}\n",
            encoding="utf-8")
        return path

    def inbox_lines(self, lines) -> None:
        self.inbox.write_text(
            "# 📥 Inbox\n\n## 💭 Мысли и идеи\n\n" + "\n".join(lines) +
            "\n\n## ✅ Задачи\n\n## 🔗 Ссылки и материалы\n", encoding="utf-8")

    def identity_of(self, path: Path) -> str:
        for thought in thought_feed.recent_thoughts(self.root, limit=100):
            if thought.path.endswith(path.name):
                return thought.identity
        raise AssertionError("нет заметки " + path.name)


# ── главная клавиатура ──────────────────────────────────────────────────────

class MainButtonContractTests(unittest.TestCase):
    def test_thoughts_button_replaces_visible_search(self) -> None:
        self.assertEqual(len(task_ui.MAIN_BUTTONS), 6)
        self.assertIn(task_ui.BTN_THOUGHTS, task_ui.MAIN_BUTTONS)
        self.assertNotIn(task_ui.BTN_SEARCH, task_ui.MAIN_BUTTONS,
                         "старая подпись поиска не занимает главную клавиатуру")

    def test_search_label_remains_legacy_alias(self) -> None:
        self.assertEqual(task_ui.BTN_SEARCH, "🔎 Найти")
        self.assertEqual(task_ui.BTN_THOUGHTS, "🧠 Мысли")

    def test_start_documents_thoughts_hub(self) -> None:
        body = function_source(TELEGRAM, "start")
        self.assertIn("BTN_THOUGHTS", body)
        self.assertNotIn("🔎 Найти", body)


# ── хаб ─────────────────────────────────────────────────────────────────────

class HubRenderTests(ThoughtBase):
    def test_overview_has_four_bounded_actions(self) -> None:
        self.note(20, "Старая")
        view = task_ui.render_thoughts(
            self.state, self.root, store=self.store, now=NOW)
        for label in ("📥 Inbox", "🆕 Последние", "💡 Вспомнить", "🔎 Найти"):
            self.assertIn(label, labels(view))

    def test_overview_shows_recent_and_inbox(self) -> None:
        self.inbox_lines(["- **10:00** свежая мысль"])
        self.note(1, "Вчерашняя")
        view = task_ui.render_thoughts(
            self.state, self.root, store=self.store, now=NOW)
        self.assertIn("свежая мысль", view.text)
        self.assertIn("Вчерашняя", view.text)

    def test_callback_data_is_short_and_has_no_paths_or_text(self) -> None:
        self.inbox_lines(["- **10:00** секретная мысль пользователя"])
        path = self.note(20, "Старая заметка", body="приватное тело заметки")
        views = [
            task_ui.render_thoughts(self.state, self.root, store=self.store, now=NOW),
            task_ui.render_thoughts(self.state, self.root, store=self.store,
                                    now=NOW, section=task_ui.THOUGHT_SECTION_INBOX),
            task_ui.render_thoughts(self.state, self.root, store=self.store,
                                    now=NOW, section=task_ui.THOUGHT_SECTION_RECENT),
        ]
        for view in views:
            for data in callback_data(view):
                self.assertLessEqual(len(data.encode("utf-8")), 64, data)
                self.assertNotIn("ZETA/", data)
                self.assertNotIn(path.name, data)
                self.assertNotIn("секретная", data)
                self.assertNotIn("приватное", data)

    def test_stale_token_fails_closed_with_refresh(self) -> None:
        self.note(20, "Старая")
        view = task_ui.render_thought(self.state, self.root, self.store,
                                      "deadbeef", now=NOW)
        self.assertTrue(view.stale)
        self.assertIn("f:h", callback_data(view))


class ForgottenLifecycleTests(ThoughtBase):
    """Обзор только показывает кандидата; показ фиксирует лишь «💡 Вспомнить»."""

    def test_overview_does_not_consume_candidate_and_remember_opens_it(self) -> None:
        path = self.note(30, "Забытая")
        identity = self.identity_of(path)

        first = task_ui.render_thoughts(
            self.state, self.root, store=self.store, now=NOW)
        self.assertIn("Забытая", first.text)
        self.assertEqual(self.store.last_surface(identity), "",
                         "открытие обзора не записывает показ")

        second = task_ui.render_thoughts(
            self.state, self.root, store=self.store, now=NOW)
        self.assertIn("Забытая", second.text,
                      "повторное открытие обзора не съедает кандидата")

        remembered = task_ui.remember_thought(
            self.state, self.root, store=self.store, now=NOW)
        self.assertFalse(remembered.stale)
        self.assertIn("Забытая", remembered.text,
                      "«Вспомнить» открывает того же кандидата, а не пустоту")
        self.assertNotEqual(self.store.last_surface(identity), "",
                            "«Вспомнить» записывает показ")
        self.assertIsNone(
            thought_feed.forgotten_thought(self.root, self.store, NOW),
            "после показа кандидат уходит в cooldown")


# ── выбранная мысль и действия ──────────────────────────────────────────────

class ThoughtActionTests(ThoughtBase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.note(20, "Лендинг", body="переписать лендинг")
        self.identity = self.identity_of(self.path)
        self.token = task_ui.issue_thought_token(self.state, self.identity)

    def test_selected_thought_has_four_actions(self) -> None:
        view = task_ui.render_thought(self.state, self.root, self.store,
                                      self.token, now=NOW)
        for label in ("➡️ В задачу", "Следующая", "Скрыть на 7 дней", "⬅️ Назад"):
            self.assertIn(label, labels(view))
        self.assertIn("Лендинг", view.text)

    def test_next_advances_through_candidates(self) -> None:
        second = self.note(25, "Вторая")
        section = task_ui.render_thoughts(
            self.state, self.root, store=self.store, now=NOW,
            section=task_ui.THOUGHT_SECTION_RECENT)
        self.assertGreaterEqual(len(callback_data(section)), 2)
        view = task_ui.next_thought(self.state, self.root, self.store,
                                    self.token, now=NOW)
        self.assertFalse(view.stale)

    def test_hide_seven_days_records_cooldown(self) -> None:
        view = task_ui.hide_thought(self.state, self.store, self.token, now=NOW)
        self.assertFalse(view.stale)
        self.assertTrue(self.store.is_hidden(self.identity, now=NOW))
        forgotten = thought_feed.forgotten_thought(self.root, self.store, NOW)
        self.assertIsNone(forgotten)

    def test_task_from_thought_creates_exactly_one_and_preserves_note(self) -> None:
        before = self.path.read_text(encoding="utf-8")
        with patch.object(dispatch, "VAULT", self.root), \
                patch.object(dispatch, "INBOX", self.inbox), \
                patch.object(dispatch, "PRIVATE", self.root / "Private" / "00 Private.md"), \
                patch.object(dispatch, "_write_via_obsidian", return_value=False):
            first = task_ui.task_from_thought(
                self.state, self.root, self.token, now=NOW)
            second = task_ui.task_from_thought(
                self.state, self.root, self.token, now=NOW)
        created = list(self.tasks.glob("*.md"))
        self.assertEqual(len(created), 1, "повторный тап не создаёт дубль")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(second.synced, "дубль не запускает sync")
        self.assertEqual(before, self.path.read_text(encoding="utf-8"),
                         "исходная мысль не удалена и не изменена")

    def test_task_from_thought_survives_stale_token(self) -> None:
        result = task_ui.task_from_thought(
            self.state, self.root, "deadbeef", now=NOW)
        self.assertFalse(result.ok)
        self.assertEqual(list(self.tasks.glob("*.md")), [])

    def test_task_from_inbox_thought_preserves_inbox_line(self) -> None:
        self.inbox_lines(["- **10:00** позвонить клиенту по офферу"])
        identity = thought_feed.inbox_thoughts(self.root)[0].identity
        token = task_ui.issue_thought_token(self.state, identity)
        before = self.inbox.read_text(encoding="utf-8")
        with patch.object(dispatch, "VAULT", self.root), \
                patch.object(dispatch, "INBOX", self.inbox), \
                patch.object(dispatch, "PRIVATE", self.root / "Private" / "00 Private.md"), \
                patch.object(dispatch, "_write_via_obsidian", return_value=False):
            result = task_ui.task_from_thought(
                self.state, self.root, token, now=NOW)
        self.assertTrue(result.ok)
        self.assertEqual(len(list(self.tasks.glob("*.md"))), 1)
        self.assertEqual(before, self.inbox.read_text(encoding="utf-8"),
                         "строка Inbox не удалена и не изменена")


# ── тонкий слой Telegram ────────────────────────────────────────────────────

class RelatedHintTests(unittest.TestCase):
    """Суточный cooldown обязателен: без store похожая мысль не показывается."""

    def load(self, namespace):
        src = TELEGRAM.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_related_hint":
                module = ast.Module(body=[node], type_ignores=[])
                exec(compile(module, str(TELEGRAM), "exec"), namespace)
                return namespace["_related_hint"]
        raise AssertionError("нет _related_hint")

    def base_namespace(self):
        return {
            "_vault": lambda: Path("/nowhere"),
            "_now": lambda: NOW,
            "runtime_store": types.SimpleNamespace(RuntimeStoreError=RuntimeError),
            "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
        }

    def test_missing_store_is_silent(self) -> None:
        calls = []
        namespace = self.base_namespace()
        namespace["_runtime_store"] = lambda: None
        namespace["thought_feed"] = types.SimpleNamespace(
            related_thought=lambda *a, **k: calls.append(1) or None)
        function = self.load(namespace)
        self.assertEqual(function("переписать лендинг"), "")
        self.assertEqual(calls, [], "без store cooldown не проверить — не ищем")

    def test_store_error_is_logged_not_raised(self) -> None:
        logged = []

        def boom(*args, **kwargs):
            raise RuntimeError("db")

        namespace = self.base_namespace()
        namespace["_runtime_store"] = lambda: object()
        namespace["thought_feed"] = types.SimpleNamespace(related_thought=boom)
        namespace["logger"] = types.SimpleNamespace(
            warning=lambda *a, **k: logged.append(a))
        function = self.load(namespace)
        self.assertEqual(function("переписать лендинг"), "")
        self.assertTrue(logged, "ошибка store логируется, а не глотается")


class RelatedHintIntegrationTests(ThoughtBase):
    """Реальный read-сбой runtime store не ломает захват: подсказка молчит."""

    def load(self, namespace):
        src = TELEGRAM.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_related_hint":
                module = ast.Module(body=[node], type_ignores=[])
                exec(compile(module, str(TELEGRAM), "exec"), namespace)
                return namespace["_related_hint"]
        raise AssertionError("нет _related_hint")

    def test_real_read_error_is_silent_and_logged(self) -> None:
        self.note(20, "Лендинг", body="переписать лендинг про оффер и цену")
        with self.store.connection() as conn:
            conn.execute("DROP TABLE meta")
        warnings = []
        function = self.load({
            "_vault": lambda: self.root,
            "_now": lambda: NOW,
            "_runtime_store": lambda: self.store,
            "runtime_store": runtime_store,
            "thought_feed": thought_feed,
            "logger": types.SimpleNamespace(
                warning=lambda *a, **k: warnings.append(a)),
        })
        self.assertEqual(function("переписать лендинг про оффер"), "")
        self.assertTrue(warnings, "сбой состояния логируется, а не глотается")


class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class _FakeUpdate:
    def __init__(self):
        self.message = _FakeMessage()


class _FakeContext:
    def __init__(self, state=None):
        self.user_data = state if state is not None else {}


class _FakeUI:
    MODE_TASK = task_ui.MODE_TASK
    MODE_NOTE = task_ui.MODE_NOTE
    MODE_VIDEO = task_ui.MODE_VIDEO
    MODE_SEARCH = task_ui.MODE_SEARCH
    SCREEN_MAIN = task_ui.SCREEN_MAIN
    SCREEN_CANCEL = task_ui.SCREEN_CANCEL

    def thought_edit_pending(self, state):
        return False

    def mode(self, state):
        return ""

    def cancel(self, state):
        return None

    def begin(self, state, name):
        return "prompt"

    def mutation_sync(self, result, sync):
        return False


async def _no_reminder(update, context, text):
    return False


class CaptureConfirmationTests(unittest.IsolatedAsyncioTestCase):
    """Сохранённый захват всегда получает подтверждение, даже если hint молчит."""

    async def test_confirmation_when_related_hint_is_silent(self) -> None:
        appended = []
        async def reply_saved(update, context, text, path="", line=""):
            await update.message.reply_text(text)

        namespace = {
            "re": __import__("re"),
            "asyncio": __import__("asyncio"),
            "ui": _FakeUI(),
            "thought_digest": __import__("thought_digest"),
            "assistant_router": __import__("assistant_router"),
            "personal_search": __import__("personal_search"),
            "ask": types.SimpleNamespace(answer=lambda query: "RESEARCH"),
            "_dispatch": types.SimpleNamespace(
                shared_prefix=lambda text: False,
                dispatch_detailed=lambda text: types.SimpleNamespace(
                    text="LEGACY", mutated=False, private=False, kind="")),
            "market": types.SimpleNamespace(parse_command=lambda text: None),
            "append_to_inbox": lambda text, kind="thought", private=False: (
                appended.append(text) or "line"),
            "_related_hint": lambda text: "",
            "_maybe_menu_reminder": _no_reminder,
            "_reply_saved_thought": reply_saved,
            "_maybe_reminder_custom": _no_reminder,
            "_maybe_reminder_intent": _no_reminder,
            "_vault": lambda: Path("/nowhere"),
            "_synced": lambda message: None,
            "_keyboard": lambda screen: None,
            "_sync_task": lambda: None,
            "_sync_note": lambda: None,
            "_sync_video": lambda: None,
            "MAIN_KEYBOARD": None,
            "logger": types.SimpleNamespace(error=lambda *a, **k: None,
                                            warning=lambda *a, **k: None),
        }
        namespace["handle_content"] = load_async_function(
            TELEGRAM, "handle_content", namespace)
        update = _FakeUpdate()
        await namespace["handle_content"](
            update, _FakeContext(), "сегодня хорошая погода", origin="text")
        self.assertEqual(appended, ["сегодня хорошая погода"])
        self.assertTrue(update.message.replies)
        self.assertIn("Записал в Inbox", update.message.replies[0][0])


class TelegramWiringTests(unittest.TestCase):
    def test_callback_routes_thought_prefix(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertIn('"f:"', body)
        self.assertIn("safe_edit", body)

    def test_callback_search_action_enters_existing_search_mode(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertIn("ui.MODE_SEARCH", body)
        self.assertIn("ui.begin", body)

    def test_thought_callbacks_use_short_prefixes_only(self) -> None:
        body = function_source(TELEGRAM, "handle_callback")
        self.assertNotIn("edit_message_text", body)
        self.assertIn("logger.error", body)

    def test_capture_flow_keeps_inbox_default(self) -> None:
        body = function_source(TELEGRAM, "handle_content")
        self.assertIn("append_to_inbox", body)


if __name__ == "__main__":
    unittest.main()
