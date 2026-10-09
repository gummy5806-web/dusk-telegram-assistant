#!/usr/bin/env python3
"""Ядро напоминаний: TaskNotes `reminders` — истина, SQLite — проекция/очередь.

Модуль не знает про Telegram, git и сеть. Долгосрочная истина — Markdown:
native и Dusk reminders живут в `TaskNotes/Tasks/*.md`. Dusk-напоминание имеет
безопасный `id` с префиксом `dusk_` и `type: absolute`; всё остальное
сохраняется побайтно по смыслу. SQLite хранит только identity/относительный
путь/время/состояние/generation и очередь доставки — без title/description.

Порядок записи как у focus: сначала Markdown под vault-wide lock, затем одна DB
транзакция. Если Markdown записан, а SQLite упал — следующий `reconcile`
восстанавливает проекцию. Duplicate id и malformed Dusk reminder — fail closed.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import runtime_store
import task_metadata
from task_metadata import ReminderEntry

REMINDER_ID_PREFIX = task_metadata.DUSK_REMINDER_PREFIX
REMINDER_ID_MAX = 48
DUSK_REMINDER_TYPE = "absolute"
DUSK_REMINDER_DESCRIPTION = "Dusk reminder"
SCAN_LIMIT = 2000
REQUEST_HASH_LENGTH = 16
_UTC = datetime.timezone.utc
_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,%d}" % REMINDER_ID_MAX)


class ReminderError(RuntimeError):
    """Напоминание невозможно: ввод, повреждённая карточка или состояние."""


@dataclass(frozen=True)
class Reminder:
    reminder_id: str
    task_path: str
    absolute_time: str
    description: str = DUSK_REMINDER_DESCRIPTION
    type: str = DUSK_REMINDER_TYPE


@dataclass(frozen=True)
class ReminderResult:
    ok: bool
    reminder: Optional[Reminder]
    reused: bool = False
    sync_required: bool = False
    message: str = ""


@dataclass(frozen=True)
class ReminderReconcileResult:
    active: tuple = ()
    cancelled: int = 0
    sync_required: bool = False


@dataclass(frozen=True)
class _Scanned:
    relpath: str
    entry: ReminderEntry
    status: str


# ── время и identity ────────────────────────────────────────────────────────

def is_valid_reminder_id(value) -> bool:
    """Callback-safe Dusk reminder id: `dusk_` + `[A-Za-z0-9_-]`, 1..48."""
    text = str(value or "")
    return text.startswith(REMINDER_ID_PREFIX) and bool(_ID_RE.fullmatch(text))


def _new_reminder_id() -> str:
    return REMINDER_ID_PREFIX + secrets.token_hex(6)


def _now(now=None) -> datetime.datetime:
    if now is None:
        return datetime.datetime.now(_UTC)
    if not isinstance(now, datetime.datetime):
        raise ReminderError("now: нужен datetime")
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ReminderError("now: нужна aware-метка")
    return now.astimezone(_UTC)


def _parse_when(value) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.datetime.fromisoformat(value.strip())
        except ValueError as error:
            raise ReminderError("когда: некорректное время") from error
    else:
        raise ReminderError("когда: нужен datetime или ISO-строка")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ReminderError("когда: нужна aware-метка")
    return moment.astimezone(_UTC)


def _markdown_iso(value, target_utc: datetime.datetime) -> str:
    """Full ISO с offset для Markdown; UTC — безопасный fallback."""
    if isinstance(value, datetime.datetime) and value.tzinfo is not None:
        return value.isoformat()
    try:
        moment = datetime.datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return target_utc.isoformat()
    return moment.isoformat() if moment.tzinfo is not None else target_utc.isoformat()


def _parse_absolute(value, relpath: str) -> datetime.datetime:
    text = (value or "").strip()
    if not text:
        raise ReminderError(f"reminder без absoluteTime: {relpath}")
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError as error:
        raise ReminderError(f"некорректный absoluteTime: {relpath}") from error
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ReminderError(f"наивный absoluteTime: {relpath}")
    return moment.astimezone(_UTC)


def _request_hash(kind: str, key: str) -> str:
    if not key:
        return ""
    return hashlib.sha256(f"{kind}:{key}".encode("utf-8")).hexdigest()[:REQUEST_HASH_LENGTH]


def _same_instant(stored: str, target_utc: datetime.datetime) -> bool:
    try:
        return _parse_absolute(stored, "reminder") == target_utc
    except ReminderError:
        return False


def _find_request(entries, request_hash: str):
    if not request_hash:
        return None
    for entry in entries:
        if entry.is_dusk() and str(entry.data.get("duskRequest") or "") == request_hash:
            return entry
    return None


# ── адаптеры ────────────────────────────────────────────────────────────────

def _read_task(vault, task) -> task_metadata.TaskSnapshot:
    try:
        return task_metadata.read_task(vault, task)
    except task_metadata.TaskMetadataError as error:
        raise ReminderError(str(error)) from error


def _write_task(vault, task, **kwargs) -> task_metadata.TaskSnapshot:
    try:
        return task_metadata.write_task(vault, task, **kwargs)
    except task_metadata.TaskMetadataError as error:
        raise ReminderError(str(error)) from error


def _from_entry(relpath: str, entry: ReminderEntry) -> Reminder:
    return Reminder(
        reminder_id=entry.id, task_path=relpath,
        absolute_time=entry.absolute_time,
        description=entry.description or DUSK_REMINDER_DESCRIPTION,
        type=entry.type or DUSK_REMINDER_TYPE)


def _from_record(record: runtime_store.ReminderRecord) -> Reminder:
    return Reminder(reminder_id=record.reminder_id, task_path=record.task_path,
                    absolute_time=record.absolute_time)


def _record_from_entry(relpath: str, entry: ReminderEntry):
    absolute = _parse_absolute(entry.absolute_time, relpath)
    return runtime_store.ReminderRecord(
        reminder_id=entry.id, task_path=relpath,
        absolute_time=absolute.isoformat(),
        state=runtime_store.REMINDER_ACTIVE, generation=1)


def _validate_dusk_entry(entry: ReminderEntry, relpath: str) -> None:
    if not is_valid_reminder_id(entry.id):
        raise ReminderError(f"некорректный reminder id: {relpath}")
    if entry.type != DUSK_REMINDER_TYPE:
        raise ReminderError(f"неподдерживаемый type Dusk reminder: {relpath}")
    _parse_absolute(entry.absolute_time, relpath)


# ── bounded scan ────────────────────────────────────────────────────────────

def _scan(vault) -> list:
    """Bounded-скан direct TaskNotes cards на Dusk reminders.

    Malformed card с literal `dusk_` маркером или session-маркером fail closed:
    доказанно native-карточку можно пропустить. Duplicate id — fail closed.
    """
    try:
        paths = task_metadata.direct_task_paths(vault, SCAN_LIMIT)
    except task_metadata.TaskMetadataError as error:
        raise ReminderError(str(error)) from error
    found = []
    seen = set()
    for path in paths:
        if path.name.startswith("."):
            continue
        if path.is_symlink():
            # Containment: прямой symlink-ребёнок нельзя молча пропустить.
            raise ReminderError(f"symlink в TaskNotes/Tasks: {path.name}")
        try:
            snapshot = _read_task(vault, path)
        except ReminderError as error:
            try:
                marked = (task_metadata.has_dusk_marker(vault, path)
                          or task_metadata.has_reminder_marker(vault, path))
            except task_metadata.TaskMetadataError as marker_error:
                raise ReminderError(
                    f"карточку нельзя доказанно исключить: {path.name}"
                ) from marker_error
            if marked:
                raise ReminderError(
                    f"повреждённая карточка с Dusk-маркером: {path.name}") from error
            continue
        for entry in snapshot.reminders:
            if not entry.is_dusk():
                continue
            _validate_dusk_entry(entry, snapshot.relpath)
            if entry.id in seen:
                raise ReminderError(f"дублируется reminder id: {entry.id}")
            seen.add(entry.id)
            found.append(_Scanned(snapshot.relpath, entry, snapshot.status))
    return found


def scan_reminders(vault) -> tuple:
    """Read-only public scan with the same validation used by reminder state."""
    return tuple(_scan(vault))


def _find_scanned(scanned, reminder_id):
    for item in scanned:
        if item.entry.id == reminder_id:
            return item
    return None


def _task_relpath(vault, task) -> str:
    try:
        path = task_metadata.resolve_task_path(vault, task)
        return path.resolve().relative_to(Path(vault).resolve()).as_posix()
    except (task_metadata.TaskMetadataError, ValueError) as error:
        raise ReminderError("карточка вне TaskNotes/Tasks") from error


def find_reminder(vault, reminder_id) -> Optional[Reminder]:
    """Точный Dusk reminder по id во всём bounded-скане."""
    if not is_valid_reminder_id(reminder_id):
        return None
    for item in _scan(vault):
        if item.entry.id == reminder_id:
            return _from_entry(item.relpath, item.entry)
    return None


def get_reminder(vault, store, reminder_id) -> Optional[Reminder]:
    """Markdown-представление; при отсутствии — SQLite-проекция."""
    found = find_reminder(vault, reminder_id)
    if found is not None:
        return found
    if store is None:
        return None
    record = store.get_reminder(reminder_id)
    return _from_record(record) if record is not None else None


# ── create / snooze / cancel ────────────────────────────────────────────────

def create_reminder(vault, task, when, store, idempotency_key="",
                    now=None) -> ReminderResult:
    moment = _now(now)
    target = _parse_when(when)
    if target <= moment:
        raise ReminderError("время напоминания уже прошло")
    if store is None:
        raise ReminderError("состояние недоступно")
    key = f"reminder:{idempotency_key}" if idempotency_key else ""
    request_hash = _request_hash("reminder", idempotency_key)
    with task_metadata.vault_lock(vault):
        # Глобальный fail-closed scan ДО любого idempotent return/мутации.
        scanned = _scan(vault)
        target_rel = _task_relpath(vault, task)
        if key:
            existing = store.reminder_idempotency(key)
            if existing:
                mapped = _find_scanned(scanned, existing)
                if mapped is None or mapped.relpath != target_rel:
                    raise ReminderError("идемпотентная запись потеряна")
                record = store.get_reminder(existing)
                if record is None:
                    raise ReminderError("идемпотентная запись потеряна")
                return ReminderResult(True, _from_record(record), reused=True)
        snapshot = _read_task(vault, task)
        if snapshot.status == "done":
            raise ReminderError("задача закрыта")
        entries = list(snapshot.reminders)
        if request_hash:
            recovered = _find_request(entries, request_hash)
            if recovered is not None:
                record = _record_from_entry(snapshot.relpath, recovered)
                stored = store.recover_reminder(record, key, now=moment)
                return ReminderResult(
                    True, _from_record(stored), reused=True, sync_required=True)

        seen_ids = {item.entry.id for item in scanned}
        reminder_id = _new_reminder_id()
        attempts = 0
        while reminder_id in seen_ids:
            attempts += 1
            if attempts > 32:
                raise ReminderError("не удалось сгенерировать уникальный id")
            reminder_id = _new_reminder_id()
        data = {
            "id": reminder_id,
            "type": DUSK_REMINDER_TYPE,
            "description": DUSK_REMINDER_DESCRIPTION,
            "absoluteTime": _markdown_iso(when, target),
        }
        if request_hash:
            data["duskRequest"] = request_hash
        entry = ReminderEntry(data)
        entries.append(entry)
        _write_task(vault, snapshot.path, reminders=entries,
                    scalars={"dateModified": moment.isoformat()})
        store.upsert_reminder(_record_from_entry(snapshot.relpath, entry),
                              key, now=moment)
    return ReminderResult(True, _from_entry(snapshot.relpath, entry),
                          reused=False, sync_required=True)


def snooze_reminder(vault, reminder_id, when, store, idempotency_key="",
                    now=None) -> ReminderResult:
    moment = _now(now)
    target = _parse_when(when)
    if target <= moment:
        raise ReminderError("время напоминания уже прошло")
    if not is_valid_reminder_id(reminder_id):
        raise ReminderError("некорректный reminder id")
    if store is None:
        raise ReminderError("состояние недоступно")
    key = f"reminder-snooze:{idempotency_key}" if idempotency_key else ""
    request_hash = _request_hash("snooze", idempotency_key)
    with task_metadata.vault_lock(vault):
        scanned = _scan(vault)
        if key:
            stored = store.reminder_idempotency(key)
            if stored:
                if stored != reminder_id:
                    raise ReminderError(
                        "idempotency key принадлежит другому напоминанию")
                item = _find_scanned(scanned, reminder_id)
                record = store.get_reminder(reminder_id)
                if item is None or record is None:
                    raise ReminderError("напоминание не найдено")
                return ReminderResult(True, _from_record(record), reused=True)
        item = _find_scanned(scanned, reminder_id)
        if item is None:
            raise ReminderError("напоминание не найдено")
        relpath = item.relpath
        snapshot = _read_task(vault, relpath)
        if snapshot.status == "done":
            raise ReminderError("задача закрыта")
        entries = list(snapshot.reminders)
        index = next((i for i, entry in enumerate(entries)
                      if entry.id == reminder_id), None)
        if index is None:
            raise ReminderError("напоминание не найдено")
        entry = entries[index]

        # Markdown-успех / DB-сбой: тот же хеш уже применён — починить DB.
        if request_hash and str(entry.data.get("duskRequest") or "") == request_hash:
            record = _record_from_entry(relpath, entry)
            store.snooze_reminder(reminder_id, relpath, record.absolute_time,
                                  key, now=moment)
            return ReminderResult(True, _from_entry(relpath, entry),
                                  reused=True, sync_required=True)

        if _same_instant(entry.absolute_time, target):
            store.snooze_reminder(reminder_id, relpath, target, key, now=moment)
            record = store.get_reminder(reminder_id)
            reminder = _from_record(record) if record else _from_entry(
                relpath, entry)
            return ReminderResult(True, reminder, reused=True)

        updated = entry.with_absolute(_markdown_iso(when, target))
        if request_hash:
            updated = updated.with_extra({"duskRequest": request_hash})
        entries[index] = updated
        _write_task(vault, snapshot.path, reminders=entries,
                    scalars={"dateModified": moment.isoformat()})
        store.snooze_reminder(reminder_id, relpath, target, key, now=moment)
        record = store.get_reminder(reminder_id)
    return ReminderResult(True, _from_record(record), reused=False,
                          sync_required=True)


def cancel_reminder(vault, reminder_id, store, now=None) -> ReminderResult:
    moment = _now(now)
    if not is_valid_reminder_id(reminder_id):
        raise ReminderError("некорректный reminder id")
    if store is None:
        raise ReminderError("состояние недоступно")
    with task_metadata.vault_lock(vault):
        scanned = _scan(vault)
        item = _find_scanned(scanned, reminder_id)
        if item is not None:
            snapshot = _read_task(vault, item.relpath)
            entries = [entry for entry in snapshot.reminders
                       if entry.id != reminder_id]
            _write_task(vault, snapshot.path, reminders=entries,
                        scalars={"dateModified": moment.isoformat()})
            store.cancel_reminder(reminder_id, now=moment)
            return ReminderResult(True, None, reused=False, sync_required=True)
        # Markdown уже без него (прошлая частичная попытка или внешнее удаление).
        record = store.get_reminder(reminder_id)
        if record is None:
            raise ReminderError("напоминание не найдено")
        if record.state == runtime_store.REMINDER_CANCELLED:
            return ReminderResult(True, _from_record(record), reused=True,
                                  sync_required=False)
        store.cancel_reminder(reminder_id, now=moment)
        updated = store.get_reminder(reminder_id) or record
        return ReminderResult(True, _from_record(updated), reused=True,
                              sync_required=True)


# ── reconcile / collect ─────────────────────────────────────────────────────

def reconcile(vault, store, now=None) -> ReminderReconcileResult:
    """Markdown → SQLite. Restart/DB-loss recovery без частичных unsafe переходов."""
    moment = _now(now)
    if store is None:
        raise ReminderError("состояние недоступно")
    with task_metadata.vault_lock(vault):
        scanned = _scan(vault)
        records = [_record_from_entry(item.relpath, item.entry)
                   for item in scanned if item.status != "done"]
        changed, cancelled = store.reconcile_reminders(records, now=moment)
        active = []
        for record in records:
            stored = store.get_reminder(record.reminder_id)
            if stored is not None and stored.state in (
                    runtime_store.REMINDER_ACTIVE, runtime_store.REMINDER_DUE):
                active.append(_from_record(stored))
    return ReminderReconcileResult(active=tuple(active), cancelled=cancelled,
                                   sync_required=False)


def collect_due(store, now=None, limit=runtime_store.REMINDER_MAX_BATCH):
    """Low-level bounded active → due + durable event. Делегирует в store."""
    if store is None:
        raise ReminderError("состояние недоступно")
    return store.collect_due(now=now, limit=limit)


def collect_due_safe(vault, store, now=None,
                     limit=runtime_store.REMINDER_MAX_BATCH):
    """Safe boundary для C2: scan/reconcile перед atomic collect.

    Сначала один Markdown scan под vault lock, отменяет missing/done, затем
    атомарно собирает due. Network I/O здесь не выполняется.
    """
    moment = _now(now)
    if store is None:
        raise ReminderError("состояние недоступно")
    with task_metadata.vault_lock(vault):
        scanned = _scan(vault)
        records = [_record_from_entry(item.relpath, item.entry)
                   for item in scanned if item.status != "done"]
        store.reconcile_reminders(records, now=moment)
        return store.collect_due(now=moment, limit=limit)


@dataclass(frozen=True)
class DeliveryCheck:
    """Результат проверки перед network delivery. Без Telegram/сети."""

    ok: bool
    reminder: Optional[Reminder] = None
    reason: str = ""


def validate_delivery(vault, store, event, now=None) -> DeliveryCheck:
    """Проверяет claimed event перед отправкой. Fail closed.

    Валидный — точный task/reminder target для C2. Invalid/missing/done/moved —
    typed no-delivery с отменой недоставленной projection/event. Локи держатся
    только на этом шаге, не во время network I/O.
    """
    moment = _now(now)
    if store is None:
        raise ReminderError("состояние недоступно")
    reminder_id = getattr(event, "reminder_id", "")
    event_id = getattr(event, "event_id", 0)
    generation = getattr(event, "generation", 0)
    attempts = getattr(event, "attempts", 0)
    if not is_valid_reminder_id(reminder_id) or not event_id:
        return DeliveryCheck(False, reason="bad_event")
    with task_metadata.vault_lock(vault):
        scanned = _scan(vault)
        item = _find_scanned(scanned, reminder_id)
        if item is None or item.status == "done":
            store.cancel_reminder(reminder_id, now=moment)
            return DeliveryCheck(False, reason="missing_or_done")
        record = store.get_reminder(reminder_id)
        if record is None or record.state != runtime_store.REMINDER_DUE:
            store.cancel_reminder_event(event_id, now=moment)
            return DeliveryCheck(False, reason="not_due")
        try:
            absolute = _parse_absolute(item.entry.absolute_time, item.relpath)
        except ReminderError:
            store.cancel_reminder(reminder_id, now=moment)
            return DeliveryCheck(False, reason="malformed")
        if (not _same_instant(record.absolute_time, absolute)
                or record.generation != generation):
            store.cancel_reminder_event(event_id, now=moment)
            return DeliveryCheck(False, reason="moved")
        current = store.get_reminder_event(event_id)
        if (current is None or current.state != runtime_store.EVENT_LEASED
                or current.attempts != attempts
                or current.generation != generation):
            return DeliveryCheck(False, reason="stale_lease")
        return DeliveryCheck(True, _from_entry(item.relpath, item.entry))


__all__ = [
    "ReminderError", "Reminder", "ReminderResult", "ReminderReconcileResult",
    "DeliveryCheck",
    "create_reminder", "snooze_reminder", "cancel_reminder", "reconcile",
    "collect_due", "collect_due_safe", "validate_delivery",
    "find_reminder", "get_reminder", "is_valid_reminder_id",
    "DUSK_REMINDER_TYPE", "DUSK_REMINDER_DESCRIPTION", "SCAN_LIMIT",
]
