from __future__ import annotations

import datetime as dt
import threading
import tempfile
import unittest
from pathlib import Path

import semantic_search_jobs as jobs


NOW = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)


class SemanticSearchJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)

    def test_enqueue_and_process_once_with_durable_result(self):
        job = jobs.enqueue(self.vault, "how is energy?", now=NOW)
        self.assertRegex(job["id"], r"^[0-9a-f]{24}$")
        calls = []

        def search(vault, query):
            calls.append((vault, query))
            return {"hits": [], "coverage": {"sources": 0}, "error": ""}

        workers = [threading.Thread(target=jobs.process_one, args=(self.vault, search), kwargs={"now": NOW}) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len(calls), 1)
        result = jobs.get_result(self.vault, job["id"])
        self.assertEqual(set(result), {"id", "query", "completed", "result"})
        self.assertEqual(result["id"], job["id"])
        self.assertEqual(result["query"], job["query"])
        self.assertEqual(jobs.process_one(self.vault, search, now=NOW), None)

    def test_queue_rejects_malformed_symlink_and_long_query(self):
        with self.assertRaises(ValueError):
            jobs.enqueue(self.vault, "x" * 161, now=NOW)
        requests = self.vault / jobs.QUEUE_ROOT / "requests"
        requests.mkdir(parents=True)
        (requests / ("a" * 24 + ".json")).write_text("{}", encoding="utf-8")
        self.assertEqual(jobs.requests(self.vault), [])
        outside = self.vault.parent / "semantic-outside"
        outside.mkdir(exist_ok=True)
        other = self.vault / "other"
        other.mkdir()
        (other / "SYSTEM").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(jobs.QueueSafetyError):
            jobs.enqueue(other, "safe", now=NOW)

    def test_worker_persists_search_error_as_valid_result(self):
        job = jobs.enqueue(self.vault, "find it", now=NOW)

        def broken(_vault, _query):
            raise RuntimeError("offline")

        self.assertEqual(jobs.process_one(self.vault, broken, now=NOW), job["id"])
        result = jobs.get_result(self.vault, job["id"])
        self.assertEqual(result["result"]["hits"], [])
        self.assertIn("RuntimeError", result["result"]["error"])


if __name__ == "__main__":
    unittest.main()
