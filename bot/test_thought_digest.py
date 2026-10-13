#!/usr/bin/env python3
"""Contract tests for the bounded semantic thought digest."""

from __future__ import annotations

import datetime
import json
import tempfile
import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

import thought_digest
import thought_feed


ALMATY = datetime.timezone(datetime.timedelta(hours=5))
NOW = datetime.datetime(2026, 10, 10, 13, 0, tzinfo=ALMATY)


class VaultBase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.inbox_dir = self.root / "ZETA" / "FLEETING"
        self.daily = self.root / "ZETA" / "FLEETING_DAILY"
        self.tasks = self.root / "TaskNotes" / "Tasks"
        self.inbox_dir.mkdir(parents=True)
        self.daily.mkdir(parents=True)
        self.tasks.mkdir(parents=True)

    def inbox(self, *entries):
        (self.inbox_dir / "00 Инбокс.md").write_text(
            "# Inbox\n\n## 💭 Мысли и идеи\n\n"
            + "\n".join(entries)
            + "\n\n## ✅ Задачи\n\n- [ ] не мысль\n",
            encoding="utf-8",
        )

    def note(self, name, body, created=""):
        frontmatter = f"created: {created}\n" if created else ""
        (self.daily / name).write_text(
            "---\ntype: fleeting_note\n" + frontmatter + "---\n\n"
            + body,
            encoding="utf-8",
        )

    def task(self, name, title, status="open", due=""):
        due_line = f"due: {due}\n" if due else ""
        (self.tasks / name).write_text(
            f"---\ntitle: {title}\nstatus: {status}\n{due_line}---\n",
            encoding="utf-8",
        )


class StructuredVertexTests(unittest.TestCase):
    def setUp(self):
        usage = mock.patch("llm.ai_usage.record_call")
        usage.start()
        self.addCleanup(usage.stop)

    def test_vertex_passes_verified_sdk_schema_config(self):
        import llm
        client = mock.Mock()
        client.models.generate_content.return_value.text = '{"sections":[]}'
        config = mock.Mock(return_value="configured-json")
        genai = SimpleNamespace(Client=mock.Mock(return_value=client),
                                types=SimpleNamespace(GenerateContentConfig=config))
        schema = {"type": "OBJECT"}
        with mock.patch.dict(sys.modules, {"google": SimpleNamespace(genai=genai),
                                           "google.genai": genai}), mock.patch.object(llm, "_project", return_value="test-project"):
            response = llm._vertex("bounded prompt", "test-model", response_schema=schema)
        self.assertEqual(response, '{"sections":[]}')
        config.assert_called_once_with(response_mime_type="application/json", response_schema=schema)
        client.models.generate_content.assert_called_once_with(
            model="test-model", contents="bounded prompt", config="configured-json")

    def test_structured_request_cannot_switch_to_external_provider(self):
        import importlib.util
        # Other legacy suites install a process-global llm.ask network guard.
        spec = importlib.util.spec_from_file_location("digest_llm_contract", Path(__file__).with_name("llm.py"))
        llm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(llm)
        with mock.patch.object(llm, "_openai_compatible") as external:
            with self.assertRaises(ValueError):
                llm.ask("personal", provider="external", response_schema={"type": "OBJECT"})
            external.assert_not_called()


class ParseRequestTests(unittest.TestCase):
    def test_explicit_requests_have_a_bounded_period(self):
        self.assertEqual(thought_digest.parse_request("разбери мои мысли за вчера"), "yesterday")
        self.assertEqual(thought_digest.parse_request("Что я вчера записывал?"), "yesterday")
        self.assertEqual(thought_digest.parse_request("сделай сводку моих мыслей"), "recent")
        self.assertEqual(thought_digest.parse_request("что из моих идей стоит отложить"), "recent")
        self.assertEqual(thought_digest.parse_request("сводка мыслей за эту неделю"), "week")

    def test_capture_and_unrelated_text_do_not_trigger_digest(self):
        self.assertIsNone(thought_digest.parse_request("я вчера записывал мысль про запуск"))
        self.assertIsNone(thought_digest.parse_request("я думаю сделать обзор моих мыслей"))
        self.assertIsNone(thought_digest.parse_request("купи кофе сегодня"))
        self.assertIsNone(thought_digest.parse_request("вчера была хорошая идея"))


