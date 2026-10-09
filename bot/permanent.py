#!/usr/bin/env python3
"""Permanent-слой: превращает повторяющиеся выводы в черновики знаний.

Зачем: fleeting-заметка — это сырьё. Zettelkasten работает, когда из повторяющихся
мыслей вырастает одна заметка «как я это понял». Владелец делает такие руками —
у него уже 33 permanent-заметки, — но система ему в этом не помогала.

Что делает этот модуль:
  1. читает ВСЕ выводы из fleeting-заметок;
  2. просит модель найти темы, которые повторяются (минимум THRESHOLD раз);
  3. на каждую тему пишет ЧЕРНОВИК permanent-заметки в его формате,
     со ссылками на исходные заметки в поле connections.

Черновик помечен как черновик: это предложение, а не готовое знание.
Он лежит рядом с его 33 заметками и ждёт, пока владелец перепишет его своими словами.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

import llm

FLEETING_DIR = "ZETA/FLEETING_DAILY"
PERMANENT_DIR = "ZETA/PERMANENT Знание в СВОЙ инсайт"
THRESHOLD = 3          # сколько РАЗНЫХ заметок должно подтверждать тему
MIN_MATERIAL = 8       # минимум выводов, чтобы искать повторы вообще.
                       # На 4 выводах любая «тема» — это совпадение, а не знание:
                       # проверено, модель начала выдумывать связи.
MAX_DRAFTS = 3         # больше за раз — это уже простыня, а не помощь


def _vault() -> Path:
    return Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))


def _conclusions(vault: Path):
    """Собирает выводы из fleeting-заметок вместе с источником."""
    out = []
    base = vault / FLEETING_DIR
    if not base.is_dir():
        return out
    for f in sorted(base.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"^##\s*Вывод\s*$(.*?)(?=^##|\Z)", text, re.M | re.S)
        if not m:
            continue
        # берём только содержательные строки, служебное «—» пропускаем
        lines = [l.strip("- ").strip() for l in m.group(1).splitlines() if l.strip().startswith("- ")]
        lines = [l for l in lines if l and l not in ("—", "-")]
        if lines:
            out.append({"note": f.stem, "lines": lines})
    return out


def _existing_titles(vault: Path):
    base = vault / PERMANENT_DIR
    if not base.is_dir():
        return set()
    return {re.sub(r"\W+", "", f.stem.lower()) for f in base.rglob("*.md")}


def _match_note(name: str, real_notes):
    """Модель пересказывает названия заметок — сопоставляем по смыслу.

    Строгое совпадение отвергало настоящие источники: в синтетическом тесте
    модель вернула «снимал видео без плана» вместо полного имени файла,
    и годная тема уходила в мусор как «выдумка».
    """
    def norm(s):
        return re.sub(r"\W+", "", s.lower())

    n = norm(name)
    if not n:
        return None
    for r in real_notes:
        if norm(r) == n:
            return r
    for r in real_notes:
        rn = norm(r)
        if n in rn or rn in n:
            return r
    best, score = None, 0
    wset = set(name.lower().split())
    for r in real_notes:
        s = len(wset & set(r.lower().split()))
        if s > score:
            best, score = r, s
    return best if score >= 3 else None


def _ask_themes(items) -> dict:
    listing = "\n".join(f"- [{i['note']}] " + " / ".join(i["lines"]) for i in items)
    prompt = f"""Ты собираешь Zettelkasten для человека. Ниже — выводы из его fleeting-заметок.

Найди темы, которые ПОВТОРЯЮТСЯ в разных выводах — то есть уже стали его знанием,
а не случайной мыслью. Каждая тема должна опираться минимум на {THRESHOLD} разных заметки.

Верни ТОЛЬКО JSON:
{{
  "themes": [
    {{
      "title": "короткое название знания, 4-10 слов, с маленькой буквы",
      "statement": "сама мысль: как это работает, одним-двумя предложениями",
      "sources": ["точные названия заметок из списка, минимум {THRESHOLD}"],
      "summary": "3-6 тезисов: что из этого следует и что делать"
    }}
  ]
}}

