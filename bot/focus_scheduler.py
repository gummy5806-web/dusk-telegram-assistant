#!/usr/bin/env python3
"""Чистый focus-scheduler: bounded cycle и доставка уведомлений.

Модуль не знает про `telegram`: `deliver` и `sync` внедряются. Один цикл:

1. `focus.expire_due` — закрыть просроченное (атомарно с событием);
2. `store.claim_event` — забрать событие под lease с generation;
3. `await deliver(notification)` — отправить;
4. `mark_event_delivered` / `mark_event_failed` по исходу.

`CancelledError` не помечается failed. Stale-worker safety обеспечивает
generation из runtime store. Цикл не busy-spin: следующий wait считает
`next_wait` по ближайшему дедлайну/backoff и ограничен `max_idle`.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from pathlib import Path

import focus
import focus_ui
import runtime_store
import task_metadata

logger = logging.getLogger(__name__)

MAX_IDLE_SECONDS = 30.0
MAX_EVENTS_PER_CYCLE = 20
LEASE_SECONDS = 120
SYNC_MESSAGE = "telegram: фокус"
_UTC = datetime.timezone.utc


@dataclass(frozen=True)
class FocusNotification:
    session_id: str
    text: str
    buttons: tuple


@dataclass(frozen=True)
class CycleResult:
    expired: int = 0
    delivered: int = 0
    failed: int = 0
    synced: bool = False
    errors: tuple = ()


def _ensure_utc(now=None) -> datetime.datetime:
    moment = now or datetime.datetime.now(_UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(_UTC)


def _parse_utc(value) -> datetime.datetime:
    moment = datetime.datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(_UTC)


class FocusScheduler:
    def __init__(self, *, vault, store, deliver, sync,
                 max_idle: float = MAX_IDLE_SECONDS,
                 max_events: int = MAX_EVENTS_PER_CYCLE,
                 lease_seconds: int = LEASE_SECONDS):
        self.vault = vault
        self.store = store
        self._deliver = deliver
        self._sync = sync
        self.max_idle = float(max_idle)
        self.max_events = int(max_events)
        self.lease_seconds = int(lease_seconds)

    # ── уведомление ──────────────────────────────────────────────

    def title_for(self, task_path) -> str:
        title = ""
        if task_path:
            try:
                title = task_metadata.read_task(self.vault, task_path).title
            except task_metadata.TaskMetadataError:
                title = ""
        if title:
            return title
        name = Path(str(task_path)).name if task_path else ""
        return name or "задача"

    def build_notification(self, session_id: str) -> FocusNotification:
        task_path = ""
        try:
            record = self.store.get_focus(session_id)
        except runtime_store.RuntimeStoreError:
            record = None
        if record is not None:
            task_path = record.task_path
        return FocusNotification(
            session_id=session_id,
            text=focus_ui.expiry_text(self.title_for(task_path)),
            buttons=focus_ui.expiry_buttons(session_id))

    # ── цикл ─────────────────────────────────────────────────────

    async def run_cycle(self, now=None) -> CycleResult:
        errors = []
        expired = delivered = failed = 0
        synced = False

        expire = None
        try:
            expire = focus.expire_due(self.vault, self.store, now=now)
        except (focus.FocusError, runtime_store.RuntimeStoreError) as error:
            logger.warning("focus expire failed: %s", type(error).__name__)
            errors.append(type(error).__name__)
        if expire is not None:
            expired = len(expire.expired)

        for _ in range(max(0, self.max_events)):
            try:
                event = self.store.claim_event(now=now,
                                               lease_seconds=self.lease_seconds)
            except runtime_store.RuntimeStoreError as error:
                logger.warning("focus claim failed: %s", type(error).__name__)
                errors.append(type(error).__name__)
                break
            if event is None:
                break
            try:
                await self._deliver(self.build_notification(event.session_id))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # сбой доставки не роняет процесс
                logger.warning("focus deliver failed: %s", type(error).__name__)
                failed += 1
                try:
                    self.store.mark_event_failed(event.event_id, event.attempts,
                                                 now=now)
                except runtime_store.RuntimeStoreError as mark_error:
                    logger.warning("focus mark_failed failed: %s",
                                   type(mark_error).__name__)
                continue
            try:
                if self.store.mark_event_delivered(event.event_id, event.attempts,
                                                   now=now):
                    delivered += 1
            except runtime_store.RuntimeStoreError as error:
                logger.warning("focus mark_delivered failed: %s",
                               type(error).__name__)
                errors.append(type(error).__name__)

        # Доставка важнее sync: медленный git не задерживает «Время вышло».
        if expire is not None and expire.sync_required:
            try:
                await asyncio.to_thread(self._sync, SYNC_MESSAGE)
                synced = True
            except asyncio.CancelledError:
                raise
            except Exception as error:  # ошибка git не роняет цикл
                logger.warning("focus sync failed: %s", type(error).__name__)
                errors.append(type(error).__name__)

        return CycleResult(expired=expired, delivered=delivered, failed=failed,
                           synced=synced, errors=tuple(errors))

    def next_wait(self, now=None) -> float:
        moment = _ensure_utc(now)
        candidates = []
        try:
            active = self.store.get_active_focus()
        except runtime_store.RuntimeStoreError:
            active = None
        if active is not None:
            candidates.append(active.planned_end)
        try:
            events = self.store.pending_events()
        except runtime_store.RuntimeStoreError:
            events = ()
        for event in events:
            if event.status == runtime_store.EVENT_LEASED and event.lease_until:
                candidates.append(event.lease_until)
            elif event.next_attempt_at:
                candidates.append(event.next_attempt_at)

        wait = self.max_idle
        for value in candidates:
            try:
                delta = (_parse_utc(value) - moment).total_seconds()
            except (TypeError, ValueError):
                continue
            wait = min(wait, max(0.0, delta))
        return max(0.0, min(self.max_idle, wait))

    async def run(self, stop_event, wake_event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # bounded retry, без падения процесса
                logger.error("focus cycle error: %s", type(error).__name__)
            wait = self.next_wait()
            try:
                await asyncio.wait_for(wake_event.wait(), timeout=wait)
                wake_event.clear()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    def stop(self, stop_event, wake_event) -> None:
        stop_event.set()
        wake_event.set()


__all__ = [
    "FocusScheduler", "FocusNotification", "CycleResult",
    "MAX_IDLE_SECONDS", "MAX_EVENTS_PER_CYCLE", "LEASE_SECONDS", "SYNC_MESSAGE",
]
