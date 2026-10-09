#!/usr/bin/env python3
"""Telegram UX: кнопочные задачи, режимы, точная идентичность, синхронизация.

Всё изолированно: временный валт, внедрённые функции записи, никакого git,
сети и токена бота. Пакет `telegram` на машине разработки не установлен,
поэтому вся логика вынесена в чистый `task_ui`, а тонкий Telegram-слой
проверяется статически (AST/текст).
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
import task_ui

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot"
NOW = datetime.datetime(
    2026, 10, 7, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5)))


def write_task(tasks: Path, name: str, title: str = "", status: str = "open",
               extra: str = "") -> Path:
    title = title or name
    path = tasks / f"{name}.md"
    path.write_text(
        "---\n"
        f'title: "{title}"\n'
        f"status: {status}\n"
        f"{extra}"
        "---\n\n"
        f"# {title}\n",
        encoding="utf-8",
    )
    return path


def function_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def callback_data(view: task_ui.View) -> list[str]:
    return [data for row in view.rows for _label, data in row]


def tokens_of(view: task_ui.View) -> list[str]:
    return [data.split(":", 1)[1] for data in callback_data(view)
            if data.startswith("t:")]


class PanelBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.tasks = self.vault / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        self.state: dict = {}

    def recorder(self):
        calls = []

        def sync():
            calls.append(1)

        return calls, sync


# ── 1. главная клавиатура ───────────────────────────────────────────────────

class MainKeyboardTests(unittest.TestCase):
    def test_required_buttons_present(self) -> None:
        expected = {
            "💭 Мысль", "🧠 Мысли", "➕ Задача",
            "📋 Задачи", "🎓 Материал", "📊 Статус",
        }
        self.assertTrue(expected.issubset(set(task_ui.MAIN_BUTTONS)), task_ui.MAIN_BUTTONS)

    def test_telegram_layer_builds_keyboard_from_shared_labels(self) -> None:
        src = (BOT / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("task_ui", src)
        self.assertIn("MAIN_BUTTONS", src)
        self.assertIn("InlineKeyboardMarkup", src)
        self.assertIn("ReplyKeyboardRemove", src)

    def test_start_explains_button_then_content(self) -> None:
        src = (BOT / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("только содержание", src.lower())


# ── 2. режимы: состояние, отмена, валидация ─────────────────────────────────

class ModeTests(PanelBase):
    def test_begin_task_sets_mode_and_prompt(self) -> None:
        prompt = task_ui.begin(self.state, task_ui.MODE_TASK)
        self.assertEqual(task_ui.mode(self.state), task_ui.MODE_TASK)
        self.assertIn("название", prompt.lower())

    def test_begin_note_and_video_have_prompts(self) -> None:
        self.assertIn("заметку", task_ui.begin(self.state, task_ui.MODE_NOTE).lower())
        self.assertIn("ссылк", task_ui.begin(self.state, task_ui.MODE_VIDEO).lower())

    def test_cancel_clears_mode(self) -> None:
        task_ui.begin(self.state, task_ui.MODE_TASK)
        task_ui.cancel(self.state)
        self.assertEqual(task_ui.mode(self.state), "")

    def test_mode_switch_does_not_leak(self) -> None:
        task_ui.begin(self.state, task_ui.MODE_TASK)
        task_ui.begin(self.state, task_ui.MODE_VIDEO)
        self.assertEqual(task_ui.mode(self.state), task_ui.MODE_VIDEO)
        task_ui.cancel(self.state)
        self.assertEqual(task_ui.mode(self.state), "")

    def test_validate_title_rejects_empty_and_service_values(self) -> None:
        for raw in ("", "   ", "\n", "/start", task_ui.BTN_TASKS, "x" * 500):
            with self.subTest(raw=raw[:20]):
                title, error = task_ui.validate_title(raw)
                self.assertEqual(title, "")
                self.assertTrue(error, raw[:20])

    def test_validate_title_accepts_normal_text(self) -> None:
        title, error = task_ui.validate_title("  Купить кабель USB-C  ")
        self.assertEqual(title, "Купить кабель USB-C")
        self.assertEqual(error, "")


# ── 3. создание задачи без префикса ─────────────────────────────────────────

class SubmitTaskTests(PanelBase):
    def test_creates_exactly_one_task_in_next_and_syncs(self) -> None:
        task_ui.begin(self.state, task_ui.MODE_TASK)
        calls, sync = self.recorder()
        result = task_ui.submit_task(
            self.state, "Купить кабель", vault=self.vault, sync=sync, now=NOW)

        self.assertTrue(result.ok)
        self.assertTrue(result.synced)
        self.assertEqual(task_ui.mode(self.state), "", "после создания режим сброшен")
        files = list(self.tasks.glob("*.md"))
        self.assertEqual(len(files), 1)
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("status: next", body)
        self.assertIn("# Купить кабель", body)
        self.assertEqual(len(calls), 1, "успешная мутация — ровно один sync")

    def test_invalid_title_keeps_mode_and_does_not_write_or_sync(self) -> None:
        task_ui.begin(self.state, task_ui.MODE_TASK)
        calls, sync = self.recorder()
        result = task_ui.submit_task(
            self.state, "   ", vault=self.vault, sync=sync, now=NOW)

        self.assertFalse(result.ok)
        self.assertFalse(result.synced)
        self.assertTrue(result.text)
        self.assertEqual(task_ui.mode(self.state), task_ui.MODE_TASK,
                         "ошибка не сбрасывает режим — можно повторить")
        self.assertEqual(list(self.tasks.glob("*.md")), [])
        self.assertEqual(calls, [], "на ошибке push не вызывается")

    def test_duplicate_title_does_not_create_second_or_sync(self) -> None:
        task_ui.begin(self.state, task_ui.MODE_TASK)
        task_ui.submit_task(self.state, "Позвонить", vault=self.vault, now=NOW)
        calls, sync = self.recorder()
        task_ui.begin(self.state, task_ui.MODE_TASK)
        again = task_ui.submit_task(
            self.state, "Позвонить", vault=self.vault, sync=sync, now=NOW)

        self.assertTrue(again.ok)
        self.assertFalse(again.synced)
        self.assertEqual(len(list(self.tasks.glob("*.md"))), 1)
        self.assertEqual(calls, [], "повтор ничего не меняет — push не нужен")


# ── 4. заметка и видео без синтаксиса ───────────────────────────────────────

class NoteAndVideoTests(PanelBase):
    def test_note_mode_saves_via_note_route(self) -> None:
        saved = []
        result = task_ui.submit_note(
            self.state, "связать задачи и заметки",
            save=lambda text: (saved.append(text), "Заметка сохранена")[1])
        self.assertTrue(result.ok)
        self.assertEqual(saved, ["связать задачи и заметки"])

    def test_empty_note_is_rejected(self) -> None:
        result = task_ui.submit_note(self.state, "   ", save=lambda text: "нет")
        self.assertFalse(result.ok)

    def test_classify_video_accepts_supported_hosts(self) -> None:
        for url in ("https://youtu.be/abcdefghijk",
                    "https://www.youtube.com/watch?v=abcdefghijk",
                    "https://vimeo.com/123456"):
            with self.subTest(url=url):
                found, error = task_ui.classify_video("смотри " + url)
                self.assertEqual(error, "")
                self.assertEqual(found, url)

    def test_classify_video_rejects_missing_and_foreign_links(self) -> None:
        for text in ("просто текст", "https://example.com/video", ""):
            with self.subTest(text=text):
                found, error = task_ui.classify_video(text)
                self.assertEqual(found, "")
                self.assertTrue(error)

    def test_video_mode_error_keeps_mode_for_retry(self) -> None:
        calls, sync = self.recorder()
        task_ui.begin(self.state, task_ui.MODE_VIDEO)
        result = task_ui.submit_video(self.state, "не ссылка", sync=sync)
        self.assertFalse(result.ok)
        self.assertEqual(task_ui.mode(self.state), task_ui.MODE_VIDEO)
        self.assertEqual(calls, [])


# ── 5. список задач, карточка, точная идентичность ──────────────────────────

class TaskListTests(PanelBase):
    def test_scan_skips_done(self) -> None:
        write_task(self.tasks, "открытая", status="open")
        write_task(self.tasks, "в работе", status="in-progress")
        write_task(self.tasks, "дальше", status="next")
        write_task(self.tasks, "закрытая", status="done")
        write_task(self.tasks, "без статуса", status="")

        titles = {card.title for card in task_ui.scan_open_tasks(self.vault)}
        self.assertNotIn("закрытая", titles)
        self.assertEqual(titles, {"открытая", "в работе", "дальше", "без статуса"})

    def test_list_renders_task_buttons(self) -> None:
        write_task(self.tasks, "первая", status="next", extra='nextStep: "шаг один"\n')
        view = task_ui.render_list(self.state, self.vault)
        labels = [label for row in view.rows for label, _ in row]
        self.assertTrue(any("первая" in label for label in labels), labels)
        self.assertEqual(len(tokens_of(view)), 1)

    def test_pagination_pages_and_bounds(self) -> None:
        for number in range(8):
            write_task(self.tasks, f"Задача {number}", status="open")

        first = task_ui.render_list(self.state, self.vault, page=0, page_size=3)
        self.assertEqual(len(tokens_of(first)), 3)
        self.assertIn("1/3", first.text)
        self.assertTrue(any(data == "l:1" for data in callback_data(first)),
                        "на первой странице есть переход вперёд")
        self.assertFalse(any(data == "l:-1" for data in callback_data(first)))

        last = task_ui.render_list(self.state, self.vault, page=2, page_size=3)
        self.assertEqual(len(tokens_of(last)), 2)
        self.assertIn("3/3", last.text)
        self.assertTrue(any(data == "l:1" for data in callback_data(last)),
                        "с последней страницы есть переход назад")
        self.assertFalse(any(data == "l:3" for data in callback_data(last)))

        clamped = task_ui.render_list(self.state, self.vault, page=99, page_size=3)
        self.assertIn("3/3", clamped.text)
        low = task_ui.render_list(self.state, self.vault, page=-5, page_size=3)
        self.assertIn("1/3", low.text)

    def test_callback_data_is_short_and_has_no_user_text(self) -> None:
        write_task(self.tasks, "секретное название", status="next")
        list_view = task_ui.render_list(self.state, self.vault)
        token = tokens_of(list_view)[0]
        card = task_ui.render_card(self.state, self.vault, token)

        for data in callback_data(list_view) + callback_data(card):
            self.assertLessEqual(len(data.encode("utf-8")), 64, data)
            self.assertNotIn("секретное", data)
            self.assertNotIn("название", data)
            self.assertNotIn("/", data)

    def test_card_shows_status_and_next_step(self) -> None:
        write_task(self.tasks, "карточка", status="next",
                   extra='nextStep: "открыть доску"\ntimeEstimate: 45\n')
        token = tokens_of(task_ui.render_list(self.state, self.vault))[0]
        view = task_ui.render_card(self.state, self.vault, token)
        self.assertIn("карточка", view.text)
        self.assertIn("➡️ Дальше", view.text)
        self.assertIn("открыть доску", view.text)
        self.assertIn("45", view.text)
        labels = [label for row in view.rows for label, _ in row]
        for expected in ("📥 В список", "➡️ Дальше", "🔥 В работу", "✅ Готово", "⬅️ Назад"):
            self.assertIn(expected, labels)

    def test_identical_titles_change_only_the_selected_card(self) -> None:
        first = write_task(self.tasks, "Одна", title="Одна", status="next")
        second = write_task(self.tasks, "Одна 1", title="Одна", status="next")
        view = task_ui.render_list(self.state, self.vault)
        got = tokens_of(view)
        self.assertEqual(len(got), 2, "одинаковые заголовки — две отдельные карточки")
        self.assertEqual(len(set(got)), 2, "у карточек разные токены")

        store = self.state[task_ui.TOKEN_KEY]["by_token"]
        chosen = next(token for token in got
                      if Path(store[token]).name == second.name)

        calls, sync = self.recorder()
        _view, changed = task_ui.apply_status(
            self.state, self.vault, chosen, "done", sync=sync, now=NOW)
        self.assertTrue(changed)
        self.assertEqual(len(calls), 1)

        self.assertEqual(
            dispatch._frontmatter_get(first.read_text(encoding="utf-8"), "status"),
            "next", "несовпадающая карточка не тронута")
        self.assertEqual(
            dispatch._frontmatter_get(second.read_text(encoding="utf-8"), "status"),
            "done", "изменена ровно выбранная карточка")

    def test_apply_status_is_idempotent_for_done(self) -> None:
        write_task(self.tasks, "идемпотентная", status="next")
        token = tokens_of(task_ui.render_list(self.state, self.vault))[0]
        calls, sync = self.recorder()

        _view, first = task_ui.apply_status(
            self.state, self.vault, token, "done", sync=sync, now=NOW)
        path = self.tasks / "идемпотентная.md"
        stamp_after_first = dispatch._frontmatter_get(
            path.read_text(encoding="utf-8"), "completedDate")

        _view, second = task_ui.apply_status(
            self.state, self.vault, token, "done", sync=sync, now=NOW)

        self.assertTrue(first)
        self.assertFalse(second, "повторный callback ничего не меняет")
        self.assertEqual(len(calls), 1, "sync только на реальном изменении")
        self.assertEqual(
            dispatch._frontmatter_get(path.read_text(encoding="utf-8"), "completedDate"),
            stamp_after_first)

    def test_unknown_or_foreign_token_is_stale_and_changes_nothing(self) -> None:
        write_task(self.tasks, "чужая", status="next")
        token = tokens_of(task_ui.render_list(self.state, self.vault))[0]

        calls, sync = self.recorder()
        view, changed = task_ui.apply_status(
            self.state, self.vault, "deadbeef", "done", sync=sync, now=NOW)
        self.assertFalse(changed)
        self.assertIn("обнов", view.text.lower())
        self.assertEqual(calls, [])

        foreign_state: dict = {}
        _view, changed = task_ui.apply_status(
            foreign_state, self.vault, token, "done", sync=sync, now=NOW)
        self.assertFalse(changed, "токен другого пользователя не разрешается")
        self.assertEqual(calls, [])
        self.assertEqual(
            dispatch._frontmatter_get(
                (self.tasks / "чужая.md").read_text(encoding="utf-8"), "status"),
            "next")

    def test_damaged_card_is_not_reported_as_success(self) -> None:
        (self.tasks / "битая.md").write_text("# только тело\n", encoding="utf-8")
        token = tokens_of(task_ui.render_list(self.state, self.vault))[0]
        calls, sync = self.recorder()
        view, changed = task_ui.apply_status(
            self.state, self.vault, token, "done", sync=sync, now=NOW)
        self.assertFalse(changed)
        self.assertNotIn("✅", view.text)
        self.assertEqual(calls, [])

    def test_status_buttons_stay_usable_after_change(self) -> None:
        write_task(self.tasks, "дальше", status="next")
        token = tokens_of(task_ui.render_list(self.state, self.vault))[0]
        view, changed = task_ui.apply_status(
            self.state, self.vault, token, "in-progress", now=NOW)
        self.assertTrue(changed)
        self.assertIn("🔥 В работе", view.text)
        labels = [label for row in view.rows for label, _ in row]
        self.assertIn("✅ Готово", labels, "кнопки остаются пригодны для следующего шага")


# ── 6. точный helper статуса в dispatch ─────────────────────────────────────

class ExactStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.tasks = self.vault / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        patcher = patch.object(dispatch, "VAULT", self.vault)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_set_status_by_exact_path(self) -> None:
        first = write_task(self.tasks, "Одна", title="Одна", status="next")
        second = write_task(self.tasks, "Одна 1", title="Одна", status="next")

        changed, message = dispatch.set_task_status(first, "done", now=NOW)
        self.assertTrue(changed)
        self.assertIn("done", message)
        self.assertEqual(
            dispatch._frontmatter_get(second.read_text(encoding="utf-8"), "status"),
            "next", "вторая карточка с тем же заголовком не тронута")

    def test_set_status_repeat_is_not_a_change(self) -> None:
        card = write_task(self.tasks, "карта", status="next")
        dispatch.set_task_status(card, "done", now=NOW)
        before = card.read_text(encoding="utf-8")
        changed, message = dispatch.set_task_status(card, "done", now=NOW)
        self.assertFalse(changed)
        self.assertIn("уже", message)
        self.assertEqual(card.read_text(encoding="utf-8"), before)

    def test_fuzzy_advance_still_works(self) -> None:
        card = write_task(self.tasks, "проверить канбан", status="open")
        message = dispatch.advance_task("готово", "канбан", now=NOW)
        self.assertIn("✅", message)
        self.assertEqual(
            dispatch._frontmatter_get(card.read_text(encoding="utf-8"), "status"),
            "done")

    def test_create_task_exact_returns_path_and_created_flag(self) -> None:
        path, created = dispatch.create_task_exact("Новая задача", now=NOW)
        self.assertTrue(created)
        self.assertTrue(path.exists())
        self.assertIn("status: next", path.read_text(encoding="utf-8"))
        again, created_again = dispatch.create_task_exact("Новая задача", now=NOW)
        self.assertFalse(created_again)
        self.assertEqual(again, path)


# ── 7. совместимость и статический Telegram-слой ────────────────────────────

class CompatibilityTests(unittest.TestCase):
    def test_old_text_commands_still_route(self) -> None:
        self.assertTrue(dispatch.shared_prefix("задача: купить"))
        self.assertEqual(dispatch.resolve_capture("задача: купить").kind, "task")
        self.assertEqual(dispatch.resolve_capture("мысль: идея").kind, "note")
        self.assertEqual(dispatch.resolve_capture("!личное").kind, "private")

    def test_telegram_keeps_legacy_handlers(self) -> None:
        src = (BOT / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("shared_prefix", src)
        self.assertRegex(src, r"\w+\.dispatch_detailed\(text\)")
        self.assertIn('CommandHandler("health"', src)
        self.assertIn("health.report", src)
        self.assertIn("📋 Что осталось", src, "старая кнопка продолжает работать")
        self.assertIn("📥 Разобрать инбокс", src)

    def test_callback_path_checks_auth(self) -> None:
        body = function_source(BOT / "telegram_bot.py", "handle_callback")
        self.assertTrue(body, "нет обработчика callback")
        self.assertIn("auth_check", body)
        self.assertIn("query.answer", body)
        src = (BOT / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("CallbackQueryHandler", src)

    def test_message_path_checks_auth(self) -> None:
        body = function_source(BOT / "telegram_bot.py", "handle_text")
        self.assertIn("auth_check", body)

    def test_git_push_messages_are_neutral(self) -> None:
        for kind, message in task_ui.SYNC_MESSAGES.items():
            with self.subTest(kind=kind):
                self.assertTrue(message)
                self.assertNotIn("{", message)
                self.assertNotIn("заголовок задачи", message)
        src = (BOT / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("git_push", src)
        self.assertIn("SYNC_MESSAGES", src)

    def test_telegram_wires_modes_and_exact_status(self) -> None:
        src = (BOT / "telegram_bot.py").read_text(encoding="utf-8")
        for token in ("ui.begin", "ui.cancel", "ui.submit_task",
                      "ui.submit_note", "ui.submit_video",
                      "ui.render_list", "ui.render_card",
                      "focus_ui.status_with_focus"):
            with self.subTest(token=token):
                self.assertIn(token, src)

    def test_authorized_truth_table(self) -> None:
        self.assertTrue(task_ui.authorized(7, 7))
        self.assertFalse(task_ui.authorized(7, 8))
        self.assertFalse(task_ui.authorized(None, 7))


# ── 8. no-push-on-failure / push-after-success через submit_task ─────────────

class SyncSemanticsTests(PanelBase):
    def test_not_created_does_not_push(self) -> None:
        target = self.tasks / "x.md"
        with patch.object(dispatch, "create_task_exact",
                          return_value=(target, False)):
            calls, sync = self.recorder()
            result = task_ui.submit_task(
                self.state, "Что-то", vault=self.vault, sync=sync, now=NOW)
        self.assertTrue(result.ok)
        self.assertFalse(result.synced, "нечего сохранять — push не нужен")
        self.assertEqual(calls, [])

    def test_successful_mutation_pushes_once(self) -> None:
        calls, sync = self.recorder()
        result = task_ui.submit_task(
            self.state, "Сделать шаг", vault=self.vault, sync=sync, now=NOW)
        self.assertTrue(result.synced)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
