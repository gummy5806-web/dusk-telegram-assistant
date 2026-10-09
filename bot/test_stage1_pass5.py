#!/usr/bin/env python3
"""Регрессии пятого прохода: сохранность исходного черновика и настоящее владение.

Изоляция: временный валт, mock внешних границ. Скрипт-воспроизведение Codex —
`work/review-stage1/parent_checks_pass4.py`; здесь те же случаи в виде тестов.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import learn

from test_learn_queue import QueueTestCase, fake_distill

URL_FIRST = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
AREA_NAME = "Тест"
ACTION = "Проверить данные"


class DraftPreservationTests(QueueTestCase):
    """Пункт 1: исходный черновик сохраняется целиком, без правок и потерь."""

    def draft(self, body: str, closed: bool, name: str = "черновик.md",
              marker: bool = True) -> Path:
        folder = self.vault / learn.HUB / AREA_NAME
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        text = f"---\nreference: {URL_FIRST}\n"
        if marker:
            text += "generator: dusk\n"
        text += "type: literature_note\n"
        text += "---\n\n# Черновик\n\n" if closed else "\n"
        path.write_text(text + body, encoding="utf-8")
        return path

    def digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def run_queue(self, title: str = "Разбор", thesis: str = "Полная проверка") -> str:
        data = {"title": title, "area": AREA_NAME, "thesis": thesis,
                "points": ["Проверить данные"], "action": ACTION}
        with patch.object(learn, "fetch_subtitles", return_value=("транскрипт", "метаданные")), \
                patch.object(learn, "_distill", return_value=data):
            return learn.process_queue()

    def complete_notes(self) -> list:
        return [p for p in self.notes()
                if learn._note_is_complete(p.read_text(encoding="utf-8"))]

    def test_open_frontmatter_draft_with_user_text_is_untouched(self) -> None:
        """Черновик с открытой шапкой: личное наблюдение не теряется."""
        user_body = "Личное наблюдение: первый эксперимент не сработал.\n"
        draft = self.draft(user_body, closed=False)
        before = self.digest(draft)
        self.write_queue(URL_FIRST)

        self.run_queue()

        self.assertEqual(self.digest(draft), before, "черновик обязан остаться дословным")
        self.assertIn(user_body, draft.read_text(encoding="utf-8"))
        self.assertEqual(len(self.complete_notes()), 1, "рядом появился завершённый результат")
        self.assertEqual(self.queue_urls(), [])

    def test_repeats_and_marker_words_in_user_block_survive(self) -> None:
        """Повторы строк и слово из тезиса не дают права удалять блок."""
        user_body = "Повторить\nПовторить\nмаркер42\n"
        draft = self.draft(user_body, closed=True)
        before = self.digest(draft)
        self.write_queue(URL_FIRST)

        self.run_queue(thesis="Полная проверка маркер42")

        self.assertEqual(self.digest(draft), before, "пользовательский блок не переписываем")
        self.assertIn(user_body, draft.read_text(encoding="utf-8"))
        self.assertEqual(len(self.complete_notes()), 1)

    def test_unmarked_legacy_note_is_not_touched(self) -> None:
        legacy = self.draft("Старый текст\n", closed=True, name="legacy.md", marker=False)
        before = self.digest(legacy)
        self.write_queue(URL_FIRST)

        self.run_queue()

        self.assertEqual(self.digest(legacy), before)
        self.assertEqual(len(self.complete_notes()), 1, "создаётся один размеченный результат")

    def test_repeat_after_success_creates_nothing_new(self) -> None:
        self.draft("Личное наблюдение\n", closed=False)
        self.write_queue(URL_FIRST)
        self.run_queue()
        notes_after_first = len(self.notes())
        tasks_after_first = len(self.tasks())

        self.write_queue(URL_FIRST)
        with patch.object(learn, "fetch_subtitles", side_effect=AssertionError("модель не нужна")), \
                patch.object(learn, "_distill", side_effect=AssertionError("модель не нужна")):
            report = learn.process_queue()

        self.assertEqual(len(self.notes()), notes_after_first, "второй результат не создаётся")
        self.assertEqual(len(self.tasks()), tasks_after_first, "вторая задача не создаётся")
        self.assertIn("уже было разобрано раньше", report)
        self.assertEqual(self.queue_urls(), [])

    def test_failed_publish_keeps_original_and_queue(self) -> None:
        """Не удалось записать результат — оригинал и очередь целы."""
        draft = self.draft("Личное наблюдение: важно.\n", closed=False)
        before = self.digest(draft)
        self.write_queue(URL_FIRST)
        original_publish = learn._publish

        def failing_publish(path, text, **kwargs):
            if str(path).startswith(str(self.vault / learn.HUB)):
                raise OSError("диск полон")
            return original_publish(path, text, **kwargs)

        with patch.object(learn, "_publish", side_effect=failing_publish):
            with self.assertRaises(OSError):
                self.run_queue()

        self.assertEqual(self.digest(draft), before, "оригинал не тронут")
        self.assertIn(URL_FIRST, self.queue_urls(), "ссылка остаётся в очереди")


class OwnershipTests(QueueTestCase):
    """Пункт 2: владение определяется только маркером `generator: dusk`."""

    def test_footer_alone_is_not_ownership(self) -> None:
        text = (
            "---\n"
            f"reference: {URL_FIRST}\n"
            "type: literature_note\n"
            "---\n\n# Ручная заметка\n\nМои наблюдения.\n"
            f"\n---\n_Источник: {URL_FIRST}_\n"
        )
        self.assertFalse(learn._is_owned(text), "подпись «Источник» не доказывает владение")
        marked = text.replace("type: literature_note", "generator: dusk\ntype: literature_note")
        self.assertTrue(learn._is_owned(marked))

    def test_marker_in_open_frontmatter_is_ownership(self) -> None:
        text = f"---\nreference: {URL_FIRST}\ngenerator: dusk\ntype: literature_note\n\nтекст\n"
        self.assertTrue(learn._is_owned(text))

    def test_manual_footer_does_not_mask_marked_action(self) -> None:
        """Ручная заметка с футером не должна подменять наш черновик с действием."""
        folder = self.vault / learn.HUB / AREA_NAME
        folder.mkdir(parents=True, exist_ok=True)
        footer = f"\n---\n_Источник: {URL_FIRST}_\n"
        (folder / "A manual.md").write_text(
            f"---\nreference: {URL_FIRST}\ntype: literature_note\n---\n\n"
            "# Ручная заметка\n\nМои наблюдения.\n" + footer, encoding="utf-8")
        generated = folder / "Z dusk.md"
        generated.write_text(
            f"---\nreference: {URL_FIRST}\ngenerator: dusk\ntype: literature_note\n---\n\n"
            "# Dusk\n\n## Что применить\n\n- [ ] Сверстать лендинг\n" + footer, encoding="utf-8")
        self.write_queue(URL_FIRST)

        with patch.object(learn, "fetch_subtitles",
                          side_effect=AssertionError("готовый размеченный результат уже есть")), \
                patch.object(learn, "_distill", side_effect=AssertionError("модель не нужна")):
            learn.process_queue()

        self.assertIsNotNone(learn._task_for_note(self.vault, generated),
                             "задача обязана ссылаться на размеченный конспект")
        self.assertEqual(self.queue_urls(), [])


if __name__ == "__main__":
    unittest.main()
