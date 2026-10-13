#!/usr/bin/env python3
"""Локальный личный поиск по Dusk: только чтение, только свои места.

Никаких индексов, сети, моделей и subprocess. Разрешён ровно whitelist из
дизайна: инбокс, fleeting/permanent заметки, задачи, проекты и сферы, дневные
заметки и три корневые сводки. `Private/`, Research Brain, `.git`, `.obsidian`,
кэши, `SYSTEM/`, `sessions/` и исходники бота не читаются вовсе.

Поиск ограничен по длине запроса, числу слов, числу файлов, размеру файла,
длине выдержки и числу результатов. Битые, бинарные, огромные и внешние
symlink-файлы молча пропускаются, не прерывая поиск.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import frontmatter
import memory_history
import thought_feed

INBOX_REL = "ZETA/FLEETING/00 Инбокс.md"
ROOT_FILES = ("Отложено.md", "Статус.md", "Недельный обзор.md")
WALK_DIRS = (
    ("ZETA/FLEETING_DAILY", "fleeting"),
    ("ZETA/PERMANENT Знание в СВОЙ инсайт", "permanent"),
    ("TaskNotes/Tasks", "task"),
    ("PARA/PROJECTS", "project"),
    ("PARA/AREAS СФЕРЫ РАЗВИТИЯ ДОЛГОСРОК", "area"),
    ("STICKY", "sticky"),
    ("Omi", "memory"),
    ("PARA/RESOURCES", "resource"),
    ("PARA/RESOURCES хобби и заметки по ним", "resource"),
    ("DAILY", "daily"),
)

MAX_QUERY_LEN = 160
MAX_TERMS = 12
MAX_FILES = 3000
MAX_FILE_BYTES = 512 * 1024
MAX_EXCERPT = 220
MAX_RESULTS = 8
MAX_RENDER_CHARS = 3900

PERSONAL_HEADER = "🧠 Из твоего Dusk"
RESEARCH_HEADER = "📚 Из агентских конспектов"
EMPTY_PERSONAL = (
    "🧠 Из твоего Dusk\n\n"
    "Ничего не нашёл. Сформулируй иначе — или просто сохрани мысль, она попадёт "
    "в Inbox."
)

KIND_LABEL = {
    "inbox": "💭 Inbox",
    "fleeting": "🗒 Заметка",
    "history": "🕘 Прошлая запись",
    "permanent": "🧠 Знание",
    "task": "✅ Задача",
    "project": "📁 Проект",
    "area": "🎯 Сфера",
    "sticky": "📌 Стикер",
    "memory": "🧩 Omi",
    "resource": "📚 Ресурс",
    "daily": "📅 День",
    "summary": "📄 Сводка",
}

# Служебные слова не считаются предметом поиска: иначе «что и как» выдало бы
# весь валт. Короткие (<3) слова отбрасываются отдельно.
STOPWORDS = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли
если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя
ничего ей может они тут где есть надо нужно это эта этот эти для при про без
над под весь вся всё мой моя мое мои твой твоя кто-то что-то делать сделать
можно нельзя был была были будет буду будешь мы вы они оно чтобы чтоб чем
the a an of and or to in on for is are was were be been it its this that
with as at by from not but if then than so my me you your we our
""".split())

_RU_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ей", "ой", "ый",
    "ий", "ая", "яя", "ое", "ее", "ов", "ев", "ам", "ям", "ах", "ях", "ом",
    "ем", "ую", "юю", "ии", "ия", "ие", "ла", "ло", "ли", "ть", "ся", "сь",
    "а", "я", "о", "е", "ы", "и", "у", "ю", "й", "ь",
)


@dataclass(frozen=True)
class Hit:
    """Один структурированный результат личного поиска."""

    path: str
    title: str
    kind: str
    excerpt: str
    score: float
    created: str = ""
    status: str = ""


def _vault(vault=None) -> Path:
    if vault is not None:
        return Path(vault)
    return Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))


