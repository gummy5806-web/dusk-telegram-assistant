#!/usr/bin/env python3
"""Этап 3: честные метрики рынка и детерминированный недельный обзор.

Всё на временных валтах: модель, сеть и git запрещены. Проверяются разделение
счётчиков и целей, строгий разбор конфигурации, обзор недели и границы владения
пользовательской зоной.
"""

from __future__ import annotations

import datetime
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import learning_cycle as lc
import learn
import llm
import market
import status
import vault_ops
import weekly_review as wr


def forbidden(*args, **kwargs):
    raise AssertionError("Внешний вызов (модель/сеть/git) запрещён в тестах этапа 3")


llm.ask = forbidden
llm._project = forbidden
vault_ops.git_push = forbidden

AREA = "Продуктивность - навык"
NOTE_DIR = f"{learn.HUB}/{AREA}/📁note summary - авто конспект"
DAY = datetime.date(2026, 10, 11)          # воскресенье недели 2026-10-05…11


class ReviewBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        (self.vault / "TaskNotes" / "Tasks").mkdir(parents=True)
        (self.vault / "Учеба").mkdir(parents=True)
        (self.vault / NOTE_DIR).mkdir(parents=True)

    def write_market(self, *, channel="контент", metric="публикация", goal="3",
                     offer="курс по продажам", whom="предприниматели", price="20000",
                     focus="публикации", extra_fields="", rows=()) -> None:
        text = (
            "# 💰 Рынок\n\n## Оффер и канал\n\n"
            f"- **Оффер:** {offer}\n- **Кому:** {whom}\n- **Цена:** {price}\n"
            f"- **Канал:** {channel}\n- **Фокус цикла:** {focus}\n"
            f"- **Ведущая метрика:** {metric}\n- **Цель/неделя:** {goal}\n"
        )
        if extra_fields:
            text += extra_fields + "\n"
        text += (
            "\n## Дни\n\n"
            "| дата | Контент | Публикации | Аутрич | Лиды | Разговоры | Офферы | Продажи | Деньги |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
        )
        for row in rows:
            text += "| " + " | ".join(str(cell) for cell in row) + " |\n"
        (self.vault / market.MARKET_PATH).write_text(text, encoding="utf-8")

    def write_pair(self, *, name="Шаг", pair="pair0001", status_="open",
                   completed="", observed=None, conclusion=None):
        note_rel = f"{NOTE_DIR}/Разбор.md"
        (self.vault / note_rel).write_text(
            "---\n"
            "reference: https://youtu.be/aaaaaaaaaaa\n"
            "generator: dusk\n"
            f"{lc.MARK}\n{lc.PAIR_FIELD}: {pair}\n"
            "---\n\n# Разбор\n", encoding="utf-8")
        task = (
            "---\n"
            f'title: "{name}"\nstatus: {status_}\ncreated: 2026-10-06T10:00\n'
            + (f"completedDate: {completed}\n" if completed else "")
            + f'source: "{note_rel}"\n{lc.MARK}\n{lc.PAIR_FIELD}: {pair}\n'
            "---\n\n"
            f"# {name}\n\n"
            f"{lc.CRITERION_HEADING}\n\nвидно результат — гипотеза\n\n"
            f"{lc.TRIED_HEADING}\n\nнет данных\n\n"
            f"{lc.OBSERVED_HEADING}\n\n{observed or lc.NO_DATA}\n\n"
            f"{lc.CONCLUSION_HEADING}\n\n{conclusion or lc.NO_DATA}\n"
        )
        (self.vault / "TaskNotes" / "Tasks" / f"{name}.md").write_text(task, encoding="utf-8")
        return note_rel

    def generate(self, day=DAY):
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            return wr.generate(self.vault, day)

    def page(self) -> str:
        return wr.review_path(self.vault).read_text(encoding="utf-8")


class CounterGoalTests(ReviewBase):
    """Пункт 1: сырые счётчики отделены от целей, дефолтов нет."""

    def test_counters_carry_no_target(self) -> None:
        for key, value in market.COUNTERS.items():
            self.assertEqual(len(value), 2, f"{key}: счётчик не должен хранить цель")

    def test_bump_without_config_records_history_and_returns_no_target(self) -> None:
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            label, now, target, unit = market.bump("контент", 60, day="2026-10-06")

        self.assertEqual((label, now, unit), ("Контент", 60, "мин"))
        self.assertIsNone(target, "без конфигурации цели нет — 100/1/100 не подставляются")
        text = (self.vault / market.MARKET_PATH).read_text(encoding="utf-8")
        self.assertIn("| 2026-10-06 | 60 |", text, "история записана без цели")

    def test_bump_targets_only_configured_leading_metric(self) -> None:
        self.write_market(channel="контент", metric="публикация", goal="3")
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            label, now, target, unit = market.bump("публикация", 1, day="2026-10-06")
            other = market.bump("контент", 5, day="2026-10-06")

        self.assertEqual((label, now, target, unit), ("Публикации", 1, 3.0, "шт"))
        self.assertIsNone(other[2], "не ведущая метрика цели не получает")

    def test_raw_command_caller_records_without_target(self) -> None:
        import dispatch
        with patch.object(dispatch, "VAULT", self.vault), \
                patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            reply = dispatch.dispatch("+контент 60")

        self.assertIn("Контент", reply)
        self.assertIn("60", reply)
        self.assertNotIn(" из ", reply, "без цели не выдумываем «из N»")
        text = (self.vault / market.MARKET_PATH).read_text(encoding="utf-8")
        self.assertIn("| 2026-", text, "команда всё равно записала историю")


