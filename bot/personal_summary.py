#!/usr/bin/env python3
"""Детерминированная личная сводка Dusk: только локальные факты, без LLM/сети.

Один read-only проход по разрешённым источникам (TaskNotes-карточки, Inbox,
fleeting-заметки) даёт компактную картину личного цикла: задачи, фокус,
напоминания, Inbox, мысли и применение советов. Модуль ничего не пишет, не
reconcile'ит и не ходит в сеть.

Markdown — source of truth: активный фокус и напоминания выводятся из
`timeEntries`/`reminders` карточек и считаются только для валидных Dusk-сессий.
SQLite — projection: его отсутствие/повреждение изолируется в одной секции
(`не удалось прочитать`) и не превращается в честный ноль и не ломает сводку.

Research Brain и приватные каталоги не читаются. Никаких productivity-score и
норм: только факты.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import focus
import learning_cycle
import reminders
import runtime_store
import task_metadata
import thought_feed
import weekly_plan

SCAN_LIMIT = 2000
MAX_ACTUAL_MINUTES = 24 * 60
MAX_CHAIN_ROWS = 5
MAX_RENDER_CHARS = 3800

SECTION_INBOX = "inbox"
SECTION_THOUGHTS = "мысли"
SECTION_TASKS = "задачи"
SECTION_FOCUS = "фокус"
SECTION_REMINDERS = "напоминания"
SECTION_KNOWLEDGE = "применение советов"

UNAVAILABLE = "не удалось прочитать"


# ── typed facts ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InboxFacts:
    total: int
    thoughts: int = 0
    recent: tuple = ()


@dataclass(frozen=True)
class ThoughtFacts:
    recent: int
    forgotten_title: str = ""
    forgotten_state: str = "none"      # none | found | unavailable


@dataclass(frozen=True)
class TaskFacts:
    open: int
    overdue: int
    closed_week: int


@dataclass(frozen=True)
class FocusFacts:
    today_sessions: int
    today_planned_minutes: int
    today_actual_minutes: int
    week_sessions: int
    week_planned_minutes: int
    week_actual_minutes: int
    active_task: str = ""
    active_deadline: str = ""
    active_minutes_left: int = 0
    active_overdue: bool = False


@dataclass(frozen=True)
class ReminderFacts:
    active: int
    next_title: str = ""
    next_local: str = ""


@dataclass(frozen=True)
class KnowledgeFacts:
    active: int
    done_week: int
    observed: int
    missing: int
    chain: tuple = ()


@dataclass(frozen=True)
class PersonalSummary:
    today: str
    week_start: str
    week_end: str
    skipped_cards: int = 0
    inbox: Optional[InboxFacts] = None
    thoughts: Optional[ThoughtFacts] = None
    tasks: Optional[TaskFacts] = None
    focus: Optional[FocusFacts] = None
    reminders: Optional[ReminderFacts] = None
    knowledge: Optional[KnowledgeFacts] = None
    errors: tuple = ()


# ── helpers ─────────────────────────────────────────────────────────────────

def _aware_now(now=None) -> datetime.datetime:
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if not isinstance(now, datetime.datetime):
        raise ValueError("now: нужен datetime")
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        return weekly_plan.to_almaty(now)
    return now.astimezone(datetime.timezone.utc)


def _aware(value):
    text = (value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        return None
    return moment


def _scan_tasks(vault: Path):
    """Bounded-скан direct TaskNotes cards. Malformed карточки пропускаются."""
    rows = []
    skipped = 0
    for path in task_metadata.direct_task_paths(vault, SCAN_LIMIT):
        try:
            rows.append(task_metadata.read_task(vault, path))
        except task_metadata.TaskMetadataError:
            skipped += 1
    return rows, skipped


def _safe(errors, name, function):
    try:
        return function()
    except (OSError, UnicodeDecodeError, ValueError,
            runtime_store.RuntimeStoreError, task_metadata.TaskMetadataError,
            focus.FocusError, reminders.ReminderError,
            thought_feed.InboxReadError):
        errors.append(name)
        return None


# ── sections ────────────────────────────────────────────────────────────────

def _task_facts(rows, today: datetime.date, week_start: datetime.date,
                week_end: datetime.date) -> TaskFacts:
    upper = min(week_end, today)
    open_count = 0
    overdue = 0
    closed = 0
    for snapshot in rows:
        if snapshot.status == "done":
            day = weekly_plan.parse_task_date(snapshot.completed)
            if day is not None and week_start <= day <= upper:
                closed += 1
            continue
        open_count += 1
        due = weekly_plan.parse_task_date(snapshot.due)
        if due is not None and due < today:
            overdue += 1
    return TaskFacts(open_count, overdue, closed)


def _focus_facts(vault: Path, rows, today: datetime.date,
                 week_start: datetime.date,
                 now: datetime.datetime) -> FocusFacts:
    today_sessions = today_planned = today_actual = 0
    week_sessions = week_planned = week_actual = 0
    titles = {snapshot.relpath: snapshot.title for snapshot in rows}
    scanned = focus.scan_sessions(vault)
    opened = [item for item in scanned if item.entry.is_open()]
    if len(opened) > 1:
        raise ValueError("несколько активных Dusk focus sessions")
    active = None
    for item in scanned:
        entry = item.entry
        start = _aware(entry.start_time)
        if start is None:
            continue                      # invalid closed history is not a fact
        start_day = weekly_plan.to_almaty(start).date()
        planned_end = _aware(entry.planned_end)
        planned = entry.planned_minutes
        if planned <= 0 and planned_end is not None and planned_end > start:
            planned = int((planned_end - start).total_seconds() // 60)
        planned = max(0, planned)
        if entry.is_open():
            # The public core scanner already validated aware start/deadline,
            # ordering, callback-safe id and duplicate ids.
            if planned_end is None:
                raise ValueError("активный focus без plannedEnd")
            title = titles.get(item.relpath) or Path(item.relpath).stem
            active = (start, planned_end, title)
            actual = 0
        else:
            end = _aware(entry.end_time)
            if end is None or end < start:
                continue
            actual = min(int((end - start).total_seconds() // 60),
                         MAX_ACTUAL_MINUTES)
        if start_day == today:
            today_sessions += 1
            today_planned += planned
            today_actual += actual
        if week_start <= start_day <= today:
            week_sessions += 1
            week_planned += planned
            week_actual += actual

    active_task = active_deadline = ""
    active_left = 0
    active_overdue = False
    if active is not None:
        _start, planned_end, title = active
        active_task = title
        if planned_end is not None:
            active_deadline = weekly_plan.to_almaty(planned_end).strftime(
                "%d.%m %H:%M")
            active_overdue = planned_end <= now
            if not active_overdue:
                active_left = max(
                    0, math.ceil((planned_end - now).total_seconds() / 60))
    return FocusFacts(today_sessions, today_planned, today_actual,
                      week_sessions, week_planned, week_actual,
                      active_task, active_deadline, active_left, active_overdue)


def _reminder_facts(vault: Path, rows, now: datetime.datetime) -> ReminderFacts:
    active = 0
    next_when = None
    next_title = ""
    titles = {snapshot.relpath: snapshot.title for snapshot in rows}
    for item in reminders.scan_reminders(vault):
        if item.status == "done":
            continue
        when = _aware(item.entry.absolute_time)
        if when is None:
            raise ValueError("Dusk reminder без aware absoluteTime")
        if when <= now:
            continue
        active += 1
        if next_when is None or when < next_when:
            next_when = when
            next_title = titles.get(item.relpath) or Path(item.relpath).stem
    local = (weekly_plan.to_almaty(next_when).strftime("%d.%m %H:%M")
             if next_when is not None else "")
    return ReminderFacts(active, next_title, local)


def _inbox_facts(vault: Path) -> InboxFacts:
    counts = thought_feed.inbox_counts(vault)
    items = thought_feed.inbox_thoughts(vault, limit=SCAN_LIMIT)
    return InboxFacts(counts.total, counts.thoughts,
                      tuple(item.title for item in items[:3]))


def _thought_facts(vault: Path, store, now: datetime.datetime) -> ThoughtFacts:
    recent = thought_feed.recent_thoughts(vault, limit=SCAN_LIMIT)
    if store is None:
        # Without the runtime projection we cannot prove hide/cooldown state.
        return ThoughtFacts(len(recent), "", "unavailable")
    try:
        forgotten = thought_feed.forgotten_thought(vault, store=store, now=now)
    except runtime_store.RuntimeStoreError:
        return ThoughtFacts(len(recent), "", "unavailable")
    if forgotten is None:
        return ThoughtFacts(len(recent), "", "none")
    return ThoughtFacts(len(recent), forgotten.title, "found")


def _knowledge_facts(vault: Path, today: datetime.date) -> KnowledgeFacts:
    review = learning_cycle.application_rows(vault, today=today)
    counts = review["counts"]
    chain = []
    for row in review["rows"]:
        if not (row["done"] or row["active"]):
            continue
        source = (f"[[{row['source_link']}]]"
                  if row["source_ok"] else "источник не найден")
        result = (row["observed_state"] if row["has_observation"]
                  else "наблюдения нет")
        chain.append((source, f"[[{row['task_link']}]]", result))
    return KnowledgeFacts(counts["active"], counts["done"], counts["observed"],
                          counts["no_data"], tuple(chain[:MAX_CHAIN_ROWS]))


def collect(vault, store=None, now=None, today=None) -> PersonalSummary:
    """Read-only сводка. Каждая секция изолирована: сбой одной не ломает остальные.

    `today` (owner date) можно передать, чтобы не звать `today_local` повторно.
    """
    vault = Path(vault)
    moment = _aware_now(now)
    today = today or weekly_plan.today_local(moment)
    week_start_iso, week_end_iso = weekly_plan.week_bounds(today)
    week_start = datetime.date.fromisoformat(week_start_iso)
    week_end = datetime.date.fromisoformat(week_end_iso)
    errors = []

    rows = None
    try:
        rows, skipped = _scan_tasks(vault)
    except (OSError, UnicodeDecodeError, ValueError,
            task_metadata.TaskMetadataError):
        skipped = 0
        errors.extend((SECTION_TASKS, SECTION_FOCUS, SECTION_REMINDERS))

    if rows is None:
        tasks = focus_facts = reminder_facts = None
    else:
        tasks = _safe(errors, SECTION_TASKS,
                      lambda: _task_facts(rows, today, week_start, week_end))
        focus_facts = _safe(
            errors, SECTION_FOCUS,
            lambda: _focus_facts(vault, rows, today, week_start, moment))
        reminder_facts = _safe(
            errors, SECTION_REMINDERS,
            lambda: _reminder_facts(vault, rows, moment))
    inbox = _safe(errors, SECTION_INBOX, lambda: _inbox_facts(vault))
    thoughts = _safe(errors, SECTION_THOUGHTS,
                     lambda: _thought_facts(vault, store, moment))
    knowledge = _safe(errors, SECTION_KNOWLEDGE,
                      lambda: _knowledge_facts(vault, today))

    return PersonalSummary(
        today=today.isoformat(), week_start=week_start_iso,
        week_end=week_end_iso, skipped_cards=skipped, inbox=inbox,
        thoughts=thoughts, tasks=tasks, focus=focus_facts,
        reminders=reminder_facts, knowledge=knowledge, errors=tuple(errors))


# ── render ──────────────────────────────────────────────────────────────────

def _fit(text: str, limit: int = MAX_RENDER_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


def _human_date(iso: str) -> str:
    try:
        return datetime.date.fromisoformat(iso).strftime("%d.%m")
    except (TypeError, ValueError):
        return iso


def section_lines(summary: PersonalSummary) -> list:
    """Компактные фактические строки для Telegram и недельного обзора."""
    lines = []
    if summary.tasks is None:
        lines.append(f"• Задачи — {UNAVAILABLE}")
    else:
        task = summary.tasks
        lines.append(f"• Задачи — открыто {task.open}, просрочено {task.overdue}, "
                     f"закрыто за неделю {task.closed_week}")

    if summary.inbox is None:
        lines.append(f"• Inbox — {UNAVAILABLE}")
    else:
        lines.append(f"• Inbox — необработанных {summary.inbox.total}, "
                     f"из них мыслей {summary.inbox.thoughts}")

    if summary.thoughts is None:
        lines.append(f"• Мысли — {UNAVAILABLE}")
    else:
        thought = summary.thoughts
        if thought.forgotten_state == "found":
            forgotten = f"забытая — {thought.forgotten_title}"
        elif thought.forgotten_state == "none":
            forgotten = "забытых нет"
        else:
            forgotten = f"забытые — {UNAVAILABLE}"
        lines.append(f"• Мысли — недавних {thought.recent}; {forgotten}")

    if summary.focus is None:
        lines.append(f"• Фокус — {UNAVAILABLE}")
    else:
        focus_facts = summary.focus
        lines.append(f"• Фокус сегодня — сессий {focus_facts.today_sessions}, "
                     f"план {focus_facts.today_planned_minutes} мин, "
                     f"факт {focus_facts.today_actual_minutes} мин")
        lines.append(f"• Фокус за неделю — сессий {focus_facts.week_sessions}, "
                     f"план {focus_facts.week_planned_minutes} мин, "
                     f"факт {focus_facts.week_actual_minutes} мин")
        if focus_facts.active_task:
            if focus_facts.active_overdue:
                state = f"дедлайн {focus_facts.active_deadline} прошёл"
            else:
                state = (f"до {focus_facts.active_deadline}, "
                         f"осталось {focus_facts.active_minutes_left} мин")
            lines.append(f"  ⏱ Активный фокус — {focus_facts.active_task} · {state}")

    if summary.reminders is None:
        lines.append(f"• Напоминания — {UNAVAILABLE}")
    else:
        reminder = summary.reminders
        if reminder.active and reminder.next_title:
            lines.append(f"• Напоминания — активных {reminder.active}; "
                         f"ближайшее {reminder.next_local} — {reminder.next_title}")
        else:
            lines.append(f"• Напоминания — активных {reminder.active}")

    if summary.knowledge is None:
        lines.append(f"• Применение советов — {UNAVAILABLE}")
    else:
        know = summary.knowledge
        lines.append(f"• Применение советов — активно {know.active}, "
                     f"закрыто за неделю {know.done_week}, "
                     f"наблюдение {know.observed}, без данных {know.missing}")
        if know.chain:
            lines.append("  источник → применение → результат")
            for source, task, result in know.chain:
                lines.append(f"  - {source} → {task} → {result}")

    if summary.skipped_cards:
        lines.append(f"  (карточек не удалось прочитать: {summary.skipped_cards})")
    return lines


def render(summary: PersonalSummary, limit: int = MAX_RENDER_CHARS) -> str:
    lines = [f"📊 Сводка — {_human_date(summary.today)}", ""]
    lines += section_lines(summary)
    if summary.errors:
        lines += ["", "Не удалось прочитать: " + ", ".join(sorted(set(summary.errors)))]
    return _fit("\n".join(lines), limit)


__all__ = [
    "PersonalSummary", "InboxFacts", "ThoughtFacts", "TaskFacts", "FocusFacts",
    "ReminderFacts", "KnowledgeFacts", "collect", "render", "section_lines",
    "MAX_RENDER_CHARS", "SCAN_LIMIT", "UNAVAILABLE",
]
