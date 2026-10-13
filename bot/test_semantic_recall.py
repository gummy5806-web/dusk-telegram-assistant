from __future__ import annotations

import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

import resources
import semantic_recall
import memory_history
import thought_feed


class SemanticRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        (self.vault / "ZETA/FLEETING").mkdir(parents=True)
        (self.vault / "ZETA/FLEETING_DAILY").mkdir(parents=True)
        (self.vault / "TaskNotes/Tasks").mkdir(parents=True)
        self.db = self.vault / "semantic.sqlite3"

    def _inbox(self, lines):
        (self.vault / "ZETA/FLEETING/00 Инбокс.md").write_text(
            "# Inbox\n\n## 💭 Мысли и идеи\n\n" + "\n".join(lines)
            + "\n\n## ✅ Задачи\n\n- [ ] system task\n\n## 🔗 Ссылки и материалы\n\n- private link\n",
            encoding="utf-8")

    def _task(self, name, title, status="next"):
        (self.vault / "TaskNotes/Tasks" / name).write_text(
            "---\n" + f'title: "{title}"\nstatus: {status}\n' + "---\n# body\n",
            encoding="utf-8")

    @staticmethod
    def embed(text, task_type):
        words = text.casefold()
        if task_type == "RETRIEVAL_QUERY":
            return [1.0, 0.0] if "wellbeing" in words or "самочув" in words else [0.0, 1.0]
        return [1.0, 0.0] if "энерг" in words or "energy" in words else [0.0, 1.0]

    def test_semantic_match_uses_user_thought_text_and_excludes_private_system(self):
        self._inbox(["- Моя энергия после прогулки заметно лучше"])
        (self.vault / "ZETA/FLEETING_DAILY/2026-10-01 — Заголовок.md").write_text(
            "---\ntitle: generated title\n---\n# Заголовок\n\nконтекст без совпадения\n",
            encoding="utf-8")
        (self.vault / "Private").mkdir()
        (self.vault / "Private/secret.md").write_text("energy secret", encoding="utf-8")
        (self.vault / "Research Brain").mkdir()
        (self.vault / "Research Brain/paper.md").write_text("energy paper", encoding="utf-8")
        result = semantic_recall.search(self.vault, "wellbeing", self.embed, db_path=self.db)
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(result["hits"][0]["kind"], "thought")
        self.assertIn("энергия", result["hits"][0]["excerpt"].casefold())
        self.assertTrue(semantic_recall.validate_hit(self.vault, result["hits"][0]))

    def test_multilingual_resource_and_exact_task_path_are_searchable(self):
        resources.save(self.vault, "https://example.test/a", "energy recovery protocol")
        self._task("Прогулка.md", "Сделать energy прогулку")
        result = semantic_recall.search(self.vault, "самочувствие", self.embed, db_path=self.db)
        self.assertEqual({hit["kind"] for hit in result["hits"]}, {"resource", "task"})
        task = next(hit for hit in result["hits"] if hit["kind"] == "task")
        self.assertEqual(task["identity"], "TaskNotes/Tasks/Прогулка.md")
        self.assertEqual(task["path"], "TaskNotes/Tasks/Прогулка.md")

    def test_unknown_query_has_no_irrelevant_backstop(self):
        self._inbox(["- Моя энергия после прогулки заметно лучше"])
        result = semantic_recall.search(self.vault, "greetings", self.embed, db_path=self.db)
        self.assertEqual(result["hits"], [])

    def test_relative_floor_requires_results_to_be_within_point_one_of_best(self):
        self._inbox(["- best semantic candidate", "- borderline semantic candidate"])

        def ranked_embed(text, task_type):
            if task_type == "RETRIEVAL_QUERY":
                return [1.0, 0.0]
            if "best" in text:
                return [0.9, math.sqrt(0.19)]
            return [0.7, math.sqrt(0.51)]

        result = semantic_recall.search(self.vault, "semantic request", ranked_embed, db_path=self.db)
        self.assertEqual(len(result["hits"]), 1)
        self.assertIn("best", result["hits"][0]["excerpt"])

    def test_default_gemini_floor_rejects_observed_point_six_two_five_greeting_match(self):
        self._inbox(["- mail administration record"])

        def greeting_embed(text, task_type):
            if task_type == "RETRIEVAL_QUERY":
                return [1.0, 0.0]
            return [0.625, math.sqrt(1.0 - 0.625 ** 2)]

        result = semantic_recall.search(self.vault, "hello there", greeting_embed, db_path=self.db)
        self.assertEqual(result["hits"], [])

    def test_changed_or_removed_sources_are_never_delivered_from_cache(self):
        self._inbox(["- Energy improves after walking"])
        first = semantic_recall.search(self.vault, "wellbeing", self.embed, db_path=self.db)
        self.assertEqual(len(first["hits"]), 1)
        self._inbox(["- Completely different note"])
        changed = semantic_recall.search(self.vault, "wellbeing", self.embed, db_path=self.db)
        self.assertEqual(changed["hits"], [])
        self._inbox([])
        removed = semantic_recall.search(self.vault, "wellbeing", self.embed, db_path=self.db)
        self.assertEqual(removed["hits"], [])

    def test_done_task_zero_and_dimension_invalid_embeddings_are_rejected(self):
        self._task("Готово.md", "Energy walk", "done")
        valid = semantic_recall.search(self.vault, "wellbeing", self.embed, db_path=self.db)
        self.assertEqual(valid["hits"], [])
        self._task("Открыто.md", "Energy walk")
        result = semantic_recall.search(
            self.vault, "wellbeing", lambda _text, kind: [0.0, 0.0] if kind == "RETRIEVAL_QUERY" else [1.0], db_path=self.db)
        self.assertEqual(result["hits"], [])
        self.assertTrue(result["error"])
        self.assertFalse(semantic_recall._valid_vector([math.inf, 0.0]))

    def test_full_structured_thought_field_and_fair_initial_indexing(self):
        body = "обычный текст " * 35 + "energy appears at the end"
        (self.vault / "ZETA/FLEETING_DAILY/2026-10-01 — Длинная.md").write_text(
            "---\ntitle: generated title\n---\n# Generated\n\n## Мысль\n\n- " + body
            + "\n\n## Контекст\n\nenergy from agent context", encoding="utf-8")
        resources.save(self.vault, "https://example.test/fair", "energy resource")
        self._task("Fair.md", "energy task")
        result = semantic_recall.search(self.vault, "wellbeing", self.embed, db_path=self.db, max_new=3)
        self.assertEqual({hit["kind"] for hit in result["hits"]}, {"thought", "resource", "task"})

    def test_edited_resource_and_task_invalidate_a_previous_hit(self):
        resource = resources.save(self.vault, "https://example.test/edit", "energy resource").resource
        self._task("Edit.md", "energy task")
        result = semantic_recall.search(self.vault, "wellbeing", self.embed, db_path=self.db)
        old_resource = next(hit for hit in result["hits"] if hit["kind"] == "resource")
        old_task = next(hit for hit in result["hits"] if hit["kind"] == "task")
        resources.update(self.vault, resource.id, resource.version, "changed energy resource")
        task_path = self.vault / "TaskNotes/Tasks/Edit.md"
        task_path.write_text(task_path.read_text(encoding="utf-8") + "edited task note body\n", encoding="utf-8")
        self.assertFalse(semantic_recall.validate_hit(self.vault, old_resource))
        self.assertFalse(semantic_recall.validate_hit(self.vault, old_task))

    def test_cold_index_round_robins_across_source_kinds(self):
        for number in range(55):
            (self.vault / "ZETA/FLEETING_DAILY" / f"note-{number:02}.md").write_text(
                "---\n---\nunrelated thought", encoding="utf-8")
        resources.save(self.vault, "https://example.test/fairness", "energy resource")
        self._task("Fairness.md", "energy task")
        result = semantic_recall.search(self.vault, "wellbeing", self.embed, db_path=self.db, max_new=3)
        self.assertEqual({hit["kind"] for hit in result["hits"]}, {"resource", "task"})
        self.assertTrue(result["coverage"]["partial"])

    def test_past_thought_never_enters_current_corpus(self):
        older_path = self.vault / "ZETA/FLEETING_DAILY/2026-10-01 — Старое.md"
        newer_path = self.vault / "ZETA/FLEETING_DAILY/2026-10-02 — Новое.md"
        older_path.write_text("---\n---\nenergy old thought", encoding="utf-8")
        newer_path.write_text("---\n---\nenergy newer thought", encoding="utf-8")
        newer, older = thought_feed.recent_thoughts(self.vault, 10)
        proposal = memory_history.prepare_supersede(self.vault, older.identity, newer.identity)
        self.assertTrue(memory_history.commit_supersede(self.vault, proposal["id"]).ok)
        result = semantic_recall.search(self.vault, "wellbeing", self.embed, db_path=self.db)
        self.assertEqual([hit["identity"] for hit in result["hits"]], [newer.identity])

    def test_background_index_batches_then_reuses_cache_and_prunes_changed_deleted_sources(self):
        self._inbox(["- energy before edit"])
        calls = []

        def document_embed(text, task_type):
            calls.append((text, task_type))
            self.assertEqual(task_type, "RETRIEVAL_DOCUMENT")
            return [1.0, 0.0]

        first = semantic_recall.index(self.vault, document_embed, db_path=self.db, max_new=20)
        self.assertEqual(first["new"], 1)
        self.assertFalse(first["error"])
        second = semantic_recall.index(self.vault, document_embed, db_path=self.db, max_new=20)
        self.assertEqual(second["new"], 0)
        self.assertEqual(len(calls), 1)
        self._inbox(["- energy after edit"])
        changed = semantic_recall.index(self.vault, document_embed, db_path=self.db, max_new=20)
        self.assertEqual(changed["new"], 1)
        self._inbox([])
        deleted = semantic_recall.index(self.vault, document_embed, db_path=self.db, max_new=20)
        self.assertEqual(deleted["sources"], 0)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0], 0)

    def test_background_index_reports_partial_for_a_bounded_batch(self):
        for number in range(25):
            (self.vault / "ZETA/FLEETING_DAILY" / f"batch-{number:02}.md").write_text(
                "---\n---\nenergy thought", encoding="utf-8")
        calls = []
        result = semantic_recall.index(
            self.vault, lambda text, task_type: calls.append((text, task_type)) or [1.0, 0.0],
            db_path=self.db, max_new=20)
        self.assertEqual(result["new"], 20)
        self.assertTrue(result["partial"])
        self.assertEqual(len(calls), 20)


if __name__ == "__main__":
    unittest.main()
