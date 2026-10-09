#!/usr/bin/env python3
"""Чистый focus-UI: меню длительностей, активная карточка, expiry-действия.

Модуль не знает про `telegram`, сеть и git. Он держит короткие ephemeral-токены
в `context.user_data`, рисует `task_ui.View` и возвращает `FocusAction` с
сигналом `sync_required` — транспорт решает, когда звать git sync и как
редактировать сообщение.

Callback namespace — `q:`, отдельный от `f:` (хаб мыслей). В callback_data
никогда нет путей, заголовков или пользовательского текста: только короткие
opaque-токены и bounded session id.
"""

from __future__ import annotations

import datetime
import math
import os
import secrets
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import focus
import runtime_store
import task_metadata
import task_ui as ui

FOCUS_NS = "q"
MENU_KEY = "focus_menus"
EXTEND_KEY = "focus_extends"
MENU_REQUEST_BYTES = 8
EXPIRY_EXTEND_MINUTES = 15
_UTC = datetime.timezone.utc

DURATION_LABELS = (
    (15, "15 мин"),
    (25, "25 мин"),
    (45, "45 мин"),
    (60, "1 ч"),
    (90, "1 ч 30 мин"),
)


@dataclass(frozen=True)
class FocusAction:
    """Результат действия: готовая View + нужен ли git sync."""

    view: "ui.View"
    sync_required: bool = False


@dataclass(frozen=True)
class StatusOutcome:
    """Итог status-кнопки: view + что именно реально изменилось."""

    view: "ui.View"
    changed: bool = False
    stopped: bool = False

    @property
    def sync_required(self) -> bool:
        return self.changed or self.stopped


# ── время и локализация ─────────────────────────────────────────────────────

def timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("DUSK_TIMEZONE", "Asia/Almaty"))


