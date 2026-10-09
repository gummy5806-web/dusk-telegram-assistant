#!/usr/bin/env python3
"""TaskNotes reminders boundary: native + Dusk, unknown keys, body byte-stable.

JSON/YAML through the real file system in a temp vault: no Telegram, no network.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import task_metadata

NATIVE = (
    "reminders:\n"
    "  - id: native-1\n"
    "    type: relative\n"
    "    relatedTo: due\n"
    "    offset: -PT1H\n"
    "    customNative: keep\n"
)
DUSK = (
    "  - id: dusk_abc123\n"
    "    type: absolute\n"
    "    description: Dusk reminder\n"
    "    absoluteTime: '2026-10-10T09:00:00+05:00'\n"
    "    customDusk: keep\n"
)


class ReminderBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)

    def write(self, name: str, frontmatter: str, body: str = "") -> Path:
        path = self.tasks / name
        path.write_text("---\n" + frontmatter + "---\n"
                        + (body or "# Задача\n\nТело\n"), encoding="utf-8")
        return path

    def base(self, extra: str = "") -> str:
        return ('title: "Задача"\nstatus: next\n'
                "created: 2026-10-01T09:00\n" + extra)


class ParseTests(ReminderBase):
    def test_reads_native_and_dusk_reminders(self) -> None:
        self.write("Задача.md", self.base(NATIVE + DUSK))
        snapshot = task_metadata.read_task(self.root, "Задача.md")
        self.assertEqual(len(snapshot.reminders), 2)
        native = [r for r in snapshot.reminders if not r.is_dusk()][0]
        dusk = [r for r in snapshot.reminders if r.is_dusk()][0]
        self.assertEqual(native.id, "native-1")
        self.assertEqual(native.data["offset"], "-PT1H")
        self.assertEqual(native.data["customNative"], "keep")
        self.assertEqual(dusk.id, "dusk_abc123")
        self.assertEqual(dusk.type, "absolute")
        self.assertEqual(dusk.absolute_time, "2026-10-10T09:00:00+05:00")
        self.assertEqual(dusk.data["customDusk"], "keep")

    def test_duplicate_reminders_key_fails_closed(self) -> None:
        self.write("Дубль.md", self.base("reminders: []\nreminders: []\n"))
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.read_task(self.root, "Дубль.md")

    def test_recursive_alias_fails_closed(self) -> None:
        self.write("Рекурсия.md", self.base("reminders: &a\n  - self: *a\n"))
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.read_task(self.root, "Рекурсия.md")

    def test_oversized_rejected(self) -> None:
        path = self.tasks / "Огромная.md"
        path.write_text("---\ntitle: x\nreminders: []\n---\n"
                        + "x" * (task_metadata.MAX_TASK_BYTES + 10),
                        encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.read_task(self.root, "Огромная.md")

    def test_outside_and_symlink_rejected(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("---\ntitle: x\nreminders: []\n---\n", encoding="utf-8")
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.resolve_task_path(self.root, "../outside.md")
        (self.tasks / "Ссылка.md").symlink_to(outside)
        with self.assertRaises(task_metadata.TaskMetadataError):
            task_metadata.resolve_task_path(self.root, "Ссылка.md")


class WritePreservationTests(ReminderBase):
    def test_append_dusk_preserves_native_unknown_keys_and_body(self) -> None:
        body = "# Задача\n\nТело с reminders: и  ---  внутри\n\n- пункт\n"
        path = self.write("Задача.md", self.base(NATIVE), body)
        snapshot = task_metadata.read_task(self.root, "Задача.md")
        entries = list(snapshot.reminders) + [task_metadata.ReminderEntry({
            "id": "dusk_new1", "type": "absolute",
            "description": "Dusk reminder",
            "absoluteTime": "2026-10-11T10:00:00+05:00"})]
        task_metadata.write_task(self.root, "Задача.md", reminders=entries)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith(body))
        after = task_metadata.read_task(self.root, "Задача.md")
        native = [r for r in after.reminders if not r.is_dusk()]
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0].data["offset"], "-PT1H")
        self.assertEqual(native[0].data["customNative"], "keep")
        self.assertEqual({r.id for r in after.reminders},
                         {"native-1", "dusk_new1"})

    def test_reminders_write_does_not_touch_time_entries(self) -> None:
        time_entries = ("timeEntries:\n"
                        "  - startTime: '2026-10-09T10:00:00+00:00'\n"
                        "    duskSessionId: sess1\n")
        self.write("Задача.md", self.base(time_entries + "reminders: []\n"))
        task_metadata.write_task(self.root, "Задача.md", reminders=[
            task_metadata.ReminderEntry({
                "id": "dusk_x", "type": "absolute",
                "absoluteTime": "2026-10-11T10:00:00+05:00"})])
        after = task_metadata.read_task(self.root, "Задача.md")
        self.assertEqual(len(after.time_entries), 1)
        self.assertEqual(after.time_entries[0].dusk_session_id, "sess1")
        self.assertEqual(len(after.reminders), 1)

    def test_time_entries_write_does_not_touch_reminders(self) -> None:
        self.write("Задача.md", self.base(NATIVE + DUSK))
        task_metadata.write_task(self.root, "Задача.md", time_entries=[
            task_metadata.TimeEntry({
                "startTime": "2026-10-09T10:00:00+00:00",
                "duskSessionId": "sess2"})])
        after = task_metadata.read_task(self.root, "Задача.md")
        ids = {r.id for r in after.reminders}
        self.assertEqual(ids, {"native-1", "dusk_abc123"})
        native = [r for r in after.reminders if not r.is_dusk()][0]
        self.assertEqual(native.data["offset"], "-PT1H")
        self.assertEqual(len(after.time_entries), 1)

    def test_clearing_reminders_keeps_native_untouched_field(self) -> None:
        self.write("Задача.md", self.base(NATIVE + DUSK))
        task_metadata.write_task(self.root, "Задача.md", reminders=[])
        after = task_metadata.read_task(self.root, "Задача.md")
        self.assertEqual(after.reminders, ())


if __name__ == "__main__":
    unittest.main()
