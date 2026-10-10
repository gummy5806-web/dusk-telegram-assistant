"""Persistent thought resolution and meaningful next-card navigation."""
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import thought_feed
import thought_digest
import task_ui
import thought_status

NOW = datetime.datetime(2026, 10, 10, tzinfo=datetime.timezone.utc)

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.daily = self.root / 'ZETA/FLEETING_DAILY'; self.daily.mkdir(parents=True)
        self.state = {}

    def note(self, name):
        p = self.daily / (name + '.md')
        p.write_text('---\ncreated: 2026-10-01\n---\n# '+name+'\n\n## Мысль\n- '+name+'\n')
        identity = thought_feed._identity_text(p.relative_to(self.root).as_posix(), p.read_text())
        return p, identity

    def test_done_preserves_note_and_excludes_it_from_active_feed_and_digest(self):
        path, identity = self.note('Идея')
        original = path.read_bytes()
        token = task_ui.issue_thought_token(self.state, identity)
        sync = Mock()
        view = task_ui.complete_thought(self.state, self.root, token, sync=sync)
        self.assertIn('Сделано', view.text)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(thought_status.is_done(self.root, identity))
        self.assertEqual(thought_feed.recent_thoughts(self.root), [])
        self.assertEqual(thought_digest.collect(self.root, 'recent')['sources'], [])
        self.assertEqual(len(thought_feed.completed_thoughts(self.root)), 1)
        again = task_ui.complete_thought(self.state, self.root, token, sync=sync)
        self.assertFalse(again.stale)
        sync.assert_called_once()
        task_ui.reopen_thought(self.state, self.root, token, sync=sync)
        self.assertFalse(thought_status.is_done(self.root, identity))
        self.assertEqual(len(thought_feed.recent_thoughts(self.root)), 1)

    def test_inbox_done_is_still_stored_but_not_active(self):
        inbox = self.root / thought_feed.INBOX_REL; inbox.parent.mkdir(parents=True)
        inbox.write_text('# Inbox\n\n## 💭 Мысли и идеи\n\n- **10:00** моя мысль\n')
        thought = thought_feed.inbox_thoughts(self.root)[0]
        thought_status.set_done(self.root, thought.identity, True)
        self.assertIn('моя мысль', inbox.read_text())
        self.assertEqual(thought_feed.inbox_thoughts(self.root), [])
        self.assertEqual(thought_feed.inbox_counts(self.root).thoughts, 0)
        self.assertEqual(thought_digest.collect(self.root, 'recent')['sources'], [])
        self.assertEqual(len(thought_feed.completed_thoughts(self.root)), 1)

    def test_next_from_one_saved_card_moves_to_different_thought(self):
        path, identity = self.note('Первая'); self.note('Вторая')
        view = task_ui.saved_thought_view(self.state, self.root, path=path.relative_to(self.root).as_posix())
        token = task_ui.issue_thought_token(self.state, identity)
        next_view = task_ui.next_thought(self.state, self.root, None, token)
        self.assertIn('Вторая', next_view.text)
        self.assertNotEqual(view.text, next_view.text)

    def test_single_card_next_explains_end(self):
        path, identity = self.note('Единственная')
        task_ui.saved_thought_view(self.state, self.root, path=path.relative_to(self.root).as_posix())
        view = task_ui.next_thought(self.state, self.root, None, task_ui.issue_thought_token(self.state, identity))
        self.assertIn('Других мыслей', view.text)
        self.assertFalse(view.stale)

    def test_next_skips_deleted_and_done_candidates(self):
        a, ia = self.note('A'); b, ib = self.note('B'); c, ic = self.note('C'); self.note('D')
        self.state[task_ui.THOUGHT_LIST_KEY] = [ia, ib, ic]
        b.unlink(); thought_status.set_done(self.root, ic, True)
        view = task_ui.next_thought(self.state, self.root, None, task_ui.issue_thought_token(self.state, ia))
        self.assertIn('D', view.text)

    def test_malformed_status_is_withheld_without_claiming_done_or_resetting_it(self):
        path, identity = self.note('Идея')
        thought_status.set_done(self.root, identity, True)
        record = next((self.root/'SYSTEM/THOUGHT_STATUS').glob('*.json'))
        record.write_text('{bad json')
        self.assertEqual(thought_feed.recent_thoughts(self.root), [])
        self.assertEqual(thought_feed.completed_thoughts(self.root), [])
        view = task_ui.render_thought(self.state, self.root, None, task_ui.issue_thought_token(self.state, identity))
        self.assertTrue(view.stale)
        self.assertIn('недоступна', view.text)
        with self.assertRaises(thought_status.StatusError): thought_status.set_done(self.root, identity, False)
        self.assertEqual(record.read_text(), '{bad json')

    def test_completed_inbox_is_not_sent_to_triage_model(self):
        import triage
        inbox = self.root / thought_feed.INBOX_REL; inbox.parent.mkdir(parents=True)
        inbox.write_text('# Inbox\n\n## 💭 Мысли и идеи\n\n- **10:00** мысль\n')
        thought_status.set_done(self.root, thought_feed.inbox_thoughts(self.root)[0].identity, True)
        with patch.dict('os.environ', {'VAULT_PATH':str(self.root)}), patch.object(triage.vault_ops, 'git_sync'), patch.object(triage.llm, 'ask') as model:
            answer = triage.run_triage()
        self.assertIn('пуст', answer)
        model.assert_not_called()

    def test_status_rejects_symlink_parent_and_unknown_identity(self):
        p, identity = self.note('Идея')
        outside = self.root / 'other'; outside.mkdir(); (self.root/'SYSTEM').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(thought_status.StatusError): thought_status.set_done(self.root, identity, True)
        self.assertEqual(list(outside.iterdir()), [])
        with self.assertRaises(thought_status.StatusError): thought_status.set_done(self.root, 'Private/secret.md#abcdef123456', True)

    def test_concurrent_done_is_idempotent_and_record_contains_no_note_text(self):
        p, identity = self.note('Личная идея')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: thought_status.set_done(self.root, identity, True), range(4)))
        self.assertEqual(sum(results), 1)
        records = list((self.root/'SYSTEM/THOUGHT_STATUS').glob('*.json'))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(set(record), {'identity', 'done'})
        self.assertIs(record['done'], True)

if __name__ == '__main__': unittest.main()
