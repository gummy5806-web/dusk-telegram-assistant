#!/usr/bin/env python3
"""Этап 2А: сохранённый источник, сегменты, порции и проверка цитат.

Все данные синтетические, сети и модели здесь нет.
"""

from __future__ import annotations

import json
import unittest

import transcript_source as ts

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"

ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:03.000
Welcome to the show

00:00:03.000 --> 00:00:06.000
Welcome to the show
today we talk about pricing

00:00:06.000 --> 00:00:09.000
today we talk about pricing
the first step is to write down

00:00:09.000 --> 00:00:12.000
the first step is to write down
UNIQUE MIDDLE MARKER 42

00:00:12.000 --> 00:00:15.000
UNIQUE MIDDLE MARKER 42
and then measure it

00:00:15.000 --> 00:00:18.000
and then measure it
that is the whole loop
"""

NO_TIME_VTT = """WEBVTT

Просто текст без таймкодов.
Вторая строка.
"""


class VideoIdTests(unittest.TestCase):
    def test_known_forms(self) -> None:
        for url in ("https://www.youtube.com/watch?v=aaaaaaaaaaa",
                    "https://youtu.be/aaaaaaaaaaa",
                    "https://www.youtube.com/shorts/aaaaaaaaaaa",
                    "https://m.youtube.com/watch?v=aaaaaaaaaaa&t=30s"):
            with self.subTest(url=url):
                self.assertEqual(ts.video_id(url), "aaaaaaaaaaa")

    def test_unknown_form_is_empty_not_invented(self) -> None:
        for url in ("https://example.com/video", "", "не ссылка"):
            with self.subTest(url=url):
                self.assertEqual(ts.video_id(url), "")


class ParseVttTests(unittest.TestCase):
    def test_segments_have_stable_ids_and_timecodes(self) -> None:
        source = ts.from_vtt(URL, ROLLING_VTT, language="en", title="Разбор")
        ids = [s.id for s in source.segments]
        self.assertEqual(ids, ["seg-0001", "seg-0002", "seg-0003",
                               "seg-0004", "seg-0005", "seg-0006"])
        self.assertEqual(source.stamp_of("seg-0002"), "00:00:03")
        self.assertEqual(source.start_of("seg-0002"), 3.0)

    def test_rolling_repeats_are_not_duplicated(self) -> None:
        source = ts.from_vtt(URL, ROLLING_VTT, language="en")
        text = source.text
        self.assertEqual(text.count("Welcome to the show"), 1)
        self.assertEqual(text.count("today we talk about pricing"), 1)
        self.assertEqual(text.count("UNIQUE MIDDLE MARKER 42"), 1)

    def test_unique_middle_thought_keeps_its_segment(self) -> None:
        source = ts.from_vtt(URL, ROLLING_VTT, language="en")
        found = ts.segments_of_text(source, "UNIQUE MIDDLE MARKER 42")
        self.assertEqual(found, ["seg-0004"])

    def test_metadata_is_kept_and_unknown_marked(self) -> None:
        source = ts.from_vtt(URL, ROLLING_VTT, language="en", title="Разбор")
        self.assertEqual(source.video_id, "aaaaaaaaaaa")
        self.assertEqual(source.language, "en")
        self.assertEqual(source.author, "", "автора не выдумываем")
        self.assertTrue(source.fetched_at, "время получения фиксируется")

    def test_missing_timecodes_are_marked_not_invented(self) -> None:
        source = ts.from_vtt(URL, NO_TIME_VTT, language="ru")
        self.assertTrue(source.segments)
        self.assertIsNone(source.segments[0].start)
        self.assertIsNone(source.stamp_of(source.segments[0].id))
        self.assertTrue(all(s.start is None for s in source.segments))

    def test_plain_text_source_has_no_timecodes(self) -> None:
        source = ts.from_text(URL, "Первый абзац. " * 40, title="Разбор")
        self.assertTrue(source.segments)
        self.assertTrue(all(s.start is None for s in source.segments))
        self.assertIn("Первый абзац.", source.text)


class ChunkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = ts.from_vtt(URL, ROLLING_VTT, language="en")

    def test_chunks_cover_every_segment_in_order(self) -> None:
        chunks = ts.chunks(self.source, limit=40)
        self.assertGreater(len(chunks), 1, "маленький лимит обязан дать несколько порций")
        seen = [sid for chunk in chunks for sid in chunk.segment_ids]
        # Фрагмент длиннее лимита делится между порциями, поэтому проверяем
        # порядок первого появления и полноту, а не уникальность в списке.
        first_seen = list(dict.fromkeys(seen))
        self.assertEqual(first_seen, [s.id for s in self.source.segments])
        self.assertEqual(set(seen), {s.id for s in self.source.segments},
                         "ни один фрагмент не потерян")
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 40, "порция не превышает лимит")

    def test_chunk_ids_are_stable(self) -> None:
        first = [c.id for c in ts.chunks(self.source, limit=40)]
        second = [c.id for c in ts.chunks(self.source, limit=40)]
        self.assertEqual(first, second)
        self.assertEqual(first[0], "chunk-001")

    def test_single_chunk_when_it_fits(self) -> None:
        chunks = ts.chunks(self.source, limit=10000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0].segment_ids), len(self.source.segments))


class QuoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = ts.from_vtt(URL, ROLLING_VTT, language="en")

    def test_exact_quote_is_confirmed_with_segment(self) -> None:
        result = ts.find_quote(self.source, "the first step is to write down")
        self.assertTrue(result.found)
        self.assertEqual(result.segment_id, "seg-0003")
        self.assertEqual(result.matched, "the first step is to write down")

    def test_whitespace_normalisation_is_allowed(self) -> None:
        result = ts.find_quote(self.source, "  the first step   is to write down\n")
        self.assertTrue(result.found, "сжатие пробелов — единственная допустимая нормализация")

    def test_fabricated_quote_is_not_confirmed(self) -> None:
        result = ts.find_quote(self.source, "автор обещал удвоить выручку за неделю")
        self.assertFalse(result.found)
        self.assertIsNone(result.segment_id)

    def test_too_short_quote_is_not_proof(self) -> None:
        result = ts.find_quote(self.source, "the")
        self.assertFalse(result.found, "короткая строка не доказывает цитату")

    def test_quotes_and_case_folding_are_not_silently_allowed(self) -> None:
        # кавычки по краям снимаем, но слова не переписываем
        with_quotes = ts.find_quote(self.source, "«the first step is to write down»")
        self.assertTrue(with_quotes.found)
        changed = ts.find_quote(self.source, "The First Step Is To Write Down")
        self.assertFalse(changed.found, "регистр не подменяем: это уже не дословно")


class SerialisationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = ts.from_vtt(URL, ROLLING_VTT, language="en", title="Разбор")

    def test_json_has_source_identity_and_segments(self) -> None:
        data = json.loads(json.dumps(ts.to_json(self.source), ensure_ascii=False))
        self.assertEqual(data["url"], URL)
        self.assertEqual(data["video_id"], "aaaaaaaaaaa")
        self.assertEqual(data["language"], "en")
        self.assertEqual(len(data["segments"]), 6)
        self.assertEqual(data["segments"][1]["id"], "seg-0002")
        self.assertEqual(data["segments"][1]["start"], 3.0)
        self.assertIn("text", data["segments"][1])

    def test_markdown_renders_timecodes_and_marks_unknowns(self) -> None:
        page = ts.render_markdown(self.source)
        self.assertIn(URL, page)
        self.assertIn("aaaaaaaaaaa", page)
        self.assertIn("00:00:03", page)
        self.assertIn("seg-0002", page)
        self.assertIn("не известен", page.lower(), "неизвестные поля помечаются")

    def test_markdown_marks_missing_timecodes(self) -> None:
        source = ts.from_vtt(URL, NO_TIME_VTT, language="ru")
        page = ts.render_markdown(source)
        self.assertIn("таймкод недоступен", page.lower())


if __name__ == "__main__":
    unittest.main()
