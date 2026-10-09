#!/usr/bin/env python3
"""Zettelkasten-слой: превращает мысль в заметку со связями.

Чего не хватало раньше: инбокс был местом ХРАНЕНИЯ — мысль лежала голой строкой
и теряла всё, ради чего метод существует. Контекст («среда рождения мысли») и
Связи (к какой области и проекту относится) не восстанавливались.

Теперь: инбокс — это ВХОД. Каждая мысль превращается в fleeting-заметку
по анатомии владельца:

    ## Мысль      — сырой текст, как пришёл
    ## Контекст   — когда, откуда, что было до него в тот день
    ## Вывод      — с его маркерами: / сделано, ? вопрос, ! важно
    ## Связи      — Areas / Projects / Skills живыми ссылками

Контекст заполняется автоматически: человек в момент захвата его писать не должен,
но через месяц именно контекст объясняет, откуда взялась мысль.

Шаблоны взяты из его файлов: SYSTEM/TEMPLATE/FORMAT/Daily fleeting.md
и существующих заметок в ZETA/FLEETING_DAILY.
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path
from typing import Callable

FLEETING_DIR = "ZETA/FLEETING_DAILY"
AREAS_DIR = "PARA/AREAS СФЕРЫ РАЗВИТИЯ ДОЛГОСРОК"
PROJECTS_DIR = "PARA/PROJECTS"
SKILLS_DIR = "PARA/AREAS СФЕРЫ РАЗВИТИЯ ДОЛГОСРОК/Productivity"


def _vault(vault: Path | None = None) -> Path:
    return vault or Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))


def taxonomy(vault: Path | None = None) -> dict:
    """Реальные области, проекты и навыки — чтобы ссылаться на то, что есть."""
    v = _vault(vault)

    def names(rel):
        d = v / rel
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.md") if not p.stem.startswith("FAQ"))

    areas = [p.name for p in (v / AREAS_DIR).iterdir() if p.is_dir()] if (v / AREAS_DIR).is_dir() else []
    projects = [p.name for p in (v / PROJECTS_DIR).iterdir() if p.is_dir()] if (v / PROJECTS_DIR).is_dir() else []
    return {"areas": sorted(areas), "projects": sorted(projects), "skills": names(SKILLS_DIR)}


def _match(value: str, candidates) -> str:
    """Модель редко попадает в точное имя — сопоставляем по смыслу."""
    want = (value or "").strip().lower()
    if not want or not candidates:
        return ""
    for c in candidates:
        if c.lower() == want:
            return c
    for c in candidates:
        key = c.lower().split(" - ")[0].strip()
        if want in c.lower() or key in want or want in key:
            return c
    best, score = "", 0
    wset = set(want.replace("-", " ").split())
    for c in candidates:
        s = len(wset & set(c.lower().replace("-", " ").split()))
        if s > score:
            best, score = c, s
    return best if score else ""


def _marker(kind: str) -> str:
    """Его маркеры в Выводе: / сделано, ? вопрос, ! важно."""
    return {"done": "/", "question": "?", "important": "!"}.get(kind, "/")


def context_line(when: datetime.datetime, source: str, before) -> str:
    """Контекст собирается сам — писать его в момент захвата никто не станет."""
    bits = [f"когда: {when.strftime('%d.%m %H:%M')}"]
    bits.append(f"откуда: {source}")
    weekday = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][when.weekday()]
    bits.append(f"день: {weekday}")
    if before:
        bits.append("до этого в тот день: " + "; ".join(b[:60] for b in before[-3:]))
    return " • ".join(bits)


def make_fleeting(
    text: str,
    title: str,
    conclusion: str = "",
    marker: str = "done",
    area: str = "",
    project: str = "",
    skill: str = "",
    when: datetime.datetime | None = None,
    source: str = "Mac",
    before=None,
    vault: Path | None = None,
    writer: Callable[[Path, str], bool] | None = None,
) -> Path:
    """Создаёт fleeting-заметку в его формате. Возвращает путь."""
    v = _vault(vault)
    when = when or datetime.datetime.now()
    tax = taxonomy(v)

    area = _match(area, tax["areas"])
    project = _match(project, tax["projects"])
    skill = _match(skill, tax["skills"])

    day = when.strftime("%Y-%m-%d")
    safe_title = re.sub(r"[\\/:*?\"<>|]", "", (title or text))[:70].strip() or "мысль"
    path = v / FLEETING_DIR / f"{day} — {safe_title}.md"
    counter = 1
    while path.exists():
        path = v / FLEETING_DIR / f"{day} — {safe_title} {counter}.md"
        counter += 1

    def link(name):
        return f"[[{name}]]" if name else ""

    conclusion = (conclusion or "").strip()
    if conclusion:
        conclusion = f"{_marker(marker)} {conclusion}"

    # связи с соседними заметками считаем ДО сборки текста: шаблон уже
    # использует {similar}, и раньше это валило создание любой заметки
    near = similar_notes(v, f"{text} {conclusion} {' '.join(filter(None, [area, project]))}", limit=3)
    similar = ""
    if near:
        similar = "- Похожие: " + " ".join(f"[[{name}]]" for name, _ in near) + "\n"

    body = f"""---
