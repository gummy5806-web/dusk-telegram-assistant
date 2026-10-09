#!/usr/bin/env python3
"""Dusk-first роутер намерений: чистая функция без Telegram, сети и файлов.

Один естественный запрос превращается в один из четырёх безопасных интентов:

* `capture` — сохранить как есть в Inbox (дефолт для всего неоднозначного);
* `task` — создать ровно одну задачу уже существующим точным helper;
* `personal_search` — найти в личном Dusk;
* `knowledge_search` — дополнительно посмотреть Research Brain.

Модуль намеренно НЕ парсит русский ответ и не угадывает действие по тексту
ответа промпта. Он не делает I/O: только регулярные правила и неизменяемый
результат `Route`. Личная формулировка (`я думал`, `моя идея`, `моя задача`,
`я писал`) всегда подавляет Research Brain, даже если рядом стоит имя автора.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KIND_CAPTURE = "capture"
KIND_TASK = "task"
KIND_PERSONAL_SEARCH = "personal_search"
KIND_KNOWLEDGE_SEARCH = "knowledge_search"

MAX_SUBJECT_LEN = 200

# Личная память — это ПОИСК, а не просто упоминание «моя идея/задача». Триггеры:
# «что/где/я ... думал/записал/помню», «где <мысль>», «найди/покажи ... мою
# заметку/идею/запись». Утверждение «моя идея ...» или «у меня был созвон» —
# не поиск и не исследование, а безопасный capture.
_PERSONAL_RE = re.compile(
    r"(?:"
    r"\bя\s+(?:(?:вчера|сегодня|недавно|потом|уже|только\s+что)\s+)?"
    r"(?:думал|думала|подумал|подумала|писал|писала|записал|записала|"
    r"записывал|записывала|отмечал|отмечала|размышлял|размышляла)\b"
    r"|\bпомнишь\b"
    r"|\bгде\b[^.!?\n]{0,40}?\b(?:мысл|иде|заметк|запис|задач|проект|ресурс)"
    r"|\bпомоги(?:\s+мне)?\s+найти\b"
    r"|\b(?:найди|поищи|отыщи|покажи|напомни)\b[^.!?\n]{0,30}?"
    r"\bмо(?:й|я|ю|е|и)\b"
    r")",
    re.IGNORECASE,
)

# Research Brain включается только явным вопросом/поисковым оборотом. Просто
# слово «термин»/«материал»/«советовал»/«research brain» в утверждении — нет.
_KNOWLEDGE_QUERY_RE = re.compile(
    r"(?:"
    # определения и термины-вопросы
    r"\bкто\s+так(?:ой|ая|ое|ие)\b"
    r"|\bчто\s+так(?:ое|ая|ие)\b"
    r"|\bчто\s+означа(?:ет|ют)\b"
    r"|\bобъясни\s+(?:концепт|термин|понятие|идею|теорию|модель)\b"
    # явный поиск по знанию: «найди/покажи/... в конспектах/термин/материал»
    r"|\b(?:найди|поищи|отыщи|покажи|напомни|спроси)\b[^.!?\n]{0,40}?"
    r"(?:в\s+конспектах|конспект\w*|термин\w*|материал\w*|research\s*brain)\b"
    # вопрос с явным предметом знания: «что в конспектах про X», «что ... термин X»
    r"|\bчто\b[^.!?\n]{0,40}?"
    r"(?:в\s+конспектах|конспект\w*|термин\w*|материал\w*|research\s*brain)\b"
    r")",
    re.IGNORECASE,
)

# «что <Автор> говорил/советовал/...» и «что говорил/советовал/... <Автор>».
# Автор — 1–3 имени собственных, каждое с заглавной буквы (кириллица/латиница),
# поэтому этот regex НЕ компилируется с IGNORECASE (иначе проверка заглавной
# буквы бессмысленна), а глаголы речи — в нижнем регистре, как в обычном тексте.
_AUTHOR_NAME = r"[А-ЯЁA-Z][\w'\-]+"
_AUTHOR_QUERY_RE = re.compile(
    r"(?:\b[Чч]то\s+(" + _AUTHOR_NAME + r"(?:\s+" + _AUTHOR_NAME + r"){0,2})\s+"
    r"(?:говорил|говорила|советовал|советовала|рассказывал|рассказывала|писал|писала)\b"
    r"|\b[Чч]то\s+"
    r"(?:говорил|говорила|советовал|советовала|рассказывал|рассказывала|писал|писала)\s+"
    r"(" + _AUTHOR_NAME + r"(?:\s+" + _AUTHOR_NAME + r"){0,2}))",
    re.UNICODE,
)
# Заглавная буква у обычного слова (в начале «Что Я советовал») — не автор.
_NON_AUTHOR_WORDS = frozenset({
    "я", "мы", "ты", "вы", "он", "она", "оно", "они", "это", "кто",
    "что", "все", "мама", "папа", "друг", "друзья", "клиент", "клиенты",
    "автор", "брат", "сестра", "жена", "муж", "коллега", "коллеги",
})

# Явное создание задачи без пунктуации. «день:»/«задачи на день:» — legacy dispatch.
_TASK_RE = re.compile(
    r"^\s*(?:"
    r"(?:создай|добавь|заведи|поставь|сделай|новая|новую)\s+задач\w*"
    r"|(?:задача|todo)"
    r")\s*[:\-]?\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)

_SEARCH_PREFIX_RE = re.compile(
    r"^\s*(?:\?|(?:спроси|найди)\s*[:\-]?)\s*",
    re.IGNORECASE,
)

_PERSONAL_STRIP = (
    re.compile(
        r"^я\s+(?:(?:вчера|сегодня|недавно|потом|уже|только\s+что)\s+)?"
        r"(?:думал\w*|подумал\w*|писал\w*|записал\w*|записывал\w*|"
        r"отмечал\w*|размышлял\w*)\s*",
        re.IGNORECASE),
    re.compile(
        r"^что\s+я\s+(?:думал\w*|подумал\w*|писал\w*|записал\w*|записывал\w*|"
        r"отмечал\w*|размышлял\w*)\s*",
        re.IGNORECASE),
    re.compile(
        r"^помнишь(?:\s+(?:идею|мысль|заметку|задачу|запись))?\s*",
        re.IGNORECASE),
    re.compile(
        r"^где\s+мо\w+\s+(?:заметк\w*|иде\w*|мысл\w*|задач\w*|запис\w*|"
        r"проект\w*|ресурс\w*)\s*",
        re.IGNORECASE),
    re.compile(
        r"^мо\w+\s+(?:иде\w*|заметк\w*|задач\w*|мысл\w*|запис\w*|"
        r"проект\w*|ресурс\w*)\s*",
        re.IGNORECASE),
    re.compile(r"^что\s+", re.IGNORECASE),
)
# Ведущие обороты поиска и служебные существительные, которые не являются
# предметом: «найди ТУ МЫСЛЬ про X», «помоги найти МОЮ ЗАПИСЬ про X».
_LEAD_NOISE_RE = re.compile(
    r"^(?:помоги(?:\s+мне)?\s+найти|найти|отыщи|поищи|где)\s*", re.IGNORECASE)
_OWNER_NOISE_RE = re.compile(
    r"(?:\bту\b|\bмо(?:й|я|ю|е|и|их|его|ей)\b"
    r"|\bмысл\w*\b|\bиде(?:я|и|ю|ей|е|й)\b"
    r"|\bзаметк\w*\b|\bзаметок\b|\bзапис\w*\b|\bзадач\w*\b"
    r"|\bпроект\w*\b|\bресурс\w*\b)",
    re.IGNORECASE)
# Служебные слова Research Brain не должны попадать в предмет поиска.
_RESEARCH_NOISE_RE = re.compile(
    r"(?:\bв\s+research\s*brain\b|\bresearch\s*brain\b|\bв\s+конспектах\b"
    r"|\bконспект\w*|\bтермин\w*|\bв\s+материал\w*|\bматериал\w*\s+по\b)",
    re.IGNORECASE)
_LEAD_PREP_RE = re.compile(r"^(?:про|о|об|по|насч[её]т|по\s+поводу)\s+", re.IGNORECASE)
_FILLER_RE = re.compile(r"\b(?:про|о|об|по|насч[её]т)\b", re.IGNORECASE)
_SPEAKER_RE = re.compile(
    r"\b(?:говорил|говорила|советовал|советовала|рассказывал|рассказывала)\b",
    re.IGNORECASE,
)
# Ведущие вопросительные обороты: «кто такой X», «что такое X», «объясни концепт X».
_QUERY_CUE_STRIP_RE = re.compile(
    r"^(?:кто\s+так(?:ой|ая|ое|ие)"
    r"|что\s+так(?:ое|ая|ие)"
    r"|что\s+означа(?:ет|ют)"
    r"|объясни(?:\s+(?:концепт|термин|понятие|идею|теорию|модель))?)"
    r"\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Route:
    """Неизменяемый результат маршрутизации одного сообщения."""

    kind: str
    subject: str = ""
    personal: bool = False
    research: bool = False
    source: str = ""


def clean_subject(text: str) -> str:
    """Убирает служебные обороты и предлоги, оставляя чистый предмет поиска."""
    value = (text or "").strip()
    value = _SEARCH_PREFIX_RE.sub("", value)
    value = _QUERY_CUE_STRIP_RE.sub("", value)

    changed = True
    while changed:
        changed = False
        for pattern in _PERSONAL_STRIP:
            stripped = pattern.sub("", value)
            if stripped != value:
                value = stripped
                changed = True

    value = _SPEAKER_RE.sub(" ", value)
    value = _LEAD_NOISE_RE.sub("", value)
    value = _RESEARCH_NOISE_RE.sub(" ", value)
    value = _OWNER_NOISE_RE.sub(" ", value)
    value = _LEAD_PREP_RE.sub("", value)
    value = _FILLER_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip(" .,;:!?-")
    return value[:MAX_SUBJECT_LEN].strip()


def _task_subject(raw: str) -> str:
    value = re.sub(r"\s+", " ", (raw or "").strip()).strip(" .,;:!?-")
    return value[:MAX_SUBJECT_LEN].strip()


def _has_personal(text: str) -> bool:
    return bool(_PERSONAL_RE.search(text or ""))


def _has_knowledge(text: str) -> bool:
    raw = text or ""
    if _KNOWLEDGE_QUERY_RE.search(raw):
        return True
    match = _AUTHOR_QUERY_RE.search(raw)
    if not match:
        return False
    author = (match.group(1) or match.group(2) or "").strip()
    return bool(author) and author.casefold() not in _NON_AUTHOR_WORDS


def route(text: str) -> Route:
    """Единственное решение о намерении. Ничего не пишет и не читает."""
    raw = (text or "").strip()
    if not raw:
        return Route(KIND_CAPTURE, "", source="empty")

    task = _TASK_RE.match(raw)
    if task and task.group(1).strip():
        return Route(KIND_TASK, _task_subject(task.group(1)), source="task")

    subject = clean_subject(raw)
    if _has_personal(raw):
        return Route(KIND_PERSONAL_SEARCH, subject,
                     personal=True, research=False, source="personal")
    if _has_knowledge(raw):
        return Route(KIND_KNOWLEDGE_SEARCH, subject,
                     personal=True, research=True, source="knowledge")
    if _SEARCH_PREFIX_RE.match(raw):
        return Route(KIND_PERSONAL_SEARCH, subject or raw,
                     personal=True, research=False, source="search")
    return Route(KIND_CAPTURE, raw[:MAX_SUBJECT_LEN], source="capture")


__all__ = [
    "KIND_CAPTURE", "KIND_TASK", "KIND_PERSONAL_SEARCH", "KIND_KNOWLEDGE_SEARCH",
    "Route", "route", "clean_subject",
]
