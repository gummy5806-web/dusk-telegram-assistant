#!/usr/bin/env python3
"""Bounded-чтение и детерминированный возврат мыслей: только чтение валта.

Пользователь не должен помнить про папки. Этот модуль сам собирает из двух
разрешённых источников:

* сырые строки `ZETA/FLEETING/00 Инбокс.md`;
* структурированные заметки `ZETA/FLEETING_DAILY`.

И отдаёт ограниченный обзор: необработанный Inbox, последние заметки и одну
старую забытую мысль. Никаких индексов, моделей, сети и subprocess — только
чтение файлов и арифметика.

Правила, которые нельзя ослаблять:

* читаются ровно два явных источника; `Private/`, Research Brain, `SYSTEM/`,
  `bot/`, `sessions/`, `.git`, `.obsidian`, кэши и symlink-выходы не читаются;
* огромные, бинарные и нечитаемые как UTF-8 файлы пропускаются;
* идентичность мысли — относительный путь плюс хеш содержимого (для строки
  Inbox — путь инбокса плюс хеш строки);
* дата берётся из frontmatter или имени файла, но никогда из `mtime`;
* ранжирование забытых детерминировано: старше и реже показанная мысль важнее;
* рендеры ограничены по длине и не содержат абсолютных путей.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import thought_status
import frontmatter

INBOX_REL = "ZETA/FLEETING/00 Инбокс.md"
FLEETING_DIR_REL = "ZETA/FLEETING_DAILY"

MAX_FILE_BYTES = 512 * 1024
MAX_INBOX_RECENT = 3
MAX_STRUCTURED_RECENT = 5
MAX_TITLE = 90
MAX_EXCERPT = 200
MAX_RENDER_CHARS = 3900
FORGOTTEN_MIN_AGE_DAYS = 2
FORGOTTEN_COOLDOWN_DAYS = 14
RELATED_MIN_SHARED = 2
RELATED_RATIO_MIN = 0.5

INBOX_HEADER = "🧠 Мысли"
# Мыслями считается только список под этим точным заголовком: чек-бокс задачи
# или ссылка из соседних разделов не должны попадать в хаб мыслей.
THOUGHT_SECTION_HEADING = "## 💭 Мысли и идеи"
TASK_SECTION_HEADING = "## ✅ Задачи"
LINK_SECTION_HEADING = "## 🔗 Ссылки и материалы"
INBOX_KIND_LABEL = {"inbox": "💭 Inbox", "fleeting": "🗒 Заметка"}

_WORD_RE = re.compile(r"[а-яёa-z0-9]{4,}")
_DATE_AT_START_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DATE_ANY_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_INBOX_LINE_RE = re.compile(
    r"^-\s+(?:\[[ xX]\]\s+)?(?:\*\*(\d{1,2}:\d{2})\*\*\s*)?(.+)$")

STOPWORDS = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы
только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли
если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя
ничего ей может они тут где есть надо нужно это эта этот эти для при про без
над под весь вся всё мой моя мое мои твой твоя делать сделать можно нельзя
the a an of and or to in on for is are was were be been it its this that
with as at by from not but if then than so my me you your we our
""".split())


@dataclass(frozen=True)
class Thought:
    """Одна мысль: identity, относительный путь, заголовок, дата и текст."""

    identity: str
    path: str
    title: str
    excerpt: str
    date: Optional[datetime.date]
    kind: str
    stamp: str = ""


@dataclass(frozen=True)
class Overview:
    """Ограниченный срез: сколько в Inbox и что показать как последнее/забытое."""

    inbox_total: int
    inbox: tuple = field(default_factory=tuple)
    recent: tuple = field(default_factory=tuple)
    forgotten: Optional[Thought] = None


@dataclass(frozen=True)
class InboxCounts:
    total: int = 0
    thoughts: int = 0
    tasks: int = 0
    links: int = 0


class InboxReadError(ValueError):
    """The public Inbox exists but cannot be verified as safe UTF-8 Markdown."""


def _now(now=None) -> datetime.datetime:
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc)


def _root(root=None) -> Path:
    if root is not None:
        return Path(root)
    return Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))


