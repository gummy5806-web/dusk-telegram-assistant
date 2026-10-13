"""Read-only, bounded Telegram rendering for the local version journal."""

from __future__ import annotations

import datetime as dt
import json
import re
import textwrap
from zoneinfo import ZoneInfo

import memory_history
import task_ui

MAX_VIEW_CHARS = 3900
MAX_EVENT_CONTENT = 1200
ALMATY = ZoneInfo("Asia/Almaty")


def view(vault, kind: str, ref: str) -> task_ui.View:
    """Show committed snapshots without changing source files or journal state."""
    events = memory_history.history(vault, kind, ref, limit=10)
    if not events:
        return task_ui.View("История не найдена: текущая запись отсутствует или ещё не изменялась.",
                            [[("⬅️ Назад", task_ui.THOUGHT_HUB)]], stale=True)
    lines = ["🕘 История", ""]
    for index, event in enumerate(reversed(events), start=1):
        before = event["before"]
        after = event["after"]
        lines.extend((
            f"{index}. Раньше · {_timestamp(before.get('timestamp') or event['created_at'])}",
            _clip(_readable(event["kind"], before["content"])),
            "",
            ("Сейчас" if after.get("is_current") else "Раньше") + " · после изменения",
            _clip(_readable(event["kind"], after["content"])),
            "",
        ))
    if not any(event["after"].get("is_current") for event in events):
        lines.append("Сейчас: текущая запись не найдена; показаны только подтверждённые версии.")
    return task_ui.View(_fit("\n".join(lines)), [[("⬅️ Назад", task_ui.THOUGHT_HUB)]])


def _clip(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= MAX_EVENT_CONTENT:
        return text
    return text[:MAX_EVENT_CONTENT - 1].rstrip() + "…"


def _readable(kind: str, content: str) -> str:
    if kind == "resource":
        return _resource_text(content)
    return _thought_text(content)


def _resource_text(content: str) -> str:
    if not content.startswith("---\n"):
        return content
    end = content.find("\n---\n", 4)
    if end < 0:
        return content
    fields = {}
    for line in content[4:end].splitlines():
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, str):
            fields[key] = decoded
    title = fields.get("title", "")
    description = fields.get("description", "")
    url = fields.get("url", "")
    pieces = [piece for piece in (title, description, url) if piece]
    return "\n".join(pieces) if pieces else content[end + 5:].strip()


def _thought_text(content: str) -> str:
    text = content
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            next_line = text.find("\n", end + 1)
            text = text[next_line + 1:] if next_line >= 0 else ""
    match = re.search(r"(?m)^## Мысль\s*$", text)
    if match:
        after = text[match.end():]
        next_heading = re.search(r"(?m)^##\s+", after)
        text = after[:next_heading.start() if next_heading else len(after)]
    return text.strip()


def _timestamp(value: str) -> str:
    try:
        moment = dt.datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        return moment.astimezone(ALMATY).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return "время неизвестно"


def _fit(value: str) -> str:
    if len(value) <= MAX_VIEW_CHARS:
        return value
    return textwrap.shorten(value, width=MAX_VIEW_CHARS, placeholder="\n…")