class ConfigTests(ReviewBase):
    """Пункт 2: строгий разбор конфигурации без догадок."""

    def test_empty_config_is_valid_input(self) -> None:
        (self.vault / market.MARKET_PATH).write_text(market.HEADER, encoding="utf-8")
        config = market.parse_config(self.vault)

        self.assertEqual(config["status"], "unconfigured")
        self.assertIn("Оффер", config["missing"])
        self.assertIsNone(config["goal"])

    def test_partial_config_lists_missing(self) -> None:
        self.write_market(offer="не задан")
        config = market.parse_config(self.vault)

        self.assertEqual(config["status"], "unconfigured")
        self.assertIn("Оффер", config["missing"])

    def test_duplicate_config_is_ambiguous(self) -> None:
        self.write_market(extra_fields="- **Оффер:** второй оффер")
        config = market.parse_config(self.vault)

        self.assertEqual(config["status"], "ambiguous")
        self.assertIn("Оффер", config["duplicates"])

    def test_malformed_goal_is_not_guessed(self) -> None:
        self.write_market(goal="много")
        config = market.parse_config(self.vault)

        self.assertEqual(config["status"], "malformed")
        self.assertIn("Цель/неделя", config["missing"])

    def test_missing_section_means_unconfigured(self) -> None:
        (self.vault / market.MARKET_PATH).write_text("# 💰 Рынок\n\n## Дни\n", encoding="utf-8")
        config = market.parse_config(self.vault)

        self.assertEqual(config["status"], "unconfigured")
        self.assertIn("секции", config["reason"])

    def test_supported_channels_and_metrics(self) -> None:
        cases = [("контент", "контент"), ("контент", "публикация"),
                 ("тёплый аутрич", "аутрич"), ("холодный аутрич", "аутрич")]
        for channel, metric in cases:
            with self.subTest(channel=channel, metric=metric):
                self.write_market(channel=channel, metric=metric)
                config = market.parse_config(self.vault)
                self.assertEqual(config["status"], "configured", config["reason"])
                self.assertEqual(config["metric"], metric)

    def test_incompatible_and_unsupported_metrics(self) -> None:
        cases = [("контент", "аутрич"), ("тёплый аутрич", "публикация"),
                 ("платная реклама", "аутрич"), ("контент", "конверсия")]
        for channel, metric in cases:
            with self.subTest(channel=channel, metric=metric):
                self.write_market(channel=channel, metric=metric)
                config = market.parse_config(self.vault)
                self.assertEqual(config["status"], "unsupported", config["reason"])

    def test_funnel_outcome_is_not_selected_as_leading_goal(self) -> None:
        self.write_market(channel="контент", metric="лид")
        config = market.parse_config(self.vault)

        self.assertEqual(config["status"], "unsupported")
        self.assertIn("воронк", config["reason"])

    def test_unknown_custom_metric_is_not_coerced(self) -> None:
        self.write_market(channel="контент", metric="моя метрика")
        config = market.parse_config(self.vault)

        self.assertEqual(config["status"], "unsupported")
        self.assertEqual(config["metric"], "")


class StatusMarketTests(ReviewBase):
    """Пункт 4: секция статуса без выдуманных норм."""

    def test_without_focus_totals_have_no_red_green(self) -> None:
        (self.vault / market.MARKET_PATH).write_text(market.HEADER, encoding="utf-8")
        text = "\n".join(status._market_section(self.vault, today=datetime.date(2026, 10, 6)))

        self.assertNotIn("🟢", text)
        self.assertNotIn("🔴", text)
        self.assertIn("не задан", text)
        self.assertIn("За неделю", text)

    def test_configured_focus_evaluates_only_leading_metric(self) -> None:
        self.write_market(rows=[("2026-10-06", "60", "2", "0", "1", "0", "0", "0", "0")])
        text = "\n".join(status._market_section(self.vault, today=datetime.date(2026, 10, 6)))

        self.assertIn("Ведущая метрика", text)
        self.assertIn("2 из 3", text)
        self.assertIn("воронка", text.lower())
        self.assertNotIn("🟢", text)
        self.assertNotIn("🔴", text)

    def test_paid_ads_gets_no_invented_activity_goal(self) -> None:
        self.write_market(channel="платная реклама", metric="аутрич")
        text = "\n".join(status._market_section(self.vault, today=datetime.date(2026, 10, 6)))

        self.assertNotIn("🟢", text)
        self.assertNotIn("🔴", text)
        self.assertIn("не используется как цель", text)


