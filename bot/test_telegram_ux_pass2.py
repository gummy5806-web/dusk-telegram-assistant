#!/usr/bin/env python3
"""Регрессии ревью-прохода 2.

Проверяются рантаймом, без Telegram-пакета: sync на всех fallback-мутациях,
boundary и concurrency точного helper, гонка создания, инвалидация поколения
токенов, зона владельца и клавиатура при ошибке ввода.
"""

from __future__ import annotations

import datetime
import ast
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import dispatch
import task_ui

NOW = datetime.datetime(
    2026, 10, 7, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5)))
UTC = datetime.timezone.utc


def task_text(title: str, status: str = "next") -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        f"status: {status}\n"
        "---\n\n"
        f"# {title}\n"
    )


def tokens_of(view: task_ui.View) -> list:
    return [data.split(":", 1)[1] for row in view.rows for _label, data in row
            if data.startswith("t:")]


def function_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def callback_data(view: task_ui.View) -> list:
    return [data for row in view.rows for _label, data in row]


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

    def write_card(self, name: str, title: str = "", status: str = "next") -> Path:
        path = self.tasks / f"{name}.md"
        path.write_text(task_text(title or name, status), encoding="utf-8")
        return path

    def recorder(self):
        calls = []

        def sync(*args):
            calls.append(args[0] if args else None)

        return calls, sync


# ── P1: sync на всех fallback-мутациях ──────────────────────────────────────

class DispatchDetailedTests(VaultBase):
    def test_task_creation_is_mutated(self) -> None:
        result = dispatch.dispatch_detailed("задача: Купить кабель")
        self.assertTrue(result.mutated)
        self.assertEqual(result.kind, "task")
        self.assertFalse(result.private)
        self.assertIn("Создано задач", result.text)

    def test_duplicate_task_is_not_mutated(self) -> None:
        dispatch.dispatch_detailed("задача: Купить кабель")
        again = dispatch.dispatch_detailed("задача: Купить кабель")
        self.assertFalse(again.mutated)
        self.assertEqual(len(list(self.tasks.glob("*.md"))), 1)

    def test_day_pack_is_mutated(self) -> None:
        result = dispatch.dispatch_detailed("день:\nПервая задача\nВторая задача")
        self.assertTrue(result.mutated)
        self.assertEqual(len(list(self.tasks.glob("*.md"))), 2)

    def test_advance_is_mutated_once(self) -> None:
        self.write_card("Проверить гипотезу")
        first = dispatch.dispatch_detailed("готово Проверить гипотезу")
        self.assertTrue(first.mutated)
        self.assertEqual(first.kind, "advance")
        second = dispatch.dispatch_detailed("готово Проверить гипотезу")
        self.assertFalse(second.mutated, "повторный done — не мутация")

    def test_not_found_advance_is_not_mutated(self) -> None:
        result = dispatch.dispatch_detailed("готово нет такой задачи")
        self.assertFalse(result.mutated)

    def test_note_is_mutated(self) -> None:
        result = dispatch.dispatch_detailed("заметка: связать задачи и заметки")
        self.assertTrue(result.mutated)
        self.assertEqual(result.kind, "note")
        notes = list((self.vault / "ZETA" / "FLEETING_DAILY").glob("*.md"))
        self.assertEqual(len(notes), 1)

    def test_private_route_is_not_tracked_mutation(self) -> None:
        result = dispatch.dispatch_detailed("!секретный текст")
        self.assertFalse(result.mutated)
        self.assertTrue(result.private)
        self.assertEqual(result.kind, "private")

    def test_looks_private_task_becomes_private(self) -> None:
        result = dispatch.dispatch_detailed("задача: записаться к врачу")
        self.assertTrue(result.private)
        self.assertFalse(result.mutated)
        self.assertEqual(list(self.tasks.glob("*.md")), [])

    def test_search_is_not_mutated(self) -> None:
        with patch.object(dispatch.ask, "answer", return_value="ответ"):
            result = dispatch.dispatch_detailed("?как считать LTV")
        self.assertFalse(result.mutated)

    def test_dispatch_text_contract_is_unchanged(self) -> None:
        self.assertIsInstance(dispatch.dispatch("задача: Текст"), str)


