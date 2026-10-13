#!/usr/bin/env python3
"""Разбор инбокса: модель предлагает структуру, код её применяет.

Почему так, а не «пусть агент сам правит файлы»:
  * модель не трогает валт напрямую — она возвращает JSON, который мы проверяем;
  * схема фиксирована, поэтому заметка не появится с битым frontmatter;
  * если модель ответит мусором, ничего не сломается — разбор просто вернёт ошибку;
  * один и тот же код работает с любой OpenAI-совместимой моделью.

Формат строки в инбоксе:  - **ЧЧ:ММ** текст
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

import thought_feed
import thought_status
import memory_history
import inbox_core
import llm
import someday
import vault_ops
import zettel

MAX_ITEMS = 10          # больше 10 задач за раз — это уже марафон, а не разбор
MAX_STEPS = 6

SYSTEM_PROMPT = """Ты разбираешь инбокс заметок на русском языке.

Верни ТОЛЬКО JSON, без пояснений и без markdown-обёртки:
{
  "items": [
    {
      "source": "точный текст строки из инбокса, без времени",
      "kind": "task" | "private" | "junk" | "thought" | "later",
      "insight": "Вывод: что из этого следует — наблюдение, вопрос или решение",
      "marker": "done | question | important",
      "title": "короткое название задачи, 3-8 слов, с маленькой буквы",
      "priority": "low" | "normal" | "high",
      "timeEstimate": 30,
      "steps": ["шаг 15-30 минут", "ещё шаг"],
      "trigger": "для kind=later: при каком условии к этому вернуться (одна строка)",
      "topic": "для kind=later: короткая тема-раздел, например DSH, Obsidian, деньги",
      "area": "одна область из списка выше",
      "project": "один проект из списка или пусто"
    }
  ]
}

Правила:
- kind="task" — это действие, которое можно начать делать.
- kind="private" — личное: здоровье, деньги, отношения, дневник, документы.
- kind="task" — действие, которое можно начать делать. kind="thought" — мысль, наблюдение, идея
  без немедленного действия. Владелец ведёт Zettelkasten: каждая мысль становится заметкой,
  поэтому thought — это нормальный результат, а не ошибка.
- kind="later" — ПОЛЕЗНО, НО НЕ СЕЙЧАС: инструмент, плагин, подход, идея на будущее.
  Это не задача: делать её сейчас не надо. Обязательно заполни trigger — при каком условии
  вернуться. Пример: "когда начну работать с телефона" или "когда снова пропущу вопрос агента".
- kind="junk" — тестовое сообщение, мусор, приветствие, случайный набор слов.
- insight (Вывод) — заполняй ВСЕГДА, даже для thought: одна строка, что из этого следует.
- marker: "done" если что-то уже сделано, "question" если это вопрос, "important" если важно.
- timeEstimate — минуты, целое число. Реалистично: от 5 до 480.
- steps — только для задач дольше 60 минут. Каждый шаг 15-30 минут, начинается с глагола.
  Для коротких задач оставь пустой список.
