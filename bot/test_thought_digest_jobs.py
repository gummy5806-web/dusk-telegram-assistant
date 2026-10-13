#!/usr/bin/env python3
"""Tests for the immutable local-to-VPS thought digest handoff."""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import subprocess
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import thought_digest_jobs as jobs
import thought_digest_worker as worker


NOW = dt.datetime(2026, 10, 10, 12, 30, tzinfo=dt.timezone.utc)


class DigestJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)

    def generator(self, snapshot):
        return {"snapshot": snapshot, "sections": [], "error": ""}

    def test_enqueue_is_deterministic_for_a_update_and_immutable(self):
        first = jobs.enqueue(self.vault, "today", key="telegram:91", now=NOW)
        second = jobs.enqueue(self.vault, "today", key="telegram:91", now=NOW + dt.timedelta(days=1))
        self.assertEqual(first, second)
        self.assertRegex(first["id"], r"^[0-9a-f]{24}$")
        request = jobs.get_request(self.vault, first["id"])
        self.assertEqual(request, first)
        self.assertEqual(len(jobs.pending(self.vault)), 1)

    def test_process_is_once_even_under_concurrent_calls(self):
        job = jobs.enqueue(self.vault, "recent", now=NOW)
        calls = []

        def generate(snapshot):
            calls.append(snapshot)
            return {"snapshot": snapshot, "sections": [], "error": ""}

        threads = [threading.Thread(target=jobs.process_one,
                                    args=(self.vault, generate), kwargs={"now": NOW})
                   for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(calls), 1)
        result = jobs.get_result(self.vault, job["id"])
        self.assertEqual(result["id"], job["id"])
        self.assertEqual(result["result"]["error"], "")

    def test_malformed_and_symlinked_queue_entries_are_rejected(self):
        root = self.vault / jobs.QUEUE_ROOT
        requests = root / "requests"
        requests.mkdir(parents=True)
        (requests / ("a" * 24 + ".json")).write_text("{}", encoding="utf-8")
        self.assertEqual(jobs.pending(self.vault), [])
        separate = self.vault / "symlink-vault"
        separate.mkdir()
        outside = self.vault.parent / "outside-digest-queue"
        outside.mkdir(exist_ok=True)
        (separate / "SYSTEM").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(jobs.QueueSafetyError):
            jobs.enqueue(separate, "today", now=NOW)
        self.assertFalse((outside / "THOUGHT_DIGEST").exists())

    def test_bad_input_and_oversize_records_never_enter_pending(self):
        with self.assertRaises(ValueError):
            jobs.enqueue(self.vault, "forever", now=NOW)
        jobs.enqueue(self.vault, "week", now=NOW)
        request_path = next((self.vault / jobs.QUEUE_ROOT / "requests").glob("*.json"))
        request_path.write_bytes(b"{" + b"x" * (jobs.MAX_JSON_BYTES + 1))
        self.assertEqual(jobs.pending(self.vault), [])


class WorkerTests(unittest.TestCase):
    def setUp(self):
        auxiliary = mock.patch.object(worker.assistant_jobs, "run", return_value=True)
        auxiliary.start()
        self.addCleanup(auxiliary.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)

    def test_worker_persists_model_error_and_commits_only_its_result(self):
        job = jobs.enqueue(self.vault, "today", now=NOW)
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(platform, "system", return_value="Darwin"), \
                mock.patch.object(worker.subprocess, "run", side_effect=run), \
                mock.patch.object(worker.jobs, "process_one", return_value=job["id"]), \
                mock.patch.object(worker.jobs, "sync_file", return_value=True):
            code = worker.run_worker(self.vault)
        self.assertEqual(code, 0)
        self.assertIn(["git", "pull", "--rebase", "--autostash"], calls)
        self.assertNotIn(["git", "add", "-A"], calls)

    def test_sync_failure_is_nonzero_and_unsynced_result_retries_next_run(self):
        job = jobs.enqueue(self.vault, "today", now=NOW)
        jobs.process_one(self.vault, now=NOW)
        attempted = []

        def fail_push(command, **kwargs):
            attempted.append(command)
            if command[:3] == ["git", "status", "--porcelain"]:
                return subprocess.CompletedProcess(command, 0,
                                                   stdout=f"?? {jobs.QUEUE_ROOT}/results/{job['id']}.json")
            return subprocess.CompletedProcess(command, 1 if command[:2] == ["git", "push"] else 0)

        with mock.patch.object(platform, "system", return_value="Darwin"), \
                mock.patch.object(worker.subprocess, "run", side_effect=fail_push):
            self.assertNotEqual(worker.run_worker(self.vault), 0)

        def pass_run(command, **kwargs):
            attempted.append(command)
            if command[:3] == ["git", "status", "--porcelain"]:
                return subprocess.CompletedProcess(command, 0,
                                                   stdout=f"?? {jobs.QUEUE_ROOT}/results/{job['id']}.json")
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(platform, "system", return_value="Darwin"), \
                mock.patch.object(worker.subprocess, "run", side_effect=pass_run):
            self.assertEqual(worker.run_worker(self.vault), 0)
        self.assertGreaterEqual(sum(1 for command in attempted if command[:2] == ["git", "push"]), 2)

    def test_sync_file_stages_only_the_requested_queue_file(self):
        job = jobs.enqueue(self.vault, "today", now=NOW)
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            code = 1 if command[:4] == ["git", "diff", "--cached", "--quiet"] else 0
            return subprocess.CompletedProcess(command, code)

        path = jobs.request_path(self.vault, job["id"])
        with mock.patch.object(jobs.subprocess, "run", side_effect=run):
            self.assertTrue(jobs.sync_file(self.vault, path))
        self.assertIn(["git", "add", "--", str(path)], calls)
        self.assertNotIn(["git", "add", "-A"], calls)

    def test_worker_reenters_its_process_lock_without_hanging(self):
        jobs.enqueue(self.vault, "today", now=NOW)
        outcome = []

        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="" if kwargs.get("text") else None)

        def invoke():
            outcome.append(worker.run_worker(
                self.vault,
                generate=lambda snapshot: {"snapshot": snapshot, "sections": [], "error": "model unavailable"},
            ))

        with mock.patch.object(platform, "system", return_value="Darwin"), \
                mock.patch.object(worker.subprocess, "run", side_effect=run):
            thread = threading.Thread(target=invoke)
            thread.start()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "nested worker lock deadlocked")
        self.assertEqual(outcome, [0])

    def test_real_git_status_finds_an_untracked_result_file(self):
        subprocess.run(["git", "init", "-q"], cwd=self.vault, check=True)
        job = jobs.enqueue(self.vault, "today", now=NOW)
        jobs.process_one(self.vault, now=NOW)
        found = worker._result_needs_sync(self.vault)
        self.assertEqual(found, jobs.result_path(self.vault, job["id"]))

    def test_worker_stops_before_processing_when_pull_fails_or_not_macos(self):
        with mock.patch.object(platform, "system", return_value="Linux"):
            self.assertNotEqual(worker.run_worker(self.vault), 0)
        with mock.patch.object(platform, "system", return_value="Darwin"), \
                mock.patch.object(worker.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 1)), \
                mock.patch.object(worker.jobs, "process_one") as process:
            self.assertNotEqual(worker.run_worker(self.vault), 0)
            process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
