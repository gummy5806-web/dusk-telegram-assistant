import tempfile
import unittest
from pathlib import Path

import memory_history
import memory_history_ui
import thought_feed
import thought_mutations


class MemoryHistoryUiTests(unittest.TestCase):
    def test_view_marks_current_and_previous_without_writing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            daily = vault / "ZETA" / "FLEETING_DAILY"
            daily.mkdir(parents=True)
            source = daily / "2026-10-01 — мысль.md"
            source.write_text("---\ntype: fleeting_note\n---\n\nРаньше\n", encoding="utf-8")
            before = thought_feed.recent_thoughts(vault)[0]
            changed = thought_mutations.edit(vault, before.identity, "Сейчас")
            original = source.read_text(encoding="utf-8")
            view = memory_history_ui.view(vault, "thought", changed.identity)
            self.assertIn("Сейчас", view.text)
            self.assertIn("Раньше", view.text)
            self.assertNotIn("type: fleeting_note", view.text)
            self.assertLessEqual(len(view.text), 3900)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertTrue(any("Назад" in label for row in view.rows for label, _ in row))

    def test_unknown_reference_is_honest(self):
        with tempfile.TemporaryDirectory() as directory:
            view = memory_history_ui.view(Path(directory), "thought", "missing#deadbeef")
            self.assertIn("не найдена", view.text)

    def test_timestamp_uses_almaty_time(self):
        self.assertEqual(memory_history_ui._timestamp("2026-10-14T00:00:00+00:00"),
                         "14.10.2026 05:00")
