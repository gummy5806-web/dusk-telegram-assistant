#!/usr/bin/env python3
"""Чистый reminder-scheduler: bounded delivery cycle и safe stop.

Модуль не импортирует `telegram`. `deliver` и `sync` внедряются. Каждый цикл:
`collect_due_safe` → bounded claim → `validate_delivery` → deliver → CAS
delivered/failed. Никакой network-отправки без validate прямо перед deliver,
никаких vault/DB lock во время await. `CancelledError` не помечается failed.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass

import reminder_ui
import reminders
import runtime_store

logger = logging.getLogger(__name__)

MAX_IDLE_SECONDS = 30.0
MAX_EVENTS_PER_CYCLE = 20
LEASE_SECONDS = 120
SYNC_MESSAGE = "telegram: напоминание"
_UTC = datetime.timezone.utc


@dataclass(frozen=True)
class CycleResult:
    due: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    errors: tuple = ()


def _ensure_utc(now=None) -> datetime.datetime:
    moment = now or datetime.datetime.now(_UTC)
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise runtime_store.RuntimeStoreError("now: нужна aware-метка")
    return moment.astimezone(_UTC)


def _parse_utc(value) -> datetime.datetime:
    moment = datetime.datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(_UTC)


class ReminderScheduler:
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

    async def run_cycle(self, now=None) -> CycleResult:
        errors = []
        due = delivered = failed = skipped = 0
        try:
            events = await asyncio.to_thread(
                reminders.collect_due_safe, self.vault, self.store, now)
        except (reminders.ReminderError, runtime_store.RuntimeStoreError) as error:
            logger.warning("reminder collect failed: %s", type(error).__name__)
            errors.append(type(error).__name__)
            events = ()
        due = len(events)

        for _ in range(max(0, self.max_events)):
            try:
                event = await asyncio.to_thread(
                    self.store.claim_reminder_event, now, self.lease_seconds)
            except runtime_store.RuntimeStoreError as error:
                logger.warning("reminder claim failed: %s", type(error).__name__)
                errors.append(type(error).__name__)
                break
            if event is None:
                break
            try:
                check = await asyncio.to_thread(
                    reminders.validate_delivery, self.vault, self.store,
                    event, now)
            except (reminders.ReminderError,
                    runtime_store.RuntimeStoreError) as error:
                logger.warning("reminder validate failed: %s",
                               type(error).__name__)
                errors.append(type(error).__name__)
                skipped += 1
                continue
            if not check.ok or check.reminder is None:
                skipped += 1
                continue

            title = "задача"
            try:
                title = await asyncio.to_thread(
                    reminder_ui.title_for, self.vault,
                    check.reminder.task_path, "задача")
            except Exception as error:  # название не должно ломать доставку
                logger.warning("reminder title failed: %s", type(error).__name__)
            notification = reminder_ui.notification(
                check.reminder, event.event_id, title, now=now)
            try:
                await self._deliver(notification)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # сбой доставки не роняет процесс
                logger.warning("reminder deliver failed: %s", type(error).__name__)
                failed += 1
                try:
                    await asyncio.to_thread(
                        self.store.mark_reminder_event_failed,
                        event.event_id, event.attempts, now)
                except runtime_store.RuntimeStoreError as mark_error:
                    logger.warning("reminder mark_failed failed: %s",
                                   type(mark_error).__name__)
                continue
            try:
                stored = await asyncio.to_thread(
                    self.store.mark_reminder_event_delivered,
                    event.event_id, event.attempts, now)
                if stored:
                    delivered += 1
            except runtime_store.RuntimeStoreError as error:
                logger.warning("reminder mark_delivered failed: %s",
                               type(error).__name__)
                errors.append(type(error).__name__)

        return CycleResult(due=due, delivered=delivered, failed=failed,
                           skipped=skipped, errors=tuple(errors))

    def next_wait(self, now=None) -> float:
        moment = _ensure_utc(now)
        candidates = []
        try:
            active = self.store.next_active_reminder_time()
        except runtime_store.RuntimeStoreError:
            active = ""
        if active:
            candidates.append(active)
        try:
            events = self.store.pending_reminder_events()
        except runtime_store.RuntimeStoreError:
            events = ()
        for event in events:
            if event.state == runtime_store.EVENT_LEASED and event.lease_until:
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
                logger.error("reminder cycle error: %s", type(error).__name__)
            wait = await asyncio.to_thread(self.next_wait)
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
    "ReminderScheduler", "CycleResult",
    "MAX_IDLE_SECONDS", "MAX_EVENTS_PER_CYCLE", "LEASE_SECONDS", "SYNC_MESSAGE",
]
