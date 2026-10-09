#!/usr/bin/env python3
"""Чистый reminder-UI: меню, карточка, custom-ввод, действия уведомления.

Модуль не импортирует `telegram`, не ходит в сеть и git. Он держит короткие
ephemeral-токены в `context.user_data`, строит `task_ui.View` и возвращает
`ReminderAction` с сигналом `sync_required`. Callback namespace — `r:`.
"""

from __future__ import annotations

import datetime
import secrets
from dataclasses import dataclass
from typing import Optional

import focus
import focus_ui
import reminder_nlp
import reminders
import runtime_store
import task_metadata
import task_ui as ui

CALLBACK_NS = "r"
MENU_KEY = "reminder_menus"
INPUT_KEY = "reminder_input"
MENU_REQUEST_BYTES = 8
PRESET_MINUTES = {"15": 15, "60": 60, "180": 180}
PRESET_LABELS = (("15", "15 мин"), ("60", "1 час"), ("180", "3 часа"),
                 ("tomorrow9", "Завтра 09:00"))
CUSTOM_PROMPT = ("Напиши, когда напомнить — например: через 2 часа, "
                 "завтра в 9, 12.10 в 14:30.")
UTC = datetime.timezone.utc


class ReminderUIError(RuntimeError):
    """Reminder-действие невозможно."""


@dataclass(frozen=True)
class ReminderAction:
    view: "ui.View"
    sync_required: bool = False


@dataclass(frozen=True)
class ReminderNotification:
    reminder_id: str
    event_id: int
    text: str
    buttons: tuple


def timezone():
    return reminder_nlp.timezone()


def _now(now=None) -> datetime.datetime:
    moment = now or datetime.datetime.now(UTC)
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ReminderUIError("now: нужна aware-метка")
    return moment.astimezone(UTC)


def _parse_utc(value) -> datetime.datetime:
    moment = datetime.datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _utc_instant(value):
    """Aware UTC instant или None (malformed/naive — fail closed)."""
    try:
        moment = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        return None
    return moment.astimezone(UTC)


def _same_instant(first, second) -> bool:
    """Один и тот же aware UTC instant (разные offset считаются равными)."""
    left = _utc_instant(first)
    right = _utc_instant(second)
    return left is not None and right is not None and left == right


def _local(value) -> datetime.datetime:
    return _parse_utc(value).astimezone(timezone())


# ── короткие токены ─────────────────────────────────────────────────────────

def _bound(store: dict) -> None:
    while len(store) > ui.MAX_TOKENS:
        store.pop(next(iter(store)), None)


def _menu_store(state) -> dict:
    store = state.get(MENU_KEY)
    if not isinstance(store, dict):
        store = {}
        state[MENU_KEY] = store
    return store


def issue_menu_token(state, path) -> str:
    store = _menu_store(state)
    token = secrets.token_hex(4)
    while token in store:
        token = secrets.token_hex(4)
    store[token] = {"path": str(path), "request": secrets.token_hex(MENU_REQUEST_BYTES)}
    _bound(store)
    return token


def resolve_menu_token(state, token):
    return _menu_store(state).get((token or "").strip())


def custom_pending(state) -> bool:
    return isinstance(state.get(INPUT_KEY), dict)


def clear_custom(state) -> None:
    state.pop(INPUT_KEY, None)


# ── общие ответы ────────────────────────────────────────────────────────────

def stale_action() -> ReminderAction:
    return ReminderAction(ui.View(
        "⚠️ Кнопка устарела — открой «📋 Задачи» заново.", [], stale=True))


def error_action() -> ReminderAction:
    return ReminderAction(ui.View(
        "⚠️ Состояние недоступно — попробуй позже.", [], stale=True))


def _message_action(text: str) -> ReminderAction:
    return ReminderAction(ui.View(text, []))


# ── меню и создание ─────────────────────────────────────────────────────────

def begin_menu(state, vault, task_token) -> ReminderAction:
    path = ui.resolve_token(state, task_token)
    if not path:
        return stale_action()
    try:
        task_metadata.resolve_task_path(vault, path)
    except task_metadata.TaskMetadataError:
        return stale_action()
    menu_token = issue_menu_token(state, path)
    rows = [
        [(label, f"{CALLBACK_NS}:s:{menu_token}:{preset}")
         for preset, label in PRESET_LABELS[:2]],
        [(label, f"{CALLBACK_NS}:s:{menu_token}:{preset}")
         for preset, label in PRESET_LABELS[2:]],
        [("Другое время", f"{CALLBACK_NS}:o:{menu_token}")],
        [("⬅️ Назад", f"{CALLBACK_NS}:b:{task_token}")],
    ]
    return ReminderAction(ui.View("🔔 Когда напомнить?", rows))


def _preset_when(preset: str, now: datetime.datetime):
    if preset == "tomorrow9":
        base = now.astimezone(timezone()) + datetime.timedelta(days=1)
        return base.replace(hour=9, minute=0, second=0, microsecond=0)
    minutes = PRESET_MINUTES.get(preset)
    if minutes is None:
        return None
    return now + datetime.timedelta(minutes=minutes)