type: fleeting
tags: [zeta/fleeting]
created: {when.strftime('%Y-%m-%d %H:%M')}
---

# {safe_title}

## Мысль
- {text.strip()}

## Контекст
- {context_line(when, source, before or [])}

## Вывод
- {conclusion if conclusion else "—"}

## Куда отнести или ## Связи
- Areas: {link(area)}
- Projects: {link(project)}
- Skills: {link(skill)}
{similar}"""
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        if writer is not None:
            if writer(path, body):
                break
        else:
            try:
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(body)
            except FileExistsError:
                path = v / FLEETING_DIR / f"{day} — {safe_title} {counter}.md"
                counter += 1
                continue
            break
        path = v / FLEETING_DIR / f"{day} — {safe_title} {counter}.md"
        counter += 1
    return path


def recent_thoughts(vault: Path | None = None, days: int = 1):
    """Что уже поймано за последние дни — идёт в Контекст следующей мысли."""
    v = _vault(vault)
    d = v / FLEETING_DIR
    if not d.is_dir():
        return []
    since = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
    out = []
    for f in sorted(d.glob("*.md")):
        if f.name[:10] >= since:
            out.append(f.stem.split(" — ", 1)[-1])
    return out

# --- Связи между заметками ---------------------------------------------------
# Zettelkasten держится не на папках, а на ссылках между заметками. Раньше
# заметка ссылалась только на Area/Project — то есть вверх по дереву, но не
# на соседей. Из-за этого сеть знаний не собиралась.

STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или
ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где
есть надо нужно это эта этот эти для при про без над под что-то кто-то мой моя мои твой
делать сделать можно нельзя весь вся всё""".split())


def _keywords(text: str, limit: int = 40):
    words = re.findall(r"[а-яёa-z]{4,}", (text or "").lower())
    seen, out = set(), []
    for w in words:
        if w in STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return set(out)


def _all_notes(vault: Path):
    """Все заметки, с которыми можно связаться."""
    out = []
    for rel in (FLEETING_DIR, "ZETA/PERMANENT Знание в СВОЙ инсайт",
                "ZETA/LITERATURE Изучение,конспект"):
        base = vault / rel
        if not base.is_dir():
            continue
        for f in base.rglob("*.md"):
            out.append(f)
    return out


def similar_notes(vault: Path, text: str, limit: int = 3, exclude: Path | None = None):
    """Находит заметки, похожие на новую — по пересечению значимых слов.

    Это и есть сеть Zettelkasten: не «в какую папку», а «с чем это связано».
    """
    want = _keywords(text)
    if not want:
        return []
    scored = []
    for f in _all_notes(vault):
        if exclude and f.resolve() == exclude.resolve():
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        have = _keywords(body)
        if not have:
            continue
        overlap = len(want & have)
        if overlap >= 2:
            scored.append((overlap / max(1, len(want) ** 0.5), f.stem, overlap))
    scored.sort(reverse=True)
    return [(name, score) for score, name, _ in scored[:limit]]
