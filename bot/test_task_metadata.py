#!/usr/bin/env python3
"""Граница TaskNotes-метаданных: точный путь, closed frontmatter, timeEntries.

Проверяется без Telegram, сети и живого валта: временный vault, реальные файлы.
Тело заметки обязано сохраняться байт-в-байт, чужие поля шапки — по возможности
дословно, а malformed/open/duplicate-шапка обязана падать без записи.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import task_metadata

NOW = "2026-10-09T12:00:00+00:00"


def task_note(frontmatter: str, body: str = "# Задача\n\nТело задачи\n") -> str:
    return "---\n" + frontmatter + "---\n" + body


class MetadataBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)

    def write(self, name: str, frontmatter: str, body: str = "") -> Path:
        path = self.tasks / name
        path.write_text(task_note(frontmatter, body or "# Задача\n\nТело\n"),
                        encoding="utf-8")
        return path

    def base_frontmatter(self, status: str = "next") -> str:
        return (
            'title: "Задача"\n'
            f"status: {status}\n"
            "priority: normal\n"
            'nextStep: "Задача"\n'
            "created: 2026-10-01T09:00\n"
            "scheduled: 2026-10-01\n"
            "tags:\n"
            "  - task\n"
            'source: "пакет дня"\n'
        )


class ResolvePathTests(MetadataBase):
    def test_bare_filename_resolves_inside_tasks_dir(self) -> None:
        path = self.write("Задача.md", self.base_frontmatter())
        self.assertEqual(task_metadata.resolve_task_path(self.root, "Задача.md"),
                         path)

    def test_relative_path_resolves(self) -> None:
        path = self.write("Задача.md", self.base_frontmatter())
        self.assertEqual(
            task_metadata.resolve_task_path(
                self.root, "TaskNotes/Tasks/Задача.md"),
            path)

    def test_missing_file_rejected(self) -> None:
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.resolve_task_path(self.root, "Нет.md")

    def test_non_markdown_rejected(self) -> None:
        (self.tasks / "Задача.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.resolve_task_path(self.root, "Задача.txt")

    def test_escape_rejected(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("x", encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.resolve_task_path(self.root, "../outside.md")

    def test_symlink_rejected(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("x", encoding="utf-8")
        (self.tasks / "Ссылка.md").symlink_to(outside)
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.resolve_task_path(self.root, "Ссылка.md")


class ReadTests(MetadataBase):
    def test_reads_scalars_and_no_entries(self) -> None:
        self.write("Задача.md", self.base_frontmatter())
        snapshot = task_metadata.read_task(self.root, "Задача.md")
        self.assertEqual(snapshot.status, "next")
        self.assertEqual(snapshot.title, "Задача")
        self.assertEqual(snapshot.time_entries, ())

    def test_reads_block_entries_and_preserves_unknown_keys(self) -> None:
        frontmatter = self.base_frontmatter() + (
            "timeEntries:\n"
            f"  - startTime: {NOW}\n"
            "    description: 'Dusk focus · 25 мин'\n"
            "    duskSessionId: abc123\n"
            "    plannedMinutes: 25\n"
            "    source: 'Dusk Telegram'\n"
            "    customKey: сохранить\n"
        )
        self.write("Задача.md", frontmatter)
        snapshot = task_metadata.read_task(self.root, "Задача.md")
        self.assertEqual(len(snapshot.time_entries), 1)
        entry = snapshot.time_entries[0]
        self.assertTrue(entry.is_dusk())
        self.assertTrue(entry.is_open())
        self.assertEqual(entry.planned_minutes, 25)
        self.assertEqual(entry.as_dict()["customKey"], "сохранить")

    def test_reads_flow_entries(self) -> None:
        frontmatter = self.base_frontmatter() + (
            "timeEntries: [{startTime: '" + NOW + "', duskSessionId: flow1}]\n")
        self.write("Задача.md", frontmatter)
        snapshot = task_metadata.read_task(self.root, "Задача.md")
        self.assertEqual(len(snapshot.time_entries), 1)
        self.assertEqual(snapshot.time_entries[0].dusk_session_id, "flow1")

    def test_open_frontmatter_fails_closed(self) -> None:
        path = self.tasks / "Обрыв.md"
        path.write_text("---\ntitle: x\nstatus: next\n\n# без конца\n", encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.read_task(self.root, "Обрыв.md")

    def test_malformed_yaml_fails_closed(self) -> None:
        path = self.tasks / "Битая.md"
        path.write_text("---\ntitle: [\n---\nтело\n", encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.read_task(self.root, "Битая.md")

    def test_duplicate_time_entries_fails_closed(self) -> None:
        frontmatter = self.base_frontmatter() + (
            "timeEntries: []\n"
            "timeEntries: []\n"
        )
        self.write("Дубль.md", frontmatter)
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.read_task(self.root, "Дубль.md")

    def test_non_list_time_entries_fails_closed(self) -> None:
        self.write("Отстой.md", self.base_frontmatter() + "timeEntries: не список\n")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.read_task(self.root, "Отстой.md")


class WriteTests(MetadataBase):
    def entry(self, session: str = "s1", planned: int = 25) -> task_metadata.TimeEntry:
        return task_metadata.TimeEntry({
            "startTime": NOW,
            "plannedEnd": "2026-10-09T12:25:00+00:00",
            "plannedMinutes": planned,
            "duskSessionId": session,
            "description": f"Dusk focus · {planned} мин",
            "source": task_metadata.DUSK_SOURCE,
        })

    def test_write_preserves_body_and_unrelated_frontmatter(self) -> None:
        body = "# Задача\n\nТело с  ---  и  timeEntries:  внутри\n\n- пункт\n"
        path = self.write("Задача.md", self.base_frontmatter(), body)
        before = path.read_text(encoding="utf-8")
        body_part = before[before.index("---\n", 4) + 4:]
        task_metadata.write_task(
            self.root, "Задача.md", time_entries=[self.entry()],
            scalars={"status": "in-progress",
                     "dateModified": "2026-10-09T12:00:00+00:00"})
        after = path.read_text(encoding="utf-8")
        self.assertTrue(after.endswith(body_part), "тело обязано сохраниться дословно")
        self.assertIn("title: \"Задача\"", after)
        self.assertIn("priority: normal", after)
        self.assertIn("status: in-progress", after)
        self.assertIn("timeEntries:", after)
        self.assertIn("duskSessionId: s1", after)

    def test_write_preserves_mode(self) -> None:
        path = self.write("Задача.md", self.base_frontmatter())
        os.chmod(path, 0o640)
        task_metadata.write_task(self.root, "Задача.md",
                                 time_entries=[self.entry()])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_write_leaves_no_temp_files(self) -> None:
        self.write("Задача.md", self.base_frontmatter())
        task_metadata.write_task(self.root, "Задача.md",
                                 time_entries=[self.entry()])
        leftovers = [p.name for p in self.tasks.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_write_preserves_unknown_entry_keys(self) -> None:
        frontmatter = self.base_frontmatter() + (
            "timeEntries:\n"
            f"  - startTime: {NOW}\n"
            "    duskSessionId: keep1\n"
            "    customKey: оставить\n"
        )
        self.write("Задача.md", frontmatter)
        snapshot = task_metadata.read_task(self.root, "Задача.md")
        entries = list(snapshot.time_entries) + [self.entry("new")]
        task_metadata.write_task(self.root, "Задача.md", time_entries=entries)
        after = task_metadata.read_task(self.root, "Задача.md")
        kept = [e for e in after.time_entries if e.dusk_session_id == "keep1"][0]
        self.assertEqual(kept.as_dict()["customKey"], "оставить")
        self.assertEqual(len(after.time_entries), 2)

    def test_write_replaces_existing_block_not_duplicates(self) -> None:
        self.write("Задача.md", self.base_frontmatter() + "timeEntries:\n  - startTime: 2020-01-01T00:00:00+00:00\n")
        task_metadata.write_task(self.root, "Задача.md",
                                 time_entries=[self.entry("fresh")])
        text = (self.tasks / "Задача.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("timeEntries:"), 1)
        self.assertNotIn("2020-01-01", text)

    def test_write_duplicate_owned_key_fails_closed_without_write(self) -> None:
        path = self.write("Дубль.md",
                          self.base_frontmatter() + "status: open\n")
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.write_task(self.root, "Дубль.md",
                                     time_entries=[self.entry()])
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_write_open_frontmatter_fails_closed(self) -> None:
        path = self.tasks / "Обрыв.md"
        text = "---\ntitle: x\n\n# тело\n"
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.write_task(self.root, "Обрыв.md",
                                     time_entries=[self.entry()])
        self.assertEqual(path.read_text(encoding="utf-8"), text)


class AtomicWriteTests(MetadataBase):
    def entry(self, session: str = "s1") -> task_metadata.TimeEntry:
        return task_metadata.TimeEntry({
            "startTime": NOW, "plannedEnd": "2026-10-09T12:25:00+00:00",
            "plannedMinutes": 25, "duskSessionId": session,
            "description": "Dusk focus · 25 мин",
            "source": task_metadata.DUSK_SOURCE})

    def assert_original_intact(self, path: Path, before: str) -> None:
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(
            [p.name for p in self.tasks.iterdir() if p.suffix == ".tmp"], [])

    def test_replace_failure_is_task_metadata_error(self) -> None:
        path = self.write("Задача.md", self.base_frontmatter())
        before = path.read_text(encoding="utf-8")
        with patch("task_metadata.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(task_metadata.TaskMetadataError):
                task_metadata.write_task(self.root, "Задача.md",
                                         time_entries=[self.entry()])
        self.assert_original_intact(path, before)

    def test_fsync_failure_is_task_metadata_error(self) -> None:
        path = self.write("Задача.md", self.base_frontmatter())
        before = path.read_text(encoding="utf-8")
        with patch("task_metadata.os.fsync", side_effect=OSError("boom")):
            with self.assertRaises(task_metadata.TaskMetadataError):
                task_metadata.write_task(self.root, "Задача.md",
                                         time_entries=[self.entry()])
        self.assert_original_intact(path, before)

    def test_temp_creation_failure_is_task_metadata_error(self) -> None:
        path = self.write("Задача.md", self.base_frontmatter())
        before = path.read_text(encoding="utf-8")
        with patch("task_metadata.tempfile.NamedTemporaryFile",
                   side_effect=OSError("boom")):
            with self.assertRaises(task_metadata.TaskMetadataError):
                task_metadata.write_task(self.root, "Задача.md",
                                         time_entries=[self.entry()])
        self.assert_original_intact(path, before)

    def test_fsync_dir_close_failure_does_not_mask(self) -> None:
        with patch("task_metadata.os.close", side_effect=OSError("close")), \
                patch("task_metadata.os.fsync", side_effect=OSError("fsync")):
            with self.assertRaises(task_metadata.TaskMetadataError):
                task_metadata._fsync_dir(self.tasks)


class FlowFrontmatterTests(MetadataBase):
    def entry(self, session: str = "s1") -> task_metadata.TimeEntry:
        return task_metadata.TimeEntry({
            "startTime": NOW, "duskSessionId": session,
            "plannedMinutes": 25,
            "plannedEnd": "2026-10-09T12:25:00+00:00"})

    def test_flow_root_mapping_rejected_unchanged(self) -> None:
        path = self.tasks / "Flow.md"
        original = "---\n{title: x, status: next, timeEntries: []}\n---\n# x\n"
        path.write_text(original, encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.read_task(self.root, "Flow.md")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.write_task(self.root, "Flow.md", time_entries=[])
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_block_empty_time_entries_is_supported(self) -> None:
        self.write("Ok.md", self.base_frontmatter() + "timeEntries: []\n")
        task_metadata.write_task(self.root, "Ok.md",
                                 time_entries=[self.entry("fresh")])
        snapshot = task_metadata.read_task(self.root, "Ok.md")
        self.assertEqual(len(snapshot.time_entries), 1)
        self.assertEqual(snapshot.time_entries[0].dusk_session_id, "fresh")


class BoundTests(MetadataBase):
    def test_oversized_task_rejected(self) -> None:
        path = self.tasks / "Огромная.md"
        path.write_text(
            "---\ntitle: x\nstatus: next\n---\n"
            + "x" * (task_metadata.MAX_TASK_BYTES + 10), encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.read_task(self.root, "Огромная.md")

    def test_recursive_alias_fails_closed(self) -> None:
        path = self.tasks / "Рекурсия.md"
        path.write_text("---\ntimeEntries: &a\n  - child: *a\n---\n# x\n",
                        encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.read_task(self.root, "Рекурсия.md")


class TaskNotesFixtureTests(MetadataBase):
    """Native TaskNotes 4.5.1 запись остаётся понятной после Dusk-правки."""

    def dusk(self, session: str = "dusk1") -> task_metadata.TimeEntry:
        return task_metadata.TimeEntry({
            "startTime": "2026-10-09T12:00:00+00:00",
            "plannedEnd": "2026-10-09T12:25:00+00:00",
            "plannedMinutes": 25, "duskSessionId": session,
            "description": "Dusk focus · 25 мин",
            "source": task_metadata.DUSK_SOURCE})

    def assert_native_preserved(self, path: Path) -> None:
        snapshot = task_metadata.read_task(self.root, path.name)
        natives = [entry for entry in snapshot.time_entries if not entry.is_dusk()]
        self.assertEqual(len(natives), 1)
        native = natives[0]
        self.assertEqual(native.as_dict()["unknownNativeKey"], "keep-me")
        self.assertTrue(native.start_time.startswith("2026-10-01T10:00:00"))
        self.assertTrue(native.end_time.startswith("2026-10-01T10:30:00"))
        self.assertEqual(native.description, "native session")
        import yaml
        text = path.read_text(encoding="utf-8")
        self.assertIsInstance(yaml.safe_load(text[4:text.find("\n---", 4)]), dict)

    def test_native_block_entry_preserved(self) -> None:
        frontmatter = (
            "title: \"Задача\"\nstatus: next\n"
            "timeEntries:\n"
            "  - startTime: 2026-10-01T10:00:00.000Z\n"
            "    endTime: 2026-10-01T10:30:00.000Z\n"
            "    description: native session\n"
            "    unknownNativeKey: keep-me\n")
        path = self.write("Задача.md", frontmatter)
        snapshot = task_metadata.read_task(self.root, "Задача.md")
        task_metadata.write_task(
            self.root, "Задача.md",
            time_entries=list(snapshot.time_entries) + [self.dusk()])
        self.assert_native_preserved(path)

    def test_native_flow_list_entry_preserved(self) -> None:
        frontmatter = (
            "title: \"Задача\"\nstatus: next\n"
            "timeEntries: [{startTime: '2026-10-01T10:00:00.000Z', "
            "endTime: '2026-10-01T10:30:00.000Z', "
            "description: native session, unknownNativeKey: keep-me}]\n")
        path = self.write("Задача.md", frontmatter)
        snapshot = task_metadata.read_task(self.root, "Задача.md")
        task_metadata.write_task(
            self.root, "Задача.md",
            time_entries=list(snapshot.time_entries) + [self.dusk()])
        self.assert_native_preserved(path)


class DuskMarkerTests(MetadataBase):
    """Bounded-проверка «может ли карточка относиться к Dusk» до production fix."""

    def test_detects_literal_marker(self) -> None:
        self.write("Dusk.md", self.base_frontmatter()
                   + "timeEntries:\n  - duskSessionId: abc\n")
        self.assertTrue(task_metadata.has_dusk_marker(self.root, "Dusk.md"))

    def test_false_without_marker(self) -> None:
        self.write("Plain.md", self.base_frontmatter())
        self.assertFalse(task_metadata.has_dusk_marker(self.root, "Plain.md"))

    def test_oversized_raises(self) -> None:
        path = self.tasks / "Big.md"
        path.write_text("---\n" + "x" * (task_metadata.MAX_TASK_BYTES + 10)
                        + "\n---\n", encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.has_dusk_marker(self.root, "Big.md")


class TimeEntryTests(unittest.TestCase):
    def test_with_end_and_with_plan_are_immutable_copies(self) -> None:
        entry = task_metadata.TimeEntry({
            "startTime": NOW, "duskSessionId": "x", "plannedMinutes": 25,
            "plannedEnd": "2026-10-09T12:25:00+00:00"})
        ended = entry.with_end("2026-10-09T12:10:00+00:00")
        replanned = entry.with_plan("2026-10-09T12:40:00+00:00", 40)
        self.assertEqual(entry.end_time, "", "исходная запись не меняется")
        self.assertEqual(ended.end_time, "2026-10-09T12:10:00+00:00")
        self.assertFalse(ended.is_open())
        self.assertEqual(replanned.planned_minutes, 40)
        self.assertEqual(replanned.planned_end, "2026-10-09T12:40:00+00:00")


if __name__ == "__main__":
    unittest.main()