def schedule_preset(state, vault, store, menu_token, preset, now=None) -> ReminderAction:
    record = resolve_menu_token(state, menu_token)
    if record is None:
        return stale_action()
    if store is None:
        return error_action()
    moment = _now(now)
    when = _preset_when(preset, moment)
    if when is None:
        return stale_action()
    try:
        result = reminders.create_reminder(
            vault, record["path"], when, store,
            idempotency_key=f"{record['request']}:{preset}", now=moment)
    except reminders.ReminderError as error:
        return _message_action(f"⚠️ {error}")
    except runtime_store.RuntimeStoreError:
        return error_action()
    return ReminderAction(card_view(state, vault, store, record["path"], now=moment),
                          result.sync_required)


def other_time(state, menu_token) -> ReminderAction:
    record = resolve_menu_token(state, menu_token)
    if record is None:
        return stale_action()
    state[INPUT_KEY] = {"path": record["path"], "request": record["request"]}
    return ReminderAction(ui.View(CUSTOM_PROMPT, [[("✖️ Отмена", "x")]]))


def submit_custom(state, vault, store, text, idempotency_key="",
                  now=None) -> ReminderAction:
    record = state.get(INPUT_KEY)
    if not isinstance(record, dict):
        return stale_action()
    if store is None:
        return error_action()
    moment = _now(now)
    parsed = reminder_nlp.parse_time(text, now=moment)
    if parsed.error:
        return _message_action(f"⚠️ {parsed.error}")
    try:
        result = reminders.create_reminder(
            vault, record["path"], parsed.when, store,
            idempotency_key=idempotency_key or f"{record['request']}:custom",
            now=moment)
    except reminders.ReminderError as error:
        return _message_action(f"⚠️ {error}")
    except runtime_store.RuntimeStoreError:
        return error_action()
    clear_custom(state)
    return ReminderAction(card_view(state, vault, store, record["path"], now=moment),
                          result.sync_required)


# ── карточка ────────────────────────────────────────────────────────────────

def nearest_line(vault, path, now=None) -> str:
    try:
        snapshot = task_metadata.read_task(vault, path)
    except task_metadata.TaskMetadataError:
        return ""
    moment = _now(now)
    best = None
    for entry in snapshot.reminders:
        if not entry.is_dusk():
            continue
        try:
            when = _parse_utc(entry.absolute_time)
        except (TypeError, ValueError):
            continue
        if when <= moment:
            continue
        if best is None or when < best:
            best = when
    if best is None:
        return ""
    local = best.astimezone(timezone())
    return f"🔔 Напоминание: {local:%d.%m %H:%M}"


def card_reminder(state, vault, task_token, now=None) -> "ui.View":
    row = [[("🔔 Напомнить", f"{CALLBACK_NS}:d:{task_token}")]]
    path = ui.resolve_token(state, task_token)
    if not path:
        return ui.View("", row)
    return ui.View(nearest_line(vault, path, now=now), row)


def card_view(state, vault, store, path, now=None) -> "ui.View":
    token = ui.issue_token(state, str(path))
    focus = focus_ui.card_focus(state, vault, store, token, now=now)
    reminder = card_reminder(state, vault, token, now=now)
    return ui.render_card(state, vault, token, focus=focus, reminder=reminder)


# ── уведомление и действия ──────────────────────────────────────────────────

def _parse_event_id(value) -> Optional[int]:
    """Decimal positive bounded SQLite event_id. Иначе None."""
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    number = int(text)
    if number <= 0 or number > 2 ** 63 - 1:
        return None
    return number


def reminder_buttons(event_id) -> tuple:
    """Durable short callbacks по event_id (без reminder_id/generation)."""
    number = _parse_event_id(event_id)
    if number is None:
        return ()
    return (
        (("✅ Готово", f"{CALLBACK_NS}:g:{number}"),),
        (("+15 мин", f"{CALLBACK_NS}:p:{number}"),
         ("+1 час", f"{CALLBACK_NS}:h:{number}")),
        (("Убрать", f"{CALLBACK_NS}:x:{number}"),),
    )


def notification(reminder: reminders.Reminder, event_id, title: str,
                 now=None) -> ReminderNotification:
    local = _local(reminder.absolute_time)
    text = f"⏰ Напоминание: {title}\n{local:%d.%m %H:%M}"
    return ReminderNotification(reminder.reminder_id, event_id, text,
                                reminder_buttons(event_id))


def _notification_context(vault, store, event_id):
    """(event, record, found) или None, если действие уже stale."""
    number = _parse_event_id(event_id)
    if number is None or store is None:
        return None
    event = store.get_reminder_event(number)
    if event is None or event.state not in (runtime_store.EVENT_LEASED,
                                            runtime_store.EVENT_DELIVERED):
        return None
    record = store.get_reminder(event.reminder_id)
    if record is None or record.state == runtime_store.REMINDER_CANCELLED:
        return None
    if record.generation != event.generation:
        return None
    found = None
    try:
        found = reminders.find_reminder(vault, event.reminder_id)
    except reminders.ReminderError:
        return None
    if found is None or found.task_path != record.task_path:
        return None
    # Markdown — source of truth: ручная правка времени (Obsidian) делает старый
    # callback stale до любой мутации, тем же fail-closed подходом, что
    # validate_delivery.
    if not _same_instant(found.absolute_time, record.absolute_time):
        return None
    try:
        snapshot = task_metadata.read_task(vault, record.task_path)
    except task_metadata.TaskMetadataError:
        return None
    if snapshot.status == "done":
        return None
    return event, record, found


