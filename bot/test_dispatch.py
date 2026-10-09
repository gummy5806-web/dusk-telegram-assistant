from __future__ import annotations

import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import dispatch


class SmartCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        vault = Path(directory.name)
        for name, value in (
            ("VAULT", vault),
            ("INBOX", vault / "inbox.md"),
            ("PRIVATE", vault / "private.md"),
        ):
            patcher = patch.object(dispatch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(dispatch, "_write_via_obsidian", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clear_actions_create_tasks(self) -> None:
        for text in (
            "Сделать плагин DeepSeek Harness",
            "Надо проверить канбан",
            "Позвонить клиенту",
            "Купи кабель, USB-C",
            "Разобраться с парсером",
        ):
            with self.subTest(text=text), patch.object(dispatch, "_daily_tasks", return_value="task") as create:
                self.assertEqual(dispatch.dispatch(text), "task")
                create.assert_called_once_with(text)

    def test_thought_creates_note_without_background_triage(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(dispatch, "VAULT", Path(directory)):
            result = dispatch.dispatch("Идея: связать задачи с заметками")
            notes = list((Path(directory) / "ZETA" / "FLEETING_DAILY").glob("*.md"))
            self.assertEqual(len(notes), 1)
            self.assertIn("связать задачи с заметками", notes[0].read_text())
            self.assertIn("Заметка", result)
            self.assertFalse((Path(directory) / "ZETA" / "FLEETING" / "00 Инбокс.md").exists())

    def test_preview_has_no_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(dispatch, "VAULT", Path(directory)):
            self.assertIn("канбан", dispatch.preview("Сделать плагин"))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_preview_counts_duplicate_task_lines_once(self) -> None:
        self.assertEqual(
            dispatch.preview("день: Сделать проверку\nСделать проверку"),
            "Задача → канбан · Дальше",
        )

    def test_explicit_note_wins_over_action(self) -> None:
        self.assertEqual(dispatch.resolve_capture("мысль: сделать удобный интерфейс").kind, "note")

    def test_negation_questions_and_completed_actions_are_not_tasks(self) -> None:
        for text in ("Не надо делать плагин", "Нужно ли сделать плагин?", "Я сделал плагин", "Память важнее скорости"):
            with self.subTest(text=text):
                self.assertEqual(dispatch.resolve_capture(text).kind, "note")

    def test_multiline_actions_are_tasks_but_multiline_thought_stays_whole(self) -> None:
        self.assertEqual(dispatch.resolve_capture("Купить кабель\nПозвонить клиенту").kind, "task")
        thought = "Идея про интерфейс\nСделать его удобным пока только идея"
        route = dispatch.resolve_capture(thought)
        self.assertEqual(route.kind, "note")
        self.assertEqual(route.text, thought)

    def test_single_task_prefix_preserves_punctuation(self) -> None:
        route = dispatch.resolve_capture("задача: Сделать плагин, затем проверить")
        self.assertEqual((route.kind, route.text), ("task", "Сделать плагин, затем проверить"))

    def test_links_use_real_hostname_not_substring(self) -> None:
        route = dispatch.resolve_capture("https://youtu.be/abc?t=3")
        self.assertEqual(route.kind, "video")
        route = dispatch.resolve_capture("https://example.com/youtube.com")
        self.assertEqual(route.kind, "link")

    def test_private_and_search_commands_keep_priority(self) -> None:
        self.assertEqual(dispatch.resolve_capture("!Сделать приватный проект").kind, "private")
        self.assertEqual(dispatch.resolve_capture("?плагин").kind, "search")

    def test_existing_privacy_guard_is_applied_before_saving(self) -> None:
        for text in ("Записаться к врачу", "Идея: хранить пароль", "задача: оплатить кредит"):
            with self.subTest(text=text):
                self.assertEqual(dispatch.resolve_capture(text).kind, "private")

    def test_video_with_action_text_still_goes_to_summary_queue(self) -> None:
        self.assertEqual(dispatch.resolve_capture("Посмотреть https://youtu.be/abc").kind, "video")
        self.assertEqual(dispatch.resolve_capture("задача: Посмотреть https://youtu.be/abc").kind, "task")

    def test_incomplete_counter_input_does_not_break_preview(self) -> None:
        self.assertEqual(dispatch.resolve_capture("+контент .").kind, "note")

    def test_pasted_image_is_a_note_not_a_private_prefix(self) -> None:
        text = "![[SYSTEM/MEDIA/Dusk/example.png]]"
        route = dispatch.resolve_capture(text)
        self.assertEqual((route.kind, route.text), ("note", text))

    def test_parallel_cli_captures_do_not_overwrite_notes(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import os
        with tempfile.TemporaryDirectory() as directory:
            def capture(_):
                return subprocess.run(
                    [sys.executable, str(Path(dispatch.__file__)), "--text", "Идея: параллельная проверка"],
                    env={**os.environ, "VAULT_PATH": directory},
                    capture_output=True, text=True, timeout=5,
                )
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(capture, range(2)))
            self.assertTrue(all(result.returncode == 0 for result in results), results)
            notes = list((Path(directory) / "ZETA" / "FLEETING_DAILY").glob("*.md"))
            self.assertEqual(len(notes), 2)

    def test_note_does_not_overwrite_same_named_note(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(dispatch, "VAULT", Path(directory)):
            dispatch.dispatch("Идея: повторяемая мысль")
            dispatch.dispatch("Идея: повторяемая мысль")
            self.assertEqual(len(list((Path(directory) / "ZETA" / "FLEETING_DAILY").glob("*.md"))), 2)

    def test_cli_advancement_is_defined_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            import os
            result = subprocess.run(
                [sys.executable, str(Path(dispatch.__file__)), "--text", "дальше nonexistent"],
                env={**os.environ, "VAULT_PATH": directory},
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


class DailyTaskParsingTests(unittest.TestCase):
    def create_tasks(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            tasks = vault / "TaskNotes" / "Tasks"
            tasks.mkdir(parents=True)

            with patch.object(dispatch, "VAULT", vault):
                dispatch._daily_tasks(text)

            return [
                path.read_text(encoding="utf-8")
                for path in sorted(tasks.glob("*.md"))
            ]

    def test_commas_stay_inside_one_dictated_task(self) -> None:
        files = self.create_tasks(
            "Сделать плагин, который читает канбан, и добавить его в Harness"
        )

        self.assertEqual(len(files), 1)
        self.assertIn("который читает канбан", files[0])

    def test_newline_separates_tasks(self) -> None:
        files = self.create_tasks("Первая задача\nВторая задача")

        self.assertEqual(len(files), 2)

    def test_more_than_ten_tasks_are_not_silently_lost(self) -> None:
        files = self.create_tasks("\n".join(f"Сделать проверку {number}" for number in range(11)))
        self.assertEqual(len(files), 11)

    def test_filename_collision_keeps_both_distinct_tasks(self) -> None:
        prefix = "Сделать " + "длинное название " * 5
        files = self.create_tasks(prefix + "первое\n" + prefix + "второе")
        self.assertEqual(len(files), 2)

    def test_duplicate_task_is_still_idempotent(self) -> None:
        self.assertEqual(len(self.create_tasks("Сделать проверку\nСделать проверку")), 1)

    def test_title_preserves_quotes_and_backslashes_in_frontmatter(self) -> None:
        import json
        title = 'Сохранить "важное" в C:\\notes'
        files = self.create_tasks(title)
        line = next(line for line in files[0].splitlines() if line.startswith("title: "))
        self.assertEqual(json.loads(line[len("title: "):]), title)


if __name__ == "__main__":
    unittest.main()
