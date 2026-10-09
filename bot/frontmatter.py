#!/usr/bin/env python3
"""Единственное чтение и правка шапки заметки (YAML frontmatter).

Зачем отдельный модуль: поля вида `completedDate:` могут встретиться в тексте
заметки — в примере, в code fence, в цитате. Раньше каждый модуль искал их по
всему файлу регуляркой с `re.M`, поэтому пример в теле попадал в статистику и в
идентичность карточки.

Правило простое: **метаданные живут только в первой шапке**. Всё остальное —
текст. Модуль намеренно маленький и без зависимостей.
"""

from __future__ import annotations

import re

OPEN = "---"


def region(text: str, allow_open: bool = False) -> str:
    """Внутренняя часть первой шапки.

    `allow_open=False` (обычное чтение) требует закрывающий `---`: шапка без него
    считается повреждённой, и её поля метаданными не являются.

    `allow_open=True` допускает шапку без закрывающего `---`. Это нужно только
    там, где обрывок записи надо **узнать**, чтобы дописать его, а не создать
    второй файл (см. `learn.ingest_result`).
    """
    if not text.startswith(OPEN):
        return ""
    end = text.find("\n" + OPEN, 3)
    if end != -1:
        return text[4:end + 1]
    if not allow_open:
        return ""
    rest = text[4:]
    cut = rest.find("\n\n")
    return rest[:cut] if cut != -1 else rest


def has_valid(text: str) -> bool:
    """Есть ли у заметки корректная закрытая шапка."""
    return region(text) != ""


def unquote(value: str) -> str:
    """Снимает кавычки: `"done"` и `'done'` — это `done`.

    Без этого `status: "done"` не совпадал с `done`, и повторная команда
    «готово» считалась новым переходом — с выдуманной датой.
    """
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def get(text: str, name: str, allow_open: bool = False) -> str:
    """Значение поля из шапки без кавычек. Тело не просматривается."""
    body = region(text, allow_open=allow_open)
    if not body:
        return ""
    m = re.search(rf"^{re.escape(name)}:[ \t]*(.*)$", body, re.M)
    return unquote(m.group(1)) if m else ""


def set_field(text: str, name: str, value) -> tuple:
    """Ставит, заменяет или убирает поле только внутри шапки.

    Возвращает `(новый текст, получилось)`. Если корректной шапки нет, текст не
    меняется, а признак успеха остаётся False — вызывающий обязан сообщить о
    неудаче, а не о переходе. Тело заметки сохраняется дословно.
    """
    body = region(text)
    if not body:
        return text, False
    start, end = 4, 4 + len(body)
    head = text[start:end]
    pattern = re.compile(rf"^{re.escape(name)}:(?:[ \t].*)?\n?", re.M)
    if value is None:
        if pattern.search(head):
            head = pattern.sub("", head, count=1)
    else:
        line = f"{name}: {value}\n"
        if pattern.search(head):
            # Значение подставляется литерально: `\` в пути не является
            # шаблоном замены и не должно ломать запись (Windows drive-prefix).
            head = pattern.sub(lambda _match: line, head, count=1)
        else:
            if not head.endswith("\n"):
                head += "\n"
            head += line
    return text[:start] + head + text[end:], True
