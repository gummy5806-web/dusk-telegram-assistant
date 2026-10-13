import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import memory_history
import resources
import thought_feed
import thought_mutations


class MemoryHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name)
        (self.vault / "ZETA" / "FLEETING_DAILY").mkdir(parents=True)
        (self.vault / "ZETA" / "FLEETING").mkdir(parents=True)

    def note(self, body="Старая мысль"):
        path = self.vault / "ZETA" / "FLEETING_DAILY" / "2026-10-01 — мысль.md"
        path.write_text("---\ntype: fleeting_note\ncreated: 2026-10-01\n---\n\n" + body + "\n",
                        encoding="utf-8")
        return path

    def inbox(self, lines):
        (self.vault / thought_feed.INBOX_REL).write_text(
            "# Inbox\n\n## 💭 Мысли и идеи\n\n" + "\n".join(lines)
            + "\n\n## ✅ Задачи\n", encoding="utf-8")

    def test_resource_update_preserves_exact_before_snapshot_and_commits_after_write(self):
        resource = resources.save(self.vault, "https://example.test/a", "раньше").resource
        result = resources.update(self.vault, resource.id, resource.version, "сейчас")
        self.assertTrue(result.ok)
        events = memory_history.history(self.vault, "resource", resource.id)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["status"], "committed")
        self.assertIn("раньше", event["before"]["content"])
        self.assertIn("сейчас", event["after"]["content"])
        self.assertNotEqual(event["before"]["hash"], event["after"]["hash"])
        self.assertTrue(event["after"]["is_current"])

    def test_stale_or_failed_source_write_never_creates_committed_version(self):
        resource = resources.save(self.vault, "https://example.test/a", "раньше").resource
        path = resources.resource_path(self.vault, resource.id)
        path.write_text(path.read_text(encoding="utf-8") + "\nвнешнее\n", encoding="utf-8")
        stale = resources.update(self.vault, resource.id, resource.version, "сейчас")
        self.assertTrue(stale.stale)
        self.assertEqual(memory_history.history(self.vault, "resource", resource.id), [])

        current = resources.load(self.vault, resource.id)
        with mock.patch("resources.queue_store.atomic_write", side_effect=OSError("disk")):
            failed = resources.update(self.vault, resource.id, current.version, "новое")
        self.assertFalse(failed.ok)
        self.assertEqual(memory_history.history(self.vault, "resource", resource.id), [])

    def test_structured_edit_keeps_frontmatter_and_resolves_old_identity_to_history(self):
        path = self.note()
        old = thought_feed.recent_thoughts(self.vault)[0]
        changed = thought_mutations.edit(self.vault, old.identity, "Новая мысль")
        self.assertTrue(changed.ok)
        self.assertTrue(path.read_text(encoding="utf-8").startswith("---\ntype: fleeting_note"))
        for ref in (old.identity, changed.identity):
            events = memory_history.history(self.vault, "thought", ref)
            self.assertEqual(len(events), 1)
            self.assertIn("Старая мысль", events[0]["before"]["content"])
            self.assertIn("Новая мысль", events[0]["after"]["content"])

    def test_inbox_keeps_distinct_line_identity_chain(self):
        self.inbox(["- **10:00** первая", "- **10:01** первая"])
        first, second = thought_feed.inbox_thoughts(self.vault, 10)
        changed = thought_mutations.edit(self.vault, first.identity, "исправленная")
        self.assertTrue(changed.ok)
        events = memory_history.history(self.vault, "thought", first.identity)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["after"]["identity"], changed.identity)
        self.assertIsNone(thought_feed.load_thought(self.vault, first.identity))
        self.assertIsNotNone(thought_feed.load_thought(self.vault, second.identity))

    def test_supersede_requires_exact_current_identities_and_restore_is_guarded(self):
        old_path = self.note("Первая")
        newer_path = self.vault / "ZETA" / "FLEETING_DAILY" / "2026-10-02 — вторая.md"
        newer_path.write_text("---\ntype: fleeting_note\n---\n\nВторая\n", encoding="utf-8")
        older, newer = thought_feed.recent_thoughts(self.vault, 10)
        proposal = memory_history.prepare_supersede(self.vault, older.identity, newer.identity)
        self.assertRegex(proposal["id"], r"^[0-9a-f]{24}$")
        self.assertEqual(memory_history.proposal(self.vault, proposal["id"])["status"], "prepared")
        committed = memory_history.commit_supersede(self.vault, proposal["id"])
        self.assertTrue(committed.ok)
        self.assertTrue(memory_history.is_past(self.vault, older.identity))
        self.assertFalse(memory_history.is_past(self.vault, newer.identity))
        self.assertEqual(memory_history.past_source(self.vault, older.identity)["newer_identity"], newer.identity)
        self.assertFalse(memory_history.commit_supersede(self.vault, proposal["id"]).ok)
        restored = memory_history.restore_current(self.vault, proposal["id"])
        self.assertTrue(restored.ok)
        self.assertFalse(memory_history.is_past(self.vault, older.identity))

        stale = memory_history.prepare_supersede(self.vault, older.identity, newer.identity)
        old_path.write_text(old_path.read_text(encoding="utf-8") + "внешне\n", encoding="utf-8")
        self.assertTrue(memory_history.commit_supersede(self.vault, stale["id"]).stale)

    def test_supersede_relation_is_inert_until_proposal_commit_and_retry_recovers(self):
        self.note("Первая")
        second = self.vault / "ZETA" / "FLEETING_DAILY" / "2026-10-02 — вторая.md"
        second.write_text("---\ntype: fleeting_note\n---\n\nВторая\n", encoding="utf-8")
        older, newer = thought_feed.recent_thoughts(self.vault, 10)
        prepared = memory_history.prepare_supersede(self.vault, older.identity, newer.identity)
        proposal_path = memory_history._proposal_path(self.vault, prepared["id"], create=False)
        original = memory_history.queue_store.atomic_write

        def fail_final_write(path, text):
            if Path(path) == proposal_path and '"status":"committed"' in text:
                raise OSError("interrupted after relation")
            return original(path, text)

        with mock.patch("memory_history.queue_store.atomic_write", side_effect=fail_final_write):
            failed = memory_history.commit_supersede(self.vault, prepared["id"])
        self.assertFalse(failed.ok)
        self.assertFalse(memory_history.is_past(self.vault, older.identity))
        self.assertEqual(memory_history.proposal(self.vault, prepared["id"])["status"], "prepared")

        recovered = memory_history.commit_supersede(self.vault, prepared["id"])
        self.assertTrue(recovered.ok)
        self.assertTrue(memory_history.is_past(self.vault, older.identity))

    def test_supersede_rejects_a_relationship_cycle(self):
        self.note("Первая")
        second = self.vault / "ZETA" / "FLEETING_DAILY" / "2026-10-02 — вторая.md"
        second.write_text("---\ntype: fleeting_note\n---\n\nВторая\n", encoding="utf-8")
        older, newer = thought_feed.recent_thoughts(self.vault, 10)
        first = memory_history.prepare_supersede(self.vault, older.identity, newer.identity)
        self.assertTrue(memory_history.commit_supersede(self.vault, first["id"]).ok)
        with self.assertRaises(memory_history.HistoryError):
            memory_history.prepare_supersede(self.vault, newer.identity, older.identity)

    def test_reverse_proposal_prepared_before_first_commit_is_rejected_at_commit(self):
        self.note("Первая")
        second = self.vault / "ZETA" / "FLEETING_DAILY" / "2026-10-02 — вторая.md"
        second.write_text("---\ntype: fleeting_note\n---\n\nВторая\n", encoding="utf-8")
        older, newer = thought_feed.recent_thoughts(self.vault, 10)
        forward = memory_history.prepare_supersede(self.vault, older.identity, newer.identity)
        reverse = memory_history.prepare_supersede(self.vault, newer.identity, older.identity)
        self.assertTrue(memory_history.commit_supersede(self.vault, forward["id"]).ok)
        rejected = memory_history.commit_supersede(self.vault, reverse["id"])
        self.assertFalse(rejected.ok)
        self.assertTrue(rejected.stale)

    def test_second_prepared_replacement_for_same_old_thought_becomes_stale(self):
        self.note("Первая")
        for day, body in (("02", "Вторая"), ("03", "Третья")):
            (self.vault / "ZETA" / "FLEETING_DAILY" / f"2026-10-{day} — {body}.md").write_text(
                "---\ntype: fleeting_note\n---\n\n" + body + "\n", encoding="utf-8")
        third, second, first = thought_feed.recent_thoughts(self.vault, 10)
        to_second = memory_history.prepare_supersede(self.vault, first.identity, second.identity)
        to_third = memory_history.prepare_supersede(self.vault, first.identity, third.identity)
        self.assertTrue(memory_history.commit_supersede(self.vault, to_second["id"]).ok)
        rejected = memory_history.commit_supersede(self.vault, to_third["id"])
        self.assertFalse(rejected.ok)
        self.assertTrue(rejected.stale)

    def test_concurrent_confirmations_choose_exactly_one_replacement(self):
        self.note("Первая")
        for day, body in (("02", "Вторая"), ("03", "Третья")):
            (self.vault / "ZETA" / "FLEETING_DAILY" / f"2026-10-{day} — {body}.md").write_text(
                "---\ntype: fleeting_note\n---\n\n" + body + "\n", encoding="utf-8")
        third, second, first = thought_feed.recent_thoughts(self.vault, 10)
        proposals = (
            memory_history.prepare_supersede(self.vault, first.identity, second.identity),
            memory_history.prepare_supersede(self.vault, first.identity, third.identity),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda item: memory_history.commit_supersede(self.vault, item["id"]),
                                     proposals))
        self.assertEqual(sum(result.ok for result in outcomes), 1)
        self.assertEqual(sum(result.stale for result in outcomes), 1)

