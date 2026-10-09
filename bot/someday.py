#!/usr/bin/env python3
"""Отложено — полезное, но не сейчас.

Зачем отдельное место: без него «полезно потом» либо превращается в задачу
(и висит мёртвым грузом), либо теряется. GTD называет это Someday/Maybe.

Ключевое поле — ТРИГГЕР: при каком условии вернуться. Без триггера запись
не отличается от свалки, а свалку никто не открывает.
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

SOMEDAY_PATH = "Отложено.md"
FOOTER = "## Как добавить запись"


def _path(vault: Path | None = None) -> Path:
    root = vault or Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))
    return root / SOMEDAY_PATH


def _entry(title: str, what: str, trigger: str, topic: str, when: str, source: str = "") -> str:
    tag = re.sub(r"[^\w/]+", "-", (topic or "разное").lower()).strip("-")
    lines = [
        f"### {title}",
        f"- **Что:** {what}",
        f"- **Триггер:** {trigger or '— не задан, вернуться в недельном обзоре'}",
    ]
    if source:
        lines.append(f"- **Где взял:** {source}")
    lines.append(f"- **Добавлено:** {when} • `#отложено/{tag}`")
    return "\n".join(lines) + "\n"


def _body(text: str) -> str:
    """Только рабочие записи: шаблон в подвале не считается."""
    return text.split(FOOTER)[0]


def add(title: str, what: str, trigger: str = "", topic: str = "",
        source: str = "", vault: Path | None = None) -> Path:
    """Дописывает запись в нужный раздел «Отложено.md»."""
    path = _path(vault)
    if not path.exists():
        path.write_text(f"# 🗄 Отложено\n\n{FOOTER}\n", encoding="utf-8")

    text = path.read_text(encoding="utf-8")
    when = datetime.date.today().isoformat()
    block = _entry(title.strip(), what.strip(), trigger.strip(), topic, when, source)

    topic = (topic or "").strip()
    heading = f"## {topic}" if topic else ""
    if heading and heading.lower() in text.lower():
        # дописываем в конец найденного раздела
        idx = text.lower().index(heading.lower())
        nxt = text.find("\n## ", idx + len(heading))
        insert_at = nxt if nxt != -1 else len(text)
        text = text[:insert_at].rstrip() + "\n\n" + block + "\n" + text[insert_at:]
    else:
        # новый раздел — перед служебным футером, чтобы он остался внизу
        section = f"\n## {topic or 'Разное'}\n\n{block}"
        if FOOTER in text:
            text = text.replace(FOOTER, section.lstrip("\n") + "\n---\n\n" + FOOTER, 1)
        else:
            text = text.rstrip() + "\n" + section
    path.write_text(text, encoding="utf-8")
    return path


def count(vault: Path | None = None) -> int:
    """Сколько записей отложено — чтобы это попадало в обзор, а не забывалось."""
    path = _path(vault)
    if not path.exists():
        return 0
    return len(re.findall(r"^### ", _body(path.read_text(encoding="utf-8")), re.M))


def with_triggers(vault: Path | None = None):
    """Записи без триггера — кандидаты на выброс в недельном обзоре."""
    path = _path(vault)
    if not path.exists():
        return []
    text = _body(path.read_text(encoding="utf-8"))
    out = []
    for m in re.finditer(r"^### (.+)$", text, re.M):
        tail = text[m.end(): m.end() + 600]
        if "**Триггер:** —" in tail:
            out.append(m.group(1).strip())
    return out