def _utc(now=None) -> datetime.datetime:
    moment = now or datetime.datetime.now(_UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(_UTC)


def _parse_utc(value) -> datetime.datetime:
    moment = datetime.datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(_UTC)


def remaining_minutes(value, now=None) -> int:
    """Сколько минут до дедлайна, округляя вверх; минимум 0."""
    seconds = (_parse_utc(value) - _utc(now)).total_seconds()
    if seconds <= 0:
        return 0
    return max(0, math.ceil(seconds / 60))


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
    store = _menu_store(state)
    return store.get((token or "").strip())


def _extend_store(state) -> dict:
    store = state.get(EXTEND_KEY)
    if not isinstance(store, dict):
        store = {}
        state[EXTEND_KEY] = store
    return store


def issue_extend_token(state, session_id, path) -> str:
    store = _extend_store(state)
    token = secrets.token_hex(4)
    while token in store:
        token = secrets.token_hex(4)
    store[token] = {"session": session_id, "path": str(path),
                    "request": secrets.token_hex(MENU_REQUEST_BYTES)}
    _bound(store)
    return token


def resolve_extend_token(state, token):
    store = _extend_store(state)
    return store.get((token or "").strip())


# ── общие ответы ────────────────────────────────────────────────────────────

def stale_action() -> FocusAction:
    return FocusAction(
        ui.View("⚠️ Кнопка устарела — открой «📋 Задачи» заново.", [], stale=True))


def error_action() -> FocusAction:
    return FocusAction(ui.View(
        "⚠️ Состояние фокуса недоступно — попробуй позже.", [], stale=True))


def _message_action(text: str) -> FocusAction:
    return FocusAction(ui.View(text, []))


def _path_key(vault, value) -> str:
    try:
        return str(task_metadata.resolve_task_path(vault, value).resolve())
    except task_metadata.TaskMetadataError:
        return ""


def _same_path(vault, first, second) -> bool:
    key = _path_key(vault, first)
    return bool(key) and key == _path_key(vault, second)


def _safe_session_id(value) -> str:
    text = str(value or "")
    return text if focus.is_valid_session_id(text) else ""


# ── отрисовка ───────────────────────────────────────────────────────────────

def _active_view(state, record, now=None) -> "ui.View":
    if not _safe_session_id(record.session_id):
        return ui.View("⚠️ Фокус повреждён — открой «📋 Задачи» заново.", [],
                       stale=True)
    deadline = _parse_utc(record.planned_end)
    local = deadline.astimezone(timezone())
    remaining = remaining_minutes(record.planned_end, now=now)
    text = (f"⏱ Фокус: {record.planned_minutes} мин · до {local:%H:%M} · "
            f"осталось {remaining} мин")
    token = issue_extend_token(state, record.session_id, record.task_path)
    rows = [[("⏹ Закончить", f"{FOCUS_NS}:x:{record.session_id}"),
             ("➕15", f"{FOCUS_NS}:e:{token}")]]
    return ui.View(text, rows)


def _active_for_path(store, vault, path):
    if store is None:
        return None
    try:
        active = store.get_active_focus()
    except runtime_store.RuntimeStoreError:
        return None
    if active is not None and _same_path(vault, path, active.task_path):
        return active
    return None


def card_focus(state, vault, store, task_token, now=None) -> "ui.View":
    """Рендер фокус-части карточки: активный блок или кнопка `⏱ Фокус`."""
    path = ui.resolve_token(state, task_token)
    if not path:
        return ui.View("", [])
    record = _active_for_path(store, vault, path)
    if record is not None:
        return _active_view(state, record, now)
    return ui.View("", [[("⏱ Фокус", f"{FOCUS_NS}:d:{task_token}")]])


def card_view(state, vault, store, path, now=None) -> "ui.View":
    """Полная карточка задачи с актуальной фокус-частью."""
    token = ui.issue_token(state, str(path))
    return ui.render_card(state, vault, token,
                          focus=card_focus(state, vault, store, token, now))


# ── действия ────────────────────────────────────────────────────────────────

def begin_duration_menu(state, vault, task_token, now=None) -> FocusAction:
    path = ui.resolve_token(state, task_token)
    if not path:
        return stale_action()
    try:
        task_metadata.resolve_task_path(vault, path)
    except task_metadata.TaskMetadataError:
        return stale_action()
    menu_token = issue_menu_token(state, path)
    items = [(label, f"{FOCUS_NS}:s:{menu_token}:{minutes}")
             for minutes, label in DURATION_LABELS]
    rows = [items[index:index + 2] for index in range(0, len(items), 2)]
    rows.append([("⬅️ Назад", f"{FOCUS_NS}:b:{task_token}")])
    return FocusAction(ui.View("⏱ Фокус — выбери длительность", rows))


def start_from_menu(state, vault, store, menu_token, minutes, now=None) -> FocusAction:
    record = resolve_menu_token(state, menu_token)
    if record is None:
        return stale_action()
    if store is None:
        return error_action()
    try:
        duration = int(minutes)
    except (TypeError, ValueError):
        return stale_action()
    try:
        result = focus.start_focus(vault, record["path"], duration, store,
                                   idempotency_key=record["request"], now=now)
    except focus.FocusError as error:
        return _message_action(f"⚠️ {error}")
    except runtime_store.RuntimeStoreError:
        return error_action()
    return FocusAction(card_view(state, vault, store, record["path"], now),
                       result.sync_required)


def extend_from_token(state, vault, store, token, now=None) -> FocusAction:
    record = resolve_extend_token(state, token)
    if record is None:
        return stale_action()
    if store is None:
        return error_action()
    try:
        result = focus.extend_focus(vault, record["session"], store,
                                    idempotency_key=record["request"], now=now)
    except focus.FocusError as error:
        return _message_action(f"⚠️ {error}")
    except runtime_store.RuntimeStoreError:
        return error_action()
    return FocusAction(card_view(state, vault, store, record["path"], now),
                       result.sync_required)


def stop_active(state, vault, store, session_id, now=None) -> FocusAction:
    if not _safe_session_id(session_id):
        return stale_action()
    if store is None:
        return error_action()
    try:
        record = store.get_focus(session_id)
    except runtime_store.RuntimeStoreError:
        return error_action()
    if record is None:
        return stale_action()
    try:
        result = focus.stop_focus(vault, session_id, store, now=now)
    except focus.FocusError:
        return stale_action()
    except runtime_store.RuntimeStoreError:
        return error_action()
    return FocusAction(card_view(state, vault, store, record.task_path, now),
                       result.sync_required)


def stop_for_done(vault, store, task_path) -> bool:
    """Идемпотентно остановить активный фокус на задаче перед status=done.

    True — Markdown реально изменился (focus остановлен), False — активного
    фокуса на этой задаче нет. `FocusError`/`RuntimeStoreError` поднимаются:
    доказать безопасность нельзя, status mutation обязана НЕ выполняться.
    """
    if store is None or not task_path:
        raise focus.FocusError("состояние фокуса недоступно")
    active = store.get_active_focus()
    if active is None or not _same_path(vault, task_path, active.task_path):
        return False
    result = focus.stop_focus(vault, active.session_id, store)
    return result.sync_required


def status_with_focus(state, vault, store, task_token, target,
                      now=None, reminder=None) -> StatusOutcome:
    """Единый путь status-кнопки. Для `done` сначала безопасно остановить focus.

    Если остановить не удалось — исключение поднимается до status mutation.
    """
    path = ui.resolve_token(state, task_token)
    stopped = False
    if target == "done" and path:
        stopped = stop_for_done(vault, store, path)
    focus_view = card_focus(state, vault, store, task_token, now=now)
    view, changed = ui.apply_status(state, vault, task_token, target,
                                    sync=None, focus=focus_view,
                                    reminder=reminder)
    return StatusOutcome(view=view, changed=changed, stopped=stopped)


def done_from_expiry(state, vault, store, session_id, now=None) -> FocusAction:
    if not _safe_session_id(session_id):
        return stale_action()
    if store is None:
        return error_action()
    try:
        record = store.get_focus(session_id)
    except runtime_store.RuntimeStoreError:
        return error_action()
    if record is None:
        return stale_action()
    try:
        path = task_metadata.resolve_task_path(vault, record.task_path)
    except task_metadata.TaskMetadataError:
        return stale_action()
    # Сначала успешно остановить возможный новый active focus на этой task.
    try:
        stopped = stop_for_done(vault, store, record.task_path)
    except (focus.FocusError, runtime_store.RuntimeStoreError):
        return error_action()
    import dispatch  # тяжёлый модуль — только при реальной мутации
    try:
        changed, _message = dispatch.set_task_status(path, "done", now=now,
                                                     vault=vault)
    except Exception:
        return error_action()
    return FocusAction(card_view(state, vault, store, record.task_path, now),
                       stopped or changed)


def extend_from_expiry(state, vault, store, session_id, callback_data,
                       now=None) -> FocusAction:
    if not _safe_session_id(session_id):
        return stale_action()
    if store is None:
        return error_action()
    try:
        record = store.get_focus(session_id)
    except runtime_store.RuntimeStoreError:
        return error_action()
    if record is None:
        return stale_action()
    try:
        result = focus.start_focus(vault, record.task_path, EXPIRY_EXTEND_MINUTES,
                                   store, idempotency_key=callback_data, now=now)
    except focus.FocusError as error:
        return _message_action(f"⚠️ {error}")
    except runtime_store.RuntimeStoreError:
        return error_action()
    return FocusAction(card_view(state, vault, store, record.task_path, now),
                       result.sync_required)


def dismiss_expiry(state, vault, store, session_id, now=None) -> FocusAction:
    if not _safe_session_id(session_id):
        return stale_action()
    if store is None:
        return error_action()
    try:
        record = store.get_focus(session_id)
    except runtime_store.RuntimeStoreError:
        return error_action()
    if record is None:
        return stale_action()
    return FocusAction(card_view(state, vault, store, record.task_path, now), False)


def expiry_buttons(session_id) -> tuple:
    """Кнопки уведомления об истечении. Unsafe session id → без inline-кнопок."""
    safe = _safe_session_id(session_id)
    if not safe:
        return ()
    return (
        (("✅ Задача готова", f"{FOCUS_NS}:g:{safe}"),),
        (("➕ Ещё 15", f"{FOCUS_NS}:E:{safe}"),),
        (("⏹ Закончил блок", f"{FOCUS_NS}:z:{safe}"),),
    )


def expiry_text(title: str) -> str:
    return (f"⏰ Время вышло: {title}\n"
            "Фокус-блок закрыт, задача не закрыта.")


__all__ = [
    "FocusAction", "StatusOutcome", "card_focus", "card_view",
    "begin_duration_menu", "start_from_menu", "extend_from_token",
    "stop_active", "stop_for_done", "status_with_focus", "done_from_expiry",
    "extend_from_expiry", "dismiss_expiry", "expiry_buttons", "expiry_text",
    "remaining_minutes", "stale_action", "error_action", "timezone",
]
