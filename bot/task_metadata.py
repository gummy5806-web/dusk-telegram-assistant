#!/usr/bin/env python3
"""Граница TaskNotes-метаданных: точный путь, closed frontmatter, timeEntries.

Модуль делает ровно две вещи, которые нельзя размазывать по вызывающим:

* читает `timeEntries` из закрытой YAML-шапки конкретной карточки
  `TaskNotes/Tasks/*.md` (flow или block), сохраняя незнакомые ключи записей;
* меняет только свои top-level поля (`timeEntries`, `status`, `dateModified`),
  оставляя тело заметки байт-в-байт и не переставляя чужие поля без нужды.

Долгосрочная истина — Markdown: TaskNotes 4.5.1 обязан понимать `startTime` и
`endTime`, а незнакомые Dusk-поля (`plannedEnd`, `duskSessionId`, …) ему
безразличны и должны сохраняться. Запись идёт через временный файл в той же
папке, `fsync`, `os.replace` с сохранением прав; при сбое временный файл
убирается. Ошибки границы — явный `TaskMetadataError`, без частичной записи.

Модуль не знает про Telegram, сеть и SQLite. Блокировка — та же vault-wide
flock-идентичность, что у `dispatch._capture_lock`, чтобы focus и смена статуса
не гоняли одну карточку.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

import dispatch
import frontmatter

TASKS_REL = "TaskNotes/Tasks"
DUSK_SOURCE = "Dusk Telegram"
TIME_ENTRIES_KEY = "timeEntries"
REMINDERS_KEY = "reminders"
DUSK_REMINDER_PREFIX = "dusk_"
OWNED_SCALARS = ("status", "dateModified")
_OWNED_KEYS = (TIME_ENTRIES_KEY, REMINDERS_KEY) + OWNED_SCALARS
_OPEN = "---"
MAX_TASK_BYTES = 512 * 1024
# Literal marker: если он есть в сырых байтах, карточка может быть Dusk.
DUSK_MARKER = b"duskSessionId"
DUSK_REMINDER_MARKER = b"dusk_"


class TaskMetadataError(RuntimeError):
    """Карточка TaskNotes недоступна для точного чтения или правки."""


# ── значения TimeEntry ──────────────────────────────────────────────────────

def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def _stringify(value):
    """Приводит YAML-значение к строково-безопасному виду.

    PyYAML сам разбирает ISO-таймстемпы в `datetime`; долгосрочная истина —
    строка, поэтому timestamps возвращаются в ISO-форму, равно как и незнакомые
    вложенные ключи.
    """
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _stringify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify(item) for item in value]
    return value


@dataclass(frozen=True)
class TimeEntry:
    """Одна запись времени. Незнакомые ключи живут в `data` и не теряются."""

    data: Mapping[str, Any] = field(default_factory=dict)

    def _value(self, name: str) -> str:
        return _text(self.data.get(name))

    @property
    def start_time(self) -> str:
        return self._value("startTime")

    @property
    def end_time(self) -> str:
        return self._value("endTime")

    @property
    def planned_end(self) -> str:
        return self._value("plannedEnd")

    @property
    def planned_minutes(self) -> int:
        raw = self.data.get("plannedMinutes")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    @property
    def dusk_session_id(self) -> str:
        return self._value("duskSessionId")

    @property
    def description(self) -> str:
        return self._value("description")

    @property
    def source(self) -> str:
        return self._value("source")

    def is_dusk(self) -> bool:
        return bool(self.dusk_session_id)

    def is_open(self) -> bool:
        return not self.end_time

    def as_dict(self) -> dict:
        return dict(self.data)

    def with_end(self, end_iso: str) -> "TimeEntry":
        data = dict(self.data)
        data["endTime"] = end_iso
        return TimeEntry(data)

    def with_plan(self, planned_end: str, planned_minutes: int) -> "TimeEntry":
        data = dict(self.data)
        data["plannedEnd"] = planned_end
        data["plannedMinutes"] = int(planned_minutes)
        return TimeEntry(data)

    def with_extra(self, values: Mapping[str, Any]) -> "TimeEntry":
        data = dict(self.data)
        data.update(values)
        return TimeEntry(data)


@dataclass(frozen=True)
class TaskSnapshot:
    """Прочитанная карточка: скаляры, все `timeEntries` и `reminders`."""

    path: Path
    relpath: str
    title: str
    status: str
    date_modified: str
    time_entries: tuple
    reminders: tuple = ()
    scheduled: str = ""
    due: str = ""
    completed: str = ""


@dataclass(frozen=True)
class ReminderEntry:
    """Одно TaskNotes-напоминание. Незнакомые ключи живут в `data`."""

    data: Mapping[str, Any] = field(default_factory=dict)

    def _value(self, name: str) -> str:
        return _text(self.data.get(name))

    @property
    def id(self) -> str:
        return self._value("id")

    @property
    def type(self) -> str:
        return self._value("type")

    @property
    def absolute_time(self) -> str:
        return self._value("absoluteTime")

    @property
    def description(self) -> str:
        return self._value("description")

    def is_dusk(self) -> bool:
        return self.id.startswith(DUSK_REMINDER_PREFIX)

    def as_dict(self) -> dict:
        return dict(self.data)

    def with_absolute(self, when_iso: str) -> "ReminderEntry":
        data = dict(self.data)
        data["absoluteTime"] = when_iso
        return ReminderEntry(data)

    def with_extra(self, values: Mapping[str, Any]) -> "ReminderEntry":
        data = dict(self.data)
        data.update(values)
        return ReminderEntry(data)


# ── точный путь ─────────────────────────────────────────────────────────────

def resolve_task_path(vault, task) -> Path:
    """Точный прямой `.md`-ребёнок `TaskNotes/Tasks` без symlink и выхода."""
    vault = Path(vault)
    tasks_dir = vault / TASKS_REL
    if isinstance(task, Path):
        candidate = task if task.is_absolute() else vault / task
    else:
        raw = str(task or "").strip()
        if not raw:
            raise TaskMetadataError("пустой путь задачи")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (tasks_dir / raw) if candidate.parent == Path(".") else vault / candidate

    if candidate.suffix != ".md":
        raise TaskMetadataError("карточка должна быть .md")
    if candidate.is_symlink() or not candidate.is_file():
        raise TaskMetadataError(f"карточка не найдена: {candidate.name}")
    try:
        tasks_resolved = tasks_dir.resolve()
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as error:
        raise TaskMetadataError("не удалось разрешить путь карточки") from error
    if resolved.parent != tasks_resolved or not resolved.name.endswith(".md"):
        raise TaskMetadataError("карточка вне TaskNotes/Tasks")
    try:
        resolved.relative_to(vault.resolve())
    except ValueError as error:
        raise TaskMetadataError("карточка вне валта") from error
    return candidate


def direct_task_paths(vault, limit: int = 2000) -> tuple:
    """Bounded direct `.md` children of `TaskNotes/Tasks`, sorted by name.

    The directory is inspected only until `limit + 1` relevant entries.  The
    extra entry is an explicit fail-closed signal instead of silently ignoring
    cards beyond the dashboard/state-machine scan boundary.  Symlink children
    are returned so each caller can apply its existing policy (focus skips;
    reminders reject); the tasks directory itself may never be a symlink or
    escape the vault.
    """
    if isinstance(limit, bool):
        raise TaskMetadataError("лимит карточек должен быть положительным числом")
    try:
        bound = int(limit)
    except (TypeError, ValueError) as error:
        raise TaskMetadataError("лимит карточек должен быть положительным числом") from error
    if bound < 1:
        raise TaskMetadataError("лимит карточек должен быть положительным числом")

    root = Path(vault)
    tasks_dir = root / TASKS_REL
    try:
        if tasks_dir.is_symlink():
            raise TaskMetadataError("каталог TaskNotes/Tasks недоступен")
        if not tasks_dir.exists():
            return ()
        if not tasks_dir.is_dir():
            raise TaskMetadataError("каталог TaskNotes/Tasks недоступен")
        tasks_dir.resolve().relative_to(root.resolve())
    except TaskMetadataError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise TaskMetadataError("каталог TaskNotes/Tasks вне валта") from error

    paths = []
    try:
        with os.scandir(tasks_dir) as entries:
            for entry in entries:
                if entry.name.startswith(".") or not entry.name.endswith(".md"):
                    continue
                if not entry.is_symlink() and not entry.is_file(follow_symlinks=False):
                    continue
                paths.append(tasks_dir / entry.name)
                if len(paths) > bound:
                    raise TaskMetadataError(
                        "слишком много карточек TaskNotes для bounded-скана")
    except TaskMetadataError:
        raise
    except OSError as error:
        raise TaskMetadataError("не удалось просканировать TaskNotes/Tasks") from error
    return tuple(sorted(paths, key=lambda path: path.name))


@contextmanager
def vault_lock(vault=None):
    """Та же блокировка, что у диспетчера: focus и статус не гоняются."""
    with dispatch._capture_lock(vault):
        yield


# ── чтение ──────────────────────────────────────────────────────────────────

def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_TASK_BYTES:
            raise TaskMetadataError(f"карточка слишком большая: {path.name}")
        data = path.read_bytes()
    except OSError as error:
        raise TaskMetadataError(f"карточка не читается: {path.name}") from error
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TaskMetadataError(f"карточка не UTF-8: {path.name}") from error


def _mapping_pairs(head: str):
    try:
        node = yaml.compose(head)
    except (yaml.YAMLError, RecursionError) as error:
        raise TaskMetadataError("повреждённый YAML в шапке") from error
    if node is None:
        return []
    if not isinstance(node, yaml.MappingNode):
        raise TaskMetadataError("шапка не является YAML-объектом")
    if node.flow_style:
        # Плоскую flow-шапку нельзя править строками, не потеряв чужие ключи.
        raise TaskMetadataError("flow-шапка не поддерживается")
    pairs = []
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode) or key_node.value is None:
            raise TaskMetadataError("неподдерживаемый ключ шапки")
        pairs.append((str(key_node.value), key_node, value_node))
    return pairs


def _reject_duplicates(pairs) -> None:
    seen = set()
    for name, _key_node, _value_node in pairs:
        if name not in _OWNED_KEYS:
            continue
        if name in seen:
            raise TaskMetadataError(f"дублируется поле {name}")
        seen.add(name)


def _load_data(head: str) -> dict:
    try:
        data = yaml.safe_load(head)
    except (yaml.YAMLError, RecursionError) as error:
        raise TaskMetadataError("повреждённый YAML в шапке") from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TaskMetadataError("шапка не является YAML-объектом")
    return data


def _parse_entries(raw) -> tuple:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TaskMetadataError("timeEntries должен быть списком")
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            raise TaskMetadataError("запись timeEntries должна быть объектом")
        try:
            entries.append(TimeEntry(_stringify(item)))
        except RecursionError as error:
            raise TaskMetadataError("рекурсивный YAML-алиас") from error
    return tuple(entries)


def _parse_reminders(raw) -> tuple:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TaskMetadataError("reminders должен быть списком")
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            raise TaskMetadataError("запись reminders должна быть объектом")
        try:
            entries.append(ReminderEntry(_stringify(item)))
        except RecursionError as error:
            raise TaskMetadataError("рекурсивный YAML-алиас") from error
    return tuple(entries)


def read_task(vault, task) -> TaskSnapshot:
    """Читает карточку. Malformed/open/duplicate-шапка — fail closed."""
    path = resolve_task_path(vault, task)
    text = _read_text(path)
    head = frontmatter.region(text)
    if not head:
        raise TaskMetadataError("нет закрытой YAML-шапки")
    pairs = _mapping_pairs(head)
    _reject_duplicates(pairs)
    data = _load_data(head)
    try:
        relpath = path.resolve().relative_to(Path(vault).resolve()).as_posix()
    except ValueError:
        relpath = path.name
    return TaskSnapshot(
        path=path,
        relpath=relpath,
        title=_text(data.get("title")),
        status=_text(data.get("status")),
        date_modified=_text(data.get("dateModified")),
        time_entries=_parse_entries(data.get(TIME_ENTRIES_KEY)),
        reminders=_parse_reminders(data.get(REMINDERS_KEY)),
        scheduled=_text(data.get("scheduled")),
        due=_text(data.get("due")),
        completed=_text(data.get("completedDate")),
    )


# ── запись ──────────────────────────────────────────────────────────────────

def _dump_list(name: str, entries: Sequence) -> str:
    payload = [entry.as_dict() if hasattr(entry, "as_dict") else dict(entry)
               for entry in entries]
    if not payload:
        return name + ": []\n"
    dumped = yaml.safe_dump(
        payload, sort_keys=False, allow_unicode=True,
        default_flow_style=False, width=1000)
    body = "".join("  " + line + "\n" for line in dumped.splitlines())
    return name + ":\n" + body


def _replace_lists(head: str, pairs, updates) -> str:
    """Заменяет только переданные top-level списки, сохраняя остальные строки."""
    lines = head.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    spans = {}
    for name, key_node, value_node in pairs:
        if name in updates and name not in spans:
            start = key_node.start_mark.line
            end = max(value_node.end_mark.line, key_node.start_mark.line + 1)
            spans[name] = (start, end)
    # Снизу вверх, чтобы сдвиг строк не ломал более ранние спаны.
    for name in sorted(spans, key=lambda item: spans[item][0], reverse=True):
        start, end = spans[name]
        block = _dump_list(name, updates[name]).split("\n")
        if block and block[-1] == "":
            block.pop()
        lines[start:end] = block
    for name, entries in updates.items():
        if name in spans:
            continue
        block = _dump_list(name, entries).split("\n")
        if block and block[-1] == "":
            block.pop()
        lines.extend(block)
    return "\n".join(lines) + "\n"


def _discard_temp(tmp_path: str, handle) -> None:
    """Убирает временный файл, не подменяя исходную ошибку записи."""
    if not handle.closed:
        with contextlib.suppress(OSError):
            handle.close()
    with contextlib.suppress(OSError):
        os.unlink(tmp_path)


def _fsync_dir(directory: Path) -> None:
    """fsync каталога после replace — долговечность переименования."""
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError as error:
        raise TaskMetadataError("не удалось открыть каталог карточки") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise TaskMetadataError("не удалось fsync каталог карточки") from error
    finally:
        # Сбой close не должен подменять исходную ошибку fsync/replace.
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _has_marker(vault, task, marker: bytes) -> bool:
    """Bounded-проверка literal-маркера в сырых байтах карточки.

    True — маркер найден; False — файл прочитан в пределах `MAX_TASK_BYTES` и
    маркера нет. TaskMetadataError — прочитать/доказать нельзя.
    """
    path = resolve_task_path(vault, task)
    try:
        before = path.stat()
    except OSError as error:
        raise TaskMetadataError(f"карточка не читается: {path.name}") from error
    if before.st_size > MAX_TASK_BYTES:
        raise TaskMetadataError(f"карточка слишком большая: {path.name}")
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_TASK_BYTES + 1)
        after = path.stat()
    except OSError as error:
        raise TaskMetadataError(f"карточка не читается: {path.name}") from error
    if len(data) > MAX_TASK_BYTES:
        raise TaskMetadataError(f"карточка слишком большая: {path.name}")
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise TaskMetadataError(
            f"карточка изменилась во время чтения: {path.name}")
    return marker in data


def has_dusk_marker(vault, task) -> bool:
    """Может ли карточка содержать Dusk-фокус-запись."""
    return _has_marker(vault, task, DUSK_MARKER)


def has_reminder_marker(vault, task) -> bool:
    """Может ли карточка содержать Dusk-напоминание."""
    return _has_marker(vault, task, DUSK_REMINDER_MARKER)


def _atomic_write(path: Path, text: str) -> None:
    """Временный файл рядом, fsync, os.replace; права исходника сохраняются."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise TaskMetadataError(f"не читаются права карточки: {path.name}") from error
    try:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent),
            prefix=path.name + ".", suffix=".tmp", delete=False)
    except OSError as error:
        raise TaskMetadataError(
            f"не удалось создать временный файл: {path.name}") from error
    tmp_path = handle.name
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException as error:
        _discard_temp(tmp_path, handle)
        if isinstance(error, TaskMetadataError):
            raise
        if isinstance(error, Exception):
            raise TaskMetadataError(
                f"не удалось записать карточку: {path.name}") from error
        raise
    _fsync_dir(path.parent)


