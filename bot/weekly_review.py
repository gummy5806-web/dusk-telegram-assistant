#!/usr/bin/env python3
"""Детерминированный недельный обзор: факты, решения и ничего лишнего.

Что это. Обзор собирается **без модели и без сети**: он читает сохранённый план
недели, карточки применения 2Б, числа `Рынок.md` и сам список задач. Это ровно
арифметика и ссылки, поэтому один и тот же валт даёт один и тот же результат.

Неделя — календарная, понедельник–воскресенье, Asia/Almaty. Даты и момент
фиксации плана разбирает `weekly_plan` (его же парсер), формат карточек
применения — `learning_cycle`, конфигурация фокуса — `market`. Здесь ничего из
этого не переписывается заново.

Владение страницы. Файл состоит из двух зон:

* **сгенерированная** зона между `<!-- dusk-review:generated:start -->` и
  `<!-- dusk-review:generated:end -->` — ею владеет генератор;
* **пользовательская** зона между `<!-- dusk-review:user:start -->` и
  `<!-- dusk-review:user:end -->` — четыре маленьких решения пользователя.

Повторный запуск заменяет только сгенерированную зону. Пользовательская зона,
префикс и суффикс сохраняются побайтно, включая CRLF и повторяющиеся строки.
Неоднозначные или повреждённые маркеры — отказ без записи: лучше оставить
старый обзор, чем затереть чужой текст.

Чего здесь нет: LLM, Telegram, git, yt-dlp, внешних API, автосоздания задач и
миграции старых заметок. Ручной запуск:

    python3 bot/weekly_review.py --vault "/путь/к/валту"
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
from pathlib import Path

import frontmatter
import learning_cycle
import market
import personal_summary
import queue_store
import weekly_plan

# Пользовательская поверхность — корень валта. Старый путь `Учеба/обзор недели.md`
# поддерживается только как признак неоднозначности: два существующих обзора не
# объединяются молча, генерация останавливается.
REVIEW_PATH = "Недельный обзор.md"
LEGACY_REVIEW_PATH = "Учеба/обзор недели.md"

GEN_START = "<!-- dusk-review:generated:start -->"
GEN_END = "<!-- dusk-review:generated:end -->"
USER_START = "<!-- dusk-review:user:start -->"
USER_END = "<!-- dusk-review:user:end -->"

DECISION_FIELDS = ("Фокус", "Ведущая метрика и цель", "Эксперимент", "Стоп-делать")

_DECISION_LINE = re.compile(r"^-\s*\*\*(?P<name>[^*]+?)\*\*\s*:?\s*(?P<value>.*?)\s*$")

AGING_DAYS = 14

USER_TEMPLATE = (
    f"{USER_START}\n"
    "## 🧭 Решение на неделю\n\n"
    "- **Фокус:** —\n"
    "- **Ведущая метрика и цель:** —\n"
    "- **Эксперимент:** —\n"
    "- **Стоп-делать:** —\n"
    f"{USER_END}\n"
)


def review_path(vault: Path) -> Path:
    return Path(vault) / REVIEW_PATH


def legacy_review_path(vault: Path) -> Path:
    return Path(vault) / LEGACY_REVIEW_PATH


def _legacy_conflict(vault: Path) -> str | None:
    """Причина отказа, если рядом лежит старый обзор.

    Два существующих обзора не сливаются: пользовательская зона старого файла
    не должна потеряться. Если есть только старый файл, генерация тоже
    останавливается — новый обзор создавать молча нельзя.
    """
    legacy = legacy_review_path(vault)
    if not legacy.exists():
        return None
    if review_path(vault).exists():
        return (f"найдено два обзора: '{REVIEW_PATH}' и '{LEGACY_REVIEW_PATH}' — "
                "не объединяю")
    return (f"старый обзор '{LEGACY_REVIEW_PATH}' не перенесён в '{REVIEW_PATH}' — "
            "генерация остановлена, чтобы не потерять пользовательскую зону")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _reference(day: datetime.date | None) -> datetime.date:
    return day or weekly_plan.today_local()


def _period(day: datetime.date | None) -> tuple:
    """(понедельник, воскресенье, верхняя дата данных) для недели `day`."""
    reference = _reference(day)
    start, end = weekly_plan.week_bounds(reference)
    start_date = datetime.date.fromisoformat(start)
    end_date = datetime.date.fromisoformat(end)
    return start_date, end_date, min(end_date, reference)


def _g(text: str) -> str:
    return f"{float(text):g}"


# ── сборка сгенерированной зоны ─────────────────────────────────────────────

def _plan_lines(vault: Path, day: datetime.date | None) -> list:
    plan = weekly_plan.metric(vault, _reference(day))
    if not plan["fixed"]:
        return [f"- План недели: {plan['reason']}."]
    lines = [f"- Зафиксировано обязательств: {plan['total']}; "
             f"выполнено: {plan['done']} ({plan['percent']}%)."]
    for row in plan["rows"]:
        mark = weekly_plan.MARKS.get(row["state"], "•")
        line = f"  - {mark} {row['title']}"
        if row.get("note"):
            line += f" — {row['note']}"
        lines.append(line)
    return lines


def _application_lines(vault: Path, day: datetime.date | None) -> list:
    review = learning_cycle.application_rows(vault, today=_reference(day))
    counts = review["counts"]
    lines = [
        f"- Действие закрыто: {counts['done']}",
        f"- Наблюдение записано: {counts['observed']}",
        f"- Без данных: {counts['no_data']}",
    ]
    for row in review["rows"]:
        if row["done"]:
            state = "действие закрыто"
        elif row["active"]:
            state = "действие открыто"
        elif row["future"]:
            state = "дата закрытия в будущем — не выполнено"
        elif row["undated"]:
            state = "закрыто без даты — не выполнено"
        else:
            state = "вне периода"
        observed = row["observed_state"] if row["has_observation"] else learning_cycle.NO_DATA
        source = f"[[{row['source_link']}]]" if row["source_ok"] else "источник не найден"
        detail = f"наблюдение: {observed}"
        if row["has_conclusion"]:
            detail += f"; вывод: {row['conclusion']}"
        lines.append(f"  - [[{row['task_link']}]] — {state}; {detail}; "
                     f"совет: {source}")
    if not review["rows"]:
        lines.append("  - карточек применения за период нет.")
    return lines


def _market_lines(vault: Path, start: datetime.date, end: datetime.date,
                  config: dict) -> list:
    totals, days_count = market.range_totals(vault, start, end, strict=True)
    lines = [f"- Дней с данными за период: {days_count}."]
    for key in market.COLUMNS:
        label, unit = market.COUNTERS[key]
        lines.append(f"- {label}: {_g(totals.get(key, 0))} {unit}")
    if config.get("status") == "configured":
        key = config["metric"]
        label, unit = market.COUNTERS[key]
        current = totals.get(key, 0)
        goal = config["goal"]
        state = "цель достигнута" if current >= goal else "цель не достигнута"
        lines.append(f"- Ведущая метрика {label.lower()}: {_g(current)} из {_g(goal)} "
                     f"{unit} — {state}.")
    else:
        lines.append(f"- Фокус цикла не настроен: {config.get('reason', '')}.")
    return lines


def _stale_cards(vault: Path, day: datetime.date):
    """Застрявшие и просроченные открытые карточки — кандидаты на пересмотр.

    Контейнер тот же, что у принятого 2Б: `learning_cycle.tasks_root` отклоняет
    симлинк каталога задач наружу, а `learning_cycle._inside` — симлинк файла
    наружу. Проверка идёт **до** чтения, внешнее содержимое в вывод не попадает.
    Даты разбирает `weekly_plan.parse_task_date`, шапку — `frontmatter`.
    """
    aging_cut = day - datetime.timedelta(days=AGING_DAYS)
    stale, overdue = [], []
    root = learning_cycle.tasks_root(vault)
    if root is None or not root.is_dir():
        return stale, overdue
    for path in sorted(root.glob("*.md")):
        if not learning_cycle._inside(path, root):
            continue                      # симлинк карточки наружу: не читаем
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if frontmatter.get(text, "status") == "done":
            continue
        scheduled = weekly_plan.parse_task_date(frontmatter.get(text, "scheduled"))
        if scheduled is not None and scheduled < aging_cut:
            stale.append((path.stem, scheduled.isoformat()))
        due = weekly_plan.parse_task_date(frontmatter.get(text, "due"))
        if due is not None and due < day:
            overdue.append((path.stem, due.isoformat()))
    return stale, overdue


def _attention_lines(vault: Path, day: datetime.date) -> list:
    lines = []
    plan = weekly_plan.metric(vault, _reference(day))
    blocking = [row for row in plan["rows"]
                if row["state"] in (weekly_plan.MISSING, weekly_plan.AMBIGUOUS,
                                    weekly_plan.FUTURE, weekly_plan.UNVERIFIED)]
    for row in blocking:
        mark = weekly_plan.MARKS.get(row["state"], "•")
        lines.append(f"- {mark} обязательство «{row['title']}» — {row['note'] or row['state']}")
    stale, overdue = _stale_cards(vault, day)
    for stem, when in stale:
        lines.append(f"- ⏳ застряло с {when}: {stem}")
    for stem, when in overdue:
        lines.append(f"- ⚠️ просрочено с {when}: {stem}")
    if not lines:
        lines.append("- ничего не блокирует: план полон, застрявших и просроченных нет.")
    return lines


def _missing_lines(vault: Path, day: datetime.date, config: dict,
                   start: datetime.date, end: datetime.date) -> list:
    missing = []
    if config.get("status") != "configured":
        if config.get("missing"):
            missing.append("конфигурация цикла: " + ", ".join(config["missing"]))
        else:
            missing.append("конфигурация цикла: " + config.get("reason", "не задана"))
    plan = weekly_plan.metric(vault, _reference(day))
    if not plan["fixed"]:
        missing.append("план недели не зафиксирован")
    totals, days_count = market.range_totals(vault, start, end, strict=True)
    if days_count == 0:
        missing.append("в Рынок.md нет строк за период")
    applications = learning_cycle.application_rows(vault, today=_reference(day))
    if applications["counts"]["total"] == 0:
        missing.append("нет карточек применения 2Б за период")
    if not missing:
        return ["- данных достаточно."]
    return [f"- {item}" for item in missing]


def _personal_cycle_lines(vault: Path, day: datetime.date | None) -> list:
    """Одна компактная секция личного цикла: те же фактические агрегаты.

    Read-only и детерминировано: `personal_summary.collect` не пишет и не
    reconcile'ит. SQLite недоступна — затронутая часть честно помечается,
    остальные факты остаются.
    """
    reference = _reference(day)
    moment = datetime.datetime.combine(reference, datetime.time(12, 0))
    summary = personal_summary.collect(vault, store=None, now=moment,
                                       today=reference)
    return personal_summary.section_lines(summary)


def generated_text(vault: Path, day: datetime.date | None = None,
                   config: dict | None = None) -> str:
    """Сгенерированная зона без маркеров. Детерминирована для валта и дня.

    Owner-day фиксируется **один раз**: `day=None` больше не доходит до
    `_stale_cards()` (иначе вычитание timedelta из `None`), и переход через
    полночь не даёт разным секциям разные даты.
    """
    vault = Path(vault)
    reference = _reference(day)
    start, end, included_end = _period(reference)
    config = config or market.parse_config(vault)
    lines = [
        f"# 📅 Обзор недели {start.isoformat()} — {end.isoformat()}",
        "",
        "> Детерминированный обзор: только факты, без модели и без выводов о причинности.",
        "",
        "## ✅ Обязательства недели",
        "",
    ]
    lines += _plan_lines(vault, reference)
    lines += ["", "## 🧭 Личный цикл (факты)", ""]
    lines += _personal_cycle_lines(vault, reference)
    lines += ["", "## 🧪 Применение советов (2Б)", ""]
    lines += _application_lines(vault, reference)
    lines += ["", "## 💰 Рынок", ""]
    lines += _market_lines(vault, start, included_end, config)
    lines += ["", "## ⏳ Требует внимания", ""]
    lines += _attention_lines(vault, reference)
    lines += ["", "## ❓ Не хватает данных", ""]
    lines += _missing_lines(vault, reference, config, start, included_end)
    return "\n".join(lines).strip() + "\n"


def _new_page(text: str) -> str:
    return f"{GEN_START}\n{text.strip()}\n{GEN_END}\n\n{USER_TEMPLATE}"


def replace_generated(existing: str, text: str) -> str:
    """Меняет только сгенерированную зону, сохраняя всё остальное побайтно.

    Неоднозначные/повреждённые маркеры — `ValueError`, без записи.
    """
    for marker in (GEN_START, GEN_END, USER_START, USER_END):
        if existing.count(marker) != 1:
            raise ValueError(f"маркер {marker} встречается не ровно один раз")
    gen_start = existing.index(GEN_START)
    gen_end = existing.index(GEN_END)
    user_start = existing.index(USER_START)
    user_end = existing.index(USER_END)
    if not (gen_start < gen_end < user_start < user_end):
        raise ValueError("маркеры обзора расположены в неверном порядке")
    new_generated = f"{GEN_START}\n{text.strip()}\n{GEN_END}"
    return existing[:gen_start] + new_generated + existing[gen_end + len(GEN_END):]


def build_page(vault: Path, day: datetime.date | None = None,
               config: dict | None = None) -> str:
    """Полный текст страницы обзора без записи (первый запуск или повтор).

    Containment проверяется **до** legacy-проверки, генерации и любого
    `exists`/`read` существующего обзора: symlink корневого обзора наружу не
    должен отдавать внешние байты.
    """
    vault = Path(vault)
    path = review_path(vault)
    if not _inside(path, vault):
        raise ValueError(f"обзор вне валта: {REVIEW_PATH}")
    conflict = _legacy_conflict(vault)
    if conflict:
        raise ValueError(conflict)
    text = generated_text(vault, day, config=config)
    if path.exists():
        try:
            existing = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"обзор не читается: {exc}") from exc
        return replace_generated(existing, text)
    return _new_page(text)


def generate(vault: Path, day: datetime.date | None = None) -> tuple:
    """Собирает и атомарно записывает обзор. Возвращает `(успех, причина/путь)`.

    До записи проверяются: путь внутри валта, однозначная конфигурация цикла,
    корректная таблица рынка и однозначные маркеры владения. Любая проблема —
    отказ, старый обзор остаётся нетронутым.
    """
    vault = Path(vault)
    if not vault.is_dir():
        return False, "валт не найден"
    path = review_path(vault)
    if not _inside(path, vault):
        return False, "обзор вне валта"
    conflict = _legacy_conflict(vault)
    if conflict:
        return False, conflict
    config = market.parse_config(vault)
    if config.get("status") in ("ambiguous", "malformed"):
        return False, f"конфигурация цикла не готова: {config['reason']}"
    try:
        page = build_page(vault, day, config=config)
    except market.MarketError as exc:
        return False, f"таблица Рынка повреждена: {exc}"
    except (OSError, ValueError) as exc:
        return False, f"обзор не перезаписан: {exc}"
    try:
        queue_store.atomic_write(path, page)
    except OSError as exc:
        return False, f"запись обзора не удалась: {exc}"
    return True, str(path)


# ── пользовательская зона (только чтение для тестов/обзора) ─────────────────

def read_decisions(text: str) -> dict:
    """Читает четыре решения пользователя из пользовательской зоны.

    Если зоны нет или она повреждена — `ValueError`. Значения не нормализуются
    и не угадываются: `—` означает «не заполнено».
    """
    if text.count(USER_START) != 1 or text.count(USER_END) != 1:
        raise ValueError("пользовательская зона обзора неоднозначна")
    start = text.index(USER_START) + len(USER_START)
    end = text.index(USER_END)
    if end < start:
        raise ValueError("пользовательская зона обзора повреждена")
    body = text[start:end]
    values = {field: "" for field in DECISION_FIELDS}
    for line in body.splitlines():
        match = _DECISION_LINE.match(line.strip())
        if not match:
            continue
        name = match.group("name").strip().rstrip(":").strip()
        if name in values:
            values[name] = match.group("value").strip()
    return values


def _main() -> int:
    parser = argparse.ArgumentParser(description="Детерминированный недельный обзор")
    parser.add_argument("--vault", default=None)
    parser.add_argument("--day", default=None, help="ISO-дата внутри нужной недели")
    args = parser.parse_args()
    vault = Path(args.vault or os.getenv("VAULT_PATH",
                                         os.path.expanduser("~/dusk-vault")))
    day = datetime.date.fromisoformat(args.day) if args.day else None
    ok, message = generate(vault, day)
    print(("Обзор записан: " if ok else "Отказ: ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
