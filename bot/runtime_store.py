#!/usr/bin/env python3
"""Операционное состояние Dusk в SQLite: только идентичность и cooldown.

Долгоживущая истина остаётся в Markdown: мысль — Inbox/fleeting note, действие —
TaskNotes task, напоминание — `reminders`. Здесь живёт лишь то, что нельзя
восстановить из заметок:

* что пользователь скрыл на 7 дней (`hidden`);
* когда мысль показывали в последний раз (`surfaced`);
* когда последний раз показывали «похожую» мысль (`meta`).

Тексты заметок в базу не попадают: колонки хранят только identity (относительный
путь плюс хеш содержимого) и временные метки. Схема создаётся транзакционно и
версионируется через `PRAGMA user_version`; более новая версия — явная ошибка,
а не молчаливая работа на непонятной схеме.

Путь задаётся `DUSK_RUNTIME_DB`; по умолчанию — `$XDG_STATE_HOME/dusk/
runtime.sqlite3` (или `~/.local/state/dusk/runtime.sqlite3`). Файл создаётся с
правами `0600`, журнал — WAL, запись — `BEGIN IMMEDIATE`, соединения —
контекстные. Только стандартная библиотека.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 5000
HIDE_DAYS = 7
RELATED_COOLDOWN_HOURS = 24
DB_MODE = 0o600

# Состояния фокус-сессии. Долгосрочная истина — timeEntries в Markdown; здесь
# только операционная проекция, по которой scheduler и Telegram узнают, что идёт.
FOCUS_ACTIVE = "active"
FOCUS_SUPERSEDED = "superseded"
FOCUS_STOPPED = "stopped"
FOCUS_EXPIRED = "expired"
FOCUS_ORPHANED = "orphaned"
REASON_MANUAL = "manual"
REASON_EXPIRED = "expired"
REASON_SUPERSEDED = "superseded"

EVENT_FOCUS_EXPIRED = "focus_expired"
EVENT_PENDING = "pending"
EVENT_LEASED = "leased"
EVENT_DELIVERED = "delivered"
EVENT_FAILED = "failed"
EVENT_LEASE_SECONDS = 120
EVENT_BASE_BACKOFF_SECONDS = 30
EVENT_MAX_BACKOFF_SECONDS = 1800

# Напоминания (schema v3). Markdown `reminders` — истина, здесь проекция/очередь.
REMINDER_ACTIVE = "active"
REMINDER_DUE = "due"
REMINDER_DELIVERED = "delivered"
REMINDER_CANCELLED = "cancelled"
REMINDER_EVENT_DUE = "reminder_due"
EVENT_CANCELLED = "cancelled"
REMINDER_LEASE_SECONDS = 120
REMINDER_MAX_BATCH = 50

DEFAULT_APP_DIR = "dusk"
DEFAULT_DB_NAME = "runtime.sqlite3"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS hidden (
        identity TEXT PRIMARY KEY,
        hidden_until TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS surfaced (
        identity TEXT PRIMARY KEY,
        last_shown TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS focus_sessions (
        session_id TEXT PRIMARY KEY,
        task_path TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        planned_end TEXT NOT NULL,
        planned_minutes INTEGER NOT NULL,
        ended_at TEXT NOT NULL DEFAULT '',
        end_reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS focus_sessions_one_active
        ON focus_sessions(status) WHERE status = 'active'
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency (
        key TEXT PRIMARY KEY,
        session_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS focus_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        message_key TEXT NOT NULL UNIQUE,
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_until TEXT NOT NULL DEFAULT '',
        next_attempt_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        delivered_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reminders (
        reminder_id TEXT PRIMARY KEY,
        task_path TEXT NOT NULL,
        absolute_time TEXT NOT NULL,
        state TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reminder_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        state TEXT NOT NULL,
        message_key TEXT NOT NULL UNIQUE,
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_until TEXT NOT NULL DEFAULT '',
        next_attempt_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        delivered_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reminder_idempotency (
        key TEXT PRIMARY KEY,
        reminder_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
)


class RuntimeStoreError(RuntimeError):
    """Операционное состояние недоступно: путь, права, схема или запись."""


@dataclass(frozen=True)
class FocusRecord:
    """Операционная проекция фокус-сессии. Без текста заметки."""

    session_id: str
    task_path: str
    status: str
    started_at: str
    planned_end: str
    planned_minutes: int
    ended_at: str = ""
    end_reason: str = ""


@dataclass(frozen=True)
class FocusEvent:
    """Событие доставки уведомления (пока только истечение фокуса)."""

    event_id: int
    session_id: str
    kind: str
    status: str
    message_key: str
    attempts: int
    lease_until: str = ""
    next_attempt_at: str = ""


def _focus_record(row) -> FocusRecord:
    return FocusRecord(
        session_id=row[0], task_path=row[1], status=row[2], started_at=row[3],
        planned_end=row[4], planned_minutes=int(row[5] or 0),
        ended_at=row[6] or "", end_reason=row[7] or "")


def _focus_event(row) -> FocusEvent:
    return FocusEvent(
        event_id=int(row[0]), session_id=row[1], kind=row[2], status=row[3],
        message_key=row[4], attempts=int(row[5] or 0), lease_until=row[6] or "",
        next_attempt_at=row[7] or "")


@dataclass(frozen=True)
class ReminderRecord:
    """Операционная проекция Dusk-напоминания. Без текста заметки."""

    reminder_id: str
    task_path: str
    absolute_time: str
    state: str
    generation: int = 1


@dataclass(frozen=True)
class ReminderEvent:
    """Событие доставки напоминания. Generation-owned, как у focus."""

    event_id: int
    reminder_id: str
    generation: int
    state: str
    message_key: str
    attempts: int
    lease_until: str = ""
    next_attempt_at: str = ""


def _reminder_record(row) -> ReminderRecord:
    return ReminderRecord(
        reminder_id=row[0], task_path=row[1], absolute_time=row[2],
        state=row[3], generation=int(row[4] or 1))


def _reminder_event(row) -> ReminderEvent:
    return ReminderEvent(
        event_id=int(row[0]), reminder_id=row[1], generation=int(row[2] or 1),
        state=row[3], message_key=row[4], attempts=int(row[5] or 0),
        lease_until=row[6] or "", next_attempt_at=row[7] or "")

_REMINDER_COLUMNS = "reminder_id, task_path, absolute_time, state, generation"
_REMINDER_EVENT_COLUMNS = ("event_id, reminder_id, generation, state, "
                           "message_key, attempts, lease_until, next_attempt_at")

_FOCUS_COLUMNS = ("session_id, task_path, status, started_at, planned_end, "
                  "planned_minutes, ended_at, end_reason")
_EVENT_COLUMNS = ("event_id, session_id, kind, status, message_key, attempts, "
                  "lease_until, next_attempt_at")


def default_db_path(env=None) -> Path:
    """Путь базы: явный `DUSK_RUNTIME_DB`, иначе state-каталог пользователя."""
    environ = os.environ if env is None else env
    override = (environ.get("DUSK_RUNTIME_DB") or "").strip()
    if override:
        return Path(override).expanduser()
    state_home = (environ.get("XDG_STATE_HOME") or "").strip()
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / DEFAULT_APP_DIR / DEFAULT_DB_NAME


def _utc(now=None) -> datetime.datetime:
    """Aware UTC. Наивные и не-datetime публичные аргументы — явная ошибка."""
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if not isinstance(now, datetime.datetime):
        raise RuntimeStoreError("время должно быть datetime")
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise RuntimeStoreError("время должно быть aware-меткой")
    return now.astimezone(datetime.timezone.utc)


def _iso(now=None) -> str:
    return _utc(now).isoformat()


def _store_stamp(value) -> str:
    """Нормализует метку к aware UTC перед записью в SQLite. Пусто — пусто."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime.datetime):
        return _utc(value).isoformat()
    try:
        moment = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise RuntimeStoreError("некорректная метка времени") from error
    return _utc(moment).isoformat()


