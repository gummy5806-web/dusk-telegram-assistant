"""Contract tests for the local, aggregate-only AI usage ledger."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import ai_usage


ALMATY = ZoneInfo("Asia/Almaty")
UTC = dt.timezone.utc


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "usage.sqlite3"
        self.env = mock.patch.dict(os.environ, {"DUSK_AI_USAGE_DB": str(self.database)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def at(self, day: int, hour: int = 12):
        return dt.datetime(2026, 10, day, hour, tzinfo=ALMATY)

    def test_known_price_totals_and_thinking_are_not_double_charged(self):
        ai_usage.record_call(
            provider="vertex", model="gemini-2.5-flash", input_tokens=1_000_000,
            output_tokens=2, thinking_tokens=3, now=self.at(10), call_id="a" * 32,
        )
        result = ai_usage.summary(now=self.at(10, 18))["today"]
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["input_tokens"], 1_000_000)
        self.assertEqual(result["output_tokens"], 2)
        self.assertEqual(result["thinking_tokens"], 3)
        self.assertEqual(result["tokens"], 1_000_005)
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.3000125)
        self.assertFalse(result["cost_unknown"])

    def test_call_id_is_idempotent_and_concurrent_writers_are_safe(self):
        now = self.at(10)
        ai_usage.record_call(provider="vertex", model="gemini-2.5-flash", now=now, call_id="b" * 32)
        ai_usage.record_call(provider="vertex", model="gemini-2.5-flash", now=now, call_id="b" * 32)
        errors = []

        def write(index):
            try:
                ai_usage.record_call(provider="vertex", model="gemini-2.5-flash", now=now,
                                     call_id=f"{index:032x}")
            except Exception as error:  # test records failures for assertion below
                errors.append(error)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(ai_usage.summary(now=now)["today"]["calls"], 31)

    def test_almaty_day_boundary_and_previous_day(self):
        ai_usage.record_call(provider="vertex", model="gemini-2.5-flash",
                             now=dt.datetime(2026, 10, 9, 18, 30, tzinfo=UTC), call_id="c" * 32)
        ai_usage.record_call(provider="vertex", model="gemini-2.5-flash",
                             now=dt.datetime(2026, 10, 9, 19, 30, tzinfo=UTC), call_id="d" * 32)
        result = ai_usage.summary(now=self.at(10, 10))
        self.assertEqual(result["today"]["date"], "2026-10-10")
        self.assertEqual(result["today"]["calls"], 1)
        self.assertEqual(result["previous_days"][0]["date"], "2026-10-09")
        self.assertEqual(result["previous_days"][0]["calls"], 1)

    def test_failed_and_unknown_models_are_honestly_unknown_cost(self):
        ai_usage.record_call(provider="vertex", model="gemini-2.5-flash", status="error",
                             now=self.at(10), call_id="e" * 32)
        ai_usage.record_call(provider="vertex", model="unpriced-model", now=self.at(10), call_id="f" * 32)
        result = ai_usage.summary(now=self.at(10))["today"]
        self.assertTrue(result["cost_unknown"])
        self.assertEqual(result["failed_calls"], 1)

    def test_embedding_cost_uses_actual_billable_characters_only(self):
        ai_usage.record_call(provider="vertex", model="gemini-embedding-001", kind="embedding",
                             billable_characters=2_000, now=self.at(10), call_id="7" * 32)
        priced = ai_usage.summary(now=self.at(10))["today"]
        self.assertAlmostEqual(priced["estimated_cost_usd"], 0.0003)
        self.assertFalse(priced["cost_unknown"])
        ai_usage.record_call(provider="vertex", model="gemini-embedding-001", kind="embedding",
                             input_tokens=999, now=self.at(10), call_id="8" * 32)
        self.assertTrue(ai_usage.summary(now=self.at(10))["today"]["cost_unknown"])

    def test_publish_read_render_and_previous_day_notice_are_bounded_and_private(self):
        vault = Path(self.directory.name) / "vault"
        vault.mkdir()
        ai_usage.record_call(provider="vertex", model="gemini-2.5-flash", input_tokens=10,
                             now=self.at(9), call_id="1" * 32)
        ai_usage.record_call(provider="vertex", model="gemini-2.5-flash", input_tokens=20,
                             now=self.at(10), call_id="2" * 32)
        path = ai_usage.publish(vault, now=self.at(10))
        self.assertEqual(path, vault / "SYSTEM" / "AI_USAGE" / "summary.json")
        loaded = ai_usage.read_published(vault)
        self.assertEqual(loaded["today"]["input_tokens"], 20)
        rendered = ai_usage.render(vault)
        self.assertLess(len(rendered), 900)
        self.assertIn("🤖 Расход AI", rendered)
        self.assertIn("примерно", rendered)
        notice_id, notice = ai_usage.daily_notice(vault, now=self.at(10))
        self.assertRegex(notice_id, r"^[a-f0-9]{24}$")
        self.assertIn("За", notice)
        self.assertIn("2026-10-09", notice)

    def test_publish_skips_unchanged_totals_and_rollover_publishes_zero_day(self):
        vault = Path(self.directory.name) / "vault"
        vault.mkdir()
        ai_usage.record_call(provider="vertex", model="gemini-2.5-flash", input_tokens=10,
                             now=self.at(10), call_id="9" * 32)
        ai_usage.publish(vault, now=self.at(10, 8))
        first = ai_usage.read_published(vault)
        with mock.patch.object(ai_usage.queue_store, "atomic_write", wraps=ai_usage.queue_store.atomic_write) as write:
            ai_usage.publish(vault, now=self.at(10, 18))
            write.assert_not_called()
        self.assertEqual(ai_usage.read_published(vault)["generated_at_utc"], first["generated_at_utc"])
        ai_usage.publish(vault, now=self.at(11, 8))
        rollover = ai_usage.read_published(vault)
        self.assertEqual(rollover["today"]["date"], "2026-10-11")
        self.assertEqual(rollover["today"]["calls"], 0)

    def test_publish_refuses_symlinked_system_path(self):
        vault = Path(self.directory.name) / "vault"
        external = Path(self.directory.name) / "external"
        vault.mkdir()
        external.mkdir()
        (vault / "SYSTEM").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            ai_usage.publish(vault, now=self.at(10))
        self.assertFalse((external / "AI_USAGE" / "summary.json").exists())


class LlmAccountingTests(unittest.TestCase):
    def test_vertex_records_sdk_usage_without_combining_candidate_and_thought_tokens(self):
        import llm
        from types import SimpleNamespace

        response = SimpleNamespace(
            text="answer",
            # Official Gemini example: prompt=6, candidates=569,
            # thoughts=1132, total=1707. Candidates exclude thoughts.
            usage_metadata=SimpleNamespace(prompt_token_count=6, candidates_token_count=569,
                                           thoughts_token_count=1132, total_token_count=1707),
        )
        client = mock.Mock()
        client.models.generate_content.return_value = response
        genai = SimpleNamespace(Client=mock.Mock(return_value=client), types=SimpleNamespace())
        with mock.patch.dict("sys.modules", {"google": SimpleNamespace(genai=genai), "google.genai": genai}), \
                mock.patch.object(llm, "_project", return_value="project"), \
                mock.patch.object(llm.ai_usage, "record_call") as record:
            self.assertEqual(llm._vertex("private prompt", "gemini-2.5-flash"), "answer")
        record.assert_called_once_with(
            provider="vertex", model="gemini-2.5-flash", kind="generation",
            input_tokens=6, output_tokens=569, thinking_tokens=1132,
            estimated=False, status="ok", call_id=mock.ANY, error_type=None,
        )

    def test_vertex_records_failed_attempt_without_breaking_error_result(self):
        import llm
        from types import SimpleNamespace

        client = mock.Mock()
        client.models.generate_content.side_effect = RuntimeError("network unavailable")
        genai = SimpleNamespace(Client=mock.Mock(return_value=client), types=SimpleNamespace())
        with mock.patch.dict("sys.modules", {"google": SimpleNamespace(genai=genai), "google.genai": genai}), \
                mock.patch.object(llm, "_project", return_value="project"), \
                mock.patch.object(llm.ai_usage, "record_call") as record:
            self.assertIn("Ошибка Vertex (RuntimeError)", llm._vertex("private prompt", "gemini-2.5-flash"))
        record.assert_called_once_with(
            provider="vertex", model="gemini-2.5-flash", kind="generation",
            input_tokens=0, output_tokens=0, thinking_tokens=0,
            estimated=True, status="error", call_id=mock.ANY, error_type="RuntimeError",
        )


if __name__ == "__main__":
    unittest.main()