class WeekDataTests(ReviewBase):
    """Пункты 3/5: точные недели, будущие строки и повреждённая таблица."""

    def test_exact_week_bounds(self) -> None:
        start, end, included = wr._period(datetime.date(2026, 10, 6))
        self.assertEqual(start, datetime.date(2026, 10, 5))
        self.assertEqual(end, datetime.date(2026, 10, 11))
        self.assertEqual(included, datetime.date(2026, 10, 6))

    def test_future_market_rows_excluded(self) -> None:
        self.write_market(rows=[
            ("2026-10-06", "10", "1", "0", "0", "0", "0", "0", "0"),
            ("2026-10-20", "99", "9", "0", "0", "0", "0", "0", "0"),
        ])
        totals, days = market.range_totals(
            self.vault, datetime.date(2026, 10, 5), datetime.date(2026, 10, 11),
            strict=True)

        self.assertEqual(totals["контент"], 10.0)
        self.assertEqual(days, 1)

    def test_almaty_timestamp_defines_the_week_day(self) -> None:
        self.write_pair(name="Таймстемп", status_="done",
                        completed="2026-10-05T20:00:00+00:00")
        review = lc.application_rows(self.vault, today=DAY)
        row = review["rows"][0]

        self.assertTrue(row["done"], "20:00 UTC — это 01:00 следующего дня в Алматы")
        self.assertEqual(row["completed"], "2026-10-06")

    def test_malformed_market_row_refuses_without_writing(self) -> None:
        self.write_market(rows=[("2026-10-06", "10", "1", "0", "0", "0", "0", "0", "0")])
        self.assertTrue(self.generate(DAY)[0])
        before = wr.review_path(self.vault).read_bytes()
        self.write_market(rows=[("2026-10-06", "10", "1")])          # мало колонок

        ok, message = self.generate(DAY)

        self.assertFalse(ok, "повреждённая строка не должна давать обзор")
        self.assertIn("повреждена", message)
        self.assertEqual(wr.review_path(self.vault).read_bytes(), before)

    def test_ambiguous_config_refuses_review(self) -> None:
        self.write_market(extra_fields="- **Оффер:** второй оффер")

        ok, message = self.generate(DAY)

        self.assertFalse(ok)
        self.assertIn("не готова", message)
        self.assertFalse(wr.review_path(self.vault).exists())


class ReviewContentTests(ReviewBase):
    """Пункты 5/6: содержание обзора, ссылки и пользовательская зона."""

    def test_empty_applications_says_so(self) -> None:
        self.write_market(rows=[])
        ok, _ = self.generate(DAY)

        self.assertTrue(ok)
        page = self.page()
        self.assertIn("карточек применения за период нет", page)
        self.assertIn("Без данных: 0", page)
        self.assertIn("Обзор недели 2026-10-05 — 2026-10-11", page)

    def test_positive_and_negative_observations_are_user_text(self) -> None:
        self.write_pair(name="Позитив", status_="done", completed="2026-10-07",
                        observed="выросло", conclusion="сработало")
        self.write_pair(name="Негатив", observed="не изменилось",
                        conclusion="гипотеза не подтвердилась")
        self.write_market(rows=[("2026-10-06", "60", "2", "0", "1", "0", "0", "0", "0")])
        ok, _ = self.generate(DAY)

        self.assertTrue(ok)
        page = self.page()
        self.assertIn("выросло", page)
        self.assertIn("не изменилось", page)
        self.assertIn("сработало", page)
        self.assertIn("гипотеза не подтвердилась", page)
        self.assertNotIn("эффектив", page.lower())

    def test_review_uses_exact_obsidian_links(self) -> None:
        note_rel = self.write_pair(name="Шаг")
        self.write_market(rows=[])
        ok, _ = self.generate(DAY)

        self.assertTrue(ok)
        page = self.page()
        self.assertIn("[[TaskNotes/Tasks/Шаг]]", page)
        self.assertIn(f"[[{note_rel[:-3]}]]", page)

    def test_frozen_plan_state_is_included(self) -> None:
        self.write_market(rows=[])
        card = self.vault / "TaskNotes" / "Tasks" / "первое.md"
        card.write_text("---\ntitle: первое\nstatus: open\ncreated: 2026-10-01T10:00\n"
                        "---\n\n# первое\n", encoding="utf-8")
        import weekly_plan
        weekly_plan.save_plan(self.vault, ["первое"], day=DAY)
        ok, _ = self.generate(DAY)

        self.assertTrue(ok)
        page = self.page()
        self.assertIn("Обязательства недели", page)
        self.assertIn("первое", page)
        self.assertNotIn("План недели: план не зафиксирован", page)

    def test_repeated_generation_is_stable_and_preserves_user_span(self) -> None:
        self.write_market(rows=[("2026-10-06", "60", "2", "0", "1", "0", "0", "0", "0")])
        self.assertTrue(self.generate(DAY)[0])
        first = self.page()
        edited = first.replace("- **Фокус:** —", "- **Фокус:** мой фокус")
        wr.review_path(self.vault).write_text(edited, encoding="utf-8")

        self.assertTrue(self.generate(DAY)[0])
        second = self.page()

        self.assertEqual(wr.read_decisions(second)["Фокус"], "мой фокус")
        gen_first = first.split(wr.GEN_START, 1)[1].split(wr.GEN_END, 1)[0]
        gen_second = second.split(wr.GEN_START, 1)[1].split(wr.GEN_END, 1)[0]
        self.assertEqual(gen_first, gen_second, "повторный обзор детерминирован")

    def test_crlf_user_span_and_suffix_are_preserved_byte_for_byte(self) -> None:
        self.write_market(rows=[])
        self.assertTrue(self.generate(DAY)[0])
        path = wr.review_path(self.vault)
        raw = path.read_bytes().decode("utf-8")
        user = raw.split(wr.USER_START, 1)[1].split(wr.USER_END, 1)[0]
        user = user.replace("- **Фокус:** —",
                            "- **Фокус:** строка\n- **Фокус:** строка ещё раз")
        custom = raw.split(wr.USER_START, 1)[0] + wr.USER_START + user + wr.USER_END \
            + "\nПользовательский хвост\nПользовательский хвост\n"
        crlf = custom.replace("\n", "\r\n")
        path.write_bytes(crlf.encode("utf-8"))
        user_before = crlf.encode("utf-8").split(
            wr.USER_START.encode(), 1)[1].split(wr.USER_END.encode(), 1)[0]
        suffix_before = crlf.encode("utf-8").split(wr.USER_END.encode(), 1)[1]

        ok, _ = self.generate(DAY)

        self.assertTrue(ok)
        out = path.read_bytes()
        self.assertEqual(out.split(wr.USER_START.encode(), 1)[1]
                         .split(wr.USER_END.encode(), 1)[0], user_before,
                         "CRLF и повторяющиеся строки пользователя сохранены")
        self.assertTrue(out.endswith(suffix_before), "хвост после зоны сохранён")

    def test_ambiguous_markers_refuse_without_changing_bytes(self) -> None:
        self.write_market(rows=[])
        self.assertTrue(self.generate(DAY)[0])
        path = wr.review_path(self.vault)
        path.write_text(self.page() + f"\n{wr.USER_START}\n", encoding="utf-8")
        before = path.read_bytes()

        ok, message = self.generate(DAY)

        self.assertFalse(ok)
        self.assertIn("маркер", message)
        self.assertEqual(path.read_bytes(), before)

    def test_write_failure_leaves_old_review_intact(self) -> None:
        self.write_market(rows=[])
        self.assertTrue(self.generate(DAY)[0])
        path = wr.review_path(self.vault)
        before = path.read_bytes()

        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}), \
                patch.object(wr.queue_store, "atomic_write",
                             side_effect=OSError("диск полон")):
            ok, message = wr.generate(self.vault, DAY)

        self.assertFalse(ok)
        self.assertEqual(path.read_bytes(), before, "старый обзор не изменён")

    def test_first_generation_has_four_user_fields(self) -> None:
        self.write_market(rows=[])
        self.assertTrue(self.generate(DAY)[0])
        decisions = wr.read_decisions(self.page())

        self.assertEqual(list(decisions), list(wr.DECISION_FIELDS))
        self.assertTrue(all(value == "—" for value in decisions.values()))

    def test_manual_cli_invocation_does_not_touch_llm(self) -> None:
        self.write_market(rows=[])
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, str(Path(wr.__file__)), "--vault", str(self.vault),
             "--day", "2026-10-06"],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(wr.review_path(self.vault).exists())