def _inside(root: Path, path: Path) -> bool:
    """Резолвнутый путь обязан остаться внутри валта (symlink-выход запрещён)."""
    try:
        resolved_root = root.resolve()
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _candidate_dir(root: Path, relative: str):
    """Каталог по относительному пути с учётом Unicode-вариантов (NFC/NFD)."""
    direct = root / relative
    if direct.is_dir():
        return direct
    parent = direct.parent
    if not parent.is_dir():
        return None
    want = unicodedata.normalize("NFC", direct.name)
    try:
        children = list(parent.iterdir())
    except OSError:
        return None
    for child in children:
        if (child.is_dir()
                and not child.is_symlink()
                and unicodedata.normalize("NFC", child.name) == want):
            return child
    return None


def _iter_notes(root: Path):
    """(path, kind) по whitelist-каталогам и трём корневым файлам."""
    seen = 0
    for relative, kind in WALK_DIRS:
        base = _candidate_dir(root, relative)
        if base is None:
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for name in filenames:
                if name.startswith(".") or not name.lower().endswith(".md"):
                    continue
                path = Path(dirpath) / name
                if path.is_symlink() or not _inside(root, path):
                    continue
                seen += 1
                if seen > MAX_FILES:
                    return
                yield path, kind
    for name in ROOT_FILES:
        path = root / name
        if path.is_file() and not path.is_symlink() and _inside(root, path):
            yield path, "summary"


