#!/usr/bin/env python3
"""Чистый русский парсер времени для напоминаний. Без Telegram, сети и LLM.

Поддерживает ровно детерминированные формы:

* `через 15 минут`, `через 2 часа`, `через 3 дня`;
* `сегодня в 18`, `сегодня 18:30`;
* `завтра в 9`, `завтра 09:15`;
* `12.10 в 14:30`, `12.10.2026 14:30`.

Возвращаемое время — aware в `DUSK_TIMEZONE` (по умолчанию Asia/Almaty).
Прошлое, наивное и невозможное время отклоняются понятным текстом.
`parse_reminder_intent` срабатывает только на явное «напомни» с распознаваемым
временем; без времени это не reminder intent (например, личный поиск).
"""

from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Almaty"
MAX_SUBJECT_LEN = 200
MAX_INPUT_LEN = 400
MAX_RELATIVE_DAYS = 3650
REJECT_PAST = "Это время уже прошло — назови будущее время."
NO_TIME = "Не понял время. Напиши, например: через 2 часа или завтра в 9."
TOO_FAR = "Слишком далеко в будущем — максимум 10 лет."
MISSING_SUBJECT = "Что напомнить? Напиши: напомни <что> завтра в 9."


class ReminderNLPError(ValueError):
    """Некорректный вход парсера (не показывается пользователю как есть)."""


@dataclass(frozen=True)
class TimeParse:
    when: Optional[datetime.datetime] = None
    error: str = ""


@dataclass(frozen=True)
class ReminderIntent:
    when: datetime.datetime
    subject: str


@dataclass(frozen=True)
class IntentParse:
    """Typed результат: none | error | missing_subject | ok."""

    kind: str
    intent: Optional[ReminderIntent] = None
    error: str = ""


def timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("DUSK_TIMEZONE", DEFAULT_TIMEZONE))


def _now(now, tz: ZoneInfo) -> datetime.datetime:
    if now is None:
        return datetime.datetime.now(tz)
    if not isinstance(now, datetime.datetime):
        raise ReminderNLPError("now: нужен datetime")
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ReminderNLPError("now: нужна aware-метка")
    return now.astimezone(tz)


_REL = re.compile(
    r"через\s+(?:(\d+)\s*)?(минут\w*|мин|час\w*|ч|день|дня|дней|дн\w*)",
    re.IGNORECASE)
_TODAY = re.compile(r"сегодня\s+(?:в\s+)?(\d{1,2})(?::(\d{2}))?", re.IGNORECASE)
_TOMORROW = re.compile(r"завтра\s+(?:в\s+)?(\d{1,2})(?::(\d{2}))?", re.IGNORECASE)
_DATE = re.compile(
    r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+(?:в\s+)?(\d{1,2})(?::(\d{2}))?",
    re.IGNORECASE)
_TRIGGER = re.compile(r"\bнапомн\w*", re.IGNORECASE)
_FILLER = re.compile(r"\b(?:мне|пожалуйста)\b", re.IGNORECASE)


def _extract(text: str, now: datetime.datetime, tz: ZoneInfo):
    """(match, when, error). match is None — временной формы нет."""
    match = _REL.search(text)
    if match:
        amount = int(match.group(1)) if match.group(1) else 1
        unit = match.group(2).lower()
        if amount <= 0:
            return match, None, "Количество должно быть больше нуля."
        if unit.startswith("мин"):
            if amount > MAX_RELATIVE_DAYS * 24 * 60:
                return match, None, TOO_FAR
            delta = datetime.timedelta(minutes=amount)
        elif unit.startswith("час") or unit == "ч":
            if amount > MAX_RELATIVE_DAYS * 24:
                return match, None, TOO_FAR
            delta = datetime.timedelta(hours=amount)
        else:
            if amount > MAX_RELATIVE_DAYS:
                return match, None, TOO_FAR
            delta = datetime.timedelta(days=amount)
        try:
            when = now + delta
        except OverflowError:
            return match, None, TOO_FAR
        return match, when, ""

    match = _TODAY.search(text)
    if match:
        when = _at_time(now, int(match.group(1)), match.group(2), tz)
        if when is None:
            return match, None, "Такого времени не существует."
        if when <= now:
            return match, None, REJECT_PAST
        return match, when, ""

    match = _TOMORROW.search(text)
    if match:
        base = now + datetime.timedelta(days=1)
        when = _at_time(base, int(match.group(1)), match.group(2), tz)
        if when is None:
            return match, None, "Такого времени не существует."
        return match, when, ""

    match = _DATE.search(text)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        year_text = match.group(3)
        if year_text:
            year = int(year_text)
            if year < 100:
                year += 2000
        else:
            year = now.year
        try:
            base = datetime.datetime(year, month, day, tzinfo=tz)
        except ValueError:
            return match, None, "Такой даты не существует."
        when = _at_time(base, int(match.group(4)), match.group(5), tz)
        if when is None:
            return match, None, "Такой даты не существует."
        if when <= now:
            return match, None, REJECT_PAST
        return match, when, ""

    return None, None, ""


def _at_time(base: datetime.datetime, hour: int, minute_text,
             tz: ZoneInfo) -> Optional[datetime.datetime]:
    minute = int(minute_text) if minute_text else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    try:
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None


def parse_time(text: str, now=None, tz: Optional[ZoneInfo] = None) -> TimeParse:
    tz = tz or timezone()
    current = _now(now, tz)
    raw = (text or "")[:MAX_INPUT_LEN].strip()
    if not raw:
        return TimeParse(error=NO_TIME)
    _match, when, error = _extract(raw, current, tz)
    if error:
        return TimeParse(error=error)
    if when is None:
        return TimeParse(error=NO_TIME)
    return TimeParse(when=when)


def _subject(text: str, match) -> str:
    combined = text[:match.start()] + " " + text[match.end():]
    combined = _TRIGGER.sub(" ", combined)
    combined = _FILLER.sub(" ", combined)
    combined = re.sub(r"\s+", " ", combined).strip(" .,;:!?-")
    return combined[:MAX_SUBJECT_LEN].strip()


def parse_intent(text: str, now=None,
                 tz: Optional[ZoneInfo] = None) -> IntentParse:
    """Typed reminder intent: none | error | missing_subject | ok."""
    tz = tz or timezone()
    raw = (text or "")[:MAX_INPUT_LEN].strip()
    if not _TRIGGER.search(raw):
        return IntentParse("none")
    current = _now(now, tz)
    match, when, error = _extract(raw, current, tz)
    if match is None:
        return IntentParse("none")
    if error:
        return IntentParse("error", error=error)
    subject = _subject(raw, match)
    if not subject:
        return IntentParse("missing_subject", error=MISSING_SUBJECT)
    return IntentParse("ok", ReminderIntent(when=when, subject=subject))


def parse_reminder_intent(text: str, now=None,
                          tz: Optional[ZoneInfo] = None) -> Optional[ReminderIntent]:
    """Совместимость: только валидный intent, иначе None."""
    result = parse_intent(text, now=now, tz=tz)
    return result.intent if result.kind == "ok" else None


__all__ = [
    "ReminderNLPError", "TimeParse", "ReminderIntent", "IntentParse",
    "parse_time", "parse_reminder_intent", "parse_intent", "timezone",
    "DEFAULT_TIMEZONE",
]