def _same_stamp(first, second) -> bool:
    try:
        return _store_stamp(first) == _store_stamp(second)
    except RuntimeStoreError:
        return False


def _bounded_limit(value) -> int:
    """0 — не собирать; non-int/bool — явная ошибка; иначе cap на batch."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeStoreError("limit должен быть целым числом")
    if value <= 0:
        return 0
    return min(value, REMINDER_MAX_BATCH)


def _parse(value: str):
    try:
        moment = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


class RuntimeStore:
    """Маленький интерфейс над SQLite. Все публичные методы явно бросают ошибки."""

    def __init__(self, path):
        self.path = Path(path)
        self._prepare()
        self._migrate()

    # ── подготовка файла и схемы ───────────────────────────────────────────

    def _prepare(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RuntimeStoreError(
                f"не удалось создать каталог состояния: {self.path.parent}") from error
        try:
            connection = self._connect()
            connection.close()
        except sqlite3.Error as error:
            raise RuntimeStoreError(
                f"не удалось открыть базу состояния: {self.path}") from error
        try:
            os.chmod(self.path, DB_MODE)
        except OSError as error:
            raise RuntimeStoreError(
                f"не удалось выставить права {oct(DB_MODE)} на {self.path}") from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _migrate(self) -> None:
        connection = None
        try:
            connection = self._connect()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeStoreError(
                    f"база состояния новее кода: схема {version}, "
                    f"поддерживается {SCHEMA_VERSION}")
            if version < SCHEMA_VERSION:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                except sqlite3.Error:
                    connection.rollback()
                    raise
                connection.commit()
        except sqlite3.Error as error:
            raise RuntimeStoreError(
                f"не удалось подготовить схему состояния: {self.path}") from error
        finally:
            if connection is not None:
                connection.close()

    # ── соединения ─────────────────────────────────────────────────────────

    @contextmanager
    def connection(self):
        """Контекстное соединение: закрывается всегда, ошибки — RuntimeStoreError.

        Любой `sqlite3.Error` (открытие, SELECT, PRAGMA) наружу не выходит:
        read-путь обязан отдавать тот же явный RuntimeStoreError, что и запись.
        """
        connection = None
        try:
            connection = self._connect()
            yield connection
        except sqlite3.Error as error:
            raise RuntimeStoreError(
                f"не удалось прочитать состояние: {self.path}") from error
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _transaction(self):
        """Одна запись под `BEGIN IMMEDIATE`: сериализует конкурентов."""
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        except sqlite3.Error as error:
            raise RuntimeStoreError(
                f"не удалось записать состояние: {self.path}") from error
        finally:
            if connection is not None:
                connection.close()

    def close(self) -> None:
        """Соединения короткоживущие; метод существует для явного teardown."""
        return None

    # ── скрытие на 7 дней ──────────────────────────────────────────────────

    def hide(self, identity: str, now=None) -> str:
        key = (identity or "").strip()
        if not key:
            raise RuntimeStoreError("пустая identity для скрытия")
        moment = _utc(now)
        until = moment + datetime.timedelta(days=HIDE_DAYS)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO hidden(identity, hidden_until, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(identity) DO UPDATE SET "
                "hidden_until=excluded.hidden_until, updated_at=excluded.updated_at",
                (key, until.isoformat(), moment.isoformat()))
        return until.isoformat()

    def is_hidden(self, identity: str, now=None) -> bool:
        key = (identity or "").strip()
        if not key:
            return False
        with self.connection() as connection:
            row = connection.execute(
                "SELECT hidden_until FROM hidden WHERE identity = ?", (key,)).fetchone()
        if not row:
            return False
        until = _parse(row[0])
        return bool(until and _utc(now) < until)

    def hidden(self, now=None):
        """Множество identity, скрытых прямо сейчас."""
        moment = _utc(now)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT identity, hidden_until FROM hidden").fetchall()
        active = set()
        for identity, raw in rows:
            until = _parse(raw)
            if until and moment < until:
                active.add(identity)
        return active

    # ── показ ──────────────────────────────────────────────────────────────

    def record_surface(self, identity: str, now=None) -> str:
        key = (identity or "").strip()
        if not key:
            raise RuntimeStoreError("пустая identity для показа")
        moment = _iso(now)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO surfaced(identity, last_shown) VALUES(?,?) "
                "ON CONFLICT(identity) DO UPDATE SET last_shown=excluded.last_shown",
                (key, moment))
        return moment

    def last_surface(self, identity: str) -> str:
        key = (identity or "").strip()
        if not key:
            return ""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT last_shown FROM surfaced WHERE identity = ?", (key,)).fetchone()
        return row[0] if row else ""

    def surfaced(self):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT identity, last_shown FROM surfaced").fetchall()
        return {identity: shown for identity, shown in rows}

    # ── cooldown похожей мысли (не чаще раза в сутки) ──────────────────────

    def claim_related(self, now=None) -> bool:
        """True, если с прошлого показа прошло больше 24 часов. Атомарно."""
        moment = _utc(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'related_shown'").fetchone()
            if row:
                last = _parse(row[0])
                if last and moment - last < datetime.timedelta(hours=RELATED_COOLDOWN_HOURS):
                    return False
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('related_shown', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (moment.isoformat(),))
        return True

    def last_related(self) -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'related_shown'").fetchone()
        return row[0] if row else ""

    # ── фокус-сессии ──────────────────────────────────────────────

    @staticmethod
    def _write_focus(connection, record: FocusRecord, moment: str) -> None:
        connection.execute(
            "INSERT INTO focus_sessions(session_id, task_path, status, "
            "started_at, planned_end, planned_minutes, ended_at, end_reason, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "task_path=excluded.task_path, status=excluded.status, "
            "started_at=excluded.started_at, planned_end=excluded.planned_end, "
            "planned_minutes=excluded.planned_minutes, "
            "ended_at=excluded.ended_at, end_reason=excluded.end_reason, "
            "updated_at=excluded.updated_at",
            (record.session_id, record.task_path, record.status,
             _store_stamp(record.started_at), _store_stamp(record.planned_end),
             int(record.planned_minutes), _store_stamp(record.ended_at),
             record.end_reason, moment, moment))

    def _stage(self, record: FocusRecord, idempotency_key: str, now,
               supersede: bool) -> None:
        """Одна транзакция: (опц. суперсед) + запись сессии + idempotency-маппинг."""
        moment = _iso(now)
        token = (idempotency_key or "").strip()
        with self._transaction() as connection:
            if supersede:
                connection.execute(
                    "UPDATE focus_sessions SET status=?, ended_at=?, "
                    "end_reason=?, updated_at=? WHERE status=? AND session_id != ?",
                    (FOCUS_SUPERSEDED, moment, REASON_SUPERSEDED, moment,
                     FOCUS_ACTIVE, record.session_id))
            self._write_focus(connection, record, moment)
            if token:
                connection.execute(
                    "INSERT INTO idempotency(key, session_id, created_at) "
                    "VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
                    (token, record.session_id, moment))

    def activate_focus_with_claim(self, record: FocusRecord,
                                  idempotency_key: str = "", now=None) -> None:
        self._stage(record, idempotency_key, now, supersede=True)

    def update_focus_with_claim(self, record: FocusRecord,
                                idempotency_key: str = "", now=None) -> None:
        self._stage(record, idempotency_key, now, supersede=False)

    def activate_focus(self, record: FocusRecord, now=None) -> None:
        self._stage(record, "", now, supersede=True)

    def upsert_focus(self, record: FocusRecord, now=None) -> None:
        self._stage(record, "", now, supersede=False)

    def finish_expired(self, record: FocusRecord, now=None) -> str:
        """Атомарно: записать expired-сессию и уникальное событие focus_expired."""
        moment = _iso(now)
        message_key = f"{EVENT_FOCUS_EXPIRED}:{record.session_id}"
        with self._transaction() as connection:
            self._write_focus(connection, record, moment)
            connection.execute(
                "INSERT INTO focus_events(session_id, kind, status, message_key, "
                "attempts, lease_until, next_attempt_at, created_at, updated_at, "
                "delivered_at) VALUES(?,?,?,?,0,'',?,?,?,'') "
                "ON CONFLICT(message_key) DO NOTHING",
                (record.session_id, EVENT_FOCUS_EXPIRED, EVENT_PENDING,
                 message_key, moment, moment, moment))
        return message_key

    def get_focus(self, session_id):
        key = (session_id or "").strip()
        if not key:
            return None
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT {_FOCUS_COLUMNS} FROM focus_sessions "
                "WHERE session_id = ?", (key,)).fetchone()
        return _focus_record(row) if row else None

    def get_active_focus(self):
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT {_FOCUS_COLUMNS} FROM focus_sessions "
                "WHERE status = ? ORDER BY started_at DESC LIMIT 1",
                (FOCUS_ACTIVE,)).fetchone()
        return _focus_record(row) if row else None

    # ── идемпотентность запросов ─────────────────────────────────

    def idempotency_session(self, key: str) -> str:
        token = (key or "").strip()
        if not token:
            return ""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT session_id FROM idempotency WHERE key = ?",
                (token,)).fetchone()
        return row[0] if row else ""

    def record_idempotency(self, key: str, session_id: str, now=None) -> None:
        token = (key or "").strip()
        if not token:
            return
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO idempotency(key, session_id, created_at) "
                "VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
                (token, session_id, _iso(now)))

    # ── очередь уведомлений ────────────────────────────────────

    def enqueue_event(self, session_id: str, kind: str, now=None) -> str:
        sid = (session_id or "").strip()
        event_kind = (kind or "").strip()
        if not sid or not event_kind:
            raise RuntimeStoreError("пустое событие уведомления")
        message_key = f"{event_kind}:{sid}"
        moment = _iso(now)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO focus_events(session_id, kind, status, message_key, "
                "attempts, lease_until, next_attempt_at, created_at, updated_at, "
                "delivered_at) VALUES(?,?,?,?,0,'',?,?,?,'') "
                "ON CONFLICT(message_key) DO NOTHING",
                (sid, event_kind, EVENT_PENDING, message_key, moment, moment, moment))
        return message_key

    def get_event(self, event_id):
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM focus_events WHERE event_id = ?",
                (int(event_id),)).fetchone()
        return _focus_event(row) if row else None

    def pending_events(self):
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM focus_events "
                "WHERE status IN (?,?,?) ORDER BY event_id",
                (EVENT_PENDING, EVENT_LEASED, EVENT_FAILED)).fetchall()
        return tuple(_focus_event(row) for row in rows)

    def claim_event(self, now=None, lease_seconds: int = EVENT_LEASE_SECONDS):
        """Забирает одно готовое событие под lease.

        Возвращает `FocusEvent` с `message_key` и генерацией попытки
        (`attempts`), которую потом обязаны предъявить delivered/failed.
        """
        moment = _utc(now)
        moment_iso = moment.isoformat()
        lease_iso = (moment + datetime.timedelta(
            seconds=max(1, int(lease_seconds)))).isoformat()
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM focus_events WHERE "
                "(status = ? AND next_attempt_at <= ?) OR "
                "(status = ? AND next_attempt_at <= ?) OR "
                "(status = ? AND lease_until <= ?) "
                "ORDER BY event_id LIMIT 1",
                (EVENT_PENDING, moment_iso, EVENT_FAILED, moment_iso,
                 EVENT_LEASED, moment_iso)).fetchone()
            if row is None:
                return None
            attempts = int(row[5] or 0) + 1
            connection.execute(
                "UPDATE focus_events SET status=?, attempts=?, lease_until=?, "
                "updated_at=? WHERE event_id=?",
                (EVENT_LEASED, attempts, lease_iso, moment_iso, row[0]))
        return FocusEvent(event_id=int(row[0]), session_id=row[1], kind=row[2],
                          status=EVENT_LEASED, message_key=row[4],
                          attempts=attempts, lease_until=lease_iso,
                          next_attempt_at=row[7] or "")

    def mark_event_delivered(self, event_id, attempt, now=None) -> bool:
        """Терминальная доставка только текущей leased-генерации. True если изменил."""
        moment = _iso(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE focus_events SET status=?, delivered_at=?, lease_until='', "
                "next_attempt_at='', updated_at=? WHERE event_id=? AND status=? "
                "AND attempts=?",
                (EVENT_DELIVERED, moment, moment, int(event_id), EVENT_LEASED,
                 int(attempt)))
            return cursor.rowcount == 1

    def mark_event_failed(self, event_id, attempt, now=None) -> bool:
        """Провал только текущей leased-генерации; чужая/устаревшая — no-op."""
        moment = _utc(now)
        moment_iso = moment.isoformat()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT attempts FROM focus_events WHERE event_id=? AND "
                "status=? AND attempts=?",
                (int(event_id), EVENT_LEASED, int(attempt))).fetchone()
            if row is None:
                return False
            attempts = max(1, int(row[0] or 1))
            delay = min(EVENT_BASE_BACKOFF_SECONDS * (2 ** (attempts - 1)),
                        EVENT_MAX_BACKOFF_SECONDS)
            next_attempt = (moment + datetime.timedelta(
                seconds=delay)).isoformat()
            cursor = connection.execute(
                "UPDATE focus_events SET status=?, lease_until='', "
                "next_attempt_at=?, updated_at=? WHERE event_id=? AND status=? "
                "AND attempts=?",
                (EVENT_FAILED, next_attempt, moment_iso, int(event_id),
                 EVENT_LEASED, int(attempt)))
            return cursor.rowcount == 1

    # ── напоминания ─────────────────────────────────────────────

    @staticmethod
    def _cancel_reminder_events(connection, reminder_id, moment,
                                keep_generation=None) -> None:
        base = ("UPDATE reminder_events SET state=?, lease_until='', "
                "next_attempt_at='', updated_at=? WHERE reminder_id=? ")
        states = "(?,?,?)"
        if keep_generation is None:
            connection.execute(
                base + f"AND state IN {states}",
                (EVENT_CANCELLED, moment, reminder_id,
                 EVENT_PENDING, EVENT_LEASED, EVENT_FAILED))
        else:
            connection.execute(
                base + f"AND generation != ? AND state IN {states}",
                (EVENT_CANCELLED, moment, reminder_id, int(keep_generation),
                 EVENT_PENDING, EVENT_LEASED, EVENT_FAILED))

    def upsert_reminder(self, record: ReminderRecord,
                        idempotency_key: str = "", now=None) -> None:
        moment = _iso(now)
        token = (idempotency_key or "").strip()
        absolute = _store_stamp(record.absolute_time)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO reminders(reminder_id, task_path, absolute_time, "
                "state, generation, created_at, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(reminder_id) DO UPDATE SET "
                "task_path=excluded.task_path, "
                "absolute_time=excluded.absolute_time, state=excluded.state, "
                "generation=excluded.generation, updated_at=excluded.updated_at",
                (record.reminder_id, record.task_path, absolute, record.state,
                 int(record.generation or 1), moment, moment))
            if token:
                connection.execute(
                    "INSERT INTO reminder_idempotency(key, reminder_id, created_at) "
                    "VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
                    (token, record.reminder_id, moment))

    def get_reminder(self, reminder_id):
        key = (reminder_id or "").strip()
        if not key:
            return None
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT {_REMINDER_COLUMNS} FROM reminders WHERE reminder_id=?",
                (key,)).fetchone()
        return _reminder_record(row) if row else None

    def recover_reminder(self, record: ReminderRecord,
                         idempotency_key: str = "", now=None) -> ReminderRecord:
        """Атомарно восстановить projection+idempotency после Markdown-success/DB-fail.

        Сохраняет existing state/generation при том же absoluteTime; missing —
        active gen1; changed time — безопасная reconcile-семантика (cancel старых
        undelivered events, generation+1, active). Конфликт idempotency key с
        другим reminder_id — fail closed. Одна транзакция, без частичных шагов.
        """
        moment = _iso(now)
        token = (idempotency_key or "").strip()
        absolute = _store_stamp(record.absolute_time)
        with self._transaction() as connection:
            if token:
                row = connection.execute(
                    "SELECT reminder_id FROM reminder_idempotency WHERE key=?",
                    (token,)).fetchone()
                if row is not None and row[0] != record.reminder_id:
                    raise RuntimeStoreError(
                        "idempotency key принадлежит другому напоминанию")
            existing = connection.execute(
                "SELECT absolute_time, state, generation FROM reminders "
                "WHERE reminder_id=?", (record.reminder_id,)).fetchone()
            if existing is None:
                state, generation = REMINDER_ACTIVE, 1
                connection.execute(
                    "INSERT INTO reminders(reminder_id, task_path, absolute_time, "
                    "state, generation, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (record.reminder_id, record.task_path, absolute, state,
                     generation, moment, moment))
            elif _same_stamp(existing[0], absolute):
                state = existing[1]
                generation = int(existing[2] or 1)
                connection.execute(
                    "UPDATE reminders SET task_path=?, updated_at=? "
                    "WHERE reminder_id=?",
                    (record.task_path, moment, record.reminder_id))
            else:
                generation = int(existing[2] or 1) + 1
                self._cancel_reminder_events(connection, record.reminder_id,
                                             moment, keep_generation=generation)
                state = REMINDER_ACTIVE
                connection.execute(
                    "UPDATE reminders SET task_path=?, absolute_time=?, state=?, "
                    "generation=?, updated_at=? WHERE reminder_id=?",
                    (record.task_path, absolute, state, generation, moment,
                     record.reminder_id))
            if token:
                connection.execute(
                    "INSERT INTO reminder_idempotency(key, reminder_id, created_at) "
                    "VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
                    (token, record.reminder_id, moment))
        return ReminderRecord(reminder_id=record.reminder_id,
                              task_path=record.task_path, absolute_time=absolute,
                              state=state, generation=generation)

    def snooze_reminder(self, reminder_id, task_path, absolute_time,
                        idempotency_key: str = "", now=None) -> int:
        moment = _iso(now)
        token = (idempotency_key or "").strip()
        absolute = _store_stamp(absolute_time)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT absolute_time, generation FROM reminders "
                "WHERE reminder_id=?", (reminder_id,)).fetchone()
            if row is None:
                generation = 1
                connection.execute(
                    "INSERT INTO reminders(reminder_id, task_path, absolute_time, "
                    "state, generation, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (reminder_id, task_path, absolute, REMINDER_ACTIVE,
                     generation, moment, moment))
            elif _same_stamp(row[0], absolute):
                generation = int(row[1] or 1)
                connection.execute(
                    "UPDATE reminders SET task_path=?, updated_at=? "
                    "WHERE reminder_id=?", (task_path, moment, reminder_id))
            else:
                generation = int(row[1] or 1) + 1
                self._cancel_reminder_events(connection, reminder_id, moment,
                                             keep_generation=generation)
                connection.execute(
                    "UPDATE reminders SET task_path=?, absolute_time=?, state=?, "
                    "generation=?, updated_at=? WHERE reminder_id=?",
                    (task_path, absolute, REMINDER_ACTIVE, generation,
                     moment, reminder_id))
            if token:
                connection.execute(
                    "INSERT INTO reminder_idempotency(key, reminder_id, created_at) "
                    "VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
                    (token, reminder_id, moment))
        return generation

    def cancel_reminder(self, reminder_id, now=None) -> None:
        moment = _iso(now)
        with self._transaction() as connection:
            connection.execute(
                "UPDATE reminders SET state=?, updated_at=? WHERE reminder_id=?",
                (REMINDER_CANCELLED, moment, reminder_id))
            self._cancel_reminder_events(connection, reminder_id, moment)

    def cancel_reminder_event(self, event_id, now=None) -> bool:
        """Отменяет только недоставленный event, не трогая projection."""
        moment = _iso(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE reminder_events SET state=?, lease_until='', "
                "next_attempt_at='', updated_at=? WHERE event_id=? "
                "AND state IN (?,?,?)",
                (EVENT_CANCELLED, moment, int(event_id), EVENT_PENDING,
                 EVENT_LEASED, EVENT_FAILED))
            return cursor.rowcount == 1

    def reconcile_reminders(self, records, now=None):
        """Markdown → SQLite: delivered сохраняется, время-изменение — новая generation."""
        moment = _iso(now)
        active_ids = {record.reminder_id for record in records}
        changed = 0
        cancelled = 0
        with self._transaction() as connection:
            for record in records:
                absolute = _store_stamp(record.absolute_time)
                row = connection.execute(
                    "SELECT absolute_time, state, generation FROM reminders "
                    "WHERE reminder_id=?", (record.reminder_id,)).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO reminders(reminder_id, task_path, "
                        "absolute_time, state, generation, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (record.reminder_id, record.task_path, absolute,
                         REMINDER_ACTIVE, 1, moment, moment))
                    changed += 1
                    continue
                if _same_stamp(row[0], absolute) and row[1] in (
                        REMINDER_ACTIVE, REMINDER_DUE, REMINDER_DELIVERED,
                        REMINDER_CANCELLED):
                    connection.execute(
                        "UPDATE reminders SET task_path=?, updated_at=? "
                        "WHERE reminder_id=?",
                        (record.task_path, moment, record.reminder_id))
                    continue
                generation = int(row[2] or 1) + 1
                self._cancel_reminder_events(connection, record.reminder_id,
                                             moment, keep_generation=generation)
                connection.execute(
                    "UPDATE reminders SET task_path=?, absolute_time=?, state=?, "
                    "generation=?, updated_at=? WHERE reminder_id=?",
                    (record.task_path, absolute, REMINDER_ACTIVE, generation,
                     moment, record.reminder_id))
                changed += 1
            rows = connection.execute(
                "SELECT reminder_id, state FROM reminders").fetchall()
            for reminder_id, state in rows:
                if reminder_id in active_ids or state in (
                        REMINDER_DELIVERED, REMINDER_CANCELLED):
                    continue
                connection.execute(
                    "UPDATE reminders SET state=?, updated_at=? "
                    "WHERE reminder_id=?",
                    (REMINDER_CANCELLED, moment, reminder_id))
                self._cancel_reminder_events(connection, reminder_id, moment)
                cancelled += 1
        return changed, cancelled

    def next_active_reminder_time(self) -> str:
        """Ближайший active deadline для scheduler wait. Пусто — нет active."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT MIN(absolute_time) FROM reminders WHERE state=?",
                (REMINDER_ACTIVE,)).fetchone()
        return row[0] if row and row[0] else ""

    def collect_due(self, now=None, limit=REMINDER_MAX_BATCH):
        """Атомарно: active → due + уникальное событие delivery на generation."""
        bounded = _bounded_limit(limit)
        if bounded == 0:
            return ()
        moment = _utc(now)
        moment_iso = moment.isoformat()
        events = []
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {_REMINDER_COLUMNS} FROM reminders WHERE state=? "
                "AND absolute_time<=? ORDER BY absolute_time, reminder_id LIMIT ?",
                (REMINDER_ACTIVE, moment_iso, bounded)).fetchall()
            for row in rows:
                record = _reminder_record(row)
                message_key = (f"{REMINDER_EVENT_DUE}:{record.reminder_id}:"
                               f"{record.generation}")
                connection.execute(
                    "UPDATE reminders SET state=?, updated_at=? "
                    "WHERE reminder_id=? AND state=?",
                    (REMINDER_DUE, moment_iso, record.reminder_id,
                     REMINDER_ACTIVE))
                connection.execute(
                    "INSERT INTO reminder_events(reminder_id, generation, state, "
                    "message_key, attempts, lease_until, next_attempt_at, "
                    "created_at, updated_at, delivered_at) "
                    "VALUES(?,?,?,?,0,'',?,?,?,'') "
                    "ON CONFLICT(message_key) DO NOTHING",
                    (record.reminder_id, record.generation, EVENT_PENDING,
                     message_key, moment_iso, moment_iso, moment_iso))
                event_row = connection.execute(
                    f"SELECT {_REMINDER_EVENT_COLUMNS} FROM reminder_events "
                    "WHERE message_key=?", (message_key,)).fetchone()
                events.append(_reminder_event(event_row))
        return tuple(events)

    def get_reminder_event(self, event_id):
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT {_REMINDER_EVENT_COLUMNS} FROM reminder_events "
                "WHERE event_id=?", (int(event_id),)).fetchone()
        return _reminder_event(row) if row else None

    def pending_reminder_events(self):
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT {_REMINDER_EVENT_COLUMNS} FROM reminder_events "
                "WHERE state IN (?,?,?) ORDER BY event_id",
                (EVENT_PENDING, EVENT_LEASED, EVENT_FAILED)).fetchall()
        return tuple(_reminder_event(row) for row in rows)

    def claim_reminder_event(self, now=None,
                             lease_seconds=REMINDER_LEASE_SECONDS):
        moment = _utc(now)
        moment_iso = moment.isoformat()
        lease_iso = (moment + datetime.timedelta(
            seconds=max(1, int(lease_seconds)))).isoformat()
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {_REMINDER_EVENT_COLUMNS} FROM reminder_events WHERE "
                "(state = ? AND next_attempt_at <= ?) OR "
                "(state = ? AND next_attempt_at <= ?) OR "
                "(state = ? AND lease_until <= ?) "
                "ORDER BY event_id LIMIT 1",
                (EVENT_PENDING, moment_iso, EVENT_FAILED, moment_iso,
                 EVENT_LEASED, moment_iso)).fetchone()
            if row is None:
                return None
            attempts = int(row[5] or 0) + 1
            connection.execute(
                "UPDATE reminder_events SET state=?, attempts=?, lease_until=?, "
                "updated_at=? WHERE event_id=?",
                (EVENT_LEASED, attempts, lease_iso, moment_iso, row[0]))
        return ReminderEvent(event_id=int(row[0]), reminder_id=row[1],
                             generation=int(row[2] or 1), state=EVENT_LEASED,
                             message_key=row[4], attempts=attempts,
                             lease_until=lease_iso, next_attempt_at=row[7] or "")

    def mark_reminder_event_delivered(self, event_id, attempt, now=None) -> bool:
        moment = _iso(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE reminder_events SET state=?, delivered_at=?, "
                "lease_until='', next_attempt_at='', updated_at=? "
                "WHERE event_id=? AND state=? AND attempts=?",
                (EVENT_DELIVERED, moment, moment, int(event_id), EVENT_LEASED,
                 int(attempt)))
            if cursor.rowcount != 1:
                return False
            row = connection.execute(
                "SELECT reminder_id FROM reminder_events WHERE event_id=?",
                (int(event_id),)).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE reminders SET state=?, updated_at=? "
                    "WHERE reminder_id=?",
                    (REMINDER_DELIVERED, moment, row[0]))
            return True

    def mark_reminder_event_failed(self, event_id, attempt, now=None) -> bool:
        moment = _utc(now)
        moment_iso = moment.isoformat()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT attempts FROM reminder_events WHERE event_id=? AND "
                "state=? AND attempts=?",
                (int(event_id), EVENT_LEASED, int(attempt))).fetchone()
            if row is None:
                return False
            attempts = max(1, int(row[0] or 1))
            delay = min(EVENT_BASE_BACKOFF_SECONDS * (2 ** (attempts - 1)),
                        EVENT_MAX_BACKOFF_SECONDS)
            next_attempt = (moment + datetime.timedelta(
                seconds=delay)).isoformat()
            cursor = connection.execute(
                "UPDATE reminder_events SET state=?, lease_until='', "
                "next_attempt_at=?, updated_at=? WHERE event_id=? AND state=? "
                "AND attempts=?",
                (EVENT_FAILED, next_attempt, moment_iso, int(event_id),
                 EVENT_LEASED, int(attempt)))
            return cursor.rowcount == 1

    def reminder_idempotency(self, key: str) -> str:
        token = (key or "").strip()
        if not token:
            return ""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT reminder_id FROM reminder_idempotency WHERE key=?",
                (token,)).fetchone()
        return row[0] if row else ""

    def record_reminder_idempotency(self, key: str, reminder_id: str,
                                    now=None) -> None:
        token = (key or "").strip()
        if not token:
            return
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO reminder_idempotency(key, reminder_id, created_at) "
                "VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
                (token, reminder_id, _iso(now)))


__all__ = [
    "RuntimeStore", "RuntimeStoreError", "default_db_path",
    "FocusRecord", "FocusEvent", "ReminderRecord", "ReminderEvent",
    "SCHEMA_VERSION", "HIDE_DAYS", "RELATED_COOLDOWN_HOURS",
    "FOCUS_ACTIVE", "FOCUS_SUPERSEDED", "FOCUS_STOPPED", "FOCUS_EXPIRED",
    "FOCUS_ORPHANED", "REASON_MANUAL", "REASON_EXPIRED", "REASON_SUPERSEDED",
    "EVENT_FOCUS_EXPIRED", "EVENT_PENDING", "EVENT_LEASED",
    "EVENT_DELIVERED", "EVENT_FAILED", "EVENT_CANCELLED",
    "REMINDER_ACTIVE", "REMINDER_DUE", "REMINDER_DELIVERED",
    "REMINDER_CANCELLED", "REMINDER_EVENT_DUE",
]