def _read_text(path: Path):
    """Текст или None. Oversized/binary/invalid-UTF-8/нечитаемое — None."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _stem(word: str) -> str:
    value = (word or "").lower()
    for ending in _RU_ENDINGS:
        if len(value) - len(ending) >= 4 and value.endswith(ending):
            return value[:-len(ending)]
    return value


def _terms(query: str):
    cut = (query or "").strip().lower()[:MAX_QUERY_LEN]
    found = []
    for word in re.findall(r"[а-яёa-z0-9]+", cut):
        if len(word) < 3 or word in STOPWORDS or word in found:
            continue
        found.append(word)
        if len(found) >= MAX_TERMS:
            break
    return found


def _phrase(query: str) -> str:
    cut = re.sub(r"\s+", " ", (query or "").strip().lower()[:MAX_QUERY_LEN])
    cut = cut.strip(" .,;:!?-")
    words = re.findall(r"[а-яёa-z0-9]+", cut)
    if not words or all(word in STOPWORDS for word in words) or len(cut) < 3:
        return ""
    return cut


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    newline = text.find("\n", end + 1)
    return text[newline + 1:] if newline != -1 else ""


def _title(text: str, path: Path) -> str:
    value = frontmatter.get(text, "title")
    if value:
        return value
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}\s*[—–-]\s*", "", path.stem).strip()
    return stem or path.stem


def _score(haystack: str, title: str, terms, phrase: str) -> float:
    text = haystack.lower()
    head = title.lower()
    score = 0.0
    matched = 0
    if phrase:
        if phrase in head:
            score += 120
            matched += 1
        if phrase in text:
            score += 80
            matched += 1
    for term in terms:
        if term in head:
            score += 30
            matched += 1
        elif term in text:
            score += 12
            matched += 1
        else:
            stem = _stem(term)
            if len(stem) >= 4:
                if stem in head:
                    score += 15
                    matched += 1
                elif stem in text:
                    score += 6
                    matched += 1
    return score if matched else 0.0


def _clip(value: str) -> str:
    text = " ".join((value or "").split())
    text = text.lstrip("#").strip()
    if len(text) <= MAX_EXCERPT:
        return text
    return text[:MAX_EXCERPT].rstrip() + "…"


def _excerpt(body: str, terms, phrase: str) -> str:
    needles = []
    if phrase:
        needles.append(phrase)
    for term in terms:
        needles.append(term)
        stem = _stem(term)
        if len(stem) >= 4:
            needles.append(stem)
    lines = [line.strip() for line in body.splitlines()]
    for line in lines:
        if not line or line.startswith("---"):
            continue
        low = line.lower()
        if any(needle and needle in low for needle in needles):
            return _clip(line)
    for line in lines:
        if line and not line.startswith("#") and not line.startswith("---"):
            return _clip(line)
    return ""


def _inbox_hits(text: str, relative: str, terms, phrase: str):
    hits = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        body = stripped[2:].strip()
        body = re.sub(r"^\*\*\d{1,2}:\d{2}\*\*\s*", "", body)
        body = re.sub(r"^\[[ xX]\]\s*", "", body).strip()
        if not body:
            continue
        score = _score(body, "", terms, phrase)
        if score <= 0:
            continue
        hits.append(Hit(
            path=relative,
            title=body[:120],
            kind="inbox",
            excerpt=_clip(body),
            score=score,
        ))
    return hits


def search(query: str, vault=None, limit: int = MAX_RESULTS):
    """Top-N личных результатов. Пустой/стоп-словный запрос не отдаёт ничего."""
    terms = _terms(query)
    phrase = _phrase(query)
    if not terms and not phrase:
        return []

    root = _vault(vault)
    if not root.is_dir():
        return []

    hits = []
    historical = bool(re.search(r"раньше|прежн|прошл|старую|старая|истори", query, re.IGNORECASE))
    inbox_path = root / INBOX_REL
    if inbox_path.is_file() and not inbox_path.is_symlink() and _inside(root, inbox_path):
        inbox_text = _read_text(inbox_path)
        if inbox_text and not historical:
            inbox_text = "\n".join(line for line in inbox_text.splitlines() if not memory_history.is_past(root, thought_feed._identity_text(INBOX_REL, line.strip())))
        if inbox_text:
            hits.extend(_inbox_hits(inbox_text, INBOX_REL, terms, phrase))

    for path, kind in _iter_notes(root):
        text = _read_text(path)
        if text is None:
            continue
        body = text if kind == "task" else _strip_frontmatter(text)
        title = _title(text, path)
        score = _score(body, title, terms, phrase)
        if score <= 0:
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.name
        past = kind == "fleeting" and memory_history.is_past(root, thought_feed._identity_text(relative, text))
        if past and not historical:
            continue
        hits.append(Hit(
            path=relative,
            title=title,
            kind="history" if past else kind,
            excerpt=_excerpt(body, terms, phrase),
            score=score,
            created=frontmatter.get(text, "created"),
            status=frontmatter.get(text, "status"),
        ))

    hits.sort(key=lambda hit: (-hit.score, hit.path))
    return hits[:max(1, int(limit))]


def _fit(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


def render(hits, limit_chars: int = MAX_RENDER_CHARS) -> str:
    """Компактный ответ для Telegram с явной пометкой личного источника."""
    if not hits:
        return EMPTY_PERSONAL

    lines = [PERSONAL_HEADER, ""]
    for hit in hits:
        lines.append(f"• {hit.title}")
        meta = " · ".join(
            value for value in
            (KIND_LABEL.get(hit.kind, hit.kind), hit.status, hit.created) if value)
        if meta:
            lines.append(f"  {meta}")
        if hit.excerpt and hit.excerpt.lower() != hit.title.lower():
            lines.append(f"  «{hit.excerpt}»")
        lines.append(f"  {hit.path}")
        lines.append("")
    return _fit("\n".join(lines).rstrip(), limit_chars)


def render_combined(personal_hits, research_text: str,
                    limit_chars: int = MAX_RENDER_CHARS) -> str:
    """Личные результаты первыми, Research Brain — отдельным блоком вторым.

    Место под Research Brain резервируется заранее: даже при очень длинных
    личных результатах оба заголовка и часть research-текста остаются видимыми,
    а общий ответ держится ниже лимита Telegram.
    """
    personal = render(personal_hits, limit_chars=limit_chars)
    if not research_text:
        return _fit(personal, limit_chars)

    header_len = len(RESEARCH_HEADER) + 2  # заголовок + пустая строка
    reserve = min(header_len + 200, max(header_len + 40, limit_chars // 4))
    personal_budget = max(200, limit_chars - reserve - 2)
    personal = _fit(personal, personal_budget)

    remaining = limit_chars - len(personal) - 2
    block = RESEARCH_HEADER + "\n\n" + research_text
    block = _fit(block, max(header_len + 1, remaining))
    return personal + "\n\n" + block


__all__ = [
    "Hit", "search", "render", "render_combined",
    "MAX_RESULTS", "MAX_FILE_BYTES",
]