class Pass2RegressionTests(ReviewBase):
    """Проход 2: строгая числовая валидация, недельная семантика, вызовы, границы."""

    # ── 1. числовая и табличная строгость ────────────────────────────────

    def test_malformed_goal_values_are_never_configured(self) -> None:
        for goal in ("NaN", "inf", "-inf", "1e400", "0", "-5"):
            with self.subTest(goal=goal):
                self.write_market(goal=goal)
                config = market.parse_config(self.vault)
                self.assertEqual(config["status"], "malformed", goal)
                self.assertIsNone(config["goal"])

    def test_malformed_goal_refuses_review_without_writing(self) -> None:
        self.write_market(rows=[])
        self.assertTrue(self.generate(DAY)[0])
        before = wr.review_path(self.vault).read_bytes()
        for goal in ("NaN", "inf", "-inf", "1e400", "0", "-5"):
            with self.subTest(goal=goal):
                self.write_market(goal=goal, rows=[])
                ok, message = self.generate(DAY)
                self.assertFalse(ok)
                self.assertIn("не готова", message)
                self.assertEqual(wr.review_path(self.vault).read_bytes(), before)

    def test_strict_table_errors_raise(self) -> None:
        cases = [
            ("нечисловое", [("2026-10-06", "abc", "1", "0", "0", "0", "0", "0", "0")]),
            ("неконечное", [("2026-10-06", "inf", "1", "0", "0", "0", "0", "0", "0")]),
            ("невозможная дата", [("2026-13-40", "1", "1", "0", "0", "0", "0", "0", "0")]),
        ]
        for label, rows in cases:
            with self.subTest(case=label):
                self.write_market(rows=rows)
                with self.assertRaises(market.MarketError):
                    market.range_totals(self.vault, datetime.date(2026, 10, 5),
                                        datetime.date(2026, 10, 11), strict=True)

    def test_invalid_table_refuses_review_without_writing(self) -> None:
        cases = [
            ("нечисловое", [("2026-10-06", "abc", "1", "0", "0", "0", "0", "0", "0")]),
            ("неконечное", [("2026-10-06", "inf", "1", "0", "0", "0", "0", "0", "0")]),
            ("невозможная дата", [("2026-13-40", "1", "1", "0", "0", "0", "0", "0", "0")]),
            ("мало колонок", [("2026-10-06", "1", "1")]),
        ]
        for label, rows in cases:
            with self.subTest(case=label):
                self.write_market(rows=[])
                self.assertTrue(self.generate(DAY)[0])
                before = wr.review_path(self.vault).read_bytes()
                self.write_market(rows=rows)
                ok, message = self.generate(DAY)
                self.assertFalse(ok)
                self.assertIn("повреждена", message)
                self.assertEqual(wr.review_path(self.vault).read_bytes(), before)

    def test_duplicate_date_rows_refuse_without_writing(self) -> None:
        self.write_market(rows=[])
        self.assertTrue(self.generate(DAY)[0])
        before = wr.review_path(self.vault).read_bytes()
        self.write_market(rows=[("2026-10-06", "1", "1", "0", "0", "0", "0", "0", "0"),
                                ("2026-10-06", "2", "2", "0", "0", "0", "0", "0", "0")])

        ok, message = self.generate(DAY)

        self.assertFalse(ok)
        self.assertIn("повреждена", message)
        self.assertEqual(wr.review_path(self.vault).read_bytes(), before)

    # ── 2. точная недельная семантика ───────────────────────────────────

    def test_previous_week_activity_does_not_satisfy_weekly_goal(self) -> None:
        self.write_market(metric="публикация", goal="100", rows=[
            ("2026-09-30", "0", "100", "0", "0", "0", "0", "0", "0"),
            ("2026-10-06", "0", "5", "0", "0", "0", "0", "0", "0"),
        ])

        text = "\n".join(status._market_section(self.vault, today=datetime.date(2026, 10, 6)))

        self.assertIn("5 из 100", text)
        self.assertNotIn("105 из 100", text)

    def test_future_rows_do_not_affect_weekly_goal(self) -> None:
        self.write_market(metric="публикация", goal="100", rows=[
            ("2026-10-06", "0", "5", "0", "0", "0", "0", "0", "0"),
            ("2026-10-20", "0", "999", "0", "0", "0", "0", "0", "0"),
        ])

        text = "\n".join(status._market_section(self.vault, today=datetime.date(2026, 10, 6)))

        self.assertIn("5 из 100", text)
        self.assertNotIn("999", text)
        self.assertNotIn("1004 из 100", text)

    def test_week_progress_is_calendar_week(self) -> None:
        self.write_market(metric="публикация", goal="100", rows=[
            ("2026-09-30", "0", "100", "0", "0", "0", "0", "0", "0"),
            ("2026-10-06", "0", "5", "0", "0", "0", "0", "0", "0"),
        ])
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            progress = market.week_progress("публикация", day=datetime.date(2026, 10, 6))

        self.assertEqual(progress["start"], "2026-10-05")
        self.assertEqual(progress["total"], 5.0)
        self.assertEqual(progress["target"], 100.0)

    # ── 3. семантика ответа на команду ─────────────────────────────────

    def test_raw_command_reports_today_and_week_separately(self) -> None:
        import dispatch
        self.write_market(metric="публикация", goal="100", rows=[
            ("2026-09-30", "0", "100", "0", "0", "0", "0", "0", "0"),
        ])
        with patch.object(dispatch, "VAULT", self.vault), \
                patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}), \
                patch.object(market, "owner_day",
                             lambda day=None: datetime.date(2026, 10, 6)):
            reply = dispatch._counter("+публикация 5")

        self.assertIn("+5", reply)
        self.assertIn("сегодня 5", reply)
        self.assertIn("5 из 100", reply, "недельная цель сравнивается с неделей")
        self.assertNotIn("🟡", reply)
        self.assertNotIn("🔴", reply)

    def test_raw_command_for_leading_counter_reports_week_progress(self) -> None:
        """`+контент 10` во вторник: сегодня отдельно, неделя пн→вт отдельно."""
        import dispatch
        self.write_market(channel="контент", metric="контент", goal="100", rows=[
            ("2026-09-30", "100", "0", "0", "0", "0", "0", "0", "0"),
        ])
        with patch.object(dispatch, "VAULT", self.vault), \
                patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}), \
                patch.object(market, "owner_day",
                             lambda day=None: datetime.date(2026, 10, 6)):
            reply = dispatch._counter("+контент 10")

        self.assertIn("+10", reply)
        self.assertIn("сегодня 10", reply)
        self.assertIn("10 из 100", reply, "прошлая среда не входит в текущую неделю")
        self.assertNotIn("110 из 100", reply)
        self.assertNotIn(" из 10 ", reply, "день не сравнивается с недельной целью")

    def test_raw_command_without_target_has_no_judgment(self) -> None:
        import dispatch
        (self.vault / market.MARKET_PATH).write_text(market.HEADER, encoding="utf-8")
        with patch.object(dispatch, "VAULT", self.vault), \
                patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}), \
                patch.object(market, "owner_day",
                             lambda day=None: datetime.date(2026, 10, 6)):
            reply = dispatch._counter("+контент 10")

        self.assertIn("сегодня 10", reply)
        self.assertNotIn(" из ", reply)
        for mark in ("🟢", "🟡", "🔴"):
            self.assertNotIn(mark, reply)
        self.assertIn("| 2026-10-06 |",
                      (self.vault / market.MARKET_PATH).read_text(encoding="utf-8"))

    def test_owner_day_defaults_to_almaty_today(self) -> None:
        import weekly_plan
        self.assertEqual(market.owner_day(None), weekly_plan.today_local())

    def test_bump_rejects_invalid_date_before_write(self) -> None:
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            with self.assertRaises(ValueError):
                market.bump("контент", 5, day="2026-13-40")

        self.assertFalse((self.vault / market.MARKET_PATH).exists(),
                         "плохая явная дата не должна ничего записывать")

    # ── 4. единственная секция конфигурации ─────────────────────────────

    def test_duplicate_config_section_refuses_and_preserves_bytes(self) -> None:
        self.write_market(rows=[])
        self.assertTrue(self.generate(DAY)[0])
        before = wr.review_path(self.vault).read_bytes()
        self.write_market(rows=[],
                          extra_fields="## Оффер и канал\n\n- **Оффер:** второй")

        ok, message = self.generate(DAY)

        self.assertFalse(ok)
        self.assertIn("не готова", message)
        self.assertEqual(wr.review_path(self.vault).read_bytes(), before)

    # ── 5. containment для чтения карточек ──────────────────────────────

    def test_external_task_symlink_is_not_read(self) -> None:
        self.write_market(rows=[])
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        external = Path(outside.name) / "Внешняя.md"
        external.write_text(
            "---\ntitle: ВНЕШНЯЯ-МЕТКА\nstatus: open\nscheduled: 2020-01-01\n"
            "---\n\n# x\n", encoding="utf-8")
        (self.vault / "TaskNotes" / "Tasks" / "Внешняя.md").symlink_to(external)

        ok, _ = self.generate(DAY)

        self.assertTrue(ok)
        self.assertNotIn("ВНЕШНЯЯ-МЕТКА", self.page())
        self.assertEqual(wr._stale_cards(self.vault, DAY), ([], []))

    def test_external_task_root_symlink_is_not_read(self) -> None:
        self.write_market(rows=[])
        tasks = self.vault / "TaskNotes" / "Tasks"
        tasks.rmdir()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / "Внешняя.md").write_text(
            "---\ntitle: ВНЕШНЯЯ-МЕТКА\nstatus: open\nscheduled: 2020-01-01\n"
            "---\n\n# x\n", encoding="utf-8")
        tasks.symlink_to(outside.name, target_is_directory=True)

        ok, _ = self.generate(DAY)

        self.assertTrue(ok)
        self.assertNotIn("ВНЕШНЯЯ-МЕТКА", self.page())
        self.assertEqual(wr._stale_cards(self.vault, DAY), ([], []))


