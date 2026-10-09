#!/usr/bin/env python3
"""Dusk-first assistant router: чистые естественные интенты без грамматики.

Проверяется только `assistant_router`: он не знает про Telegram, сеть и файлы.
Модуль либо возвращает сфокусированный subject для Dusk-поиска, либо задачу,
либо безопасный capture. Никаких магических разборов русского ответа.
"""

from __future__ import annotations

import dataclasses
import unittest

import assistant_router as ar


class RouteShapeTests(unittest.TestCase):
    def test_route_is_immutable_structured_result(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(ar.Route))
        self.assertTrue(ar.Route.__dataclass_params__.frozen,
                        "Route должен быть неизменяемым")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ar.Route(kind=ar.KIND_CAPTURE).kind = "task"  # type: ignore[misc]

    def test_empty_input_is_safe_capture(self) -> None:
        route = ar.route("   ")
        self.assertEqual(route.kind, ar.KIND_CAPTURE)
        self.assertEqual(route.subject, "")
        self.assertFalse(route.research)

    def test_ambiguous_plain_text_is_capture_not_task(self) -> None:
        route = ar.route("сегодня хорошая погода и немного ветра")
        self.assertEqual(route.kind, ar.KIND_CAPTURE)
        self.assertFalse(route.research)
        self.assertFalse(route.personal)


class PersonalRecallTests(unittest.TestCase):
    def _personal(self, text: str, subject: str) -> None:
        route = ar.route(text)
        self.assertEqual(route.kind, ar.KIND_PERSONAL_SEARCH, text)
        self.assertEqual(route.subject, subject, text)
        self.assertTrue(route.personal, text)
        self.assertFalse(route.research, text)

    def test_thought_about_audit(self) -> None:
        self._personal("что я думал про аудит", "аудит")

    def test_remember_offer_idea(self) -> None:
        self._personal("помнишь идею про оффер", "оффер")

    def test_where_is_my_note_about_agents(self) -> None:
        self._personal("где моя заметка про агентов", "агентов")

    def test_find_what_i_wrote_about_sales(self) -> None:
        self._personal("найди что я писал про продажи", "продажи")

    def test_find_my_note_about_agents(self) -> None:
        self._personal("найди мою заметку про агентов", "агентов")

    def test_personal_wording_suppresses_research_even_with_author(self) -> None:
        route = ar.route("что я думал про Хормози")
        self.assertEqual(route.kind, ar.KIND_PERSONAL_SEARCH)
        self.assertFalse(route.research, "личная формулировка не идёт в Research Brain")
        self.assertEqual(route.subject, "Хормози")

    def test_where_is_the_thought_about_offer(self) -> None:
        self._personal("где мысль про оффер", "оффер")

    def test_find_that_thought_about_offer(self) -> None:
        self._personal("найди ту мысль про оффер", "оффер")

    def test_find_idea_about_offer(self) -> None:
        self._personal("найди идею про оффер", "оффер")

    def test_wrote_a_thought_yesterday(self) -> None:
        self._personal("я вчера записал мысль про оффер", "оффер")

    def test_help_find_my_note_about_offer(self) -> None:
        self._personal("помоги найти мою запись про оффер", "оффер")

    def test_find_my_note_about_term_keeps_personal(self) -> None:
        route = ar.route("найди мою заметку про термин agent loop")
        self.assertEqual(route.kind, ar.KIND_PERSONAL_SEARCH)
        self.assertTrue(route.personal)
        self.assertFalse(route.research,
                         "личный поиск подавляет Research, даже если есть «термин»")

    def test_statement_about_a_thought_stays_capture(self) -> None:
        route = ar.route("у меня появилась мысль про оффер")
        self.assertEqual(route.kind, ar.KIND_CAPTURE,
                         "утверждение — не поиск, а безопасный capture")

    def test_where_my_project(self) -> None:
        self._personal("где мой проект запуск", "запуск")

    def test_where_my_resource(self) -> None:
        self._personal("где мой ресурс по маркетингу", "маркетингу")

    def test_find_my_project(self) -> None:
        self._personal("найди мой проект запуск", "запуск")


class AmbiguousStatementTests(unittest.TestCase):
    """Одиночные слова/глаголы в утверждении не включают поиск и Research."""

    CAPTURES = (
        "моя идея сделать новую воронку",
        "моя задача сегодня купить кабель",
        "у меня был созвон с клиентом",
        "друг советовал купить кабель",
        "я советовал другу купить кабель",
        "ты рассказывал про отпуск",
        "я посмотрел материал по маркетингу",
        "изучил термин agent loop",
        "Хормози советовал чаще публиковаться",
        "что друг советовал купить кабель",
        "что мама рассказывала про отпуск",
        "что я советовал клиенту",
        "мой проект запуск",
        "мой ресурс по маркетингу",
    )

    def test_plain_statements_are_capture_without_research(self) -> None:
        for text in self.CAPTURES:
            with self.subTest(text=text):
                route = ar.route(text)
                self.assertEqual(route.kind, ar.KIND_CAPTURE, text)
                self.assertFalse(route.research, text)


