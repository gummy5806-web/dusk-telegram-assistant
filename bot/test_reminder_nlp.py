#!/usr/bin/env python3
"""Чистый русский парсер времени: границы, timezone, reminder intent."""

from __future__ import annotations

import datetime
import os
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import reminder_nlp

ALMATY = ZoneInfo("Asia/Almaty")
NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=ALMATY)


class RelativeTests(unittest.TestCase):
    def test_minutes_hours_days(self) -> None:
        self.assertEqual(reminder_nlp.parse_time("через 15 минут", now=NOW).when,
                         NOW + datetime.timedelta(minutes=15))
        self.assertEqual(reminder_nlp.parse_time("через 2 часа", now=NOW).when,
                         NOW + datetime.timedelta(hours=2))
        self.assertEqual(reminder_nlp.parse_time("через 3 дня", now=NOW).when,
                         NOW + datetime.timedelta(days=3))
        self.assertEqual(reminder_nlp.parse_time("через час", now=NOW).error, "")

    def test_zero_rejected(self) -> None:
        self.assertTrue(reminder_nlp.parse_time("через 0 минут", now=NOW).error)


class AbsoluteTests(unittest.TestCase):
    def test_today_and_tomorrow(self) -> None:
        self.assertEqual(reminder_nlp.parse_time("сегодня в 18", now=NOW).when,
                         NOW.replace(hour=18, minute=0, second=0, microsecond=0))
        self.assertEqual(reminder_nlp.parse_time("сегодня 18:30", now=NOW).when,
                         NOW.replace(hour=18, minute=30, second=0, microsecond=0))
        tomorrow = (NOW + datetime.timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0)
        self.assertEqual(reminder_nlp.parse_time("завтра в 9", now=NOW).when,
                         tomorrow)
        self.assertEqual(reminder_nlp.parse_time("завтра 09:15", now=NOW).when,
                         tomorrow.replace(hour=9, minute=15))

    def test_dates(self) -> None:
        expected = datetime.datetime(2026, 10, 12, 14, 30, tzinfo=ALMATY)
        self.assertEqual(reminder_nlp.parse_time("12.10 в 14:30", now=NOW).when,
                         expected)
        self.assertEqual(
            reminder_nlp.parse_time("12.10.2026 14:30", now=NOW).when, expected)
        self.assertEqual(
            reminder_nlp.parse_time("12.10.26 14:30", now=NOW).when, expected)

    def test_past_and_impossible_rejected(self) -> None:
        self.assertTrue(reminder_nlp.parse_time("сегодня в 9", now=NOW).error)
        self.assertTrue(reminder_nlp.parse_time("31.02 в 10:00", now=NOW).error)
        self.assertTrue(reminder_nlp.parse_time("сегодня в 25:00", now=NOW).error)

    def test_empty_and_unparsed(self) -> None:
        self.assertTrue(reminder_nlp.parse_time("", now=NOW).error)
        self.assertTrue(reminder_nlp.parse_time("потом как-нибудь", now=NOW).error)

    def test_naive_now_rejected(self) -> None:
        naive = datetime.datetime(2026, 10, 9, 12, 0)
        with self.assertRaises(reminder_nlp.ReminderNLPError):
            reminder_nlp.parse_time("через 2 часа", now=naive)

    def test_timezone_env_override(self) -> None:
        with patch.dict(os.environ, {"DUSK_TIMEZONE": "Europe/Berlin"}):
            parsed = reminder_nlp.parse_time("завтра в 9", now=None)
        self.assertEqual(parsed.when.hour, 9)
        self.assertEqual(parsed.when.tzinfo.key, "Europe/Berlin")


class IntentTests(unittest.TestCase):
    def test_before_time_subject(self) -> None:
        intent = reminder_nlp.parse_reminder_intent(
            "напомни завтра в 9 отправить отчёт", now=NOW)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.subject, "отправить отчёт")
        self.assertEqual(intent.when.hour, 9)

    def test_after_time_subject(self) -> None:
        intent = reminder_nlp.parse_reminder_intent(
            "напомни отправить отчёт через 2 часа", now=NOW)
        self.assertEqual(intent.subject, "отправить отчёт")
        self.assertEqual(intent.when, NOW + datetime.timedelta(hours=2))

    def test_without_time_is_not_intent(self) -> None:
        self.assertIsNone(reminder_nlp.parse_reminder_intent(
            "напомни мою мысль про оффер", now=NOW))

    def test_without_trigger_is_not_intent(self) -> None:
        self.assertIsNone(reminder_nlp.parse_reminder_intent(
            "что я думал про оффер завтра в 9", now=NOW))

    def test_missing_subject_is_not_intent(self) -> None:
        self.assertIsNone(reminder_nlp.parse_reminder_intent(
            "напомни завтра в 9", now=NOW))


class TypedIntentTests(unittest.TestCase):
    def test_none_when_no_time_or_trigger(self) -> None:
        self.assertEqual(
            reminder_nlp.parse_intent("напомни мою мысль про X", now=NOW).kind,
            "none")
        self.assertEqual(
            reminder_nlp.parse_intent("что я думал про X", now=NOW).kind,
            "none")
        self.assertEqual(reminder_nlp.parse_intent("напомни", now=NOW).kind,
                         "none")

    def test_error_when_matched_but_invalid(self) -> None:
        past = reminder_nlp.parse_intent("напомни сегодня в 9 отправить отчёт",
                                         now=NOW)
        self.assertEqual(past.kind, "error")
        impossible = reminder_nlp.parse_intent(
            "напомни 31.02 в 10:00 отчёт", now=NOW)
        self.assertEqual(impossible.kind, "error")
        huge = reminder_nlp.parse_intent(
            "напомни через 999999999999999999999 дней отчёт", now=NOW)
        self.assertEqual(huge.kind, "error")
        self.assertIn("10 лет", huge.error)

    def test_missing_subject(self) -> None:
        parsed = reminder_nlp.parse_intent("напомни завтра в 9", now=NOW)
        self.assertEqual(parsed.kind, "missing_subject")

    def test_ok_intent(self) -> None:
        parsed = reminder_nlp.parse_intent(
            "напомни завтра в 9 отправить отчёт", now=NOW)
        self.assertEqual(parsed.kind, "ok")
        self.assertEqual(parsed.intent.subject, "отправить отчёт")

    def test_oversized_input_is_none(self) -> None:
        parsed = reminder_nlp.parse_intent(
            "напомни " + "x" * 1000 + " через 2 часа", now=NOW)
        self.assertEqual(parsed.kind, "none")

    def test_parse_reminder_intent_compat(self) -> None:
        self.assertIsNone(reminder_nlp.parse_reminder_intent(
            "напомни завтра в 9", now=NOW))
        self.assertIsNotNone(reminder_nlp.parse_reminder_intent(
            "напомни завтра в 9 отчёт", now=NOW))


if __name__ == "__main__":
    unittest.main()
