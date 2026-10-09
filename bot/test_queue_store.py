#!/usr/bin/env python3
"""Изолированные проверки queue_store: нормализация ссылок и атомарная запись.

Внешние вызовы не используются: только временные файлы.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import queue_store


class NormalizeUrlTests(unittest.TestCase):
    def test_youtube_forms_are_the_same_entry(self) -> None:
        canonical = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=120s",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1&index=3",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?si=abcdef",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "http://youtube.com/watch?v=dQw4w9WgXcQ",
        ):
            with self.subTest(url=url):
                self.assertEqual(queue_store.normalize_url(url), canonical)

    def test_different_videos_stay_different(self) -> None:
        first = queue_store.normalize_url("https://www.youtube.com/watch?v=aaaaaaaaaaa")
        second = queue_store.normalize_url("https://www.youtube.com/watch?v=bbbbbbbbbbb")
        self.assertNotEqual(first, second)

    def test_tracking_parameters_dropped(self) -> None:
        self.assertEqual(
            queue_store.normalize_url("https://example.com/post?utm_source=x&utm_medium=y&page=2"),
            "https://example.com/post?page=2",
        )
        self.assertEqual(
            queue_store.normalize_url("https://example.com/post#fragment"),
            "https://example.com/post",
        )

    def test_broken_input_is_returned_untouched(self) -> None:
        self.assertEqual(queue_store.normalize_url(""), "")
        self.assertEqual(queue_store.normalize_url("просто текст"), "просто текст")

    def test_video_hosts(self) -> None:
        self.assertTrue(queue_store.is_video_url("https://youtu.be/dQw4w9WgXcQ"))
        self.assertTrue(queue_store.is_video_url("https://vimeo.com/12345"))
        self.assertFalse(queue_store.is_video_url("https://example.com/video"))


class QueueEntriesTests(unittest.TestCase):
    def test_entries_and_selective_removal(self) -> None:
        text = (
            "# Очередь\n"
            "- https://youtu.be/aaaaaaaaaaa\n"
            "прочая строка\n"
            "- https://www.youtube.com/watch?v=bbbbbbbbbbb\n"
        )
        entries = queue_store.queue_entries(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["key"], "https://www.youtube.com/watch?v=aaaaaaaaaaa")

        # удаляем только первую запись: остальное остаётся дословно
        updated = queue_store.remove_entries(text, [entries[0]])
        self.assertNotIn("aaaaaaaaaaa", updated)
        self.assertIn("bbbbbbbbbbb", updated)
        self.assertIn("прочая строка", updated)
        self.assertIn("# Очередь", updated)

    def test_removal_keeps_line_when_file_changed(self) -> None:
        """Если строка сдвинулась, удалять её нельзя — лучше разобрать дважды."""
        entries = queue_store.queue_entries("- https://youtu.be/aaaaaaaaaaa\n")
        changed = "# новая шапка\n- https://youtu.be/aaaaaaaaaaa\n"
        updated = queue_store.remove_entries(changed, entries)
        self.assertIn("aaaaaaaaaaa", updated)


class AtomicWriteTests(unittest.TestCase):
    def test_write_replaces_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.md"
            path.write_text("старое", encoding="utf-8")
            queue_store.atomic_write(path, "новое\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "новое\n")

    def test_failed_replace_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.md"
            path.write_text("важная ссылка\n", encoding="utf-8")
            with patch.object(queue_store.os, "replace", side_effect=OSError("диск отвалился")):
                with self.assertRaises(OSError):
                    queue_store.atomic_write(path, "потеряно\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "важная ссылка\n")
            leftovers = [p.name for p in Path(directory).iterdir() if p.name != "queue.md"]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