class Pass3RegressionTests(ReviewBase):
    """Проход 3: публичный ввод счётчика и оценка цели только по целым данным."""

    # ── 1. публичный ввод счётчика ─────────────────────────────────────

    def test_parse_command_rejects_nonfinite_overflow_and_nonpositive(self) -> None:
        for bad in ("9" * 1000, "0", "-5", "inf", "NaN", "1e400"):
            with self.subTest(amount=bad):
                self.assertIsNone(market.parse_command("+контент " + bad))
        self.assertEqual(market.parse_command("+контент 60"), ("контент", 60.0))

    def test_dispatch_does_not_recognise_overflow_command(self) -> None:
        import dispatch
        big = "9" * 1000
        self.assertNotEqual(dispatch.resolve_capture("+контент " + big).kind, "counter")
        with patch.object(dispatch, "VAULT", self.vault), \
                patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            reply = dispatch._counter("+контент " + big)

        self.assertEqual(reply, "")
        self.assertNotIn("inf", reply)
        self.assertFalse((self.vault / market.MARKET_PATH).exists(),
                         "недопустимая команда не создаёт файл")

    def test_bump_rejects_bad_amounts_without_creating_file(self) -> None:
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            for bad in (float("nan"), float("inf"), float("1e400"), 0, -3):
                with self.subTest(amount=bad):
                    with self.assertRaises(ValueError):
                        market.bump("контент", bad, day="2026-10-06")
            self.assertFalse((self.vault / market.MARKET_PATH).exists())

    def test_bump_rejects_corrupt_cell_and_overflow_without_write(self) -> None:
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            market.bump("контент", 5, day="2026-10-06")
        path = self.vault / market.MARKET_PATH

        text = path.read_text(encoding="utf-8").replace(
            "| 2026-10-06 | 5 |", "| 2026-10-06 | abc |")
        path.write_text(text, encoding="utf-8")
        before = path.read_bytes()
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            with self.assertRaises(ValueError):
                market.bump("контент", 1, day="2026-10-06")
        self.assertEqual(path.read_bytes(), before)

        text = path.read_text(encoding="utf-8").replace(
            "| 2026-10-06 | abc |", "| 2026-10-06 | 1e308 |")
        path.write_text(text, encoding="utf-8")
        before = path.read_bytes()
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            with self.assertRaises(ValueError):
                market.bump("контент", 1e308, day="2026-10-06")
        self.assertEqual(path.read_bytes(), before)

    def test_bump_rejects_invalid_day_without_creating_file(self) -> None:
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            with self.assertRaises(ValueError):
                market.bump("контент", 5, day="2026-13-40")
        self.assertFalse((self.vault / market.MARKET_PATH).exists())

    # ── 2. статус не оценивает цель по повреждённым данным ─────────────

    def test_status_market_section_reports_corruption_without_judgment(self) -> None:
        cases = {
            "нечисловое": [("2026-10-06", "abc", "1", "0", "0", "0", "0", "0", "0")],
            "неконечное": [("2026-10-06", "inf", "1", "0", "0", "0", "0", "0", "0")],
            "невозможная дата": [("2026-13-40", "1", "1", "0", "0", "0", "0", "0", "0")],
            "дубль даты": [("2026-10-06", "1", "1", "0", "0", "0", "0", "0", "0"),
                            ("2026-10-06", "2", "2", "0", "0", "0", "0", "0", "0")],
            "мало колонок": [("2026-10-06", "1", "1")],
        }
        for label, rows in cases.items():
            with self.subTest(case=label):
                self.write_market(metric="контент", goal="100", rows=rows)
                text = "\n".join(
                    status._market_section(self.vault, today=datetime.date(2026, 10, 6)))
                self.assertIn("поврежд", text)
                self.assertNotIn("достигнута", text)
                self.assertNotIn("| Контент,", text, "повреждённое не показываем как ноль")

    def test_status_unconfigured_corruption_is_not_a_truthful_zero(self) -> None:
        self.write_market(offer="не задан",
                          rows=[("2026-10-06", "abc", "1", "0", "0", "0", "0", "0", "0")])
        text = "\n".join(
            status._market_section(self.vault, today=datetime.date(2026, 10, 6)))

        self.assertIn("поврежд", text)
        self.assertNotIn("| Контент,", text)


