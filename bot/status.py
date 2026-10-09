#!/usr/bin/env python3
"""Статус — одна страница, которую открываешь и видишь всё.

Что здесь есть и откуда:

* Scorecard — небольшой набор недельных чисел, каждое «в норме / не в норме».
  Это метод управления из EOS/Traction (Scorecard, Level 10):
  https://www.eosworldwide.com/level-10-meeting
  Из него же — рок-цели цикла (1–3).
* Процент выполнения считается по СОХРАНЁННОМУ плану недели
  (`bot/weekly_plan.py`), а не по текущим датам карточек. Пока план не
  зафиксирован вручную, метрика честно говорит «план не зафиксирован».
* Неделя — календарная, понедельник–воскресенье, в зоне Asia/Almaty,
  обе границы включительны.

Чего здесь нет:
* Правила «задачу старше двух недель выбрасывать» в EOS нет, и здесь его тоже
  нет. Порог AGING_DAYS — личная настройка: возраст задачи это повод её
  пересмотреть, а не автоматический приговор. Ничего не удаляется и не
  архивируется автоматически.

Числа считаются арифметикой по валту. Модель (провайдер из настроек) делает
только то, что требует суждения: какие 3 дела на сегодня, какие рок-цели.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from pathlib import Path

import frontmatter
import learning_cycle
import llm
import market
import someday
import weekly_plan
import vault_ops

WEEK = 7
AGING_DAYS = 14          # ЛИЧНАЯ настройка, не правило метода: сколько дней
                         # без движения считать поводом пересмотреть задачу
TARGET_EXECUTION = 85    # порог 12 Week Year (метод); для одного человека — ориентир,
                         # а не доказанная норма
TARGET_AGING = 3         # ЛИЧНЫЙ ориентир: сколько застрявших задач терпимо

STATUS_PATH = "Статус.md"


def _tasks(vault: Path):
    """Карточки: все поля — только из первой валидной шапки.

    Пример `completedDate:` в тексте заметки метаданными не является, поэтому
    регулярка по всему файлу здесь больше не используется.
    """
    rows = []
    for f in sorted((vault / "TaskNotes" / "Tasks").glob("*.md")):
        content = f.read_text(encoding="utf-8")

        def field(name):
            return frontmatter.get(content, name)

        rows.append({
            "title": field("title") or f.stem,
            "status": field("status"),
            "priority": field("priority") or "normal",
            "scheduled": field("scheduled"),
            "due": field("due"),
            "completed": field("completedDate"),
            "minutes": field("timeEstimate"),
        })
    return rows


def _metrics(vault: Path) -> dict:
    today = weekly_plan.today_local()
    week_start, week_end = weekly_plan.week_bounds(today)
    aging_cut = today - datetime.timedelta(days=AGING_DAYS)

    rows = _tasks(vault)
    # Даты разбирает одна строгая функция: полная дата или полный timestamp с
    # приведением к Asia/Almaty. Закрытие считается внутри недели и не позже
    # сегодня — фактическое закрытие не может быть в будущем.
    week_start_date = datetime.date.fromisoformat(week_start)
    closed_upper = min(datetime.date.fromisoformat(week_end), today)
    closed_week = sum(
        1 for r in rows
        if r["status"] == "done"
        and (completed_day := weekly_plan.parse_task_date(r["completed"])) is not None
        and week_start_date <= completed_day <= closed_upper
    )
    open_rows = [r for r in rows if r["status"] != "done"]
    overdue = sum(
        1 for r in open_rows
        if (due_day := weekly_plan.parse_task_date(r["due"])) is not None and due_day < today
    )
    aging = [
        r for r in open_rows
        if (planned_day := weekly_plan.parse_task_date(r["scheduled"])) is not None
        and planned_day < aging_cut
    ]

    # Процент выполнения — по сохранённому плану недели, а не по датам карточек.
    # Метрика read-only: план здесь не создаётся, только читается.
    plan = weekly_plan.metric(vault, today)
    execution = plan["percent"] if plan["fixed"] else None

    # Строки, добавленные в инбокс по данным git. Это НЕ число захватов:
    # переформатирование строки тоже считается, а незакоммиченный захват — нет.
    captures = 0
    try:
        out = subprocess.run(
            ["git", "log", f"--since={WEEK} days ago", "--numstat", "--format=", "--", "ZETA/FLEETING/00 Инбокс.md"],
            cwd=vault, capture_output=True, text=True, timeout=30).stdout
        captures = sum(int(l.split("\t")[0]) for l in out.splitlines()
                       if l and l.split("\t")[0].isdigit())
    except Exception:
        pass

    return {
        "today": today.isoformat(),
        "week_start": week_start,
        "week_end": week_end,
        "plan": plan,
        "closed_week": closed_week,
        "execution": execution,
        "overdue": overdue,
        "aging": aging,
        "aging_count": len(aging),
        "captures": captures,
        "open_count": len(open_rows),
        "open_rows": open_rows,
    }


def _light(value, green, yellow):
    """🟢/🟡/🔴 — on-track/off-track из EOS, только нагляднее.

    green/yellow — границы: value >= green → 🟢, value >= yellow → 🟡, иначе 🔴.
    Для «чем меньше, тем лучше» границы передаются отрицательными.
    """
    if value >= green:
        return "🟢"
    if value >= yellow:
        return "🟡"
    return "🔴"


def _ask_model(m: dict) -> dict:
    """Суждение модели: рок-цели, три дела на сегодня, что выбросить."""
    listing = "\n".join(
        f"- [{r['priority']}] {r['title']}"
        + (f" (план {r['scheduled'][:10]})" if r["scheduled"] else "")
        + (f" (срок {r['due']})" if r["due"] else "")
        + (f" ~{r['minutes']}м" if r["minutes"] else "")
        for r in m["open_rows"][:40]
    )
    execution_text = (
        f"{m['execution']}%" if m["execution"] is not None
        else "не считается: план недели не зафиксирован"
    )
    prompt = f"""Ты операционный директор. Помоги владельцу валта увидеть картину и решить, что делать.

