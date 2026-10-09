#!/usr/bin/env python3
"""Поиск по базе с проверяемым происхождением — «?вопрос».

Отвечает на вопрос из чата: «?как хормози считает LTV» — и показывает, на каком
именно материале основан каждый фрагмент: точный путь, заголовок, 1-based строки,
слой происхождения (источник / рекомендация автора / интерпретация Dusk /
наблюдение пользователя), явные конфликты и то, что пользователь уже пробовал.

Читает только `research_bridge`: размеченные `raw/`, `wiki/`, `links/`
research-brain и подтверждённые карточки применения stage-2B в Dusk. База
read-only: ни индекса, ни записи, ни моделей, ни сети. Совпадение слов влияет
только на порядок выдачи, но никогда на слой, relation или истинность.

Советы из базы не превращаются в задачи автоматически: ответ — это ссылки на
материал с происхождением, а не команда на действие.
"""

from __future__ import annotations

import os
from pathlib import Path

import research_bridge

LAYER_LABEL = {
    "source": "источник",
    "author_summary": "рекомендация автора",
    "dusk_interpretation": "интерпретация Dusk",
    "user_observation": "наблюдение пользователя",
    "unknown": "слой не размечен",
}

# Telegram держит ~4096 символов. Держим ответ строго короче и усекаем
# по целым результатам с явным сообщением о сокращении.
MAX_ANSWER_CHARS = 4000
MAX_LINK_PATH = 160
MAX_LINK_HEADING = 60
MAX_CONFLICT_PATH = 160
FOOTER = "\nЭто ссылки на материал с происхождением, а не автоматическая задача."
NO_EVIDENCE = ("🔍 Доказательств по базе не нашлось.\n\n"
               "Смотрю только размеченные материалы research-brain "
               "(raw/wiki/links) и подтверждённые применения Dusk. "
               "Попробуй другие слова или добавь материал в базу.")


def vaults() -> list:
    """Все валты базы: my dusk + соседний research-brain.

    Оставлено для совместимости: сам поиск делегируется `research_bridge`,
    который читает только размеченные подкаталоги.
    """
    main = Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))
    found = [main]
    for cand in (main.parent / "research-brain",
                 Path.home() / "Documents/obsidian/research-brain"):
        if cand.is_dir() and cand != main and cand not in found:
            found.append(cand)
            break
    return found


def search(query: str, limit: int = 6) -> list:
    """Фрагменты с происхождением.

    Сохраняет прежние ключи `vault, path, title, excerpt, score` и добавляет
    `id, heading, line_start, line_end, layer, relations, conflicts, observations,
    domain, untrusted`.
    """
    records = research_bridge.search(query, limit=limit)
    hits = []
    for record in records:
        hits.append({
            "vault": record["vault"],
            "path": record["path"],
            "title": record["title"],
            "excerpt": record["fragment"],
            "score": record["score"],
            "id": record["id"],
            "heading": record["heading"],
            "line_start": record["line_start"],
            "line_end": record["line_end"],
            "layer": record["layer"],
            "relations": record["relations"],
            "conflicts": record["conflicts"],
            "observations": record["observations"],
            "domain": record["domain"],
            "untrusted": record["untrusted"],
        })
    return hits


def _safe_link(value: str, limit: int = MAX_LINK_PATH) -> str:
    """Часть вики-ссылки без символов, ломающих `[[путь#заголовок]]`, с лимитом."""
    value = value if isinstance(value, str) else str(value or "")
    for ch in "[]|#\n\r<>":
        value = value.replace(ch, " ")
    value = " ".join(value.split())
    return value[:limit]


def _clip(value, limit: int) -> str:
    """Безопасная одна строка для вывода в Telegram."""
    text = value if isinstance(value, str) else str(value or "")
    for ch in "[]<>|\n\r":
        text = text.replace(ch, " ")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _render_block(number: int, hit: dict) -> list:
    path = _safe_link(hit["path"])
    heading = _safe_link(hit["heading"], MAX_LINK_HEADING)
    link = f"[[{path}#{heading}]]" if heading else f"[[{path}]]"
    vault = "" if hit["vault"] == "dusk" else f" · {_clip(hit['vault'], 40)}"
    layer = LAYER_LABEL.get(hit["layer"], hit["layer"])
    lines = [f"\n{number}. {link} · стр. {hit['line_start']} · слой: {layer}{vault}"]
    if hit["excerpt"]:
        quote = ("[недоверенная цитата] " if hit["untrusted"] else "") + hit["excerpt"]
        lines.append(f"   «{quote}»")
    if hit["domain"]:
        lines.append(f"   домен: {_clip(hit['domain'], 60)}")
    for conflict in hit["conflicts"]:
        lines.append(f"   ⚠️ конфликт ({_clip(conflict['direction'], 20)}): "
                     f"[[{_safe_link(conflict['path'], MAX_CONFLICT_PATH)}]]")
    for obs in hit["observations"]:
        state = "действие: выполнено" if obs.get("done") else "действие: не подтверждено выполненным"
        lines.append(f"   👤 {state}; наблюдение: {_clip(obs.get('observed'), 200)}; "
                     f"вывод: {_clip(obs.get('conclusion'), 200)}")
    return lines


def answer(query: str) -> str:
    """Короткий ответ для Telegram: путь, строки, слой, конфликт, результат.

    Общий лимит `MAX_ANSWER_CHARS`: ответ усекается по целым результатам и явно
    сообщает о сокращении, чтобы Telegram не обрезал его случайно.
    """
    hits = search(query)
    if not hits:
        return NO_EVIDENCE
    header = f"🔍 Нашёл {len(hits)} фрагмент(ов) с происхождением:"
    blocks = [_render_block(i, hit) for i, hit in enumerate(hits, 1)]

    def render(shown: int) -> str:
        text = "\n".join([header] + [line for block in blocks[:shown] for line in block])
        if shown < len(hits):
            text += (f"\n…показаны первые {shown} из {len(hits)} (ответ сокращён)."
                     if shown else "\n…ответ сокращён: результаты не поместились.")
        return text + FOOTER

    shown = len(blocks)
    while shown > 0 and len(render(shown)) > MAX_ANSWER_CHARS:
        shown -= 1
    text = render(shown)
    return text if len(text) <= MAX_ANSWER_CHARS else text[:MAX_ANSWER_CHARS]
