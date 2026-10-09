#!/usr/bin/env python3
"""Focus state machine поверх Markdown + SQLite. Без Telegram и сети.

Долгосрочная истина — TaskNotes `timeEntries` в карточке: `startTime` понимает
TaskNotes 4.5.1, а Dusk-поля (`plannedEnd`, `plannedMinutes`, `duskSessionId`,
`duskRequest`, `duskExtend`) для него безвредны. SQLite хранит только
операционную проекцию: какой токен активен, какие запросы уже применены
(идемпотентность) и какие уведомления надо доставить.

Правила, зашитые в переходы:

* одна активная Dusk-сессия на валт; старт новой закрывает все прошлые открытые
  Dusk-записи временем старта и помечает прошлую сессию `superseded`;
* чужой (не-Dusk) открытый учёт времени не закрывается молча — старт падает;
* продление — ровно +15 минут по публичному токену, с идемпотентным ключом;
  просроченный дедлайн не продлевается;
* стоп закрывает ровно нужную запись `endTime=now`, повтор ничего не переписывает;
* истечение пишется атомарно с событием и закрывает `endTime` по плановому
  дедлайну, а не по времени пробуждения;
* восстановление после рестарта идёт от Markdown; строка SQLite без открытой
  записи в Markdown становится `orphaned`, а не воскрешается.

Порядок записи выбран так, чтобы сбой на любом шаге оставлял восстановимое
состояние: сначала карточка-цель, затем одна DB-транзакция (активация +
идемпотентность), затем закрытие чужих карточек. Если последнее упало, `reconcile`
детерминированно оставит самую новую запись активной и закроет старые.

Все границы времени — aware UTC; наивные/битые метки отклоняются.
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

DURATIONS = (15, 25, 45, 60, 90)
EXPAND_MINUTES = 15
STATUS_IN_PROGRESS = "in-progress"
STATUS_DONE = "done"
SCAN_LIMIT = 2000
REQUEST_HASH_LENGTH = 16
SESSION_ID_MAX_LENGTH = 48
# session id попадает в Telegram callback_data (`q:x:`, `q:E:`), поэтому он
# обязан быть ASCII, без `:` и без разделителей — иначе ломается parser/markup.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,%d}" % SESSION_ID_MAX_LENGTH)
_UTC = datetime.timezone.utc

# Собственные поля Dusk-записи. Наличие любого делает запись потенциально
# Dusk-like: её нельзя молча пропустить при скане.
_DUSK_FIELDS = ("duskSessionId", "plannedEnd", "plannedMinutes",
                "duskRequest", "duskExtend")


class FocusError(RuntimeError):
    """Фокус-переход невозможен: плохой ввод, чужой учёт или потеря состояния."""


# ── результат перехода ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class FocusSession:
    session_id: str
    task_path: str
    task_name: str
    status: str
    started_at: str
    planned_end: str
    planned_minutes: int
    ended_at: str = ""
    end_reason: str = ""


@dataclass(frozen=True)
class FocusResult:
    ok: bool
    session: Optional[FocusSession]
    reused: bool = False
    message: str = ""
    deferred: tuple = ()
    sync_required: bool = False


@dataclass(frozen=True)
class FocusNotice:
    session_id: str
    kind: str
    message_key: str


@dataclass(frozen=True)
class ExpireResult:
    expired: tuple = ()
    events: tuple = ()
    deferred: tuple = ()
    sync_required: bool = False


@dataclass(frozen=True)
class ReconcileResult:
    active: Optional[FocusSession] = None
    expired: tuple = ()
    superseded: tuple = ()
    orphaned: tuple = ()
    deferred: tuple = ()
    sync_required: bool = False


@dataclass(frozen=True)
class _Found:
    relpath: str
    entry: task_metadata.TimeEntry


# ── время ───────────────────────────────────────────────────────────────────

def _require_aware(moment, label: str = "now") -> datetime.datetime:
    if not isinstance(moment, datetime.datetime):
        raise FocusError(f"{label}: нужен datetime")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise FocusError(f"{label}: нужна aware-метка")
    return moment.astimezone(_UTC)


def _now(now=None) -> datetime.datetime:
    if now is not None:
        return _require_aware(now)
    return datetime.datetime.now(_UTC)


def _iso(moment: datetime.datetime) -> str:
    return moment.astimezone(_UTC).isoformat()


def _parse_iso(value, label: str) -> datetime.datetime:
    text = (value or "").strip()
    if not text:
        raise FocusError(f"{label}: нет времени")
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError as error:
        raise FocusError(f"{label}: некорректное время") from error
    return _require_aware(moment, label)


# ── значения сессии ─────────────────────────────────────────────────────────

def _validate_duration(minutes) -> int:
    if isinstance(minutes, bool):
        raise FocusError("длительность должна быть числом")
    try:
        value = int(minutes)
    except (TypeError, ValueError) as error:
        raise FocusError("длительность должна быть числом") from error
    if value not in DURATIONS:
        raise FocusError("допустимые длительности: "
                         + ", ".join(str(item) for item in DURATIONS))
    return value


def _new_session_id() -> str:
    return secrets.token_hex(8)


def _request_hash(kind: str, key: str) -> str:
    """Opaque bounded хеш запроса: в Markdown не попадает исходный текст."""
    if not key:
        return ""
    return hashlib.sha256(f"{kind}:{key}".encode("utf-8")).hexdigest()[:REQUEST_HASH_LENGTH]


def _new_entry(session_id: str, minutes: int, moment: datetime.datetime,
               request_hash: str = "") -> task_metadata.TimeEntry:
    data = {
        "startTime": _iso(moment),
        "plannedEnd": _iso(moment + datetime.timedelta(minutes=minutes)),
        "plannedMinutes": minutes,
        "duskSessionId": session_id,
        "description": f"Dusk focus · {minutes} мин",
        "source": task_metadata.DUSK_SOURCE,
    }
    if request_hash:
        data["duskRequest"] = request_hash
    return task_metadata.TimeEntry(data)


def _find_entry_index(entries, session_id: str):
    for index, entry in enumerate(entries):
        if entry.dusk_session_id == session_id:
            return index
    return None


def _find_request(entries, field: str, value: str):
    if not value:
        return None
    for entry in entries:
        if entry.is_dusk() and str(entry.data.get(field) or "") == value:
            return entry
    return None


def _read_task(vault, task) -> task_metadata.TaskSnapshot:
    """Чтение карточки с переброской ошибки границы в публичную `FocusError`."""
    try:
        return task_metadata.read_task(vault, task)
    except task_metadata.TaskMetadataError as error:
        raise FocusError(str(error)) from error


def _write_task(vault, task, **kwargs) -> task_metadata.TaskSnapshot:
    try:
        return task_metadata.write_task(vault, task, **kwargs)
    except task_metadata.TaskMetadataError as error:
        raise FocusError(str(error)) from error


def _session_from(record: runtime_store.FocusRecord) -> FocusSession:
    return FocusSession(
        session_id=record.session_id,
        task_path=record.task_path,
        task_name=Path(record.task_path).stem,
        status=record.status,
        started_at=record.started_at,
        planned_end=record.planned_end,
        planned_minutes=record.planned_minutes,
        ended_at=record.ended_at,
        end_reason=record.end_reason,
    )


def _record_from_entry(relpath: str, entry: task_metadata.TimeEntry, *, status: str,
                       ended_at: str = "", end_reason: str = "") -> runtime_store.FocusRecord:
    """Строит операционную запись из Markdown-записи, нормализуя время в UTC."""
    start = _parse_iso(entry.start_time, "startTime")
    deadline = _parse_iso(entry.planned_end, "plannedEnd")
    minutes = entry.planned_minutes
    if not minutes:
        minutes = max(0, int((deadline - start).total_seconds() // 60))
    return runtime_store.FocusRecord(
        session_id=entry.dusk_session_id, task_path=relpath, status=status,
        started_at=_iso(start), planned_end=_iso(deadline),
        planned_minutes=minutes, ended_at=ended_at, end_reason=end_reason)


# ── старт ───────────────────────────────────────────────────────────────────

def start_focus(vault, task, minutes, store, idempotency_key: str = "",
                now=None) -> FocusResult:
    duration = _validate_duration(minutes)
    moment = _now(now)
    key = f"start:{idempotency_key}" if idempotency_key else ""
    request_hash = _request_hash("start", idempotency_key)
    with task_metadata.vault_lock(vault):
        if key:
            existing = store.idempotency_session(key)
            if existing:
                record = store.get_focus(existing)
                if record is None:
                    raise FocusError("идемпотентная сессия потеряна")
                return FocusResult(True, _session_from(record), reused=True,
                                   message="Фокус уже запущен")

        snapshot = _read_task(vault, task)
        if snapshot.status == STATUS_DONE:
            raise FocusError("задача уже закрыта")
        entries = list(snapshot.time_entries)
        for entry in entries:
            if entry.is_open() and not entry.is_dusk():
                raise FocusError("в задаче уже идёт чужой учёт времени")

        # Markdown-успех / DB-сбой: тот же request hash уже в заметке — вернуть
        # ту же сессию и восстановить DB-проекцию, не создавая вторую запись.
        if request_hash:
            recovered = _find_request(entries, "duskRequest", request_hash)
            if recovered is not None and recovered.is_open():
                record = _record_from_entry(
                    snapshot.relpath, recovered,
                    status=runtime_store.FOCUS_ACTIVE)
                store.activate_focus_with_claim(record, key, now=moment)
                return FocusResult(True, _session_from(record), reused=True,
                                   message="Фокус уже запущен",
                                   sync_required=True)

        for index, entry in enumerate(entries):
            if entry.is_open() and entry.is_dusk():
                entries[index] = entry.with_end(_iso(moment))

        session_id = _new_session_id()
        entries.append(_new_entry(session_id, duration, moment, request_hash))

        # 0) Fail-closed инвариант ДО любой записи: повреждённая карточка,
        #    которую нельзя доказанно исключить из Dusk, останавливает старт,
        #    не трогая ни цель, ни прошлые сессии.
        others = [item for item in _scan_dusk(vault, open_only=True)
                  if item.relpath != snapshot.relpath]

        # 1) Сначала цель: если запись не удалась, прошлый фокус не тронут.
        _write_task(vault, snapshot.path, time_entries=entries,
                    scalars={"status": STATUS_IN_PROGRESS,
                             "dateModified": _iso(moment)})

        # 2) Активация и идемпотентность одной DB-транзакцией.
        record = runtime_store.FocusRecord(
            session_id=session_id, task_path=snapshot.relpath,
            status=runtime_store.FOCUS_ACTIVE, started_at=_iso(moment),
            planned_end=_iso(moment + datetime.timedelta(minutes=duration)),
            planned_minutes=duration)
        store.activate_focus_with_claim(record, key, now=moment)

        # 3) Закрываем чужие открытые Dusk-записи; сбой отдаём reconcile.
        deferred = []
        for item in others:
            try:
                _close_entry(vault, item.relpath, item.entry.dusk_session_id,
                             moment)
            except FocusError:
                deferred.append(item.entry.dusk_session_id)
                continue
            store.upsert_focus(_record_from_entry(
                item.relpath, item.entry,
                status=runtime_store.FOCUS_SUPERSEDED,
                ended_at=_iso(moment),
                end_reason=runtime_store.REASON_SUPERSEDED), now=moment)

    return FocusResult(True, _session_from(record), reused=False,
                       message=f"Фокус {duration} мин начат",
                       deferred=tuple(deferred), sync_required=True)


# ── продление ───────────────────────────────────────────────────────────────

def extend_focus(vault, token, store, idempotency_key: str = "",
                 now=None) -> FocusResult:
    moment = _now(now)
    key = f"extend:{idempotency_key}" if idempotency_key else ""
    request_hash = _request_hash("extend", idempotency_key)
    with task_metadata.vault_lock(vault):
        if key and store.idempotency_session(key):
            record = store.get_focus(token)
            if record is None:
                raise FocusError("сессия фокуса не найдена")
            return FocusResult(True, _session_from(record), reused=True,
                               message="Продление уже применено")

        record = store.get_focus(token)
        if record is not None and record.status != runtime_store.FOCUS_ACTIVE:
            raise FocusError("фокус не активен")
        if record is not None:
            relpath = record.task_path
        else:
            found = _find_entry_by_token(vault, token)
            if found is None:
                raise FocusError("нет активного фокуса с таким токеном")
            relpath = found.relpath

        snapshot = _read_task(vault, relpath)
        entries = list(snapshot.time_entries)
        index = _find_entry_index(entries, token)
        if index is None:
            raise FocusError("запись фокуса не найдена в задаче")
        entry = entries[index]
        if not entry.is_open():
            raise FocusError("фокус уже закрыт")

        # Markdown-успех / DB-сбой: тот же extend-хеш уже применён — починить
        # DB-проекцию и вернуть reused, не добавляя вторые 15 минут.
        if request_hash and str(entry.data.get("duskExtend") or "") == request_hash:
            repaired = _record_from_entry(
                snapshot.relpath, entry, status=runtime_store.FOCUS_ACTIVE)
            store.update_focus_with_claim(repaired, key, now=moment)
            return FocusResult(True, _session_from(repaired), reused=True,
                               message="Продление уже применено",
                               sync_required=True)

        current_end = _parse_iso(entry.planned_end, "plannedEnd")
        if current_end <= moment:
            raise FocusError("дедлайн уже прошёл — сначала истечение")
        start = _parse_iso(entry.start_time, "startTime")
        base_minutes = entry.planned_minutes or max(
            0, int((current_end - start).total_seconds() // 60))
        planned_minutes = base_minutes + EXPAND_MINUTES
        new_end = current_end + datetime.timedelta(minutes=EXPAND_MINUTES)
        updated = entry.with_plan(_iso(new_end), planned_minutes)
        if request_hash:
            updated = updated.with_extra({"duskExtend": request_hash})
        entries[index] = updated

        _write_task(vault, snapshot.path, time_entries=entries,
                    scalars={"dateModified": _iso(moment)})
        record = runtime_store.FocusRecord(
            session_id=token, task_path=snapshot.relpath,
            status=runtime_store.FOCUS_ACTIVE, started_at=_iso(start),
            planned_end=_iso(new_end), planned_minutes=planned_minutes)
        store.update_focus_with_claim(record, key, now=moment)
    return FocusResult(True, _session_from(record), reused=False,
                       message=f"+{EXPAND_MINUTES} мин", sync_required=True)


# ── стоп ────────────────────────────────────────────────────────────────────

def stop_focus(vault, token, store, now=None) -> FocusResult:
    moment = _now(now)
    with task_metadata.vault_lock(vault):
        record = store.get_focus(token)
        if record is None:
            raise FocusError("неизвестный токен фокуса")
        if record.status in (runtime_store.FOCUS_STOPPED,
                             runtime_store.FOCUS_EXPIRED):
            return FocusResult(True, _session_from(record), reused=True,
                               message="Фокус уже завершён")
        if record.status != runtime_store.FOCUS_ACTIVE:
            raise FocusError("фокус не активен")

        snapshot = _read_task(vault, record.task_path)
        entries = list(snapshot.time_entries)
        index = _find_entry_index(entries, token)
        changed = False
        if index is not None and entries[index].is_open():
            entries[index] = entries[index].with_end(_iso(moment))
            _write_task(
                vault, snapshot.path, time_entries=entries,
                scalars={"dateModified": _iso(moment)})
            changed = True

        finished = runtime_store.FocusRecord(
            session_id=record.session_id, task_path=record.task_path,
            status=runtime_store.FOCUS_STOPPED, started_at=record.started_at,
            planned_end=record.planned_end, planned_minutes=record.planned_minutes,
            ended_at=_iso(moment), end_reason=runtime_store.REASON_MANUAL)
        store.upsert_focus(finished, now=moment)
    return FocusResult(True, _session_from(finished), reused=False,
                       message="Фокус остановлен", sync_required=changed)


# ── истечение ───────────────────────────────────────────────────────────────

def expire_due(vault, store, now=None) -> ExpireResult:
    """Закрывает просроченные Dusk-записи. Работает и без активной строки в DB."""
    moment = _now(now)
    expired = []
    events = []
    deferred = []
    changed = False
    with task_metadata.vault_lock(vault):
        markdown = {item.entry.dusk_session_id: item
                    for item in _scan_dusk(vault, open_only=True)}
        targets = {}
        record = store.get_active_focus()
        if record is not None:
            item = markdown.get(record.session_id)
            if item is not None:
                deadline = _parse_iso(item.entry.planned_end, "plannedEnd")
                if deadline <= moment:
                    targets[record.session_id] = (
                        item.relpath, deadline, item.entry, record)
            else:
                deadline = _parse_iso(record.planned_end, "plannedEnd")
                if deadline <= moment:
                    targets[record.session_id] = (
                        record.task_path, deadline, None, record)
        for session_id, item in markdown.items():
            if session_id in targets:
                continue
            deadline = _parse_iso(item.entry.planned_end, "plannedEnd")
            if deadline <= moment:
                targets[session_id] = (item.relpath, deadline, item.entry, None)

        for session_id, (relpath, deadline, entry, record) in targets.items():
            if record is None:
                record = _record_from_entry(
                    relpath, entry, status=runtime_store.FOCUS_EXPIRED,
                    ended_at=_iso(deadline),
                    end_reason=runtime_store.REASON_EXPIRED)
            else:
                record = runtime_store.FocusRecord(
                    session_id=record.session_id, task_path=relpath,
                    status=runtime_store.FOCUS_EXPIRED,
                    started_at=record.started_at, planned_end=_iso(deadline),
                    planned_minutes=record.planned_minutes,
                    ended_at=_iso(deadline),
                    end_reason=runtime_store.REASON_EXPIRED)
            # Атомарно: expired-запись + уникальное событие.
            key = store.finish_expired(record, now=moment)
            try:
                if _close_entry(vault, relpath, session_id, deadline):
                    changed = True
            except FocusError:
                deferred.append(session_id)
            expired.append(_session_from(record))
            events.append(FocusNotice(session_id,
                                      runtime_store.EVENT_FOCUS_EXPIRED, key))
    return ExpireResult(tuple(expired), tuple(events), deferred=tuple(deferred),
                        sync_required=changed)


# ── восстановление после рестарта ───────────────────────────────────────────

def reconcile(vault, store, now=None) -> ReconcileResult:
    moment = _now(now)
    expired = []
    superseded = []
    orphaned = []
    deferred = []
    changed = False
    active = None
    with task_metadata.vault_lock(vault):
        found = _scan_dusk(vault, open_only=True)
        open_ids = {item.entry.dusk_session_id for item in found}

        db_active = store.get_active_focus()
        if db_active is not None and db_active.session_id not in open_ids:
            store.upsert_focus(runtime_store.FocusRecord(
                session_id=db_active.session_id, task_path=db_active.task_path,
                status=runtime_store.FOCUS_ORPHANED,
                started_at=db_active.started_at,
                planned_end=db_active.planned_end,
                planned_minutes=db_active.planned_minutes,
                ended_at=db_active.ended_at, end_reason=db_active.end_reason),
                now=moment)
            orphaned.append(db_active.session_id)

        past = []
        future = []
        for item in found:
            deadline = _parse_iso(item.entry.planned_end, "plannedEnd")
            (past if deadline <= moment else future).append((item, deadline))

        for item, deadline in past:
            record = _record_from_entry(
                item.relpath, item.entry, status=runtime_store.FOCUS_EXPIRED,
                ended_at=_iso(deadline), end_reason=runtime_store.REASON_EXPIRED)
            # Атомарно: expired-запись + одно уникальное событие.
            store.finish_expired(record, now=moment)
            try:
                if _close_entry(vault, item.relpath,
                                item.entry.dusk_session_id, deadline):
                    changed = True
            except FocusError:
                deferred.append(item.entry.dusk_session_id)
            expired.append(_session_from(record))

        if future:
            future.sort(key=lambda pair: _parse_iso(
                pair[0].entry.start_time, "startTime"))
            newest_item, _newest_deadline = future[-1]
            newest_start = _parse_iso(newest_item.entry.start_time, "startTime")
            for item, deadline in future[:-1]:
                end = min(deadline, newest_start)
                record = _record_from_entry(
                    item.relpath, item.entry,
                    status=runtime_store.FOCUS_SUPERSEDED,
                    ended_at=_iso(end),
                    end_reason=runtime_store.REASON_SUPERSEDED)
                try:
                    if _close_entry(vault, item.relpath,
                                    item.entry.dusk_session_id, end):
                        changed = True
                except FocusError:
                    deferred.append(item.entry.dusk_session_id)
                store.upsert_focus(record, now=moment)
                superseded.append(_session_from(record))

            record = _record_from_entry(
                newest_item.relpath, newest_item.entry,
                status=runtime_store.FOCUS_ACTIVE)
            store.activate_focus(record, now=moment)
            active = _session_from(record)

    return ReconcileResult(active=active, expired=tuple(expired),
                           superseded=tuple(superseded), orphaned=tuple(orphaned),
                           deferred=tuple(deferred), sync_required=changed)


def _close_entry(vault, relpath, session_id, end_moment) -> bool:
    """Закрывает запись в Markdown. True — если карточка реально изменилась."""
    snapshot = _read_task(vault, relpath)
    entries = list(snapshot.time_entries)
    index = _find_entry_index(entries, session_id)
    if index is None or not entries[index].is_open():
        return False
    entries[index] = entries[index].with_end(_iso(end_moment))
    _write_task(vault, snapshot.path, time_entries=entries,
                scalars={"dateModified": _iso(end_moment)})
    return True


def _is_dusk_like(entry) -> bool:
    """Есть ли у записи хотя бы один собственный признак Dusk."""
    data = entry.data
    if any(field in data for field in _DUSK_FIELDS):
        return True
    return entry.source == task_metadata.DUSK_SOURCE


def is_valid_session_id(value) -> bool:
    """Callback-safe Dusk session id: 1..48 ASCII `[A-Za-z0-9_-]`."""
    return bool(_SESSION_ID_RE.fullmatch(str(value or "")))


def _dusk_session_id(entry) -> str:
    return str(entry.data.get("duskSessionId") or "").strip()


def _scan_dusk(vault, *, open_only: bool = False) -> list:
    """Bounded-скан прямых карточек TaskNotes на Dusk-записи.

    Превышение лимита — fail closed: нельзя молча пропустить активную запись.
    Нечитаемая/повреждённая карточка с literal `duskSessionId` тоже fail closed:
    доказанно непричастную malformed-карточку без маркера можно пропустить.

    Каждая OPEN Dusk-like запись обязана доказать корректность: непустой
    bounded `duskSessionId`, aware ISO `startTime`/`plannedEnd` и end > start.
    Недоказуемая корректность — `FocusError` до любой мутации. Закрытая
    историческая запись не блокирует open-скан (она уже не активна).
    """
    try:
        paths = task_metadata.direct_task_paths(vault, SCAN_LIMIT)
    except task_metadata.TaskMetadataError as error:
        raise FocusError(str(error)) from error
    found = []
    for path in paths:
        if path.is_symlink() or path.name.startswith("."):
            continue
        try:
            snapshot = task_metadata.read_task(vault, path)
        except task_metadata.TaskMetadataError as error:
            # Нельзя молча пропустить карточку, которая может быть Dusk.
            try:
                dusk_like = task_metadata.has_dusk_marker(vault, path)
            except task_metadata.TaskMetadataError as marker_error:
                raise FocusError(
                    "карточку нельзя доказанно исключить из Dusk: "
                    f"{path.name}") from marker_error
            if dusk_like:
                raise FocusError(
                    f"повреждённая Dusk-карточка: {path.name}") from error
            continue
        for entry in snapshot.time_entries:
            if not _is_dusk_like(entry):
                continue
            is_open = entry.is_open()
            if open_only and not is_open:
                continue
            session_id = _dusk_session_id(entry)
            if is_open:
                if not is_valid_session_id(session_id):
                    raise FocusError(
                        f"некорректный duskSessionId: {snapshot.relpath}")
                start = _parse_iso(entry.start_time, "startTime")
                end = _parse_iso(entry.planned_end, "plannedEnd")
                if end <= start:
                    raise FocusError(
                        "plannedEnd должен быть позже startTime: "
                        f"{snapshot.relpath}")
            elif not is_valid_session_id(session_id):
                # Закрытая история без валидного токена не участвует в поиске.
                continue
            found.append(_Found(snapshot.relpath, entry))

    # Дубликат открытого токена нельзя схлопнуть в set или перезаписать в SQLite.
    open_ids = set()
    for item in found:
        if not item.entry.is_open():
            continue
        session_id = _dusk_session_id(item.entry)
        if session_id in open_ids:
            raise FocusError(f"дублируется открытый duskSessionId: {session_id}")
        open_ids.add(session_id)
    return found


def scan_sessions(vault, *, open_only: bool = False) -> tuple:
    """Read-only public scan with the same fail-closed contract as transitions."""
    return tuple(_scan_dusk(vault, open_only=open_only))


def _scan_open_dusk(vault) -> list:
    return _scan_dusk(vault, open_only=True)


def _find_entry_by_token(vault, token):
    for item in _scan_dusk(vault):
        if item.entry.dusk_session_id == token:
            return item
    return None


__all__ = [
    "FocusError", "FocusSession", "FocusResult", "FocusNotice",
    "ExpireResult", "ReconcileResult",
    "start_focus", "extend_focus", "stop_focus", "expire_due", "reconcile",
    "DURATIONS", "EXPAND_MINUTES", "is_valid_session_id",
    "SESSION_ID_MAX_LENGTH",
]
