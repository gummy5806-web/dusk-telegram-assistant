#!/usr/bin/env python3
"""Проход 3: целостность снимка, границы слов, честное покрытие, бюджет сводки.

Все проверки offline: temp-vault и подменённые сеть/модель.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import learn
import transcript_source as ts

from test_learn_queue import AREA
from test_stage2a_ingest import SYNTHESIS, URL, Stage2ABase, fake_model, is_synthesis


def summary(**overrides) -> dict:
    data = dict(SYNTHESIS)
    data.update(overrides)
    return data


class SnapshotIntegrityTests(Stage2ABase):
    """Пункт 1: каждый путь успеха проверяет весь согласованный набор."""

    def publish(self):
        ok, message = self.run_ingest()
        self.assertTrue(ok, message)
        return self.our_source_set(), self.our_notes()[-1]

    def retry(self) -> str:
        self.write_queue(URL)
        with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            return learn.process_queue()

    def test_retry_rejects_corrupted_raw(self) -> None:
        known, _ = self.publish()
        known["raw"].write_text("CORRUPTED\n", encoding="utf-8")

        report = self.retry()

        self.assertIn(URL, self.queue_urls(), "повреждённый raw не подтверждает очередь")
        self.assertIn("Не вышло", report)

    def test_retry_rejects_changed_json(self) -> None:
        known, _ = self.publish()
        data = json.loads(known["json"].read_text(encoding="utf-8"))
        data["segments"][0]["text"] = "подменённый текст фрагмента"
        known["json"].write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        self.retry()

        self.assertIn(URL, self.queue_urls(), "изменённый JSON не подтверждает очередь")

    def test_retry_rejects_changed_markdown_fragments(self) -> None:
        known, _ = self.publish()
        page = known["md"].read_text(encoding="utf-8")
        self.assertIn("UNIQUE MIDDLE MARKER 42", page, "маркер есть в расшифровке")
        known["md"].write_text(page.replace("UNIQUE MIDDLE MARKER 42", "подменённый текст"),
                               encoding="utf-8")

        self.retry()

        self.assertIn(URL, self.queue_urls(), "подменённая расшифровка не подтверждает очередь")

    def test_retry_accepts_healthy_set(self) -> None:
        self.publish()

        self.retry()

        self.assertNotIn(URL, self.queue_urls(), "здоровый снимок повтор подтверждает")

    def test_absolute_transcript_path_is_refused(self) -> None:
        _, note = self.publish()
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "source.md"
            original = "<!-- dusk-source: forged -->\n# чужая страница\n".encode("utf-8")
            external.write_bytes(original)
            text = re.sub(r"^transcript:.*$", f'transcript: "{external.as_posix()}"',
                          note.read_text(encoding="utf-8"), flags=re.MULTILINE)
            note.write_text(text, encoding="utf-8")

            self.retry()

            self.assertEqual(external.read_bytes(), original,
                             "запись за пределы валта недопустима")
        self.assertIn(URL, self.queue_urls(), "внешний путь не считается источником")

    def test_traversal_transcript_path_is_refused(self) -> None:
        _, note = self.publish()
        text = re.sub(r"^transcript:.*$", 'transcript: "../../outside/source.md"',
                      note.read_text(encoding="utf-8"), flags=re.MULTILINE)
        note.write_text(text, encoding="utf-8")

        self.retry()

        self.assertIn(URL, self.queue_urls(), "выход из валта через .. недопустим")

    def test_symlink_escape_is_refused(self) -> None:
        known, _ = self.publish()
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "source.md"
            external.write_text("<!-- dusk-source: forged -->\n# чужая страница\n",
                                encoding="utf-8")
            link = known["md"].with_name("linked.md")
            if link.exists():
                link.unlink()
            os.symlink(external, link)
            _, note = self.our_source_set(), self.our_notes()[-1]
            rel = link.relative_to(self.vault).as_posix()
            text = re.sub(r"^transcript:.*$", f'transcript: "{rel}"',
                          note.read_text(encoding="utf-8"), flags=re.MULTILINE)
            note.write_text(text, encoding="utf-8")

            self.retry()

            self.assertEqual(external.read_text(encoding="utf-8"),
                             "<!-- dusk-source: forged -->\n# чужая страница\n",
                             "внешний файл не изменяется")
        self.assertIn(URL, self.queue_urls(), "symlink за пределы валта недопустим")

    def test_raw_name_cannot_escape_the_set(self) -> None:
        known, _ = self.publish()
        data = json.loads(known["json"].read_text(encoding="utf-8"))
        data["raw_name"] = "../../evil.txt"
        known["json"].write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        self.retry()

        self.assertIn(URL, self.queue_urls(), "raw_name не может вести наружу")

    def test_stem_link_is_not_an_exact_backlink(self) -> None:
        known, note = self.publish()
        page = known["md"].read_text(encoding="utf-8")
        page = re.sub(r"^- \[\[.*\]\]\n?", "", page, flags=re.MULTILINE)
        page = page.rstrip("\n") + f"\n- [[{note.stem}]]\n"
        known["md"].write_text(page, encoding="utf-8")

        self.retry()

        text = known["md"].read_text(encoding="utf-8")
        exact = f"[[{note.relative_to(self.vault).with_suffix('').as_posix()}]]"
        self.assertIn(exact, text,
                      "старое [[имя]] не считается точной связью: путь от корня дописан")
        self.assertIn(f"[[{note.stem}]]", text, "старая строка остаётся как была")
        self.assertNotIn(URL, self.queue_urls(), "связь восстановлена — очередь подтверждена")

    def test_repair_keeps_immutable_hash_valid(self) -> None:
        known, note = self.publish()
        data = json.loads(known["json"].read_text(encoding="utf-8"))

        self.assertTrue(learn._read_source_set(known["dir"]) is not None,
                        "набор остаётся валидным после публикации")

        self.write_queue(URL)
        with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            learn.process_queue()

        self.assertIsNotNone(learn._read_source_set(known["dir"]),
                             "обратная ссылка не ломает неизменяемую часть")
        page = known["md"].read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(learn._immutable_body(page).encode("utf-8")).hexdigest(),
                         data["md_sha256"], "хеш неизменяемой части не меняется от ссылок")


class WordBoundaryTests(unittest.TestCase):
    """Пункт 2: перекрытие считается по целым словам."""

    def texts(self, raw: str) -> list:
        return [segment.text for segment in ts.from_vtt(URL, raw).segments]

    def test_prefix_of_a_word_is_not_an_overlap(self) -> None:
        raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n"
               "A robust approach is language model\n\n"
               "00:00:02.000 --> 00:00:04.000\nlanguage modeling works well\n")
        self.assertEqual(self.texts(raw),
                         ["A robust approach is language model", "language modeling works well"])

    def test_cyrillic_word_is_not_split(self) -> None:
        raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nмодель обучения\n\n"
               "00:00:02.000 --> 00:00:04.000\nмодельный подход\n")
        self.assertEqual(self.texts(raw), ["модель обучения", "модельный подход"])

    def test_whole_word_overlap_is_still_deduped(self) -> None:
        raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:03.000\ntoday we talk about pricing\n\n"
               "00:00:03.000 --> 00:00:06.000\ntoday we talk about pricing\n"
               "and then measure it\n")
        self.assertEqual(self.texts(raw),
                         ["today we talk about pricing", "and then measure it"])

    def test_punctuation_is_preserved(self) -> None:
        raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nfirst we ship the model,\n\n"
               "00:00:03.000 --> 00:00:06.000\nfirst we ship the model, and then measure\n")
        self.assertEqual(self.texts(raw),
                         ["first we ship the model,", "and then measure"])

    def test_repeat_at_another_time_is_kept(self) -> None:
        raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nsame chorus line\n\n"
               "00:01:00.000 --> 00:01:03.000\nsame chorus line\n")
        self.assertEqual(self.texts(raw), ["same chorus line", "same chorus line"])


class CoverageRangeTests(unittest.TestCase):
    """Пункт 3: покрытие считается по реально успешным частям."""

    def run_distill(self, source, fail_when=None):
        def model(prompt):
            if fail_when and fail_when(prompt):
                raise RuntimeError("chunk failed")
            if "СВОДКА" in prompt:
                return json.dumps(summary())
            return json.dumps({"points": [], "quotes": []})

        with patch.object(learn, "_owner_context", return_value=""), \
                patch.object(learn, "_ask_model", side_effect=model):
            return learn._distill("Fixture", ts.as_transcript(source), [AREA])

    def test_failed_piece_does_not_claim_full_coverage(self) -> None:
        source = ts.Source(url=URL, segments=[ts.Segment("seg-0001", None, None, "A" * 30000)])
        result = self.run_distill(source, fail_when=lambda p: "[ПОРОЦИЯ chunk-002]" in p)

        self.assertIn("error", result)
        self.assertIn("chunk-002", result["coverage"]["missing"])
        self.assertLess(result["coverage"]["chars_covered"], result["coverage"]["chars"])
        self.assertLess(result["coverage"]["percent"], 100,
                        "исчезнувшая часть не может показывать 100%")

    def test_full_coverage_has_equal_units(self) -> None:
        source = ts.Source(url=URL, segments=[ts.Segment("seg-0001", None, None, "A" * 30000)])
        result = self.run_distill(source)

        self.assertNotIn("error", result)
        self.assertEqual(result["coverage"]["chars_covered"], result["coverage"]["chars"])
        self.assertEqual(result["coverage"]["percent"], 100)

    def test_spaces_at_boundaries_do_not_break_units(self) -> None:
        source = ts.from_text(URL, " ".join(["slovo"] * 9000))
        result = self.run_distill(source)

        self.assertNotIn("error", result)
        self.assertEqual(result["coverage"]["chars_covered"], result["coverage"]["chars"])
        self.assertEqual(result["coverage"]["percent"], 100)

    def test_coverage_is_not_double_counted(self) -> None:
        source = ts.Source(url=URL, segments=[ts.Segment("seg-0001", None, None, "A" * 30000)])
        parts = ts.chunks(source, limit=12000)

        covered = sum(learn._part_chars(source, part, source.text) for part in parts)

        self.assertEqual(covered, 30000)

    def test_small_loss_does_not_round_up_to_100(self) -> None:
        source = ts.Source(url=URL, segments=[ts.Segment("seg-0001", None, None, "A" * 30000)])
        result = self.run_distill(source, fail_when=lambda p: "[ПОРОЦИЯ chunk-001]" in p)

        self.assertIn("error", result)
        self.assertLess(result["coverage"]["percent"], 100)


class SummaryBudgetTests(unittest.TestCase):
    """Пункт 4: у каждого запроса сводки явный бюджет."""

    BUDGET = 8000

    def distill(self, points, summary_response, marker: str = ""):
        source = ts.from_text(URL, "A useful complete source sentence.")
        sizes = []

        def model(prompt):
            if "СВОДКА" in prompt:
                sizes.append(len(prompt))
                return json.dumps(summary_response)
            if marker and marker not in prompt:
                return json.dumps({"points": [], "quotes": []})
            return json.dumps({"points": points, "quotes": []})

        with patch.object(learn, "_owner_context", return_value=""), \
                patch.object(learn, "SUMMARY_BUDGET", self.BUDGET), \
                patch.object(learn, "_ask_model", side_effect=model):
            result = learn._distill("Fixture", ts.as_transcript(source), [AREA])
        return result, sizes

    def test_long_points_are_grouped_by_size(self) -> None:
        points = [{"text": f"Point {index}: " + "substantiated content " * 50,
                   "segments": ["seg-0001"]} for index in range(81)]
        result, sizes = self.distill(points, summary())

        self.assertNotIn("error", result)
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), self.BUDGET,
                             f"запросы сводки превышают бюджет: {sorted(sizes)[-3:]}")

    def test_final_aggregation_is_bounded(self) -> None:
        points = [{"text": f"Point {index}: source content", "segments": ["seg-0001"]}
                  for index in range(160)]
        huge = dict(summary(), thesis="Supported thesis " * 200,
                    action="Inspect one small sample " * 160)
        result, sizes = self.distill(points, huge)

        self.assertNotIn("error", result)
        self.assertLessEqual(max(sizes), self.BUDGET)

    def test_single_oversized_point_is_split_not_truncated(self) -> None:
        tail = "ФИНАЛЬНЫЙ МАРКЕР В КОНЦЕ ПУНКТА"
        points = [{"text": "начало " + "слово " * 4000 + tail, "segments": ["seg-0001"]}]
        prompts = []
        source = ts.from_text(URL, "A useful complete source sentence.")

        def model(prompt):
            if "СВОДКА" in prompt:
                prompts.append(prompt)
                return json.dumps(summary())
            return json.dumps({"points": points, "quotes": []})

        with patch.object(learn, "_owner_context", return_value=""), \
                patch.object(learn, "SUMMARY_BUDGET", self.BUDGET), \
                patch.object(learn, "_ask_model", side_effect=model):
            result = learn._distill("Fixture", ts.as_transcript(source), [AREA])

        self.assertNotIn("error", result)
        self.assertTrue(prompts)
        self.assertLessEqual(max(len(prompt) for prompt in prompts), self.BUDGET)
        self.assertIn(tail, "\n".join(prompts),
                      "хвост пункта не теряется при разделении")

    def test_multi_level_aggregation_bounds_every_request(self) -> None:
        marker = "ПОЗДНИЙ МАРКЕР ИЗ ГРУППЫ"
        points = [{"text": f"Пункт {index}: " + "содержательный текст " * 60,
                   "segments": ["seg-0001"]} for index in range(120)]
        points[100]["text"] = marker + " " + points[100]["text"]
        result, sizes = self.distill(points, summary())

        self.assertNotIn("error", result)
        self.assertGreater(len(sizes), 1)
        self.assertLessEqual(max(sizes), self.BUDGET)

    def test_non_reducing_level_errors_instead_of_looping(self) -> None:
        points = [{"text": f"Point {index}: " + "content " * 900, "segments": ["seg-0001"]}
                  for index in range(12)]
        huge = dict(summary(), thesis="T " * 3000, action="A " * 3000)

        result, sizes = self.distill(points, huge)

        self.assertIn("error", result, "ливень без уменьшения объёма должен падать явно")
        self.assertLessEqual(max(sizes) if sizes else 0, self.BUDGET)

    def test_cost_formula_is_reported(self) -> None:
        points = [{"text": f"Point {index}: " + "content " * 300, "segments": ["seg-0001"]}
                  for index in range(30)]
        result, sizes = self.distill(points, summary())

        self.assertNotIn("error", result)
        self.assertEqual(result["model_calls"], 1 + len(sizes),
                         "фактические вызовы: порции плюс сводки")


if __name__ == "__main__":
    unittest.main()