def _inside(root: Path, path: Path) -> bool:
    """Резолвнутый путь обязан остаться внутри валта (symlink-выход запрещён)."""
    try:
        resolved = path.resolve()
        resolved_root = root.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _safe_read(root: Path, path: Path):
    """Текст файла или None. Symlink/oversized/binary/non-UTF8/снаружи — None.

    Все проверки выполняются ДО чтения байтов: symlink-выход и гигантский файл
    не читаются вообще.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if not _inside(root, path):
            return None
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


def _digest(value) -> str:
    data = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha1(data).hexdigest()[:12]


def _identity_text(relative: str, text: str) -> str:
    return f"{relative}#{_digest(text)}"


def _parse_date(value) -> Optional[datetime.date]:
    match = _DATE_ANY_RE.search(value or "")
    if not match:
        return None
    try:
        return datetime.date(int(match.group(1)), int(match.group(2)),
                             int(match.group(3)))
    except ValueError:
        return None


def _filename_date(path: Path) -> Optional[datetime.date]:
    match = _DATE_AT_START_RE.match(path.stem)
    if not match:
        return None
    try:
        return datetime.date(int(match.group(1)), int(match.group(2)),
                             int(match.group(3)))
    except ValueError:
        return None


def _clip(value: str, limit: int = MAX_EXCERPT) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


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
        return _clip(value, MAX_TITLE)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return _clip(stripped[2:], MAX_TITLE)
    stem = _DATE_AT_START_RE.sub("", path.stem).lstrip(" —–-").strip()
    return _clip(stem or path.stem, MAX_TITLE)


def _excerpt(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        return _clip(stripped)
    return ""


def _fleet_dir(root: Path) -> Optional[Path]:
    base = root / FLEETING_DIR_REL
    try:
        if base.is_symlink() or not base.is_dir():
            return None
    except OSError:
        return None
    return base


def _allowed_structured(root: Path, path: Path) -> bool:
    base = _fleet_dir(root)
    if base is None:
        return False
    try:
        resolved_base = base.resolve()
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved.parent == resolved_base


def _structured_thoughts(root: Path, include_done=False):
    """Все структурированные заметки, свежие первыми. Только чтение."""
    base = _fleet_dir(root)
    if base is None:
        return []
    thoughts = []
    for path in sorted(base.glob("*.md")):
        if path.name.startswith("."):
            continue
        if not _allowed_structured(root, path):
            continue
        text = _safe_read(root, path)
        if text is None:
            continue
        body = _strip_frontmatter(text)
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        date = _parse_date(frontmatter.get(text, "created")) or _filename_date(path)
        identity = _identity_text(relative, text)
        if not include_done and (thought_status.is_done(root, identity) or _is_past(root, identity)):
            continue
        thoughts.append(Thought(
            identity=identity,
            path=relative,
            title=_title(text, path),
            excerpt=_excerpt(body),
            date=date,
            kind="fleeting",
        ))
    return thoughts


def _sorted_recent(thoughts):
    return sorted(
        thoughts,
        key=lambda item: (item.date is None,
                          -(item.date.toordinal() if item.date else 0),
                          item.path),
    )


def recent_thoughts(root=None, limit: int = MAX_STRUCTURED_RECENT):
    """Последние структурированные заметки, свежие первыми."""
    items = _sorted_recent(_structured_thoughts(_root(root)))
    return items[:max(1, int(limit))]


def _parse_inbox_sections(text: str):
    headings = {
        THOUGHT_SECTION_HEADING: "thoughts",
        TASK_SECTION_HEADING: "tasks",
        LINK_SECTION_HEADING: "links",
    }
    entries = {name: [] for name in headings.values()}
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = headings.get(stripped, "")
            continue
        if not section or not stripped.startswith("- "):
            continue
        match = _INBOX_LINE_RE.match(stripped)
        if not match:
            continue
        body = " ".join(match.group(2).split())
        if body:
            entries[section].append((match.group(1) or "", body, stripped))
    return entries


def _parse_inbox(text: str):
    """Только элементы списка под точным заголовком «## 💭 Мысли и идеи».

    Задачи, материалы и строки до заголовка мыслей (или в других разделах)
    мыслями не считаются: иначе чек-бокс задачи попадал бы в счётчик Inbox,
    в список мыслей и в кандидаты «похожей старой мысли».
    """
    return _parse_inbox_sections(text)["thoughts"]


def inbox_counts(root=None) -> InboxCounts:
    """Counts all three known public Inbox sections without treating them as thoughts.

    A missing Inbox is an honest empty Inbox.  An existing path that is unsafe,
    oversized, binary or non-UTF-8 is an explicit error so callers never render
    a misleading zero.
    """
    vault = _root(root)
    path = vault / INBOX_REL
    try:
        if not path.exists() and not path.is_symlink():
            return InboxCounts()
    except OSError as error:
        raise InboxReadError("Inbox недоступен") from error
    text = _safe_read(vault, path)
    if text is None:
        raise InboxReadError("Inbox существует, но безопасно не читается")
    sections = _parse_inbox_sections(text)
    thoughts = sum(not thought_status.is_done(vault, _identity_text(INBOX_REL, line)) and not _is_past(vault, _identity_text(INBOX_REL, line))
                   for _stamp, _body, line in sections["thoughts"])
    tasks = len(sections["tasks"])
    links = len(sections["links"])
    return InboxCounts(thoughts + tasks + links, thoughts, tasks, links)


def _inbox_thoughts(root: Path, include_done=False):
    path = root / INBOX_REL
    text = _safe_read(root, path)
    if text is None:
        return []
    thoughts = []
    for stamp, body, line in _parse_inbox(text):
        identity = _identity_text(INBOX_REL, line)
        if not include_done and (thought_status.is_done(root, identity) or _is_past(root, identity)):
            continue
        thoughts.append(Thought(
            identity=identity,
            path=INBOX_REL,
            title=_clip(body, MAX_TITLE),
            excerpt=_clip(body),
            date=None,
            kind="inbox",
            stamp=stamp,
        ))
    return thoughts


def inbox_thoughts(root=None, limit: int = MAX_INBOX_RECENT):
    """Сырые записи Inbox, новые первыми."""
    items = _inbox_thoughts(_root(root))
    return list(reversed(items))[:max(1, int(limit))]


def _is_past(root, identity):
    import memory_history
    return memory_history.is_past(root, identity)


def _inbox_total(root: Path) -> int:
    path = root / INBOX_REL
    text = _safe_read(root, path)
    if text is None:
        return 0
    return sum(not thought_status.is_done(root, _identity_text(INBOX_REL, line)) and not _is_past(root, _identity_text(INBOX_REL, line))
               for _stamp, _body, line in _parse_inbox(text))


def completed_thoughts(root=None, limit=MAX_STRUCTURED_RECENT):
    """Resolved captures are still accessible and may be reopened."""
    vault = _root(root)
    items = list(reversed(_inbox_thoughts(vault, include_done=True))) + _sorted_recent(
        _structured_thoughts(vault, include_done=True))
    completed = []
    for item in items:
        try:
            if thought_status.get_done(vault, item.identity):
                completed.append(item)
        except thought_status.StatusError:
            continue
        if len(completed) >= max(1, int(limit)):
            break
    return completed


def _recently_surfaced(store, identity: str, now) -> bool:
    if store is None:
        return False
    raw = store.last_surface(identity)
    if not raw:
        return False
    try:
        moment = datetime.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return (_now(now) - moment) < datetime.timedelta(days=FORGOTTEN_COOLDOWN_DAYS)


def forgotten_thought(root=None, store=None, now=None):
    """Одна старая мысль, которую давно не показывали. Или None.

    Кандидаты: структурированные заметки старше двух дней, не скрытые и не
    показанные в пределах cooldown. Ранжирование: старше — важнее; при равном
    возрасте та, что показывалась раньше (или никогда).
    """
    vault = _root(root)
    today = _now(now).date()
    candidates = []
    for thought in _structured_thoughts(vault):
        if thought.date is None:
            continue
        age = (today - thought.date).days
        if age <= FORGOTTEN_MIN_AGE_DAYS:
            continue
        if store is not None and store.is_hidden(thought.identity, now=now):
            continue
        if _recently_surfaced(store, thought.identity, now):
            continue
        last = store.last_surface(thought.identity) if store is not None else ""
        candidates.append((age, last, thought))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1], item[2].identity))
    return candidates[0][2]


def overview(root=None, store=None, now=None) -> Overview:
    """Ограниченный обзор для экрана `🧠 Мысли`."""
    vault = _root(root)
    return Overview(
        inbox_total=_inbox_total(vault),
        inbox=tuple(inbox_thoughts(vault, limit=MAX_INBOX_RECENT)),
        recent=tuple(recent_thoughts(vault, limit=MAX_STRUCTURED_RECENT)),
        forgotten=forgotten_thought(vault, store, now),
    )


def load_thought(root=None, identity=None):
    """Мысль по identity, с перепроверкой хеша содержимого. Иначе None."""
    vault = _root(root)
    key = (identity or "").strip()
    relative, _sep, _digest_part = key.rpartition("#")
    if not relative or not _digest_part:
        return None

    if relative == INBOX_REL:
        for thought in _inbox_thoughts(vault, include_done=True):
            if thought.identity == key:
                return thought
        return None

    path = vault / relative
    if not _allowed_structured(vault, path):
        return None
    text = _safe_read(vault, path)
    if text is None:
        return None
    if _identity_text(relative, text) != key:
        return None
    body = _strip_frontmatter(text)
    date = _parse_date(frontmatter.get(text, "created")) or _filename_date(path)
    return Thought(
        identity=key,
        path=relative,
        title=_title(text, path),
        excerpt=_excerpt(body),
        date=date,
        kind="fleeting",
    )


def _tokens(text: str):
    found = []
    for word in _WORD_RE.findall((text or "").lower()):
        if word in STOPWORDS or word in found:
            continue
        found.append(word)
    return set(found)


def _normalized(text: str) -> str:
    return " ".join((text or "").casefold().split())


def related_thought(root=None, text=None, store=None, now=None):
    """Одна действительно похожая СТАРАЯ мысль или None. Без модели и сети.

    Кандидаты — только структурированные заметки с реальной датой старше
    сегодняшнего дня. Строка Inbox без даты или сегодняшняя заметка «старой
    мыслью» не считается. Совпадение считается сильным, только если
    пересечение значимых слов детерминировано и покрывает заметную долю
    запроса. Показ дополнительно ограничен суточным cooldown в `store`; без
    `store` cooldown не применяется.
    """
    vault = _root(root)
    captured = _tokens(text)
    if not captured:
        return None
    exact = _normalized(text)
    today = _now(now).date()

    best = None
    best_key = None
    for thought in _structured_thoughts(vault):
        if thought.date is None or thought.date >= today:
            continue
        if _normalized(thought.excerpt) == exact or _normalized(thought.title) == exact:
            continue
        candidate = _tokens(f"{thought.title} {thought.excerpt}")
        shared = captured & candidate
        if len(shared) < RELATED_MIN_SHARED:
            continue
        if len(shared) / len(captured) < RELATED_RATIO_MIN:
            continue
        key = (-len(shared), thought.date.toordinal(), thought.identity)
        if best_key is None or key < best_key:
            best_key, best = key, thought
    if best is None:
        return None
    if store is not None:
        if not store.claim_related(now=now):
            return None
        store.record_surface(best.identity, now=now)
    return best


def _fit(text: str, limit: int = MAX_RENDER_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


def _date_label(thought: Thought) -> str:
    if thought.date is not None:
        return thought.date.isoformat()
    if thought.stamp:
        return thought.stamp
    return "без даты"


def render_overview(overview_data: Overview) -> str:
    """Компактный текст экрана `🧠 Мысли` без абсолютных путей."""
    lines = [INBOX_HEADER, ""]
    lines.append(f"📥 Inbox: {overview_data.inbox_total} необработанных")
    for thought in overview_data.inbox:
        prefix = f"{thought.stamp} " if thought.stamp else ""
        lines.append(f"• {prefix}{thought.title}")
    lines.append("")
    lines.append(f"🆕 Последние ({len(overview_data.recent)}):")
    if overview_data.recent:
        for thought in overview_data.recent:
            lines.append(f"• {_date_label(thought)} — {thought.title}")
    else:
        lines.append("• пока пусто")
    lines.append("")
    lines.append("💡 Вспомнить:")
    if overview_data.forgotten is not None:
        lines.append(f"• {_date_label(overview_data.forgotten)} — "
                     f"{overview_data.forgotten.title}")
    else:
        lines.append("• пока нечего — новых забытых мыслей нет.")
    return _fit("\n".join(lines))


def render_thought(thought: Thought) -> str:
    """Текст выбранной мысли с относительным путём и без абсолютных путей."""
    lines = [f"🧠 {thought.title}", ""]
    lines.append(f"{INBOX_KIND_LABEL.get(thought.kind, thought.kind)} · "
                 f"{_date_label(thought)}")
    if thought.excerpt and _normalized(thought.excerpt) != _normalized(thought.title):
        lines.append(f"«{thought.excerpt}»")
    lines.append(f"📍 {thought.path}")
    return _fit("\n".join(lines))


def render_list(thoughts, header: str) -> str:
    """Короткий текст раздела: заголовок и одна строка на мысль."""
    lines = [header, ""]
    if not thoughts:
        lines.append("• пусто")
    else:
        for thought in thoughts:
            lines.append(f"• {_date_label(thought)} — {thought.title}")
    return _fit("\n".join(lines))


__all__ = [
    "Thought", "Overview", "InboxCounts", "InboxReadError", "overview",
    "inbox_counts", "inbox_thoughts",
    "recent_thoughts", "forgotten_thought", "load_thought", "related_thought",
    "render_overview", "render_thought", "render_list",
    "INBOX_REL", "FLEETING_DIR_REL",
    "MAX_INBOX_RECENT", "MAX_STRUCTURED_RECENT", "MAX_RENDER_CHARS",
    "FORGOTTEN_MIN_AGE_DAYS", "FORGOTTEN_COOLDOWN_DAYS",
]