Правила:
- Только настоящие повторы. Если повторов нет — верни пустой список themes.
- title — это ЗНАНИЕ, а не тема разговора. «телефон сбивает фокус» вместо «про телефон».
- sources — точные названия в квадратных скобках из списка, не выдумывай.
- Пиши по-русски, без воды.

Выводы:
{listing}"""
    raw = llm.ask(prompt)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.MULTILINE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {"error": (raw or "")[:300]}


def _write_draft(vault: Path, theme: dict, when: datetime.datetime) -> Path:
    title = re.sub(r"[\\/:*?\"<>|]", "", theme.get("title") or "знание").strip()[:70]
    sources = [s for s in (theme.get("sources") or []) if isinstance(s, str)]
    body_points = [p for p in (theme.get("summary") or []) if isinstance(p, str)]
    statement = (theme.get("statement") or "").strip()

    links = " ".join(f"[[{s}]]" for s in sources)
    text = f"""---
connections: {links}
tags:
  - permanent_note
  - черновик
type: permanent_note
created: {when.strftime('%Y-%m-%d %H:%M')}
---

**Select Connection:** `INPUT[inlineListSuggester(optionQuery(#permanent_note), optionQuery(#literature_note), optionQuery(#fleeting_note)):connections]`

> ✍️ **Черновик от системы.** Собрано из {len(sources)} твоих заметок. Перепиши своими словами и убери тег `черновик`.

### {title}

{statement}

"""
    if body_points:
        text += "\n".join(f"- {p}" for p in body_points) + "\n\n"
    if sources:
        text += "### Откуда\n\n" + "\n".join(f"- [[{s}]]" for s in sources) + "\n"

    path = vault / PERMANENT_DIR / f"{title}.md"
    counter = 1
    while path.exists():
        path = vault / PERMANENT_DIR / f"{title} ({counter}).md"
        counter += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def draft_permanents(vault: Path | None = None) -> str:
    """Основной проход. Возвращает отчёт для Telegram."""
    v = vault or _vault()
    items = _conclusions(v)
    if len(items) < MIN_MATERIAL:
        return (f"🧠 Permanent-черновики: пока рано — выводов всего {len(items)}, "
                f"нужно минимум {MIN_MATERIAL}. На малом числе заметок любая «тема» "
                f"оказывается случайным совпадением, а не знанием.")

    data = _ask_themes(items)
    if "error" in data:
        return f"⚠️ Модель не ответила: {data['error']}"

    themes = [t for t in (data.get("themes") or []) if isinstance(t, dict)][:MAX_DRAFTS]
    if not themes:
        return (f"🧠 Permanent-черновики: повторяющихся тем не нашлось "
                f"(проверено {len(items)} выводов). Это нормально, пока мыслей мало.")

    known = _existing_titles(v)
    real_notes = {i["note"] for i in items}
    made, skipped, rejected = [], [], []
    for th in themes:
        norm = re.sub(r"\W+", "", (th.get("title") or "").lower())
        if norm and norm in known:
            skipped.append(th.get("title"))
            continue

        # Источники проверяем сами: модель любит дописать лишнюю заметку,
        # чтобы дотянуть до порога. Считаем только реально существующие
        # и относящиеся к делу.
        src, seen = [], set()
        for s in (th.get("sources") or []):
            r = _match_note(s, real_notes) if isinstance(s, str) else None
            if r and r not in seen:
                seen.add(r)
                src.append(r)
        if len(src) < THRESHOLD:
            rejected.append((th.get("title"), len(src)))
            continue
        th["sources"] = src
        made.append(_write_draft(v, th, datetime.datetime.now()))

    out = [f"🧠 Permanent-черновики: {len(made)} из {len(themes)} тем"]
    for p in made:
        out.append(f"• {p.stem}")
    if skipped:
        out.append(f"↩️ уже есть такие знания: {len(skipped)}")
    if rejected:
        out.append(f"⛔ отклонено как выдумка (мало реальных источников): {len(rejected)}")
    if made:
        out.append("")
        out.append("Это ЧЕРНОВИКИ. Перепиши своими словами и убери тег `черновик`.")
    return "\n".join(out)