class Pass4RegressionTests(ReviewBase):
    """Проход 4: строгий разбор именно блока `## Дни` и его таблицы рынка."""

    MALFORMED = {
        "short_day": "2026-10-1",
        "slashes": "2026/10/06",
        "text": "итого",
    }

    def test_malformed_dates_raise_in_strict_parse(self) -> None:
        for label, date in self.MALFORMED.items():
            with self.subTest(case=label):
                self.write_market(rows=[(date, "5", "0", "0", "0", "0", "0", "0", "0")])
                with self.assertRaises(market.MarketError):
                    market.range_totals(self.vault, datetime.date(2026, 10, 5),
                                        datetime.date(2026, 10, 11), strict=True)

    def test_malformed_dates_block_review_without_changing_bytes(self) -> None:
        for label, date in self.MALFORMED.items():
            with self.subTest(case=label):
                self.write_market(metric="контент", goal="100", rows=[
                    ("2026-10-06", "5", "0", "0", "0", "0", "0", "0", "0")])
                self.assertTrue(self.generate(DAY)[0])
                before = wr.review_path(self.vault).read_bytes()
                self.write_market(metric="контент", goal="100", rows=[
                    (date, "5", "0", "0", "0", "0", "0", "0", "0")])

                ok, message = self.generate(DAY)

                self.assertFalse(ok, date)
                self.assertIn("повреждена", message)
                self.assertEqual(wr.review_path(self.vault).read_bytes(), before)

    def test_malformed_dates_block_status_judgment(self) -> None:
        for label, date in self.MALFORMED.items():
            with self.subTest(case=label):
                self.write_market(metric="контент", goal="100", rows=[
                    (date, "5", "0", "0", "0", "0", "0", "0", "0")])
                text = "\n".join(
                    status._market_section(self.vault, today=datetime.date(2026, 10, 6)))
                self.assertIn("поврежд", text)
                self.assertNotIn("достигнута", text)
                self.assertNotIn("| Контент,", text)

    def test_non_market_table_outside_days_is_ignored(self) -> None:
        self.write_market(metric="контент", goal="100", rows=[
            ("2026-10-06", "5", "0", "0", "0", "0", "0", "0", "0")])
        path = self.vault / market.MARKET_PATH
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text + "\n## Другое\n\n| 2026-10-06 | 999 |\n|---|---|\n",
            encoding="utf-8")

        totals, days = market.range_totals(
            self.vault, datetime.date(2026, 10, 5), datetime.date(2026, 10, 11),
            strict=True)

        self.assertEqual(totals["контент"], 5.0)
        self.assertEqual(days, 1)
        ok, _ = self.generate(DAY)
        self.assertTrue(ok)
        self.assertNotIn("999", self.page())
        status_text = "\n".join(
            status._market_section(self.vault, today=datetime.date(2026, 10, 6)))
        self.assertIn("5 из 100", status_text)

    def test_stray_row_without_market_header_is_corruption(self) -> None:
        self.write_market(rows=[])
        path = self.vault / market.MARKET_PATH
        text = path.read_text(encoding="utf-8")
        # таблица без известной шапки внутри ## Дни
        broken = text.split("## Дни", 1)[0] + "## Дни\n\n| 2026-10-06 | 5 |\n|---|---|\n"
        path.write_text(broken, encoding="utf-8")
        with self.assertRaises(market.MarketError):
            market.range_totals(self.vault, datetime.date(2026, 10, 5),
                               datetime.date(2026, 10, 11), strict=True)