class MutationSyncTests(unittest.TestCase):
    def sync_calls(self, result):
        calls = []

        def sync(message):
            calls.append(message)

        task_ui.mutation_sync(result, sync)
        return calls

    def test_task_mutation_syncs_once_neutral(self) -> None:
        calls = self.sync_calls(SimpleNamespace(mutated=True, private=False, kind="task"))
        self.assertEqual(calls, [task_ui.SYNC_MESSAGES["task"]])

    def test_advance_mutation_uses_status_message(self) -> None:
        calls = self.sync_calls(SimpleNamespace(mutated=True, private=False, kind="advance"))
        self.assertEqual(calls, [task_ui.SYNC_MESSAGES["status"]])

    def test_note_and_link_use_note_message(self) -> None:
        for kind in ("note", "link"):
            with self.subTest(kind=kind):
                calls = self.sync_calls(SimpleNamespace(mutated=True, private=False, kind=kind))
                self.assertEqual(calls, [task_ui.SYNC_MESSAGES["note"]])

    def test_video_and_counter_have_neutral_messages(self) -> None:
        for kind in ("video", "counter"):
            with self.subTest(kind=kind):
                calls = self.sync_calls(SimpleNamespace(mutated=True, private=False, kind=kind))
                self.assertEqual(calls, [task_ui.SYNC_MESSAGES[kind]])

    def test_no_sync_without_mutation_or_for_private(self) -> None:
        self.assertEqual(self.sync_calls(SimpleNamespace(mutated=False, private=False, kind="task")), [])
        self.assertEqual(self.sync_calls(SimpleNamespace(mutated=True, private=True, kind="task")), [])

    def test_all_neutral_messages_are_static(self) -> None:
        for kind in ("task", "status", "note", "video", "counter"):
            message = task_ui.SYNC_MESSAGES[kind]
            self.assertTrue(message)
            self.assertNotIn("{", message)


# ── P1: boundary и concurrency точного helper ───────────────────────────────

class ExactHelperBoundaryTests(VaultBase):
    def setUp(self) -> None:
        super().setUp()
        self.outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside_dir.cleanup)
        self.outside = Path(self.outside_dir.name)

    def test_outside_file_is_rejected(self) -> None:
        target = self.outside / "чужая.md"
        target.write_text(task_text("чужая"), encoding="utf-8")
        before = target.read_text(encoding="utf-8")
        changed, _message = dispatch.set_task_status(target, "done", now=NOW)
        self.assertFalse(changed)
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_symlink_outside_is_rejected(self) -> None:
        target = self.outside / "цель.md"
        target.write_text(task_text("цель"), encoding="utf-8")
        before = target.read_text(encoding="utf-8")
        link = self.tasks / "ссылка.md"
        link.symlink_to(target)
        changed, _message = dispatch.set_task_status(link, "done", now=NOW)
        self.assertFalse(changed)
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_non_markdown_in_tasks_dir_is_rejected(self) -> None:
        path = self.tasks / "заметка.txt"
        path.write_text(task_text("не md"), encoding="utf-8")
        changed, _message = dispatch.set_task_status(path, "done", now=NOW)
        self.assertFalse(changed)

    def test_traversal_is_rejected(self) -> None:
        escaped = self.tasks / ".." / ".." / "escaped.md"
        escaped.parent.mkdir(parents=True, exist_ok=True)
        escaped.write_text(task_text("побег"), encoding="utf-8")
        changed, _message = dispatch.set_task_status(escaped, "done", now=NOW)
        self.assertFalse(changed)

    def test_repeated_callback_is_single_change(self) -> None:
        card = self.write_card("карточка")
        first, _ = dispatch.set_task_status(card, "done", now=NOW)
        second, _ = dispatch.set_task_status(card, "done", now=NOW)
        self.assertTrue(first)
        self.assertFalse(second)

    def test_concurrent_callbacks_yield_single_change(self) -> None:
        card = self.write_card("гонка статуса", status="next")
        results = []
        barrier = threading.Barrier(2)

        def run():
            barrier.wait(timeout=5)
            results.append(dispatch.set_task_status(card, "done", now=NOW)[0])

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(results), [False, True],
                         "два callback дают ровно одно изменение")

    def test_dispatch_advance_does_not_deadlock(self) -> None:
        self.write_card("без дедлока")
        messages = []

        def worker():
            messages.append(dispatch.dispatch("готово без дедлока"))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "вложенный lock не должен вешать dispatch")
        self.assertTrue(messages)
        self.assertIn("✅", messages[0])


# ── P1: гонка создания не даёт дубль ─────────────────────────────────────────