def write_task(vault, task, *, time_entries=None, reminders=None,
               scalars=None) -> TaskSnapshot:
    """Меняет только переданные top-level списки и разрешённые скаляры.

    `time_entries=None`/`reminders=None` означает «не трогать это поле»: focus
    не должен затирать reminders и наоборот. Тело — байт-в-байт.
    """
    path = resolve_task_path(vault, task)
    original = _read_text(path)
    head = frontmatter.region(original)
    if not head:
        raise TaskMetadataError("нет закрытой YAML-шапки")
    pairs = _mapping_pairs(head)
    _reject_duplicates(pairs)
    _load_data(head)

    owned = dict(scalars or {})
    unknown = set(owned) - set(OWNED_SCALARS)
    if unknown:
        raise TaskMetadataError(f"нельзя менять поля: {sorted(unknown)}")

    updates = {}
    if time_entries is not None:
        updates[TIME_ENTRIES_KEY] = time_entries
    if reminders is not None:
        updates[REMINDERS_KEY] = reminders

    new_head = _replace_lists(head, pairs, updates) if updates else head
    new_text = original[:4] + new_head + original[4 + len(head):]
    for name in OWNED_SCALARS:
        if name not in owned:
            continue
        new_text, ok = frontmatter.set_field(new_text, name, _text(owned[name]))
        if not ok:
            raise TaskMetadataError(f"не удалось записать поле {name}")

    _atomic_write(path, new_text)
    return read_task(vault, path)


__all__ = [
    "TaskMetadataError", "TimeEntry", "ReminderEntry", "TaskSnapshot",
    "resolve_task_path", "direct_task_paths", "read_task", "write_task", "vault_lock",
    "TASKS_REL", "DUSK_SOURCE", "TIME_ENTRIES_KEY", "REMINDERS_KEY",
    "DUSK_REMINDER_PREFIX", "OWNED_SCALARS",
    "MAX_TASK_BYTES", "has_dusk_marker", "has_reminder_marker",
    "DUSK_MARKER", "DUSK_REMINDER_MARKER",
]
