#!/usr/bin/env python3
"""Проход 4: граница создания и записи источника.

Всё на временных данных: проверяется, что недопустимый вход отвергается
ДО создания каталогов и записи, а symlink-выход не обходит проверку.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import learn
import transcript_source as ts

from test_learn_queue import AREA
from test_stage2a_ingest import URL, Stage2ABase

MANUAL = b"KEEP MY MANUAL FILE\n"


class SourceBoundaryTests(Stage2ABase):
    def source(self):
        return ts.from_text(URL, "Synthetic source payload.", title="Fixture")

    def refused(self, source) -> None:
        with self.assertRaises((ValueError, OSError)):
            learn.save_source(self.vault, source)

    def test_absolute_raw_name_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as external_root:
            external = Path(external_root) / "manual.txt"
            external.write_bytes(MANUAL)
            source = self.source()
            source.raw_name = external.as_posix()

            self.refused(source)

            self.assertEqual(external.read_bytes(), MANUAL)
            self.assertFalse((self.vault / learn.SOURCES_ROOT).exists(),
                             "недопустимый вход не создаёт даже каталог")

    def test_traversal_raw_name_is_refused(self) -> None:
        root = self.vault / learn.SOURCES_ROOT
        root.mkdir(parents=True)
        manual = root / "manual.txt"
        manual.write_bytes(MANUAL)
        source = self.source()
        source.raw_name = "../manual.txt"

        self.refused(source)

        self.assertEqual(manual.read_bytes(), MANUAL)
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["manual.txt"],
                         "ничего лишнего не создано")

    def test_key_with_directory_is_refused(self) -> None:
        source = self.source()
        source.video_id = "../outside"

        self.refused(source)

        self.assertFalse((self.vault / learn.SOURCES_ROOT).exists())
        self.assertFalse((self.vault / learn.SOURCES_ROOT).parent / "outside" in [])
        self.assertFalse((self.vault / "Учеба" / "outside").exists())

    def test_absolute_key_is_refused(self) -> None:
        source = self.source()
        source.video_id = "/tmp/outside"

        self.refused(source)

    def test_service_file_names_are_refused(self) -> None:
        for name in (learn.SOURCE_PAGE, "source.json"):
            with self.subTest(name=name):
                source = self.source()
                source.raw_name = name
                self.refused(source)

    def test_empty_and_nul_names_are_refused(self) -> None:
        for name in ("", "   ", "a\x00b", ".", ".."):
            with self.subTest(name=repr(name)):
                source = self.source()
                source.raw_name = name
                self.refused(source)

    def test_symlinked_sources_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as external_root:
            external = Path(external_root)
            manual = external / "manual.txt"
            manual.write_bytes(MANUAL)
            root = self.vault / learn.SOURCES_ROOT
            root.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(external, root)
            before = sorted(p.name for p in external.iterdir())

            self.refused(self.source())

            self.assertEqual(manual.read_bytes(), MANUAL)
            self.assertEqual(sorted(p.name for p in external.iterdir()), before,
                             "через symlink-корень ничего не записано")

    def test_symlinked_ancestor_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as external_root:
            external = Path(external_root)
            study = self.vault / "Учеба"
            if study.is_symlink():
                study.unlink()
            elif study.exists():
                import shutil
                shutil.rmtree(study)
            os.symlink(external, study)

            self.refused(self.source())

            self.assertEqual(sorted(p.name for p in external.iterdir()), [],
                             "через symlink-предка ничего не записано")

    def test_normal_creation_still_works(self) -> None:
        known = learn.save_source(self.vault, self.source())

        self.assertTrue(known["md"].is_file())
        self.assertIsNotNone(learn._read_source_set(known["dir"], self.vault))
        self.assertTrue(str(known["dir"].resolve()).startswith(
            str((self.vault / learn.SOURCES_ROOT).resolve())))

    def test_manual_directory_collision_still_uses_free_directory(self) -> None:
        source = self.source()
        occupied = learn.source_dir(self.vault, source)
        occupied.mkdir(parents=True)
        (occupied / "source.md").write_bytes(MANUAL)

        known = learn.save_source(self.vault, source)

        self.assertNotEqual(known["dir"], occupied)
        self.assertEqual((occupied / "source.md").read_bytes(), MANUAL)

    def test_ingest_keeps_url_and_does_not_call_model(self) -> None:
        source = self.source()
        source.video_id = "../outside"
        self.write_queue(URL)
        calls = []

        with patch.object(learn, "obtain_source", return_value=(source, "Fixture")), \
                patch.object(learn, "_ask_model",
                             side_effect=lambda prompt: calls.append(prompt) or "{}"):
            report = learn.process_queue()

        self.assertIn(URL, self.queue_urls(), "недопустимый источник не подтверждает ссылку")
        self.assertEqual(calls, [], "до модели дело не доходит")
        self.assertEqual(self.our_notes(), [])
        self.assertIn("Не вышло", report)

    def test_retry_keeps_url_when_root_is_symlinked(self) -> None:
        ok, message = self.run_ingest()
        self.assertTrue(ok, message)
        page = self.our_source_set()["md"]
        # превращаем корень источников в symlink наружу уже после сохранения
        with tempfile.TemporaryDirectory() as outside:
            moved = Path(outside) / "источники"
            page.parent.parent.rename(moved)
            page.parent.parent.symlink_to(moved, target_is_directory=True)

            self.write_queue(URL)
            with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                    patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
                report = learn.process_queue()

        self.assertIn(URL, self.queue_urls(), "symlink-корень не обходит проверку чтения")
        self.assertEqual(len(self.our_notes()), 1, "вторая заметка не создаётся")
        self.assertIn("Не вышло", report)

    def test_direct_helper_raises_explicitly(self) -> None:
        source = self.source()
        source.raw_name = "../escape.txt"
        with self.assertRaises(ValueError) as ctx:
            learn.save_source(self.vault, source)
        self.assertIn("raw_name", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