Числа: незакрытых {m['open_count']}, застряло дольше {AGING_DAYS} дней {m['aging_count']}, выполнение плана недели {execution_text}, закрыто за неделю ({m['week_start']}…{m['week_end']}) {m['closed_week']}.

Его открытые дела:
{listing}

Верни ТОЛЬКО JSON:
{{
  "rocks": ["1-3 цели на цикл, 3-6 слов, из его дел — то, что реально двигает"],
  "today": [{{"title": "точное название из списка", "minutes": 25, "why": "зачем именно это сегодня, 1 строка"}}],
  "cut": ["что выбросить или отложить без потерь, 1-4 пункта, с причиной"]
}}

Правила:
- today ровно 3 пункта, бери из его списка, названия не переписывай.
- Предпочитай то, что закрывает висящее дольше двух недель или приближает к деньгам/контенту.
- cut: честно называй то, что не сделается никогда. Это нормально.
- Без мотивационных фраз. Коротко."""

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


def _market_section(vault: Path, today: datetime.date | None = None) -> list:
    """Факты рынка и один явный фокус цикла.

    Недельная цель сравнивается только с текущей календарной неделей
    понедельник→сегодня в `Asia/Almaty`: строки до понедельника и после дня
    владельца не влияют на состояние цели. Скользящего окна здесь нет, поэтому
    его нельзя перепутать с целью/неделей. Без полного фокуса — только факты и
    недостающее, без красного/зелёного. Причинность не подразумевается.
    """
    owner = today or weekly_plan.today_local()
    start_iso, _end_iso = weekly_plan.week_bounds(owner)
    week_start = datetime.date.fromisoformat(start_iso)
    period = f"{start_iso[5:]}–{owner.isoformat()[5:]}"
    lines = ["## 💰 Рынок — факты и фокус", ""]

    config = market.parse_config(vault)
    if config["status"] == "configured":
        label, unit = market.COUNTERS[config["metric"]]
        lines.append(f"> Фокус цикла: **{config['focus']}** · канал: {config['channel_label']} · "
                     f"ведущая метрика: {label.lower()}, {unit} · "
                     f"цель/неделя: {config['goal']:g}.")
    else:
        lines.append(f"> Фокус цикла не задан полностью: {config['reason']}.")
        if config["missing"]:
            lines.append("> Не хватает: " + ", ".join(config["missing"]) + ".")
        if config["unsupported"]:
            lines.append("> Не поддерживается: " + ", ".join(config["unsupported"]) + ".")
        if config["duplicates"]:
            lines.append("> Неоднозначность: " + ", ".join(config["duplicates"]) + ".")
        lines.append("> Пока цели нет — показываю только факты, без оценки.")
    lines.append("")

    # Точная календарная неделя пн→сегодня, строго: повреждённые данные не
    # превращаются в честный ноль и не дают оценку цели.
    try:
        totals, days = market.range_totals(vault, week_start, owner, strict=True)
        corrupted = ""
    except market.MarketError as exc:
        totals, corrupted = {}, str(exc)
    if corrupted:
        lines.append(f"> ⚠️ Данные рынка повреждены: {corrupted}. "
                     "Факты не показываю, цель не оценивается.")
        lines.append("")
        return lines

    # Факты без оценки: никакого красного/зелёного, пока нет явной цели.
    lines.append(f"| Счётчик | За неделю пн→сегодня ({period}) |")
    lines.append("|---|---|")
    for key in market.COLUMNS:
        label, unit = market.COUNTERS[key]
        lines.append(f"| {label}, {unit} | {totals.get(key, 0):g} |")
    lines.append("")

    # Оценивается только настроенная ведущая метрика/цель.
    if config["status"] == "configured":
        key = config["metric"]
        label, unit = market.COUNTERS[key]
        current = totals.get(key, 0)
        goal = config["goal"]
        state = "цель недели достигнута" if current >= goal else "цель недели не достигнута"
        lines.append(f"**Ведущая метрика: {label.lower()}, {unit}.** За неделю пн→сегодня "
                     f"({period}) {current:g} из {goal:g} — {state}.")
        lines.append("")
    elif config["metric"]:
        lines.append("> Метрика распознана, но не используется как цель: "
                     "факты воронки ниже.")
        lines.append("")

    funnel = " → ".join(f"{market.COUNTERS[k][0].lower()} {totals.get(k, 0):g}"
                        for k in market.FUNNEL_COUNTERS)
    lines.append(f"**Воронка за неделю пн→сегодня ({period}):** {funnel}.")
    lines.append("")

    if all(totals.get(k, 0) == 0 for k in market.COLUMNS):
        lines.append("> Чисел нет. Отмечай повторения в Telegram — "
                     "`+контент 60`, `+аутрич 20`.")
        lines.append("")
    return lines


def build_status() -> str:
    vault = Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))
    m = _metrics(vault)
    judged = _ask_model(m)

    now = datetime.datetime.now()
    out = [f"# 📊 Статус — {now.strftime('%d.%m %H:%M')}", ""]
    out.append("> Куда что писать и где смотреть — [[Пульт]]. Эта страница пересобирается сама.")
    out.append("")

    if "error" in judged:
        out += [f"> ⚠️ Модель не ответила: {judged['error']}", ""]

    rocks = judged.get("rocks") or []
    out.append("## 🎯 Рок-цели цикла (1–3)")
    if rocks:
        out += [f"- [ ] {r}" for r in rocks[:3]]
    else:
        out.append("- [ ] не заданы — цели в `PARA/.../Goals` пустые шаблоны")
    out.append("")

    out.append("## 📊 Scorecard")
    out.append("| Метрика | Сейчас | Норма | |")
    out.append("|---|---|---|---|")
    week_label = f"{m['week_start'][5:]}–{m['week_end'][5:]}"
    unverified = (m["plan"]["unverified"] if m["plan"]["fixed"] else 0)
    if m["execution"] is None:
        out.append(f"| Выполнение плана недели ({week_label}) | не зафиксирован | — | ⚪ |")
    elif unverified:
        # Часть связей не подтверждена: это не доказанный провал, поэтому
        # называем число подтверждённым и не ставим красный.
        out.append(f"| Выполнение плана недели ({week_label}) | подтверждено "
                   f"{m['execution']}% | ≥{TARGET_EXECUTION}% | ⚪ |")
    else:
        out.append(f"| Выполнение плана недели ({week_label}) | {m['execution']}% | ≥{TARGET_EXECUTION}% | "
                   f"{_light(m['execution'], TARGET_EXECUTION, 60)} |")
    out.append(f"| Застряло >{AGING_DAYS} дней | {m['aging_count']} | ≤{TARGET_AGING} | "
               f"{_light(-m['aging_count'], -TARGET_AGING, -8)} |")
    out.append(f"| Просрочено | {m['overdue']} | 0 | {_light(-m['overdue'], 0, -2)} |")
    out.append(f"| Незакрытых всего | {m['open_count']} | ≤15 | {_light(-m['open_count'], -15, -25)} |")
    out.append(f"| Закрыто за неделю (пн–вс) | {m['closed_week']} | ≥5 | "
               f"{_light(m['closed_week'], 5, 2)} |")
    out.append(f"| Строк в инбоксе добавлено (git) | {m['captures']} | — | ⚪ |")
    deferred = someday.count(vault)
    no_trigger = someday.with_triggers(vault)
    out.append(f"| Отложено на потом | {deferred} | — | {'🟡' if no_trigger else '🟢'} |")
    out.append("")
    if m["execution"] is None:
        out.append(f"> 📌 {m['plan']['reason']} — процент не выдумываю. Зафиксировать план: "
                   f"`python3 bot/weekly_plan.py freeze \"задача 1\" \"задача 2\"` "
                   f"(подробнее — `bot/WEEKLY-PLAN.md`).")
        out.append("")
    elif unverified:
        missing = m["plan"]["missing"]
        ambiguous = m["plan"]["ambiguous"]
        parts = [f"не подтверждена связь у {unverified} обязательств (план без устойчивого ID)"]
        if missing:
            parts.append(f"не найдено карточек: {missing}")
        if ambiguous:
            parts.append(f"неоднозначных: {ambiguous}")
        out.append("> ⚠️ Выполнение плана: " + "; ".join(parts) +
                   ". Процент выше — только подтверждённая часть; неподтверждённые "
                   "обязательства остаются в знаменателе и не начисляют выполнение.")
        out.append("")
    out.append("> Строки в инбоксе — это изменения файла по данным git, а не число "
               "захватов: переформатирование тоже считается, а незакоммиченный захват — нет.")
    out.append("")

    # Применение советов: детерминированный обзор, без модели и без выводов о
    # причинности. Формулы scorecard и сохранённый план не затрагиваются.
    out += learning_cycle.render_section(vault)

    out += _market_section(vault)

    if no_trigger:
        out.append(f"> 🗄 В [[Отложено]] {len(no_trigger)} записей без триггера — "
                   "в недельном обзоре их надо либо удалить, либо дописать условие.")
        out.append("")


    today = judged.get("today") or []
    out.append("## ⏱ Сегодня — 3 блока (если → то)")
    if today:
        clock = ["10:00", "13:00", "16:00"]
        for i, item in enumerate(today[:3]):
            when = clock[i] if i < len(clock) else "—"
            minutes = item.get("minutes") or 25
            why = item.get("why") or ""
            out.append(f"- [ ] **если {when}** → {item.get('title')} · {minutes} мин"
                       + (f"\n      _{why}_" if why else ""))
        out.append("")
        out.append("- [ ] **буфер** → инбокс, почта, мелочи")
    else:
        out.append("- нет данных от модели")
    out.append("")

    cut = judged.get("cut") or []
    out.append("## 🚧 Решить: не делать")
    out += [f"- {c}" for c in cut] if cut else ["- нет данных"]
    out.append("")

    if m["aging"]:
        out.append(f"<details><summary>Застряло дольше {AGING_DAYS} дней ({m['aging_count']})</summary>")
        out.append("")
        out += [f"- {r['scheduled'][:10]} — {r['title']}" for r in m["aging"][:25]]
        out.append("")
        out.append("</details>")
        out.append("")

    out.append(f"---")
    out.append(f"_Обновляется автоматически (`bot/status.py`). Руками не править — перезапишется._")

    page = "\n".join(out) + "\n"
    (vault / STATUS_PATH).write_text(page, encoding="utf-8")
    return page


if __name__ == "__main__":
    print(build_status())