def done_action(vault, store, event_id, now=None) -> ReminderAction:
    moment = _now(now)
    if store is None:
        return error_action()
    context = _notification_context(vault, store, event_id)
    if context is None:
        return stale_action()
    _event, record, _found = context
    try:
        task_path = task_metadata.resolve_task_path(vault, record.task_path)
    except task_metadata.TaskMetadataError:
        return stale_action()
    sync = False
    try:
        stopped = focus_ui.stop_for_done(vault, store, record.task_path)
    except (focus.FocusError, runtime_store.RuntimeStoreError):
        return error_action()
    sync = sync or stopped
    import dispatch  # тяжёлый модуль — только при реальной мутации
    try:
        changed, _message = dispatch.set_task_status(
            task_path, "done", now=moment, vault=vault)
    except Exception:
        return ReminderAction(ui.View(
            "⚠️ Не удалось закрыть задачу — попробуй ещё раз."), sync)
    sync = sync or changed
    try:
        cancelled = reminders.cancel_reminder(
            vault, record.reminder_id, store, now=moment).sync_required
    except (reminders.ReminderError, runtime_store.RuntimeStoreError):
        return ReminderAction(ui.View(
            "⚠️ Задача закрыта, но напоминание не убралось — "
            "проверь «📋 Задачи»."), sync)
    sync = sync or cancelled
    return ReminderAction(ui.View("✅ Готово — задача закрыта."), sync)


def snooze_action(vault, store, event_id, delta_minutes, now=None) -> ReminderAction:
    moment = _now(now)
    if store is None:
        return error_action()
    context = _notification_context(vault, store, event_id)
    if context is None:
        return stale_action()
    event, record, found = context
    try:
        base = _parse_utc(found.absolute_time)
    except (TypeError, ValueError):
        return stale_action()
    target = base + datetime.timedelta(minutes=int(delta_minutes))
    if target <= moment:
        target = moment + datetime.timedelta(minutes=int(delta_minutes))
    try:
        result = reminders.snooze_reminder(
            vault, record.reminder_id, target, store,
            idempotency_key=f"snooze-event:{event.event_id}", now=moment)
    except reminders.ReminderError:
        return stale_action()
    except runtime_store.RuntimeStoreError:
        return error_action()
    local = target.astimezone(timezone())
    return ReminderAction(ui.View(f"⏰ Напомню позже — {local:%d.%m %H:%M}"),
                          result.sync_required)


def remove_action(vault, store, event_id, now=None) -> ReminderAction:
    moment = _now(now)
    if store is None:
        return error_action()
    context = _notification_context(vault, store, event_id)
    if context is None:
        return stale_action()
    _event, record, _found = context
    try:
        result = reminders.cancel_reminder(vault, record.reminder_id, store,
                                           now=moment)
    except reminders.ReminderError:
        return stale_action()
    except runtime_store.RuntimeStoreError:
        return error_action()
    return ReminderAction(ui.View("🔕 Напоминание убрано."), result.sync_required)


def natural_action(state, vault, store, intent, idempotency_key="",
                   now=None) -> ReminderAction:
    """«напомни … время» → ровно одна exact-задача + reminder."""
    if store is None:
        return error_action()
    moment = _now(now)
    import dispatch
    try:
        path, created = dispatch.create_task_exact(
            intent.subject, now=moment, vault=vault,
            source="Telegram · напоминание")
    except Exception:
        return error_action()
    if path is None:
        return error_action()
    try:
        result = reminders.create_reminder(
            vault, path, intent.when, store,
            idempotency_key=idempotency_key, now=moment)
    except reminders.ReminderError as error:
        return _message_action(f"⚠️ {error}")
    except runtime_store.RuntimeStoreError:
        return error_action()
    title = _title(vault, path, intent.subject)
    local = intent.when.astimezone(timezone())
    return ReminderAction(ui.View(f"⏰ Напомню: {title} — {local:%d.%m %H:%M}"),
                          bool(created or result.sync_required))


def _title(vault, path, fallback: str) -> str:
    try:
        return task_metadata.read_task(vault, path).title or fallback
    except task_metadata.TaskMetadataError:
        return fallback


def title_for(vault, path, fallback: str = "задача") -> str:
    return _title(vault, path, fallback) if path else fallback


__all__ = [
    "ReminderAction", "ReminderNotification", "ReminderUIError",
    "begin_menu", "schedule_preset", "other_time", "submit_custom",
    "card_reminder", "card_view", "nearest_line",
    "notification", "reminder_buttons", "done_action", "snooze_action",
    "remove_action", "natural_action", "title_for",
    "custom_pending", "clear_custom", "resolve_menu_token", "issue_menu_token",
    "stale_action", "error_action", "timezone",
]