class ExplicitKnowledgeTests(unittest.TestCase):
    """Только семантически явный запрос об изученном включает Research."""

    def _knowledge(self, text: str, subject: str) -> None:
        route = ar.route(text)
        self.assertEqual(route.kind, ar.KIND_KNOWLEDGE_SEARCH, text)
        self.assertTrue(route.research, text)
        self.assertTrue(route.personal, text)
        self.assertEqual(route.subject, subject, text)

    def test_who_is_author(self) -> None:
        self._knowledge("кто такой Карпати", "Карпати")

    def test_what_is_concept(self) -> None:
        self._knowledge("что такое retrieval augmented generation",
                        "retrieval augmented generation")

    def test_what_means_abbreviation(self) -> None:
        self._knowledge("что означает LTV", "LTV")

    def test_explain_concept(self) -> None:
        self._knowledge("объясни концепт second-order thinking",
                        "second-order thinking")

    def test_author_advice_reordered(self) -> None:
        self._knowledge("что советовал Хормози про продажи", "Хормози продажи")

    def test_what_in_notes(self) -> None:
        self._knowledge("что в конспектах про оффер", "оффер")

    def test_full_name_talk(self) -> None:
        self._knowledge("что Алекс Хормози говорил про оффер",
                        "Алекс Хормози оффер")

    def test_full_name_advice(self) -> None:
        self._knowledge("что Андрей Карпати советовал по агентам",
                        "Андрей Карпати агентам")

    def test_full_name_advice_reordered_latin(self) -> None:
        self._knowledge("что советовал Alex Hormozi про продажи",
                        "Alex Hormozi продажи")

    def test_capitalized_what_single_name(self) -> None:
        self._knowledge("Что Хормози говорил про оффер", "Хормози оффер")

    def test_capitalized_what_full_name(self) -> None:
        self._knowledge("Что Алекс Хормози говорил про оффер",
                        "Алекс Хормози оффер")


class TaskIntentTests(unittest.TestCase):
    def test_create_task(self) -> None:
        route = ar.route("создай задачу купить кабель")
        self.assertEqual(route.kind, ar.KIND_TASK)
        self.assertEqual(route.subject, "купить кабель")

    def test_add_task(self) -> None:
        route = ar.route("добавь задачу проверить воронку")
        self.assertEqual(route.kind, ar.KIND_TASK)
        self.assertEqual(route.subject, "проверить воронку")

    def test_create_task_without_subject_is_safe_capture(self) -> None:
        route = ar.route("создай задачу")
        self.assertEqual(route.kind, ar.KIND_CAPTURE)


class KnowledgeIntentTests(unittest.TestCase):
    def test_author_talk_is_knowledge_with_research(self) -> None:
        route = ar.route("что Хормози говорил про оффер")
        self.assertEqual(route.kind, ar.KIND_KNOWLEDGE_SEARCH)
        self.assertTrue(route.research)
        self.assertTrue(route.personal, "Dusk-часть всё равно ищется первой")

    def test_author_advice_is_knowledge(self) -> None:
        route = ar.route("что Карпати советовал по агентам")
        self.assertEqual(route.kind, ar.KIND_KNOWLEDGE_SEARCH)
        self.assertTrue(route.research)

    def test_term_in_notes_is_knowledge(self) -> None:
        route = ar.route("найди в конспектах термин agent loop")
        self.assertEqual(route.kind, ar.KIND_KNOWLEDGE_SEARCH)
        self.assertTrue(route.research)
        self.assertEqual(route.subject, "agent loop")

    def test_research_brain_mention_is_knowledge(self) -> None:
        route = ar.route("что в research brain про оффер")
        self.assertEqual(route.kind, ar.KIND_KNOWLEDGE_SEARCH)
        self.assertTrue(route.research)
        self.assertEqual(route.subject, "оффер")

    def test_author_talk_has_clean_subject(self) -> None:
        route = ar.route("что Хормози говорил про оффер")
        self.assertEqual(route.subject, "Хормози оффер")


class LegacyCompatibilityTests(unittest.TestCase):
    def test_question_mark_is_dusk_first_search(self) -> None:
        route = ar.route("?моя идея про оффер")
        self.assertEqual(route.kind, ar.KIND_PERSONAL_SEARCH)
        self.assertFalse(route.research)

    def test_colon_search_stays_internal(self) -> None:
        route = ar.route("найди: мою заметку про агентов")
        self.assertEqual(route.kind, ar.KIND_PERSONAL_SEARCH)
        self.assertEqual(route.subject, "агентов")

    def test_colon_task_prefix_is_task(self) -> None:
        route = ar.route("задача: купить кабель")
        self.assertEqual(route.kind, ar.KIND_TASK)
        self.assertEqual(route.subject, "купить кабель")


if __name__ == "__main__":
    unittest.main()