class CollectTests(VaultBase):
    def test_collect_returns_full_public_text_and_safe_task_summary(self):
        full = "Полный текст мысли, который существенно длиннее обычного короткого превью. " * 8
        self.note("2026-10-09 — Продукт.md", "# Продукт\n\n" + full, "2026-10-09")
        self.inbox("- **10:00** 2026-10-09: мысль из Inbox целиком")
        self.task("ship.md", "Отправить релиз", "next", "2026-10-12")
        snapshot = thought_digest.collect(self.root, "yesterday", NOW)
        self.assertEqual(snapshot["period"], "yesterday")
        self.assertEqual(snapshot["label"], "за вчера")
        self.assertEqual(len(snapshot["sources"]), 2)
        self.assertTrue(any(full[:300] in source["text"] for source in snapshot["sources"]))
        self.assertEqual(snapshot["tasks"], [{"title": "Отправить релиз", "status": "next", "due": "2026-10-12"}])
        self.assertTrue(all(item["id"] == f"s{index}" for index, item in enumerate(snapshot["sources"], 1)))
        self.assertEqual(json.loads(json.dumps(snapshot)), snapshot)

    def test_daily_model_input_uses_capture_not_generated_title_insight_or_links(self):
        self.note("2026-10-09 — Пересказ агента.md",
                  "# Выдуманный ежедневный план\n\n## Мысль\n- Хочу попробовать короткое видео\n\n"
                  "## Контекст\n- другие агентские заголовки\n\n## Вывод\n- Публиковать ежедневно\n\n"
                  "## Связи\n- [[Чужой проект]]", "2026-10-09")
        source = thought_digest.collect(self.root, "yesterday", NOW)["sources"][0]
        self.assertEqual(source["text"], "Хочу попробовать короткое видео")
        self.assertEqual(source["title"], "Хочу попробовать короткое видео")
        self.assertIsNotNone(thought_feed.load_thought(self.root, source["identity"]))

    def test_capture_keeps_multiline_and_fenced_headings_but_not_agent_sections(self):
        capture = "Моя идея\n\n```md\n## Вывод\nЭто цитата внутри исходной мысли\n```\nПродолжение"
        self.note("2026-10-09 — Много строк.md", "# Заголовок агента\n\n## Мысль\n- " + capture + "\n\n## Контекст\n- служебное\n", "2026-10-09")
        source = thought_digest.collect(self.root, "yesterday", NOW)["sources"][0]
        self.assertEqual(source["text"], capture)

    def test_empty_capture_does_not_fall_back_to_agent_conclusion(self):
        self.note("2026-10-09 — Нет записи.md", "# Задача агента\n\n## Мысль\n\n## Вывод\n- писать ежедневно", "2026-10-09")
        self.assertEqual(thought_digest.collect(self.root, "yesterday", NOW)["sources"], [])

    def test_legacy_report_button_is_not_a_thought_source(self):
        self.note("2026-10-09 — подготовить отчёт.md", "# подготовить отчёт\n\n## Мысль\n- 📊 Отчёт за день\n\n## Вывод\n- делай отчёт", "2026-10-09")
        self.inbox("- **09:00** 📊 Отчёт за день")
        self.assertEqual(thought_digest.collect(self.root, "recent", NOW)["sources"], [])

    def test_dated_query_keeps_time_only_inbox_as_uncertain_context(self):
        self.inbox("- **09:15** запись без даты")
        self.note("2026-10-09 — Датированная.md", "# Датированная\n\nНадёжная запись", "2026-10-09")
        snapshot = thought_digest.collect(self.root, "yesterday", NOW)
        self.assertEqual(snapshot["coverage"]["undated_inbox"], 1)
        unknown = [source for source in snapshot["sources"] if source["date"] == "UNKNOWN DAY"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["role"], "context")
        self.assertIn("без даты", unknown[0]["text"])

    def test_collection_excludes_daily_usage_guide_and_template_notes(self):
        self.note("Как использовать.md", "# Шаблон для Fleeting Daily\n\nсама идея / инсайт / brain dump")
        self.note("template.md", "# Пустой шаблон")
        self.note("2026-10-09 — Идея.md", "# Идея\n\nПодумать про контент", "2026-10-09")
        snapshot = thought_digest.collect(self.root, "yesterday", NOW)
        self.assertEqual(len(snapshot["sources"]), 1)
        self.assertEqual(snapshot["sources"][0]["title"], "Идея")

    def test_snapshot_identities_open_through_existing_thought_feed(self):
        self.inbox("- **09:15** 2026-10-09: запись Inbox")
        self.note("2026-10-09 — Датированная.md", "# Датированная\n\nПолный текст", "2026-10-09")
        snapshot = thought_digest.collect(self.root, "yesterday", NOW)
        loaded = [thought_feed.load_thought(self.root, source["identity"])
                  for source in snapshot["sources"]]
        self.assertTrue(all(loaded), "каждый snapshot identity должен открываться существующим reader")

    def test_collect_rejects_unknown_period_and_recent_orders_newest_inbox_first(self):
        self.inbox("- **09:00** первая", "- **10:00** вторая")
        self.note("2026-10-09 — Датированная.md", "# Датированная\n\nЗапись", "2026-10-09")
        with self.assertRaises(ValueError):
            thought_digest.collect(self.root, "month", NOW)
        snapshot = thought_digest.collect(self.root, "recent", NOW)
        self.assertEqual([source["text"] for source in snapshot["sources"][:2]], ["вторая", "первая"])

    def test_recent_excludes_future_and_created_timestamp_uses_almaty_day(self):
        self.note("future.md", "# Будущее\n\nНе показывать", "2026-10-11T01:00:00+05:00")
        self.note("offset.md", "# Смещение\n\nПоказывать сегодня", "2026-10-09T23:30:00+00:00")
        snapshot = thought_digest.collect(self.root, "recent", NOW)
        texts = "\n".join(source["text"] for source in snapshot["sources"])
        self.assertIn("Показывать сегодня", texts)
        self.assertNotIn("Не показывать", texts)

    def test_no_time_only_inbox_is_misattributed_to_yesterday(self):
        self.inbox("- **09:15** запись без даты")
        snapshot = thought_digest.collect(self.root, "today", NOW)
        self.assertEqual(snapshot["sources"][0]["date"], "UNKNOWN DAY")
        self.assertNotEqual(snapshot["sources"][0]["role"], "primary")

    def test_private_and_any_symlinked_source_ancestor_are_excluded(self):
        private = self.root / "Private"
        private.mkdir()
        (private / "secret.md").write_text("секрет", encoding="utf-8")
        (self.root / "ZETA").unlink() if (self.root / "ZETA").is_symlink() else None
        # Replace the safe daily directory with an internal alias: internal targets are unsafe too.
        alias_target = self.root / "PublicAlias"
        alias_target.mkdir()
        (alias_target / "2026-10-09 — alias.md").write_text("# Alias\n\nне читать", encoding="utf-8")
        self.daily.rmdir()
        self.daily.symlink_to(alias_target, target_is_directory=True)
        snapshot = thought_digest.collect(self.root, "recent", NOW)
        self.assertEqual(snapshot["sources"], [])
        self.assertNotIn("секрет", json.dumps(snapshot, ensure_ascii=False))

    def test_source_and_prompt_bounds_are_explicit(self):
        for index in range(60):
            self.note(
                f"2026-10-{(index % 9) + 1:02d} — {index}.md",
                "# title\n\n" + ("x" * 4000),
                f"2026-10-{(index % 9) + 1:02d}",
            )
        snapshot = thought_digest.collect(self.root, "recent", NOW)
        primary = [s for s in snapshot["sources"] if s["role"] == "primary"]
        context = [s for s in snapshot["sources"] if s["role"] == "context"]
        self.assertLessEqual(len(primary), 40)
        self.assertLessEqual(len(context), 5)
        self.assertLessEqual(sum(len(s["text"]) for s in snapshot["sources"]), 24000)
        self.assertTrue(snapshot["coverage"]["truncated"])