class Pass5RegressionTests(ReviewBase):
    """Проход 5: `bump` строго валидирует весь `## Дни` до записи."""

    Z = ("0",) * 8

    def market_bytes(self) -> bytes:
        return (self.vault / market.MARKET_PATH).read_bytes()

    def bump(self, amount=1, day="2026-10-06"):
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            return market.bump("контент", amount, day=day)

    def test_bump_short_current_row_refuses_and_preserves_bytes(self) -> None:
        self.write_market(rows=[("2026-10-06", "5")])
        before = self.market_bytes()

        with self.assertRaises(market.MarketError):
            self.bump()

        self.assertEqual(self.market_bytes(), before)

    def test_bump_malformed_historical_date_refuses(self) -> None:
        self.write_market(rows=[("2026-10-1",) + self.Z])
        before = self.market_bytes()

        with self.assertRaises(market.MarketError):
            self.bump()

        self.assertEqual(self.market_bytes(), before)

    def test_bump_duplicate_historical_date_refuses(self) -> None:
        self.write_market(rows=[("2026-10-05",) + self.Z, ("2026-10-05",) + self.Z])
        before = self.market_bytes()

        with self.assertRaises(market.MarketError):
            self.bump()

        self.assertEqual(self.market_bytes(), before)

    def test_bump_nonfinite_and_nonnumeric_historical_cell_refuse(self) -> None:
        for value in ("inf", "abc", "NaN", "1e400"):
            with self.subTest(value=value):
                self.write_market(rows=[("2026-10-05", value) + self.Z[1:]])
                before = self.market_bytes()

                with self.assertRaises(market.MarketError):
                    self.bump()

                self.assertEqual(self.market_bytes(), before)

    def test_valid_bump_still_updates_only_its_row(self) -> None:
        self.write_market(rows=[("2026-10-05",) + self.Z])

        self.bump(amount=3)

        text = (self.vault / market.MARKET_PATH).read_text(encoding="utf-8")
        self.assertIn("| 2026-10-06 | 3 |", text)
        self.assertIn("| 2026-10-05 | 0 |", text)

    def test_missing_file_creates_valid_header_and_row(self) -> None:
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            label, now, _target, unit = market.bump("контент", 7, day="2026-10-06")

        self.assertEqual((label, now, unit), ("Контент", 7, "мин"))
        text = (self.vault / market.MARKET_PATH).read_text(encoding="utf-8")
        self.assertIn("## Дни", text)
        self.assertIn("| 2026-10-06 | 7 |", text)
        totals, days = market.range_totals(
            self.vault, datetime.date(2026, 10, 5), datetime.date(2026, 10, 11),
            strict=True)
        self.assertEqual(totals["контент"], 7.0)
        self.assertEqual(days, 1)