class ConcurrentCreateTests(VaultBase):
    def test_race_loser_rechecks_identity(self) -> None:
        real_write = dispatch._write_task
        seen = []

        def racing_write(path, body):
            if not seen:
                seen.append(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
                return False
            return real_write(path, body)

        with patch.object(dispatch, "_write_task", racing_write):
            path, created = dispatch.create_task_exact("Гонка")

        files = list(self.tasks.glob("*.md"))
        self.assertEqual(len(files), 1, "проигравший не создаёт «Название 1.md»")
        self.assertFalse(created)
        self.assertEqual(Path(path).name, "Гонка.md")

    def test_threads_create_one_file(self) -> None:
        results = []
        barrier = threading.Barrier(6)

        def run():
            barrier.wait(timeout=5)
            results.append(dispatch.create_task_exact("Одновременно")[1])

        threads = [threading.Thread(target=run) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(list(self.tasks.glob("*.md"))), 1)


# ── P1: инвалидация поколения токенов ───────────────────────────────────────

class GenerationTests(VaultBase):
    def test_new_root_invalidates_previous_tokens(self) -> None:
        self.write_card("первая")
        first = task_ui.render_list(self.state, self.vault, root=True)
        token = tokens_of(first)[0]

        task_ui.render_list(self.state, self.vault, root=True)

        calls, sync = self.recorder()
        view, changed = task_ui.apply_status(
            self.state, self.vault, token, "done", sync=sync, now=NOW)
        self.assertFalse(changed, "старый токен после нового списка не меняет карточку")
        self.assertEqual(calls, [], "stale-токен не запускает sync")
        self.assertIn("обнов", view.text.lower())

    def test_tokens_survive_within_generation(self) -> None:
        self.write_card("живая")
        first = task_ui.render_list(self.state, self.vault, root=True)
        token = tokens_of(first)[0]

        task_ui.render_list(self.state, self.vault, page=0)  # не root

        _view, changed = task_ui.apply_status(
            self.state, self.vault, token, "in-progress", now=NOW)
        self.assertTrue(changed)

    def test_pagination_keeps_generation(self) -> None:
        for number in range(5):
            self.write_card(f"Задача {number}")
        first = task_ui.render_list(self.state, self.vault, page=0, page_size=2, root=True)
        token = tokens_of(first)[0]

        task_ui.render_list(self.state, self.vault, page=1, page_size=2)

        _view, changed = task_ui.apply_status(
            self.state, self.vault, token, "in-progress", now=NOW)
        self.assertTrue(changed)

    def test_callback_data_stays_short_without_text(self) -> None:
        self.write_card("секретное имя")
        view = task_ui.render_list(self.state, self.vault, root=True)
        for data in callback_data(view):
            self.assertLessEqual(len(data.encode("utf-8")), 64)
            self.assertNotIn("секретное", data)
            self.assertNotIn("/", data)


# ── P2: зона владельца ──────────────────────────────────────────────────────

class OwnerTimeTests(VaultBase):
    def test_utc_evening_is_next_almaty_day(self) -> None:
        moment = datetime.datetime(2026, 10, 5, 20, 0, tzinfo=UTC)
        path, created = dispatch.create_task_exact("Ночная", now=moment)
        self.assertTrue(created)
        body = path.read_text(encoding="utf-8")
        self.assertIn("scheduled: 2026-10-06", body)
        self.assertIn("created: 2026-10-06T01:00", body)


# ── P2: клавиатура при ошибке ввода ─────────────────────────────────────────

class KeyboardChoiceTests(unittest.TestCase):
    def test_invalid_input_keeps_cancel(self) -> None:
        bad = task_ui.Submission(False, "ошибка", False)
        good = task_ui.Submission(True, "ок", True)
        self.assertEqual(task_ui.keyboard_for(bad), task_ui.SCREEN_CANCEL)
        self.assertEqual(task_ui.keyboard_for(good), task_ui.SCREEN_MAIN)

    def test_telegram_uses_keyboard_choice_and_attaches_main(self) -> None:
        src = (Path(__file__).resolve().parent / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("ui.keyboard_for", src)
        self.assertIn("MAIN_KEYBOARD", src)

    def test_legacy_status_health_attach_main_keyboard(self) -> None:
        bot = Path(__file__).resolve().parent / "telegram_bot.py"
        for name in ("_show_status", "_show_inbox", "handle_health"):
            body = function_source(bot, name)
            self.assertTrue(body, name)
            self.assertIn("MAIN_KEYBOARD", body, name)
        # Ветки старой кнопки и здоровья в handle_text тоже возвращают клавиатуру.
        text_body = function_source(bot, "handle_text")
        self.assertGreaterEqual(text_body.count("reply_markup=MAIN_KEYBOARD"), 3)
        # Режимы и content-путь живут в handle_content: их же видит голос.
        content_body = function_source(bot, "handle_content")
        self.assertIn("_keyboard(ui.keyboard_for(result))", content_body)


if __name__ == "__main__":
    unittest.main()
