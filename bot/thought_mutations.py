#!/usr/bin/env python3
"""Bounded, recoverable edits for public Dusk thoughts.

Only the two sources exposed by :mod:`thought_feed` can be changed.  Every
operation revalidates the content identity immediately before writing, so a
stale Telegram callback cannot address an arbitrary vault path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

import inbox_core
import thought_feed

TRASH_REL = ".trash/dusk-thoughts"
MAX_EDIT_CHARS = 64 * 1024
MAX_BACKUP_BYTES = thought_feed.MAX_FILE_BYTES * 4
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Mutation:
    ok: bool
    message: str
    mutated: bool = False
    kind: str = ""
    path: str = ""
    identity: str = ""
    backup: str = ""


def edit(vault, identity: str, text: str) -> Mutation:
    """Replace the full editable body of one current thought.

    Structured notes retain their exact frontmatter and file path. Inbox input
    is flattened because an Inbox thought is exactly one Markdown list line.
    """
    root = Path(vault)
    new_text = _clean_edit(text)
    if not new_text:
        return Mutation(False, "Пустой текст — мысль не изменена.")
    if len(new_text) > MAX_EDIT_CHARS:
        return Mutation(False, "Текст слишком длинный — мысль не изменена.")
    current = thought_feed.load_thought(root, identity)
    if current is None:
        return Mutation(False, "⚠️ Мысль устарела — открой «🧠 Мысли» заново.")
    if current.kind == "inbox":
        return _edit_inbox(root, identity, new_text)
    return _edit_structured(root, identity, new_text)


def delete(vault, identity: str) -> Mutation:
    """Move a current thought into an ignored, recoverable trash envelope."""
    root = Path(vault)
    current = thought_feed.load_thought(root, identity)
    if current is None:
        return Mutation(False, "⚠️ Мысль устарела — открой «🧠 Мысли» заново.")
    if current.kind == "inbox":
        return _delete_inbox(root, identity)
    return _delete_structured(root, identity)


def restore(vault, backup: str) -> Mutation:
    """Restore exactly one previously deleted thought without overwriting data."""
    root = Path(vault)
    record_path = _backup_path(root, backup)
    if record_path is None:
        return Mutation(False, "⚠️ Восстановление устарело.")
    try:
        if record_path.stat().st_size > MAX_BACKUP_BYTES:
            return Mutation(False, "⚠️ Резервная копия слишком большая.")
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return Mutation(False, "⚠️ Резервная копия недоступна.")
    if not isinstance(record, dict):
        return Mutation(False, "⚠️ Резервная копия недоступна.")
    if record.get("kind") == "inbox":
        return _restore_inbox(root, record, record_path)
    if record.get("kind") == "fleeting":
        return _restore_structured(root, record, record_path)
    return Mutation(False, "⚠️ Резервная копия недоступна.")


def full_text(vault, identity: str) -> str:
    """Return full editable content after identity validation, else an empty string."""
    root = Path(vault)
    current = thought_feed.load_thought(root, identity)
    if current is None:
        return ""
    if current.kind == "inbox":
        found = _find_inbox_line(root, identity)
        return found["body"] if found else ""
    path = _structured_path(root, current.path)
    if path is None:
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return _structured_editable_body(raw).strip()


def _edit_structured(root: Path, identity: str, text: str) -> Mutation:
    current = thought_feed.load_thought(root, identity)
    if current is None or current.kind != "fleeting":
        return Mutation(False, "⚠️ Мысль устарела — открой «🧠 Мысли» заново.")
    path = _structured_path(root, current.path)
    if path is None:
        return Mutation(False, "⚠️ Мысль недоступна.")
    with inbox_core.locked_write(path):
        raw = _read_current_structured(root, path, identity)
        if raw is None:
            return Mutation(False, "⚠️ Мысль устарела — открой «🧠 Мысли» заново.")
        rewritten = _replace_structured_editable_body(raw, text)
        if rewritten is None:
            return Mutation(False, "⚠️ У этой заметки нет безопасного раздела «Мысль».")
        if rewritten == raw:
            return Mutation(False, "Мысль уже содержит этот текст.")
        inbox_core.atomic_write(path, rewritten)
    rel = current.path
    changed = thought_feed.load_thought(root, thought_feed._identity_text(rel, rewritten))
    return Mutation(True, "✏️ Мысль исправлена.", True, "thought", rel,
                    changed.identity if changed else "")


def _edit_inbox(root: Path, identity: str, text: str) -> Mutation:
    path = root / thought_feed.INBOX_REL
    if not _safe_inbox(root, path):
        return Mutation(False, "⚠️ Inbox недоступен.")
    with inbox_core.locked_write(path):
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return Mutation(False, "⚠️ Inbox недоступен.")
        located = _locate_inbox_text(raw, identity)
        if located is None:
            return Mutation(False, "⚠️ Мысль устарела — открой «🧠 Мысли» заново.")
        line = located["line"]
        new_line = _replace_inbox_body(line, text)
        if new_line == line:
            return Mutation(False, "Мысль уже содержит этот текст.")
        lines = raw.splitlines()
        lines[located["index"]] = new_line
        rewritten = "\n".join(lines) + ("\n" if raw.endswith("\n") else "")
        inbox_core.atomic_write(path, rewritten)
    return Mutation(True, "✏️ Мысль исправлена.", True, "inbox", thought_feed.INBOX_REL,
                    thought_feed._identity_text(thought_feed.INBOX_REL, new_line))


def _delete_structured(root: Path, identity: str) -> Mutation:
    current = thought_feed.load_thought(root, identity)
    if current is None or current.kind != "fleeting":
        return Mutation(False, "⚠️ Мысль устарела — открой «🧠 Мысли» заново.")
    path = _structured_path(root, current.path)
    if path is None:
        return Mutation(False, "⚠️ Мысль недоступна.")
    with inbox_core.locked_write(path):
        raw = _read_current_structured(root, path, identity)
        if raw is None:
            return Mutation(False, "⚠️ Мысль устарела — открой «🧠 Мысли» заново.")
        try:
            backup = _write_backup(root, {"kind": "fleeting", "path": current.path,
                                          "identity": identity, "content": raw})
        except (OSError, ValueError):
            return Mutation(False, "⚠️ Корзина недоступна — мысль не удалена.")
        try:
            path.unlink()
        except OSError:
            _remove_backup(backup)
            return Mutation(False, "Не смог удалить мысль.")
    return Mutation(True, "🗑 Мысль перемещена в корзину.", True, "thought",
                    current.path, backup=backup)


def _delete_inbox(root: Path, identity: str) -> Mutation:
    path = root / thought_feed.INBOX_REL
    if not _safe_inbox(root, path):
        return Mutation(False, "⚠️ Inbox недоступен.")
    with inbox_core.locked_write(path):
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return Mutation(False, "⚠️ Inbox недоступен.")
        located = _locate_inbox_text(raw, identity)
        if located is None:
            return Mutation(False, "⚠️ Мысль устарела — открой «🧠 Мысли» заново.")
        try:
            backup = _write_backup(root, {"kind": "inbox", "path": thought_feed.INBOX_REL,
                                          "identity": identity, "line": located["line"],
                                          "position": located["position"]})
        except (OSError, ValueError):
            return Mutation(False, "⚠️ Корзина недоступна — мысль не удалена.")
        lines = raw.splitlines()
        lines.pop(located["index"])
        rewritten = "\n".join(lines) + ("\n" if raw.endswith("\n") else "")
        try:
            inbox_core.atomic_write(path, rewritten)
        except OSError:
            _remove_backup(backup)
            return Mutation(False, "Не смог удалить мысль.")
    return Mutation(True, "🗑 Мысль перемещена в корзину.", True, "inbox",
                    thought_feed.INBOX_REL, backup=backup)


def _restore_structured(root: Path, record: dict, record_path: Path) -> Mutation:
    rel = record.get("path")
    content = record.get("content")
    path = _structured_path(root, rel)
    if (path is None or not isinstance(content, str)
            or len(content.encode("utf-8")) > thought_feed.MAX_FILE_BYTES
            or record.get("identity") != thought_feed._identity_text(rel, content)):
        return Mutation(False, "⚠️ Резервная копия недоступна.")
    with inbox_core.locked_write(path):
        if path.exists() or path.is_symlink():
            return Mutation(False, "⚠️ Нельзя восстановить: по исходному пути уже есть заметка.")
        try:
            inbox_core.atomic_write(path, content)
        except OSError:
            return Mutation(False, "Не смог восстановить мысль.")
    _remove_backup(record_path)
    identity = thought_feed._identity_text(rel, content)
    return Mutation(True, "↩️ Мысль восстановлена.", True, "thought", rel, identity)


def _restore_inbox(root: Path, record: dict, record_path: Path) -> Mutation:
    path = root / thought_feed.INBOX_REL
    line = record.get("line")
    position = _safe_position(record.get("position"))
    if (not _safe_inbox(root, path) or record.get("path") != thought_feed.INBOX_REL
            or not isinstance(line, str) or len(line) > MAX_EDIT_CHARS
            or any(char in line for char in "\r\n\x00")
            or not thought_feed._INBOX_LINE_RE.fullmatch(line)
            or record.get("identity") != thought_feed._identity_text(thought_feed.INBOX_REL, line)
            or position is None):
        return Mutation(False, "⚠️ Резервная копия недоступна.")
    with inbox_core.locked_write(path):
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return Mutation(False, "⚠️ Inbox недоступен.")
        if line in [entry["line"] for entry in _inbox_entries(raw)]:
            return Mutation(False, "⚠️ Нельзя восстановить: такая строка уже есть.")
        entries = _inbox_entries(raw)
        positions = [entry["index"] for entry in entries]
        index = positions[position] if position < len(positions) else _thought_section_end(raw)
        lines = raw.splitlines()
        lines.insert(index, line)
        rewritten = "\n".join(lines) + ("\n" if raw.endswith("\n") else "")
        try:
            inbox_core.atomic_write(path, rewritten)
        except OSError:
            return Mutation(False, "Не смог восстановить мысль.")
    _remove_backup(record_path)
    return Mutation(True, "↩️ Мысль восстановлена.", True, "inbox", thought_feed.INBOX_REL,
                    thought_feed._identity_text(thought_feed.INBOX_REL, line))


def _clean_edit(value: str) -> str:
    return (value or "").replace("\x00", "").strip()


def _replace_inbox_body(line: str, text: str) -> str:
    match = thought_feed._INBOX_LINE_RE.match(line.strip())
    if not match:
        return ""
    prefix = "- " + ((f"**{match.group(1)}** ") if match.group(1) else "")
    return prefix + " ".join(text.split())


def _structured_path(root: Path, relative) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    path = root / relative
    if not _plain_path(root, path) or not thought_feed._allowed_structured(root, path):
        return None
    return path


def _read_current_structured(root: Path, path: Path, identity: str) -> str | None:
    if path.is_symlink() or not thought_feed._allowed_structured(root, path):
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    relative = path.relative_to(root).as_posix()
    if thought_feed._identity_text(relative, raw) != identity:
        return None
    return raw


def _split_frontmatter(raw: str) -> tuple[str, str]:
    if not raw.startswith("---\n"):
        return "", raw
    end = raw.find("\n---", 4)
    if end < 0:
        return "", raw
    end_line = raw.find("\n", end + 1)
    if end_line < 0:
        return raw, ""
    body_start = end_line + 1
    while body_start < len(raw) and raw[body_start] == "\n":
        body_start += 1
    return raw[:body_start], raw[body_start:]


def _body_after_frontmatter(raw: str) -> str:
    return _split_frontmatter(raw)[1]


def _structured_editable_body(raw: str) -> str:
    """Return only the saved transcription section when the note has one."""
    body = _body_after_frontmatter(raw)
    match = re.search(r"(?m)^## Мысль\s*$", body)
    if not match:
        return body
    start = match.end()
    next_heading = re.search(r"(?m)^##\s+", body[start:])
    end = start + next_heading.start() if next_heading else len(body)
    section = body[start:end].strip("\n")
    if section.startswith("- "):
        return section[2:].replace("\n  ", "\n").strip()
    return section.strip()


def _replace_structured_editable_body(raw: str, text: str) -> str | None:
    """Preserve Zettel context/conclusions/links while correcting the thought."""
    prefix, body = _split_frontmatter(raw)
    match = re.search(r"(?m)^## Мысль\s*$", body)
    if not match:
        if re.search(r"(?m)^##\s+", body):
            return None
        return prefix + text.rstrip() + "\n"
    start = match.end()
    next_heading = re.search(r"(?m)^##\s+", body[start:])
    end = start + next_heading.start() if next_heading else len(body)
    old_section = body[start:end]
    leading = "\n\n" if old_section.startswith("\n\n") else "\n"
    trailing = "\n\n" if old_section.endswith("\n\n") else "\n"
    replacement = leading + "- " + text.rstrip().replace("\n", "\n  ") + trailing
    return prefix + body[:start] + replacement + body[end:]


def _plain_path(root: Path, path: Path) -> bool:
    """Reject symlink aliases at every component below the selected vault."""
    try:
        parts = path.relative_to(root).parts
        current = root
        for part in parts:
            if part in (".", ".."):
                return False
            current = current / part
            if current.is_symlink():
                return False
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _safe_inbox(root: Path, path: Path) -> bool:
    try:
        return (_plain_path(root, path) and path.is_file()
                and path.resolve().relative_to(root.resolve()) is not None)
    except (OSError, ValueError, RuntimeError):
        return False


def _safe_position(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        position = int(value)
    except (TypeError, ValueError):
        return None
    return position if 0 <= position <= 100_000 else None


def _inbox_entries(raw: str) -> list[dict]:
    entries = []
    section = False
    position = 0
    for index, original in enumerate(raw.splitlines()):
        stripped = original.strip()
        if stripped.startswith("## "):
            section = stripped == thought_feed.THOUGHT_SECTION_HEADING
            continue
        if not section:
            continue
        match = thought_feed._INBOX_LINE_RE.match(stripped)
        if not match:
            continue
        body = " ".join(match.group(2).split())
        if body:
            entries.append({"index": index, "line": stripped, "body": body,
                            "position": position})
            position += 1
    return entries


def _locate_inbox_text(raw: str, identity: str) -> dict | None:
    matches = [entry for entry in _inbox_entries(raw)
               if thought_feed._identity_text(thought_feed.INBOX_REL, entry["line"]) == identity]
    return matches[0] if len(matches) == 1 else None


def _find_inbox_line(root: Path, identity: str) -> dict | None:
    path = root / thought_feed.INBOX_REL
    if not _safe_inbox(root, path):
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _locate_inbox_text(raw, identity)


def _thought_section_end(raw: str) -> int:
    lines = raw.splitlines()
    active = False
    for index, line in enumerate(lines):
        if line.strip() == thought_feed.THOUGHT_SECTION_HEADING:
            active = True
            continue
        if active and line.strip().startswith("## "):
            return index
    return len(lines)


def _backup_path(root: Path, token: str) -> Path | None:
    if not re.fullmatch(r"[a-f0-9]{24}", token or ""):
        return None
    directory = _trash_directory(root, create=False)
    if directory is None:
        return None
    path = directory / (token + ".json")
    if path.is_symlink() or not path.is_file():
        return None
    return path


def _write_backup(root: Path, data: dict) -> str:
    directory = _trash_directory(root, create=True)
    if directory is None:
        raise OSError("trash path is unsafe")
    token = secrets.token_hex(12)
    path = directory / (token + ".json")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    inbox_core.atomic_write(path, payload + "\n")
    return token


def _remove_backup(token_or_path) -> None:
    path = Path(token_or_path)
    if not path.suffix:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        logger.warning("trash cleanup failed: %s", type(error).__name__)


def _trash_directory(root: Path, create: bool) -> Path | None:
    """A non-symlink ignored trash directory strictly below this vault root."""
    base = root / ".trash"
    directory = root / TRASH_REL
    try:
        resolved_root = root.resolve()
        # resolve(strict=False) follows existing parent links before mkdir; a
        # symlinked .trash therefore fails containment before any write.
        directory.resolve().relative_to(resolved_root)
        if base.is_symlink() or directory.is_symlink():
            return None
        if base.exists() and not base.is_dir():
            return None
        if directory.exists() and not directory.is_dir():
            return None
        if create:
            directory.mkdir(parents=True, exist_ok=True)
            if base.is_symlink() or directory.is_symlink():
                return None
            directory.resolve().relative_to(resolved_root)
    except (OSError, ValueError, RuntimeError):
        return None
    return directory


__all__ = ["Mutation", "edit", "delete", "restore", "full_text", "TRASH_REL"]