class Pass6RegressionTests(ReviewBase):
    """Проход 6: точная схема шапки/разделителя таблицы рынка."""

    CONFIG = (
        "# 💰 Рынок\n\n## Оффер и канал\n\n"
        "- **Оффер:** курс\n- **Кому:** x\n- **Цена:** 1\n"
        "- **Канал:** контент\n- **Фокус цикла:** f\n"
        "- **Ведущая метрика:** контент\n- **Цель/неделя:** 100\n\n")
    HEADER = "| " + " | ".join(market.EXPECTED_HEADER) + " |\n"
    SEP = "|" + "---|" * len(market.EXPECTED_HEADER) + "\n"

    BAD_SCHEMA = {
        "wrong_label": HEADER.replace("| Контент |", "| Другая |") + SEP,
        "short_header": "| дата | Контент |\n|---|---|\n",
        "reordered": ("| дата | Публикации | Контент | Аутрич | Лиды | Разговоры | "
                      "Офферы | Продажи | Деньги |\n" + SEP),
        "extra_column": HEADER.rstrip("\n") + " | Лишнее |\n" + "|" + "---|" * 10 + "\n",
        "short_separator": HEADER + "|---|---|\n",
        "long_separator": HEADER + "|" + "---|" * 10 + "\n",
        "bad_separator": HEADER + "| x | x | x | x | x | x | x | x | x |\n",
    }

    def write_days(self, block: str) -> None:
        (self.vault / market.MARKET_PATH).write_text(
            self.CONFIG + "## Дни\n\n" + block, encoding="utf-8")

    def test_bump_bad_schema_refuses_without_write(self) -> None:
        for label, block in self.BAD_SCHEMA.items():
            with self.subTest(case=label):
                self.write_days(block)
                before = (self.vault / market.MARKET_PATH).read_bytes()
                with self.assertRaises(market.MarketError):
                    with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
                        market.bump("контент", 1, day="2026-10-06")
                self.assertEqual((self.vault / market.MARKET_PATH).read_bytes(), before)

    def test_status_bad_schema_has_no_judgment(self) -> None:
        for label, block in self.BAD_SCHEMA.items():
            with self.subTest(case=label):
                self.write_days(block)
                text = "\n".join(
                    status._market_section(self.vault, today=datetime.date(2026, 10, 6)))
                self.assertIn("поврежд", text)
                self.assertNotIn("достигнута", text)

    def test_review_bad_schema_refuses_and_preserves_bytes(self) -> None:
        for label, block in self.BAD_SCHEMA.items():
            with self.subTest(case=label):
                self.write_market(metric="контент", goal="100", rows=[
                    ("2026-10-06", "5", "0", "0", "0", "0", "0", "0", "0")])
                self.assertTrue(self.generate(DAY)[0])
                before = wr.review_path(self.vault).read_bytes()
                self.write_days(block)

                ok, message = self.generate(DAY)

                self.assertFalse(ok)
                self.assertIn("повреждена", message)
                self.assertEqual(wr.review_path(self.vault).read_bytes(), before)

    def test_valid_schema_and_missing_bootstrap_still_work(self) -> None:
        self.write_market(metric="контент", goal="100", rows=[
            ("2026-10-05", "5", "0", "0", "0", "0", "0", "0", "0")])
        with patch.dict(os.environ, {"VAULT_PATH": str(self.vault)}):
            label, now, _target, unit = market.bump("контент", 2, day="2026-10-06")
        self.assertEqual((label, now, unit), ("Контент", 2, "мин"))
        totals, days = market.range_totals(
            self.vault, datetime.date(2026, 10, 5), datetime.date(2026, 10, 11),
            strict=True)
        self.assertEqual(totals["контент"], 7.0)
        self.assertEqual(days, 2)


if __name__ == "__main__":
    unittest.main()
