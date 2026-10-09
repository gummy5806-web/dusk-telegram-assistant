#!/usr/bin/env python3
"""Regression contract for bounded thought correction and recoverable deletion."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import task_ui
import thought_feed


class ThoughtMutationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.daily = self.vault / "ZETA" / "FLEETING_DAILY"
        self.daily.mkdir(parents=True)
        self.inbox = self.vault / "ZETA" / "FLEETING" / "00 Инбокс.md"
        self.inbox.parent.mkdir(parents=True)
        self.state = {}

    def note(self, body="старый полный текст\nсо второй строкой"):
        path = self.daily / "2026-10-01 — мысль.md"
        path.write_text("---\ntype: fleeting_note\ncreated: 2026-10-01\n---\n\n" + body + "\n",
                        encoding="utf-8")
        return path

    def inbox_text(self, thoughts):
        self.inbox.write_text(
            "# 📥 Inbox\n\n## 💭 Мысли и идеи\n\n" + "\n".join(thoughts) +
            "\n\n## ✅ Задачи\n\n- [ ] задача\n\n## 🔗 Ссылки и материалы\n",
            encoding="utf-8")

    def test_edit_structured_preserves_frontmatter_and_path(self):
        path = self.note()
        identity = thought_feed.recent_thoughts(self.vault, 10)[0].identity
        token = task_ui.issue_thought_token(self.state, identity)

        prompt = task_ui.begin_thought_edit(self.state, self.vault, token)
        self.assertTrue(task_ui.thought_edit_pending(self.state))
        self.assertIn("старый полный текст", prompt.text)
        result = task_ui.submit_thought_edit(self.state, self.vault, "исправленный\nполный текст")

        self.assertTrue(result.ok)
        self.assertTrue(result.synced)
        self.assertEqual(result.path, "ZETA/FLEETING_DAILY/2026-10-01 — мысль.md")
        saved = path.read_text(encoding="utf-8")
        self.assertTrue(saved.startswith("---\ntype: fleeting_note\ncreated: 2026-10-01\n---\n"))
        self.assertIn("исправленный\nполный текст", saved)
        self.assertNotIn("старый полный текст", saved)
        self.assertFalse(task_ui.thought_edit_pending(self.state))

    def test_edit_inbox_changes_only_unique_line_and_normalizes_multiline(self):
        self.inbox_text(["- **10:00** первая", "- **10:01** вторая"])
        thought = thought_feed.inbox_thoughts(self.vault, 10)[1]
        token = task_ui.issue_thought_token(self.state, thought.identity)
        task_ui.begin_thought_edit(self.state, self.vault, token)

        result = task_ui.submit_thought_edit(self.state, self.vault, "первая\nс уточнением")

        self.assertTrue(result.ok)
        self.assertIn("- **10:00** первая с уточнением", self.inbox.read_text(encoding="utf-8"))
        self.assertIn("- **10:01** вторая", self.inbox.read_text(encoding="utf-8"))

    def test_duplicate_or_changed_inbox_identity_rejects_edit(self):
        self.inbox_text(["- **10:00** одинаковая", "- **10:00** одинаковая"])
        identity = thought_feed.inbox_thoughts(self.vault, 10)[0].identity
        token = task_ui.issue_thought_token(self.state, identity)
        stale = task_ui.begin_thought_edit(self.state, self.vault, token)
        self.assertTrue(stale.stale)

    def test_delete_then_restore_note_never_overwrites_new_file(self):
        path = self.note("оригинал")
        identity = thought_feed.recent_thoughts(self.vault, 10)[0].identity
        token = task_ui.issue_thought_token(self.state, identity)
        confirmation = task_ui.confirm_thought_delete(self.state, self.vault, token)
        self.assertIn("Удалить", confirmation.text)

        deleted = task_ui.delete_thought(self.state, self.vault, token)
        self.assertFalse(path.exists())
        restore_token = [data.split(":")[-1] for row in deleted.rows for label, data in row
                         if label == "↩️ Восстановить"][0]
        path.write_text("новая заметка", encoding="utf-8")
        restored = task_ui.restore_thought(self.state, self.vault, restore_token)
        self.assertTrue(restored.stale)
        self.assertEqual(path.read_text(encoding="utf-8"), "новая заметка")

    def test_delete_and_restore_inbox_keeps_new_neighbours(self):
        self.inbox_text(["- **10:00** первая", "- **10:01** вторая"])
        thought = thought_feed.inbox_thoughts(self.vault, 10)[1]
        token = task_ui.issue_thought_token(self.state, thought.identity)
        task_ui.confirm_thought_delete(self.state, self.vault, token)
        deleted = task_ui.delete_thought(self.state, self.vault, token)
        text = self.inbox.read_text(encoding="utf-8")
        self.assertNotIn("первая", text)
        self.inbox.write_text(text.replace("- **10:01** вторая", "- **10:01** вторая\n- **10:02** новая"), encoding="utf-8")
        restore_token = [data.split(":")[-1] for row in deleted.rows for label, data in row
                         if label == "↩️ Восстановить"][0]
        restored = task_ui.restore_thought(self.state, self.vault, restore_token)
        self.assertFalse(restored.stale)
        restored_text = self.inbox.read_text(encoding="utf-8")
        self.assertIn("первая", restored_text)
        self.assertIn("вторая", restored_text)
        self.assertIn("новая", restored_text)

    def test_delete_requires_the_matching_confirmation(self):
        self.note("оригинал")
        identity = thought_feed.recent_thoughts(self.vault, 10)[0].identity
        token = task_ui.issue_thought_token(self.state, identity)
        rejected = task_ui.delete_thought(self.state, self.vault, token)
        self.assertTrue(rejected.stale)
        self.assertTrue((self.daily / "2026-10-01 — мысль.md").exists())

    def test_cancelled_confirmation_cannot_be_replayed(self):
        path = self.note("оригинал")
        identity = thought_feed.recent_thoughts(self.vault, 10)[0].identity
        token = task_ui.issue_thought_token(self.state, identity)
        task_ui.confirm_thought_delete(self.state, self.vault, token)
        task_ui.clear_thought_delete(self.state)
        rejected = task_ui.delete_thought(self.state, self.vault, token)
        self.assertTrue(rejected.stale)
        self.assertTrue(path.exists())


class SafetyRegressionTests(ThoughtMutationTests):
    def delete_note(self):
        path = self.note('Точное содержимое')
        thought = thought_feed.recent_thoughts(self.vault, 10)[0]
        token = task_ui.issue_thought_token(self.state, thought.identity)
        task_ui.confirm_thought_delete(self.state, self.vault, token)
        view = task_ui.delete_thought(self.state, self.vault, token)
        restore = next(data[4:] for row in view.rows for label, data in row if data.startswith('f:u:'))
        return path, restore

    def test_edit_noncanonical_sections_preserves_context(self):
        path = self.note('## Контекст\nВажный контекст\n\n## Связи\n[[Важная связь]]')
        raw = path.read_bytes()
        thought = thought_feed.recent_thoughts(self.vault, 10)[0]
        token = task_ui.issue_thought_token(self.state, thought.identity)
        task_ui.begin_thought_edit(self.state, self.vault, token)
        result = task_ui.submit_thought_edit(self.state, self.vault, 'Новая расшифровка')
        self.assertFalse(result.ok)
        self.assertEqual(path.read_bytes(), raw)

    def test_symlinked_trash_never_writes_external_directory(self):
        with tempfile.TemporaryDirectory() as external:
            (self.vault / '.trash').symlink_to(external, target_is_directory=True)
            path = self.note('Не удалять')
            thought = thought_feed.recent_thoughts(self.vault, 10)[0]
            token = task_ui.issue_thought_token(self.state, thought.identity)
            task_ui.confirm_thought_delete(self.state, self.vault, token)
            view = task_ui.delete_thought(self.state, self.vault, token)
            self.assertTrue(view.stale)
            self.assertTrue(path.exists())
            self.assertEqual(list(Path(external).iterdir()), [])

    def test_restore_survives_restart_without_user_data(self):
        path, token = self.delete_note()
        view = task_ui.restore_thought({}, self.vault, token)
        self.assertFalse(view.stale)
        self.assertTrue(path.exists())
        self.assertTrue(task_ui.restore_thought({}, self.vault, token).stale)

    def test_invalid_backup_json_shapes_are_rejected(self):
        import json
        for invalid in ([], 'text', 42, {'kind':'fleeting', 'path':[], 'content':'x'}):
            with self.subTest(invalid=invalid):
                path, token = self.delete_note()
                backup = self.vault / '.trash' / 'dusk-thoughts' / (token + '.json')
                backup.write_text(json.dumps(invalid))
                view = task_ui.restore_thought({}, self.vault, token)
                self.assertTrue(view.stale)
                self.assertFalse(path.exists())

    def test_invalid_inbox_envelope_positions_and_line_breaks_are_rejected(self):
        import json
        self.inbox_text(['- **10:00** Мысль'])
        original = self.inbox.read_bytes()
        for change in ({'position':[]}, {'line':'- **10:00** текст\n## ✅ Задачи'}):
            thought = thought_feed.inbox_thoughts(self.vault, 10)[0]
            token = task_ui.issue_thought_token(self.state, thought.identity)
            task_ui.confirm_thought_delete(self.state, self.vault, token)
            view = task_ui.delete_thought(self.state, self.vault, token)
            undo = next(data[4:] for row in view.rows for _, data in row if data.startswith('f:u:'))
            backup = self.vault / '.trash' / 'dusk-thoughts' / (undo + '.json')
            record=json.loads(backup.read_text()); record.update(change)
            record['identity']=thought_feed._identity_text(thought_feed.INBOX_REL,record['line'])
            backup.write_text(json.dumps(record))
            before=self.inbox.read_bytes()
            self.assertTrue(task_ui.restore_thought({}, self.vault, undo).stale)
            self.assertEqual(self.inbox.read_bytes(), before)
            self.inbox.write_bytes(original)

    def test_exact_note_feedback_works_beyond_200_same_day_notes(self):
        for index in range(205):
            (self.daily / ('2026-10-01 — a%03d.md' % index)).write_text('---\ntype: fleeting_note\ncreated: 2026-10-01\n---\n\nold')
        path = self.daily / '2026-10-01 — z-new.md'
        path.write_text('---\ntype: fleeting_note\ncreated: 2026-10-01\n---\n\nnew')
        view=task_ui.saved_thought_view(self.state,self.vault,path=path.relative_to(self.vault).as_posix())
        self.assertFalse(view.stale)
        self.assertTrue(any(data.startswith('f:e:') for row in view.rows for _,data in row))

    def test_capture_does_not_create_synced_lock_files(self):
        import inbox_core
        inbox_core.append_line(self.inbox, 'захват')
        self.assertEqual(list(self.vault.rglob('*.lock')), [])

    def test_inbox_parent_alias_to_private_is_not_mutated(self):
        import thought_mutations
        import shutil
        import inbox_core
        private = self.vault / 'Private'
        private.mkdir()
        target = private / '00 Инбокс.md'
        target.write_text(inbox_core.DEFAULT_BODY.replace('## 💭 Мысли и идеи', '## 💭 Мысли и идеи\n- **10:00** Личное'))
        shutil.rmtree(self.inbox.parent)
        self.inbox.parent.symlink_to(private, target_is_directory=True)
        before=target.read_bytes()
        identity=thought_feed._identity_text(thought_feed.INBOX_REL, '- **10:00** Личное')
        result=thought_mutations.edit(self.vault,identity,'Переписать')
        self.assertFalse(result.ok)
        self.assertEqual(target.read_bytes(),before)


if __name__ == "__main__":
    unittest.main()