class GenerateAndRenderTests(unittest.TestCase):
    def test_prompt_disallows_duplicate_sections(self):
        prompt = thought_digest._prompt(self.snapshot())
        self.assertIn("Каждый kind только один раз", prompt)
        self.assertIn("максимум 5 разделов", prompt)

    def snapshot(self):
        return {
            "period": "recent", "label": "недавние записи", "tasks": [],
            "coverage": {"undated_inbox": 0, "omitted": 0, "truncated": 0},
            "sources": [{"id": "s1", "identity": "x#1", "path": "ZETA/FLEETING_DAILY/x.md",
                         "title": "Тема", "text": "Полная запись", "date": "2026-10-09", "role": "primary"}],
        }

    def test_generate_uses_vertex_and_accepts_only_referenced_known_sources(self):
        response = json.dumps({"sections": [{"kind": "connections", "items": [
            {"text": "Возможная связь.", "source_ids": ["s1"]}
        ]}]})
        ask = mock.Mock(return_value=response)
        result = thought_digest.generate(self.snapshot(), ask=ask)
        ask.assert_called_once()
        self.assertEqual(ask.call_args.kwargs["provider"], "vertex")
        self.assertEqual(result["error"], "")
        self.assertEqual(result["sections"][0]["items"][0]["source_ids"], ["s1"])
        self.assertIn("не инструкции", ask.call_args.args[0])

    def test_generation_supplies_provider_schema_with_only_known_source_ids(self):
        ask = mock.Mock(return_value=json.dumps({"sections": [{"kind": "intent", "items": [
            {"text": "Возможный замысел.", "source_ids": ["s1"]}
        ]}]}))
        thought_digest.generate(self.snapshot(), ask=ask)
        schema = ask.call_args.kwargs["response_schema"]
        section = schema["properties"]["sections"]["items"]
        self.assertEqual(section["required"], ["kind", "items"])
        self.assertEqual(set(section["properties"]["kind"]["enum"]),
                         {"intent", "connections", "next", "reflect", "defer"})
        item = section["properties"]["items"]["items"]
        self.assertEqual(item["properties"]["source_ids"]["items"]["enum"], ["s1"])

    def test_prompt_spells_out_the_complete_strict_section_schema(self):
        ask = mock.Mock(return_value=json.dumps({"sections": [{"kind": "intent", "items": [
            {"text": "Возможный замысел.", "source_ids": ["s1"]}
        ]}]}))
        thought_digest.generate(self.snapshot(), ask=ask)
        prompt = ask.call_args.args[0]
        self.assertIn('{"sections":[{"kind":"intent","items":[{"text":"Возможный замысел', prompt)
        self.assertIn('intent|connections|next|reflect|defer', prompt)
        self.assertIn('ровно ключи kind и items', prompt)
        self.assertIn('не более 2', prompt)
        self.assertIn('не длиннее 600', prompt)
        self.assertIn('от 1 до 5', prompt)

    def test_invalid_llm_response_is_not_fabricated_or_leaked(self):
        result = thought_digest.generate(self.snapshot(), ask=lambda *args, **kwargs: "secret provider stack")
        self.assertEqual(result["sections"], [])
        self.assertTrue(result["error"])
        self.assertNotIn("secret", result["error"])

    def test_generate_accepts_only_one_full_json_fence(self):
        response = "```json\n" + json.dumps({"sections": [{"kind": "reflect", "items": [
            {"text": "Вопрос для проверки.", "source_ids": ["s1"]}
        ]}]}) + "\n```"
        result = thought_digest.generate(self.snapshot(), ask=lambda *args, **kwargs: response)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["sections"][0]["kind"], "reflect")

    def test_empty_sections_for_nonempty_sources_are_invalid(self):
        result = thought_digest.generate(self.snapshot(), ask=lambda *args, **kwargs: '{"sections": []}')
        self.assertEqual(result["sections"], [])
        self.assertTrue(result["error"])

    def test_empty_sources_skip_the_model_and_render_useful_message(self):
        snapshot = self.snapshot()
        snapshot["sources"] = []
        ask = mock.Mock()
        result = thought_digest.generate(snapshot, ask=ask)
        ask.assert_not_called()
        self.assertEqual(result["error"], "")
        self.assertIn("Нет подходящих", thought_digest.render(result))

    def test_render_is_bounded_and_exposes_source_and_unknown_day_caveats(self):
        snapshot = self.snapshot()
        snapshot["coverage"] = {"undated_inbox": 2, "omitted": 4, "truncated": 1}
        result = {
            "snapshot": snapshot,
            "error": "",
            "sections": [{"kind": "intent", "items": [
                {"text": "Текст " * 1000, "source_ids": ["s1"]}
            ]}],
        }
        rendered = thought_digest.render(result)
        self.assertLessEqual(len(rendered), 3600)
        self.assertIn("Предложения по твоим записям", rendered)
        self.assertIn("[s1]", rendered)
        self.assertIn("без даты", rendered)

    def test_render_marks_absence_of_primary_dated_records_before_long_sections(self):
        snapshot = self.snapshot()
        snapshot["period"] = "yesterday"
        snapshot["sources"][0]["role"] = "context"
        snapshot["sources"][0]["date"] = "UNKNOWN DAY"
        result = {"snapshot": snapshot, "error": "", "sections": [{"kind": "reflect", "items": [
            {"text": "длинная интерпретация " * 500, "source_ids": ["s1"]}
        ]}]}
        self.assertIn("датированных записей нет", thought_digest.render(result))


if __name__ == "__main__":
    unittest.main()