- Не выдумывай задачи, которых нет в тексте. Если строка непонятна — kind="junk".
- Если строк несколько про одно и то же — сделай одну задачу, source укажи первую.
"""


# --- Детерминированная защита приватного -------------------------------------
# Приватность нельзя вешать только на модель: на тесте qwen-plus отнёс
# "записаться к врачу" к задачам, и медицинская запись ушла бы в git.
# Поэтому личное определяется ещё и по словам — это не зависит от модели
# и срабатывает даже если модель ошиблась. Ошибка в эту сторону безопасна:
# запись просто окажется в Private/, а не в репозитории.
# ВАЖНО: совпадение только с НАЧАЛА слова. Голый стем "жен" ловился внутри
# "должен", и обычная задача про рил уезжала в Private/ — то есть терялась.
# Поэтому короткие стемы дополнены окончаниями через регулярку.
from capture_privacy import PRIVATE_PATTERNS, looks_private as _looks_private


def _extract_json(raw: str):
    """Достаёт JSON даже если модель обернула его в ``` или добавила текст."""
    if not raw:
        return None, "пустой ответ модели"
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1]), None
        except json.JSONDecodeError as e:
            return None, f"не разобрал JSON: {e}"
    return None, "в ответе модели нет JSON"


def _clean(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _existing_titles(tasks_dir: Path):
    titles = set()
    for f in tasks_dir.glob("*.md"):
        try:
            content = f.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r'^title:\s*"?(.*?)"?\s*$', content, re.M)
        t = (m.group(1) if m else f.stem)
        titles.add(re.sub(r"\W+", "", t.lower()))
    return titles


def _create_task(tasks_dir: Path, item: dict, source: str, now: datetime.datetime,
                 backlink: str = "") -> Path:
    title = _clean(item.get("title") or item.get("source"), 80) or "задача из инбокса"
    safe = "".join(c for c in title if c.isalnum() or c in " -_" or "\u0400" <= c <= "\u04ff").strip()[:50]
    path = tasks_dir / f"{safe or 'task'}.md"
    counter = 1
    while path.exists():
        path = tasks_dir / f"{safe} {counter}.md"
        counter += 1

    try:
        minutes = int(float(item.get("timeEstimate") or 0))
    except (TypeError, ValueError):
        minutes = 0
    minutes = max(5, min(480, minutes)) if minutes else 0

    priority = item.get("priority") if item.get("priority") in ("low", "normal", "high") else "normal"

    steps = [s for s in (item.get("steps") or []) if isinstance(s, str) and s.strip()][:MAX_STEPS]
    steps_block = ""
    if steps:
        # рендерим всегда, когда модель их дала: раньше условие "> 60" съедало
        # разбивку ровно на часовой задаче
        steps_block = "\n## Шаги\n\n" + "\n".join(f"- [ ] {_clean(s, 120)}" for s in steps) + "\n"

    yaml_title = title.replace('"', "'")
    # следующий шаг прямо в поле — чтобы он был виден на карточке доски,
    # а не только внутри заметки
    next_step = (steps[0].replace('"', "'")[:90] if steps else title.replace('"', "'")[:90])
    body = f"""---
title: "{yaml_title}"
status: open
priority: {priority}
created: {now.strftime('%Y-%m-%dT%H:%M')}
scheduled: {now.strftime('%Y-%m-%d')}
timeEstimate: {minutes}
nextStep: "{next_step}"
tags:
  - task
source: "{backlink or 'ZETA/FLEETING/00 Инбокс.md'}"
---

# {title}
{steps_block}"""
    path.write_text(body, encoding="utf-8")
    return path


def run_triage(limit: int = MAX_ITEMS) -> str:
    """Разбирает инбокс. Возвращает текст для Telegram."""
    # Записи приезжают с телефона через VPS и git — без подтягивания
    # разбор читал бы устаревший инбокс.
    vault_ops.git_sync()

    vault = Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))
    inbox = vault / "ZETA" / "FLEETING" / "00 Инбокс.md"
    tasks_dir = vault / "TaskNotes" / "Tasks"
    private = vault / "Private" / "00 Private.md"

    if not inbox.exists():
        return "Инбокс пуст — разбирать нечего."

    lines = inbox.read_text(encoding="utf-8").splitlines()
    entries = []
    for i, l in enumerate(lines):
        if l.startswith("- "):
            if (thought_status.is_done(vault, thought_feed._identity_text("ZETA/FLEETING/00 Инбокс.md", l.strip()))
                    or memory_history.is_past(vault, thought_feed._identity_text("ZETA/FLEETING/00 Инбокс.md", l.strip()))):
                continue
            text = re.sub(r"^- \*\*\d{2}:\d{2}\*\*\s*", "", l).strip()
            if text:
                entries.append((i, l, text))

    if not entries:
        return "📥 Инбокс пуст — разбирать нечего."

    listing = "\n".join(f"{n}. {t}" for n, (_, _, t) in enumerate(entries, 1))
    tax = zettel.taxonomy(vault)
    context = (f"\n\nЕго области: {', '.join(tax['areas'])}"
               f"\nЕго проекты: {', '.join(tax['projects'])}"
               f"\nЕго навыки: {', '.join(tax['skills'])}")
    # личный контент уходит только в свой Vertex (см. bot/llm.py)
    raw = llm.ask(f"{SYSTEM_PROMPT}{context}\n\nСтроки инбокса:\n{listing}")
    data, error = _extract_json(raw)

    if error:
        return f"⚠️ Разбор не удался: {error}\n\nОтвет модели:\n{raw[:600]}"

    items = data.get("items")
    if not isinstance(items, list) or not items:
        return "⚠️ Модель не вернула ни одного пункта. Инбокс не тронут."

    # если модель вернула больше, остальное оставляем на следующий проход
    items = items[:limit]

    tasks_dir.mkdir(parents=True, exist_ok=True)
    known = _existing_titles(tasks_dir)
    now = datetime.datetime.now()

    by_source = {}
    for it in items:
        if isinstance(it, dict):
            key = re.sub(r"\W+", "", _clean(it.get("source"), 120).lower())
            by_source[key] = it

    created, to_private, junk, untouched, skipped = [], [], [], [], []
    fleeting, thoughts, later = [], [], []
    removed_idx = set()          # какие строки инбокса убрать (по индексу, не по тексту)

    for idx, line, text in entries:
        key = re.sub(r"\W+", "", text.lower())
        item = by_source.get(key)
        if item is None:
            # нечёткое совпадение: модель могла слегка переписать строку
            for k, v in by_source.items():
                if k and (k in key or key in k) and abs(len(k) - len(key)) < 25:
                    item = v
                    break
        if item is None:
            untouched.append(text)
            continue

        kind = item.get("kind")
        if kind != "private" and _looks_private(text):
            kind = "private"      # предохранитель важнее мнения модели
        if kind == "later":
            # полезно, но не сейчас: не задача, а запись с триггером возврата
            someday.add(
                title=_clean(item.get("title") or text, 80),
                what=text,
                trigger=item.get("trigger") or "",
                topic=item.get("topic") or "",
                source="из инбокса",
                vault=vault,
            )
            later.append(_clean(item.get("title") or text, 60))
            removed_idx.add(idx)
        elif kind == "private":
            # переносим исходную строку как есть — время захвата сохраняется
            inbox_core.append_raw(private, line, "thought",
                                  default_body=inbox_core.DEFAULT_PRIVATE_BODY)
            to_private.append(text)
            removed_idx.add(idx)
        elif kind == "junk":
            junk.append(text)
            removed_idx.add(idx)
        else:
            # Zettelkasten: мысль НЕ остаётся голой строкой, а становится заметкой
            # с Контекстом (среда рождения) и Связями (область, проект, навык).
            earlier = [e[2] for e in entries if e[0] < idx][-3:]
            f_path = zettel.make_fleeting(
                text=text,
                title=item.get("title") or text,
                conclusion=item.get("insight") or "",
                marker=item.get("marker") or ("done" if kind == "task" else "question"),
                area=item.get("area") or "",
                project=item.get("project") or "",
                when=now,
                source="Telegram-бот / инбокс",
                before=earlier or zettel.recent_thoughts(vault),
                vault=vault,
            )
            fleeting.append(f_path.name)

            if kind == "task":
                norm = re.sub(r"\W+", "", (_clean(item.get("title"), 80) or text).lower())
                if norm in known:
                    skipped.append(text)
                else:
                    path = _create_task(tasks_dir, item, text, now, backlink=f_path.stem)
                    known.add(norm)
                    created.append(path.name)
            else:
                thoughts.append(text)
            removed_idx.add(idx)

    if removed_idx:
        keep = [l for i, l in enumerate(lines) if i not in removed_idx]
        inbox.write_text("\n".join(keep).rstrip() + "\n", encoding="utf-8")

    # Коммитим результат: без этого созданные задачи остались бы лежать на VPS
    # и никогда не доехали бы до Mac — валт синхронизируется через git.
    if created or to_private or junk or skipped:
        vault_ops.git_push(f"triage: разобрано {len(removed_idx)} записей инбокса")

    handled = len(removed_idx)

    out = [f"📥 Разбор инбокса: обработано {handled} из {len(entries)}"]
    if fleeting:
        out.append("")
        out.append(f"🧠 Заметок в ZETA/FLEETING_DAILY ({len(fleeting)}) — с Контекстом и Связями")
        out += [f"• {n[:-3]}" for n in fleeting[:limit]]
    if later:
        out.append("")
        out.append(f"🗄 Отложено на потом ({len(later)}) — [[Отложено]]")
        out += [f"• {n}" for n in later[:limit]]
    if created:
        out.append("")
        out.append(f"✅ Задачи ({len(created)})")
        out += [f"• {n[:-3]}" for n in created[:limit]]
    if to_private:
        out.append("")
        out.append(f"🔒 Унесено в Private/ ({len(to_private)}) — вне git")
    if skipped:
        out.append("")
        out.append(f"↩️ Уже есть такие задачи ({len(skipped)})")
    if junk:
        out.append("")
        out.append(f"🗑 Мусор убран ({len(junk)})")
    if thoughts:
        out.append("")
        out.append(f"💭 Мыслей без действия: {len(thoughts)} — уже в заметках, не задачи")
    if untouched:
        out.append("")
        out.append(f"⚠️ Модель не разобрала: {len(untouched)} — остались в инбоксе")
        out += [f"• {_clean(t, 60)}" for t in untouched[:3]]

    return "\n".join(out)
