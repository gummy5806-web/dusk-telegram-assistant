#!/usr/bin/env python3
"""Доработка этапа 2А: шесть групп дефектов из независимой проверки.

Каждый тест падал до правки. Сеть и модель подменены, данные временные.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from unittest.mock import patch

import learn
import transcript_source as ts

from test_learn_queue import AREA
from test_stage2a_ingest import (ACTION, MARKER, SYNTHESIS, URL, Stage2ABase,
                                 fake_model, is_synthesis, segments_in_prompt)


def summary(**overrides) -> dict:
    data = dict(SYNTHESIS)
    data.update(overrides)
    return data


class SnapshotOwnershipTests(Stage2ABase):
    """Пункт 1: чужой файл не становится нашим источником."""

    def manual(self, name: str, payload: bytes):
        folder = learn.source_dir(self.vault, self.source())
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(payload)
        return path

    def test_manual_source_md_collision_keeps_bytes(self) -> None:
        manual = self.manual("source.md", b"# My manual source\n\nDo not alter.\n")

        ok, message = self.run_ingest()

        self.assertTrue(ok, message)
        self.assertEqual(manual.read_bytes(), b"# My manual source\n\nDo not alter.\n",
                         "чужой файл не изменяется и не дописывается")
        sets = self.source_dirs()
        self.assertEqual(len(sets), 2, "наш набор создаётся отдельно")

    def test_manual_raw_collision_keeps_bytes(self) -> None:
        manual = self.manual("original.txt", b"my own transcript\n")

        ok, _ = self.run_ingest()

        self.assertTrue(ok)
        self.assertEqual(manual.read_bytes(), b"my own transcript\n")

    def test_manual_json_collision_keeps_bytes(self) -> None:
        manual = self.manual("source.json", b'{"url": "manual"}\n')

        ok, _ = self.run_ingest()

        self.assertTrue(ok)
        self.assertEqual(manual.read_bytes(), b'{"url": "manual"}\n')

    def test_partial_set_is_not_a_ready_source(self) -> None:
        folder = learn.source_dir(self.vault, self.source())
        folder.mkdir(parents=True)
        (folder / "source.json").write_text(
            json.dumps({"url": URL, "owner": "dusk", "snapshot": "x",
                        "segments": []}), encoding="utf-8")

        ok, _ = self.run_ingest()

        self.assertTrue(ok)
        saved = self.our_source_set()
        self.assertNotEqual(saved["dir"], folder, "неполный набор не считается готовым")

    def test_foreign_json_is_not_reused(self) -> None:
        folder = learn.source_dir(self.vault, self.source())
        folder.mkdir(parents=True)
        (folder / "source.json").write_text(
            json.dumps({"url": URL, "segments": [], "raw_name": "original.txt"}),
            encoding="utf-8")
        (folder / "original.txt").write_text("чужой текст", encoding="utf-8")
        (folder / "source.md").write_text("# чужое", encoding="utf-8")

        ok, _ = self.run_ingest()

        self.assertTrue(ok)
        self.assertNotEqual(self.our_source_set()["dir"], folder,
                            "набор без маркера владения не наш")

    def test_new_text_does_not_reuse_old_snapshot(self) -> None:
        """После сбоя публикации другой текст создаёт новый снимок, а не берёт старый."""
        source = self.source()
        original_publish = learn._publish

        def failing_note_publish(path, text):
            if learn.SOURCES_DIRNAME in str(path):
                return original_publish(path, text)
            raise OSError("диск полон")

        with patch.object(learn, "obtain_source", return_value=(source, "Fixture")), \
                patch.object(learn, "_ask_model", side_effect=fake_model()), \
                patch.object(learn, "CHUNK_CHARS", 220), \
                patch.object(learn, "_publish", side_effect=failing_note_publish):
            with self.assertRaises(OSError):
                learn.ingest_result(URL)
        first = self.our_source_set()
        first_digest = hashlib.sha256(first["json"].read_bytes()).hexdigest()

        other = ts.from_text(URL, "Совсем другой текст источника. " * 30, title="Другой")
        ok, message = self.run_ingest(source=other)

        self.assertTrue(ok, message)
        sets = learn.saved_source_sets(self.vault)
        self.assertEqual(len(sets), 2, "новый текст — новый снимок источника")
        self.assertEqual(hashlib.sha256(first["json"].read_bytes()).hexdigest(),
                         first_digest, "старый снимок не перезаписан")
        self.assertIn(str(self.our_source_set()["dir"].relative_to(self.vault)),
                      self.note_text(), "заметка ссылается на новый снимок")

    def test_repeat_reuses_the_same_snapshot(self) -> None:
        self.run_ingest()
        first = self.our_source_set()["dir"]
        self.write_queue(URL)
        ok, message = self.run_ingest()

        self.assertTrue(ok, message)
        self.assertEqual(self.our_source_set()["dir"], first)
        self.assertEqual(len(self.source_dirs()), 1)


class BacklinkTests(Stage2ABase):
    """Пункт 1-2: обратная ссылка — с путём от корня, и восстанавливается."""

    def our_source_set(self):
        return super().our_source_set()

    def test_backlink_uses_vault_root_path(self) -> None:
        ok, _ = self.run_ingest()
        self.assertTrue(ok)
        page = (self.our_source_set()["dir"] / "source.md").read_text(encoding="utf-8")
        note = self.our_notes()[-1]
        rel = note.relative_to(self.vault).with_suffix("").as_posix()
        self.assertIn(f"[[{rel}]]", page,
                      "ссылка на заметку должна быть путём от корня валта")

    def test_retry_repairs_backlink_after_interruption(self) -> None:
        source = self.source()
        with patch.object(learn, "obtain_source", return_value=(source, "Fixture")), \
                patch.object(learn, "_ask_model", side_effect=fake_model()):
            with patch.object(learn, "link_source_to_note",
                              side_effect=RuntimeError("crash")):
                try:
                    first_ok, _ = learn.ingest_result(URL, make_task=False)
                except RuntimeError:
                    first_ok = False
                self.assertFalse(first_ok, "обрыв публикации не может считаться успехом")
            ok, message = learn.ingest_result(URL, make_task=False)

        self.assertTrue(ok, message)
        page = (self.our_source_set()["dir"] / "source.md").read_text(encoding="utf-8")
        self.assertIn(self.our_notes()[-1].stem, page, "повтор обязан восстановить связь")

    def test_retry_fails_when_source_page_is_missing(self) -> None:
        ok, _ = self.run_ingest()
        self.assertTrue(ok)
        note = self.our_notes()[-1]
        (self.our_source_set()["dir"] / "source.md").unlink()

        self.write_queue(URL)
        with patch.object(learn, "obtain_source", side_effect=AssertionError("модель не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            report = learn.process_queue()

        self.assertIn(URL, self.queue_urls(),
                      "нельзя подтверждать успех без восстановимой связи с источником")
        self.assertIn("источник", report.lower())

    def test_stage1_note_without_transcript_still_ok(self) -> None:
        folder = self.vault / learn.HUB / AREA / f"{learn.NOTE_DIR_PREFIX} - авто конспект"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "Старый конспект.md").write_text(
            "---\nconnections:\n" + f"reference: {URL}\n" + "generator: dusk\n"
            "type: literature_note\n---\n\n# Старый конспект\n\n## Что применить\n\n- [ ] шаг\n"
            + f"\n---\n_Источник: {URL}_\n", encoding="utf-8")

        with patch.object(learn, "obtain_source", side_effect=AssertionError("сеть не нужна")), \
                patch.object(learn, "_ask_model", side_effect=AssertionError("модель не нужна")):
            ok, message = learn.ingest_result(URL)

        self.assertTrue(ok, "заметка этапа 1 без transcript остаётся совместимой")


class FragmentAnchorTests(Stage2ABase):
    """Пункт 3: ссылка на фрагмент ведёт на реальный заголовок."""

    def resolve(self, note_path, link: str):
        target, _, anchor = link.partition("#")
        page = (note_path.parent / target).resolve()
        if not page.is_file():
            return None, None
        return page, anchor

    def test_every_fragment_link_points_at_a_real_heading(self) -> None:
        ok, _ = self.run_ingest()
        self.assertTrue(ok)
        note = self.our_notes()[-1]
        text = note.read_text(encoding="utf-8")
        links = re.findall(r"\]\(<([^>]+)>\)", text)
        fragment_links = [link for link in links if "#seg-" in link]
        self.assertTrue(fragment_links, "у тезисов обязана быть ссылка на фрагмент")

        for link in set(fragment_links):
            page, anchor = self.resolve(note, link)
            self.assertIsNotNone(page, f"файл источника не найден по {link}")
            page_text = page.read_text(encoding="utf-8")
            heading = re.search(r"^#{1,6}\s+" + re.escape(anchor) + r"\s*$",
                                page_text, re.MULTILINE)
            self.assertIsNotNone(heading, f"нет заголовка {anchor} в {page.name}")

    def test_fragment_heading_exists_for_every_segment(self) -> None:
        source = self.source()
        page = ts.render_markdown(source)
        for segment in source.segments:
            self.assertIsNotNone(
                re.search(r"^#{1,6}\s+" + re.escape(segment.id) + r"\s*$",
                          page, re.MULTILINE),
                f"нет заголовка {segment.id}")


class ModelSchemaTests(Stage2ABase):
    """Пункт 4: явная схема ответа модели, а не «JSON распарсился»."""

    def run_with(self, model, expect_ok: bool):
        self.write_queue(URL)
        with patch.object(learn, "obtain_source", return_value=(self.source(), "Fixture")), \
                patch.object(learn, "_ask_model", side_effect=model), \
                patch.object(learn, "CHUNK_CHARS", 220), \
                patch.object(learn.vault_ops, "git_push", return_value=None):
            ok, message = learn.ingest_result(URL)
        self.assertEqual(ok, expect_ok, message)
        return message

    def chunk(self, payload) -> str:
        return "{}" if payload is None else json.dumps(payload)

    def test_empty_object_per_chunk_is_a_failure(self) -> None:
        message = self.run_with(lambda prompt: "{}", expect_ok=False)
        self.assertIn(URL, self.queue_urls(), "битая схема не подтверждает ссылку")
        self.assertEqual(self.our_notes(), [], "конспект не публикуется")
        self.assertIn("схем", message.lower())

    def test_missing_required_field_is_a_failure(self) -> None:
        def model(prompt):
            if is_synthesis(prompt):
                return json.dumps(summary())
            return json.dumps({"quotes": [], "translation": []})
        self.run_with(model, expect_ok=False)

    def test_wrong_collection_types_are_a_failure(self) -> None:
        def model(prompt):
            if is_synthesis(prompt):
                return json.dumps(summary())
            return json.dumps({"points": 123, "quotes": {}, "translation": None})
        self.run_with(model, expect_ok=False)

    def test_incomplete_summary_is_a_failure(self) -> None:
        def model(prompt):
            if is_synthesis(prompt):
                return json.dumps({"title": "Result"})
            return json.dumps({"points": [], "quotes": [], "translation": []})
        self.run_with(model, expect_ok=False)

    def test_foreign_segment_id_in_chunk_is_a_failure(self) -> None:
        def model(prompt):
            if is_synthesis(prompt):
                return json.dumps(summary())
            return json.dumps({"points": [{"text": "чужой фрагмент",
                                           "segments": ["seg-9999"]}],
                               "quotes": [], "translation": []})
        self.run_with(model, expect_ok=False)

    def test_empty_arrays_are_valid_for_a_chunk(self) -> None:
        def model(prompt):
            if is_synthesis(prompt):
                return json.dumps(summary())
            return json.dumps({"points": [], "quotes": [], "translation": []})
        self.run_with(model, expect_ok=True)

    def test_failure_reasons_are_reported(self) -> None:
        with patch.object(learn, "obtain_source", return_value=(self.source(), "Fixture")), \
                patch.object(learn, "_ask_model", side_effect=lambda prompt: "{}"), \
                patch.object(learn, "CHUNK_CHARS", 220):
            data = learn._distill("Fixture", ts.as_transcript(self.source()), [AREA])

        self.assertIn("error", data)
        self.assertTrue(data["coverage"]["failed"])
        self.assertIn("схем", data["coverage"]["failed"][0]["reason"].lower())


class TextIntegrityTests(unittest.TestCase):
    """Пункт 5: текст не портится, длинный сегмент не обходит лимит."""

    def test_separate_cues_keep_their_letters(self) -> None:
        raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\ngraphs\n\n"
               "00:00:02.000 --> 00:00:03.000\nsales matter to people\n")
        source = ts.from_vtt(URL, raw)
        self.assertEqual([item.text for item in source.segments],
                         ["graphs", "sales matter to people"])

    def test_real_rolling_overlap_is_still_deduped(self) -> None:
        raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:03.000\ntoday we talk about pricing\n\n"
               "00:00:03.000 --> 00:00:06.000\ntoday we talk about pricing\n"
               "and then measure it\n")
        source = ts.from_vtt(URL, raw)
        self.assertEqual([item.text for item in source.segments],
                         ["today we talk about pricing", "and then measure it"])

    def test_repeated_line_at_another_time_is_kept(self) -> None:
        raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nsame chorus line\n\n"
               "00:01:00.000 --> 00:01:03.000\nsame chorus line\n")
        source = ts.from_vtt(URL, raw)
        self.assertEqual([item.text for item in source.segments],
                         ["same chorus line", "same chorus line"],
                         "настоящий повтор на других таймкодах сохраняем")

    def test_long_segment_is_split_without_losing_characters(self) -> None:
        text = "word " * 10000
        source = ts.from_text(URL, text)
        parts = ts.chunks(source, limit=12000)
        self.assertLessEqual(max(len(part.text) for part in parts), 12000)
        joined = " ".join(segment.text for segment in source.segments)
        self.assertEqual(joined, " ".join(text.split()),
                         "разделение не теряет и не дублирует символы")

    def test_long_vtt_cue_is_split_and_keeps_ids_unique(self) -> None:
        raw = ("WEBVTT\n\n00:00:00.000 --> 00:10:00.000\n" + " ".join(["slovo"] * 3000) + "\n")
        source = ts.from_vtt(URL, raw)
        ids = [segment.id for segment in source.segments]
        self.assertGreater(len(ids), 1, "длинная реплика делится")
        self.assertEqual(len(ids), len(set(ids)), "ID не повторяются")
        self.assertEqual(" ".join(s.text for s in source.segments),
                         " ".join(["slovo"] * 3000))

    def test_split_segments_do_not_double_count_coverage(self) -> None:
        source = ts.Source(url=URL, segments=[
            ts.Segment("seg-0001", None, None, "A" * 30000)])
        parts = ts.chunks(source, limit=12000)
        covered = sum(learn._part_chars(source, part, source.text) for part in parts)
        self.assertEqual(covered, 30000, "сегмент в двух порциях считается один раз")


class AggregationTests(unittest.TestCase):
    """Пункт 6: сводка не теряет поздние тезисы, покрытие считается единообразно."""

    def test_coverage_counts_are_consistent(self) -> None:
        source = ts.Source(url=URL, segments=[
            ts.Segment("seg-0001", None, None, "A"),
            ts.Segment("seg-0002", None, None, "B"),
            ts.Segment("seg-0003", None, None, "C")])

        def model(prompt):
            if "СВОДКА" in prompt:
                return json.dumps(summary())
            return json.dumps({"points": [], "quotes": [], "translation": []})

        with patch.object(learn, "_owner_context", return_value=""), \
                patch.object(learn, "_ask_model", side_effect=model):
            data = learn._distill("Fixture", ts.as_transcript(source), [AREA])

        self.assertNotIn("error", data)
        self.assertEqual(data["coverage"]["chars_covered"], data["coverage"]["chars"])
        self.assertEqual(data["coverage"]["percent"], 100)

    def test_late_point_participates_in_summary(self) -> None:
        # Тезисы длинные: иначе 91 короткий тезис укладывается в один запрос
        # сводки, и проверка «поздний тезис участвует при агрегации» теряет смысл.
        segments = [ts.Segment(f"seg-{index + 1:04d}", None, None,
                               f"Meaningful isolated source segment number {index:02d}. " * 25)
                    for index in range(91)]
        source = ts.Source(url=URL, segments=segments)
        synthesis_inputs = []

        def model(prompt):
            if "СВОДКА" in prompt:
                synthesis_inputs.append(prompt)
                return json.dumps(summary())
            ids = re.findall(r"\[(seg-\d+)", prompt)
            return json.dumps({"points": [{"text": ("UNIQUE LATE MIDDLE MARKER"
                                                    if sid == "seg-0046"
                                                    else f"Point from {sid}: preserved fact"),
                                           "segments": [sid]} for sid in ids],
                               "quotes": [], "translation": []})

        with patch.object(learn, "_owner_context", return_value=""), \
                patch.object(learn, "CHUNK_CHARS", 65), \
                patch.object(learn, "_ask_model", side_effect=model):
            data = learn._distill("Fixture", ts.as_transcript(source), [AREA])

        self.assertNotIn("error", data)
        self.assertIn("UNIQUE LATE MIDDLE MARKER", "\n".join(synthesis_inputs),
                      "поздний тезис обязан участвовать в сводке")
        self.assertGreater(len(synthesis_inputs), 1, "большой объём агрегируется порциями")

    def test_each_summary_prompt_is_bounded(self) -> None:
        segments = [ts.Segment(f"seg-{index + 1:04d}", None, None,
                               f"Isolated source segment number {index:02d}.")
                    for index in range(120)]
        source = ts.Source(url=URL, segments=segments)
        sizes = []

        def model(prompt):
            if "СВОДКА" in prompt:
                sizes.append(len(prompt))
                return json.dumps(summary())
            ids = re.findall(r"\[(seg-\d+)", prompt)
            return json.dumps({"points": [{"text": f"point {sid}", "segments": [sid]}
                                          for sid in ids],
                               "quotes": [], "translation": []})

        with patch.object(learn, "_owner_context", return_value=""), \
                patch.object(learn, "CHUNK_CHARS", 65), \
                patch.object(learn, "_ask_model", side_effect=model):
            learn._distill("Fixture", ts.as_transcript(source), [AREA])

        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 8000,
                             "сводка не превращается в один неограниченный промпт")

    def test_failed_aggregation_does_not_confirm(self) -> None:
        segments = [ts.Segment(f"seg-{index + 1:04d}", None, None,
                               f"Isolated source segment number {index:02d}.")
                    for index in range(60)]
        source = ts.Source(url=URL, segments=segments)

        def model(prompt):
            if "СВОДКА" in prompt:
                raise RuntimeError("сводка недоступна")
            ids = re.findall(r"\[(seg-\d+)", prompt)
            return json.dumps({"points": [{"text": f"point {sid}", "segments": [sid]}
                                          for sid in ids],
                               "quotes": [], "translation": []})

        with patch.object(learn, "_owner_context", return_value=""), \
                patch.object(learn, "CHUNK_CHARS", 65), \
                patch.object(learn, "_ask_model", side_effect=model):
            data = learn._distill("Fixture", ts.as_transcript(source), [AREA])

        self.assertIn("error", data)


class QueueLevelTests(Stage2ABase):
    """Сквозная проверка: при нарушении схемы очередь не подтверждается."""

    def test_schema_violation_keeps_url_in_queue(self) -> None:
        self.write_queue(URL)
        with patch.object(learn, "obtain_source", return_value=(self.source(), "Fixture")), \
                patch.object(learn, "_ask_model", side_effect=lambda prompt: "{}"), \
                patch.object(learn, "CHUNK_CHARS", 220):
            report = learn.process_queue()

        self.assertIn(URL, self.queue_urls(), "битая схема не подтверждает ссылку")
        self.assertIn("Не вышло", report)
        self.assertEqual(self.our_notes(), [])

    def test_empty_summary_keeps_url_in_queue(self) -> None:
        self.write_queue(URL)

        def model(prompt):
            if is_synthesis(prompt):
                return "{}"
            ids = segments_in_prompt(prompt)
            return json.dumps({"points": [{"text": "тезис", "segments": ids[:1]}],
                               "quotes": [], "translation": []})
        with patch.object(learn, "obtain_source", return_value=(self.source(), "Fixture")), \
                patch.object(learn, "_ask_model", side_effect=model), \
                patch.object(learn, "CHUNK_CHARS", 220):
            learn.process_queue()

        self.assertIn(URL, self.queue_urls(), "пустая сводка не подтверждает ссылку")
        self.assertEqual(self.our_notes(), [])


if __name__ == "__main__":
    unittest.main()
