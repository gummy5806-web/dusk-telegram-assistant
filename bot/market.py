#!/usr/bin/env python3
"""Рынок — факты, фокус цикла и один явный ведущий показатель.

Что изменилось по сравнению с прошлой версией. Раньше `COUNTERS` хранил
одновременно и счётчик, и цель Rule of 100 (100 минут, 1 публикация, 100
обращений). Это противоречит аудиту: активность — это ФАКТ, а цель выбирает
владелец, и ровно одну. Поэтому:

* счётчики — это только сырые факты (`COUNTERS`: подпись и единица);
* цели не зашиты: пока в `Рынок.md` не задан фокус цикла, ведущая метрика и
  цель/неделя, `bump()` возвращает `target=None`, а не выдуманные 100/1/100;
* `bump()` продолжает записывать историю даже без цели — команды
  `+контент`, `+аутрич`, `+продажа` и т.д. работают как раньше.

Из Хормози ($100M Leads) берём только то, что подтверждает аудит: рост — это
«больше клиентов» или «дороже клиент», воронка считается в штуках
(лид → разговор → оффер → продажа → деньги), новое создаётся после «больше» и
«лучше». Сам Rule of 100 как универсальная норма активности не навязывается.

Файл `Рынок.md` читается глазами: одна строка = один день. Счётчик обновляется
одним сообщением в Telegram, чтобы это стоило ноль усилий.
"""

from __future__ import annotations

import datetime
import math
import os
import re
from pathlib import Path

import weekly_plan

MARKET_PATH = "Рынок.md"

# Сырые счётчики. Целей здесь нет — цель приходит из конфигурации цикла.
COUNTERS = {
    "контент":    ("Контент", "мин"),
    "публикация": ("Публикации", "шт"),
    "аутрич":     ("Аутрич", "шт"),
    "лид":        ("Лиды", "шт"),
    "разговор":   ("Разговоры", "шт"),
    "оффер":      ("Офферы", "шт"),
    "продажа":    ("Продажи", "шт"),
    "деньги":     ("Деньги", "₽"),
}

COLUMNS = list(COUNTERS)
EXPECTED_HEADER = ["дата"] + [COUNTERS[k][0] for k in COLUMNS]

# Исходы воронки можно показывать, но нельзя молча выбрать ведущей целью.
FUNNEL_COUNTERS = ("лид", "разговор", "оффер", "продажа", "деньги")

ALIASES = {
    "рил": "публикация", "рилс": "публикация", "видео": "публикация",
    "шортс": "публикация", "пост": "публикация", "выложил": "публикация",
    "минуты": "контент", "минут": "контент",
    "outreach": "аутрич", "написал": "аутрич", "дм": "аутрич", "dm": "аутрич",
    "лиды": "лид", "lead": "лид", "leads": "лид",
    "разговоры": "разговор", "созвон": "разговор", "звонок": "разговор",
    "офферы": "оффер", "предложение": "оффер",
    "продажи": "продажа", "оплата": "продажа", "sales": "продажа",
    "бабки": "деньги", "money": "деньги", "выручка": "деньги", "касса": "деньги",
}

HEADER = (
    "# 💰 Рынок\n\n"
    "> Числа важнее дел. Один тап в Telegram: `+контент 60`, `+рил`, `+аутрич 20`, `+продажа 5000`.\n\n"
    "## Оффер и канал\n\n"
    "- **Оффер:** не задан\n"
    "- **Кому:** не задано\n"
    "- **Цена:** не задана\n"
    "- **Канал:** не выбран (из четырёх: тёплый аутрич / контент / холодный аутрич / платная реклама)\n\n"
    "## Дни\n\n"
    "| дата | " + " | ".join(COUNTERS[k][0] for k in COUNTERS) + " |\n"
    "|" + "---|" * (len(COUNTERS) + 1) + "\n"
)

# ── конфигурация цикла из человекочитаемой секции ──────────────────────────

CONFIG_SECTION = "## Оффер и канал"
CONFIG_FIELDS = ("Оффер", "Кому", "Цена", "Канал",
                 "Фокус цикла", "Ведущая метрика", "Цель/неделя")
REQUIRED_FIELDS = ("Оффер", "Кому", "Цена", "Канал")
CYCLE_FIELDS = ("Фокус цикла", "Ведущая метрика", "Цель/неделя")

_FIELD_LINE = re.compile(r"^-\s*\*\*(?P<name>[^*]+?)\*\*\s*:?\s*(?P<value>.*?)\s*$")

_PLACEHOLDER_PREFIXES = (
    "не задан", "не задана", "не задано",
    "не выбран", "не выбрана", "не выбрано",
    "нет данных", "неизвестн",
)

CHANNEL_ALIASES = {
    "контент": "content",
    "тёплый аутрич": "warm_outreach",
    "теплый аутрич": "warm_outreach",
    "warm outreach": "warm_outreach",
    "холодный аутрич": "cold_outreach",
    "cold outreach": "cold_outreach",
    "платная реклама": "paid_ads",
    "paid ads": "paid_ads",
    "paid advertising": "paid_ads",
}
CHANNEL_LABELS = {
    "content": "контент",
    "warm_outreach": "тёплый аутрич",
    "cold_outreach": "холодный аутрич",
    "paid_ads": "платная реклама",
}

METRIC_ALIASES = {
    "контент": "контент", "минуты": "контент", "минут": "контент",
    "публикация": "публикация", "публикации": "публикация",
    "аутрич": "аутрич", "outreach": "аутрич",
    "лид": "лид", "лиды": "лид",
    "разговор": "разговор", "разговоры": "разговор",
    "оффер": "оффер", "офферы": "оффер",
    "продажа": "продажа", "продажи": "продажа",
    "деньги": "деньги",
}

# Какие ведущие метрики совместимы с каналом. Платная реклама универсальной
# нормы активности не получает: для неё ведущей activity-метрики нет.
SUPPORTED_METRICS = {
    "content": ("контент", "публикация"),
    "warm_outreach": ("аутрич",),
    "cold_outreach": ("аутрич",),
    "paid_ads": (),
}


class MarketError(ValueError):
    """Данные рынка неполны/повреждены: расчёт или запись отменяются."""


def _path(vault: Path | None = None) -> Path:
    root = vault or Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))
    return root / MARKET_PATH


def _ensure(path: Path) -> str:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER, encoding="utf-8")
    return path.read_text(encoding="utf-8")


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _row_values(line: str):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DAY_SECTION = "## Дни"
_SEPARATOR = re.compile(r"^:?-{2,}:?$")


def _is_placeholder(value: str) -> bool:
    raw = (value or "").strip().strip("*").strip()
    if not raw:
        return True
    low = raw.lower()
    return any(low == p or low.startswith(p) for p in _PLACEHOLDER_PREFIXES)


def _config_sections(text: str) -> tuple:
    """Строки секции конфигурации и число её заголовков.

    Берётся секция `## Оффер и канал` до следующего заголовка `## `. Ноль
    заголовков — конфигурации нет; два и больше — неоднозначность, которую нельзя
    молча свести к первой секции.
    """
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines)
              if line.strip() == CONFIG_SECTION]
    if not starts:
        return None, 0
    body = []
    for line in lines[starts[0] + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return body, len(starts)


def parse_config(vault: Path | None = None) -> dict:
    """Разбирает конфигурацию цикла строго и без догадок.

    Возвращает словарь со `status` одним из:
    `unconfigured` (нет обязательных полей), `ambiguous` (дублирующиеся поля),
    `malformed` (цель не число/<=0), `unsupported` (канал или метрика не
    поддерживаются/несовместимы), `configured` (всё задано явно и корректно).

    Пропуск, плейсхолдер, дубль или повреждённая строка никогда не угадываются:
    неизвестное так и называется, а не превращается в выдуманную цель.
    """
    result = {
        "status": "unconfigured",
        "reason": "нет файла Рынок.md",
        "fields": {field: "" for field in CONFIG_FIELDS},
        "missing": list(CONFIG_FIELDS),
        "duplicates": [],
        "unsupported": [],
        "channel": "",
        "channel_label": "",
        "metric": "",
        "goal": None,
        "focus": "",
    }
    path = _path(vault)
    if not path.exists():
        return result
    text = _read(path)
    if not text:
        result["reason"] = "Рынок.md не читается"
        return result

    section, sections_count = _config_sections(text)
    if sections_count == 0:
        result["reason"] = f"нет секции {CONFIG_SECTION!r}"
        return result
    if sections_count > 1:
        result["status"] = "ambiguous"
        result["reason"] = f"секция {CONFIG_SECTION!r} встречается несколько раз"
        return result

    fields: dict = {}
    duplicates = []
    for line in section:
        match = _FIELD_LINE.match(line.strip())
        if not match:
            continue
        name = match.group("name").strip().rstrip(":").strip()
        if name not in CONFIG_FIELDS:
            continue
        if name in fields:
            duplicates.append(name)
        else:
            fields[name] = match.group("value").strip()
    result["fields"] = {field: fields.get(field, "") for field in CONFIG_FIELDS}

    if duplicates:
        result["status"] = "ambiguous"
        result["duplicates"] = sorted(set(duplicates))
        result["reason"] = "дублирующиеся поля: " + ", ".join(result["duplicates"])
        return result

    missing = [field for field in REQUIRED_FIELDS if _is_placeholder(fields.get(field, ""))]
    if missing:
        result["missing"] = missing
        result["reason"] = "не заданы: " + ", ".join(missing)
        return result

    channel_raw = fields["Канал"].strip()
    channel = CHANNEL_ALIASES.get(channel_raw.lower())
    if not channel:
        result["status"] = "unsupported"
        result["unsupported"] = ["Канал"]
        result["reason"] = f"канал не поддерживается: {channel_raw!r}"
        return result
    result["channel"] = channel
    result["channel_label"] = CHANNEL_LABELS[channel]

    missing_cycle = [field for field in CYCLE_FIELDS
                     if _is_placeholder(fields.get(field, ""))]
    if missing_cycle:
        result["missing"] = missing_cycle
        result["reason"] = "не задан фокус цикла: " + ", ".join(missing_cycle)
        return result

    result["focus"] = fields["Фокус цикла"].strip()
    metric_raw = fields["Ведущая метрика"].strip()
    metric = METRIC_ALIASES.get(metric_raw.lower())
    if not metric:
        result["status"] = "unsupported"
        result["unsupported"] = ["Ведущая метрика"]
        result["reason"] = f"метрика не поддерживается: {metric_raw!r}"
        return result
    result["metric"] = metric

    if metric in FUNNEL_COUNTERS:
        result["status"] = "unsupported"
        result["unsupported"] = ["Ведущая метрика"]
        result["reason"] = ("исход воронки не выбирается ведущей метрикой: "
                            f"{metric_raw!r}")
        return result
    if metric not in SUPPORTED_METRICS.get(channel, ()):
        result["status"] = "unsupported"
        result["unsupported"] = ["Ведущая метрика"]
        result["reason"] = (f"метрика {metric_raw!r} несовместима с каналом "
                            f"{result['channel_label']}")
        return result

    goal_raw = fields["Цель/неделя"].strip().replace(" ", "").replace(",", ".")
    try:
        goal = float(goal_raw)
    except ValueError:
        result["status"] = "malformed"
        result["missing"] = ["Цель/неделя"]
        result["reason"] = f"цель/неделя не число: {fields['Цель/неделя']!r}"
        return result
    if not math.isfinite(goal) or goal <= 0:
        result["status"] = "malformed"
        result["missing"] = ["Цель/неделя"]
        result["reason"] = ("цель/неделя должна быть конечным положительным "
                            f"числом: {fields['Цель/неделя']!r}")
        return result

    result["goal"] = goal
    result["status"] = "configured"
    result["reason"] = ""
    result["missing"] = []
    return result


def configured_target(key: str, config: dict | None = None,
                      vault: Path | None = None):
    """Цель для конкретного счётчика или None, если он не ведущая метрика.

    Это единственное место, где число цели берётся из конфигурации. Никаких
    зашитых 100/1/100 здесь нет.
    """
    config = config or parse_config(vault)
    if config.get("status") != "configured":
        return None
    if config.get("metric") != key:
        return None
    goal = config.get("goal")
    return float(goal) if goal is not None else None


def owner_day(day=None) -> datetime.date:
    """Дата владельца (`Asia/Almaty`) из аргумента, до любой записи.

    `None` — сегодня в Алматы, а не неявная зона хоста. Строка обязана быть
    корректной ISO-датой: опечатка в явной дате не должна ничего записывать.
    """
    if day is None:
        return weekly_plan.today_local()
    if isinstance(day, datetime.datetime):
        return weekly_plan.to_almaty(day).date()
    if isinstance(day, datetime.date):
        return day
    if isinstance(day, str):
        try:
            return datetime.date.fromisoformat(day.strip())
        except ValueError as exc:
            raise ValueError(f"дата не корректный ISO-день: {day!r}") from exc
    raise ValueError("дата должна быть ISO-строкой или date")


def bump(key: str, amount: float = 1, day=None) -> tuple:
    """Прибавляет к сегодняшнему счётчику (день владельца, `Asia/Almaty`).

    Возвращает `(подпись, стало за сегодня, недельная цель, единица)`. Цель —
    `float` только если этот счётчик объявлен ведущей метрикой цикла; иначе
    `None`. Это НЕ дневная норма: сравнивать сегодняшнее число с недельной целью
    нельзя. Точный недельный прогресс считает `week_progress`. Запись истории от
    наличия цели не зависит.
    """
    key = ALIASES.get(key.lower().strip(), key.lower().strip())
    if key not in COUNTERS:
        raise KeyError(key)
    try:
        amount = float(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"amount не число: {amount!r}") from exc
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError(f"amount должен быть конечным положительным: {amount!r}")

    owner = owner_day(day)               # до `_ensure`: плохая дата ничего не пишет
    day_iso = owner.isoformat()
    path = _path()
    text = _ensure(path)
    lines = text.splitlines()

    names = COLUMNS
    col = names.index(key) + 1

    section_start, section_end = _day_section(lines)
    separator, rows = _market_table(lines, section_start, section_end)
    if separator is None:
        raise MarketError(f"таблица рынка не найдена в секции {DAY_SECTION!r}")
    _strict_rows(rows)                   # fail before любой вставки/update/записи

    row_idx = None
    for i, cells in rows:
        if cells and cells[0] == day_iso:
            row_idx = i
            break
    if row_idx is None:
        new_row = "| " + day_iso + " | " + " | ".join("0" for _ in names) + " |"
        lines.insert(separator + 1, new_row)
        row_idx = separator + 1

    cells = _row_values(lines[row_idx])
    try:
        current = float(cells[col])
    except ValueError as exc:
        raise ValueError(
            f"ячейка {day_iso} ({key}) не число: {cells[col]!r}") from exc
    if not math.isfinite(current):
        raise ValueError(f"ячейка {day_iso} ({key}) не конечна: {cells[col]!r}")
    result = current + amount
    if not math.isfinite(result):
        raise ValueError("результат сложения не конечен")
    cells[col] = _fmt(result)
    lines[row_idx] = "| " + " | ".join(cells) + " |"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    label, unit = COUNTERS[key]
    return label, result, configured_target(key), unit


def week_progress(key: str, day=None, vault: Path | None = None) -> dict:
    """Точный прогресс текущей календарной недели пн→сегодня (Asia/Almaty).

    Строки до понедельника и после дня владельца в сумму не входят. Недельная
    цель сравнивается только здесь — не с дневным числом.
    """
    owner = owner_day(day)
    start_iso, end_iso = weekly_plan.week_bounds(owner)
    start = datetime.date.fromisoformat(start_iso)
    totals, days_count = range_totals(vault, start, owner, strict=True)
    label, unit = COUNTERS[key]
    return {
        "key": key, "label": label, "unit": unit,
        "start": start_iso, "end": end_iso,
        "total": totals.get(key, 0.0), "target": configured_target(key, vault=vault),
        "days": days_count,
    }


def parse_command(text: str):
    """Разбирает `+контент 60`, `+рил`, `+аутрич 20`, `+продажа 5000`."""
    m = re.match(r"^\+\s*([A-Za-zА-Яа-яёЁ]+)\s*([\d\s.,]*)$", (text or "").strip())
    if not m:
        return None
    key = m.group(1).lower()
    key = ALIASES.get(key, key)
    if key not in COUNTERS:
        return None
    raw = (m.group(2) or "").replace(" ", "").replace(",", ".")
    if not raw:
        return key, 1.0
    try:
        amount = float(raw)
    except ValueError:
        return None
    if not math.isfinite(amount) or amount <= 0:
        return None                       # NaN/inf/переполнение/≤0 — не команда
    return key, amount


def _day_section(lines):
    """Индексы тела секции `## Дни` (без заголовка) или `(None, None)`.

    Данные рынка читаются только отсюда: произвольные markdown-таблицы вне этой
    секции рынком не считаются. Секция должна быть ровно одна.
    """
    starts = [i for i, line in enumerate(lines) if line.strip() == DAY_SECTION]
    if not starts:
        return None, None
    if len(starts) > 1:
        raise MarketError(f"секция {DAY_SECTION!r} встречается несколько раз")
    start = starts[0] + 1
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return start, end


def _market_table(lines, start, end):
    """`(индекс разделителя, [(индекс, ячейки)])` таблицы рынка.

    Шапка обязана точно совпадать со схемой `['дата', *подписи счётчиков]`
    (та же ширина, тот же порядок, без дублей/пропусков/лишних колонок), а
    markdown-разделитель — ту же ширину и форму. Всё остальное — данные.
    Отсутствие таблицы — `(None, [])`; несовпадение схемы — `MarketError`.
    """
    if start is None:
        return None, []
    indices = [i for i in range(start, end) if lines[i].strip().startswith("|")]
    if not indices:
        return None, []
    header = _row_values(lines[indices[0]])
    if header != EXPECTED_HEADER:
        raise MarketError(
            f"шапка таблицы рынка не совпадает со схемой: {header!r} "
            f"(ожидалось {EXPECTED_HEADER!r})")
    if len(indices) < 2:
        raise MarketError("нет markdown-разделителя таблицы рынка")
    separator = _row_values(lines[indices[1]])
    if len(separator) != len(EXPECTED_HEADER) or not all(
            _SEPARATOR.match(cell) for cell in separator):
        raise MarketError(
            "markdown-разделитель таблицы рынка не совпадает по ширине/форме")
    rows = [(i, _row_values(lines[i])) for i in indices[2:] if lines[i].strip()]
    return indices[1], rows


def _validated_values(names, cells):
    """Строгая проверка одной строки данных → `{счётчик: float}`.

    Требуется точная ширина, строгий реальный `YYYY-MM-DD` и конечные числа.
    """
    if len(cells) != len(names) + 1:
        raise MarketError(
            f"строка {cells[0]} содержит {len(cells) - 1} колонок, "
            f"ожидалось {len(names)}")
    if not _DATE_RE.fullmatch(cells[0]):
        raise MarketError(f"дата не в формате YYYY-MM-DD: {cells[0]!r}")
    try:
        datetime.date.fromisoformat(cells[0])
    except ValueError as exc:
        raise MarketError(f"невозможная календарная дата: {cells[0]!r}") from exc
    values = {}
    for i, k in enumerate(names, start=1):
        try:
            value = float(cells[i])
        except ValueError as exc:
            raise MarketError(
                f"нечисловое значение {cells[i]!r} в {cells[0]} ({k})") from exc
        if not math.isfinite(value):
            raise MarketError(
                f"неконечное значение {cells[i]!r} в {cells[0]} ({k})")
        values[k] = value
    return values


def _strict_rows(rows):
    """Строго валидирует все строки таблицы рынка → `[(дата, cells, values)]`.

    Единый путь проверки и для strict totals, и для `bump`: точная ширина,
    строгий реальный ISO-день, уникальные даты, все числа конечны.
    """
    names = COLUMNS
    seen = set()
    out = []
    for _idx, cells in rows:
        values = _validated_values(names, cells)
        if cells[0] in seen:
            raise MarketError(f"дублирующаяся строка даты: {cells[0]}")
        seen.add(cells[0])
        out.append((cells[0], cells, values))
    return out


def _sum_rows(text: str, start: str, end: str, *, strict: bool) -> tuple:
    """(суммы, число дней) по таблице рынка секции `## Дни` включительно.

    Читается именно таблица рынка: `## Дни` → известная шапка → разделитель →
    строки данных. Строгая валидация — общая (`_strict_rows`): точная ширина,
    строгий реальный ISO-день, уникальные даты, конечные числа; иначе —
    `MarketError`. Нестрогий режим игнорирует непонятные строки. Таблицы вне
    `## Дни` рынком не считаются.
    """
    names = COLUMNS
    totals = {k: 0.0 for k in names}
    per_day = {}
    try:
        lines = text.splitlines()
        section_start, section_end = _day_section(lines)
        _separator, rows = _market_table(lines, section_start, section_end)
        if strict:
            validated = _strict_rows(rows)
    except MarketError:
        if strict:
            raise
        return totals, 0
    if strict:
        for date, _cells, values in validated:
            if not (start <= date <= end):
                continue
            for k, value in values.items():
                totals[k] += value
            per_day[date] = True
        return totals, len(per_day)
    for _idx, cells in rows:
        if len(cells) != len(names) + 1 or not _DATE_RE.fullmatch(cells[0]):
            continue
        if not (start <= cells[0] <= end):
            continue
        for i, k in enumerate(names, start=1):
            try:
                value = float(cells[i])
            except ValueError:
                value = 0.0
            totals[k] += value
        per_day[cells[0]] = True
    return totals, len(per_day)


def week_totals(vault: Path | None = None, days: int = 7, *, strict: bool = False,
                create: bool = True):
    """Суммы за последние N дней + число дней с данными."""
    path = _path(vault)
    text = _ensure(path) if create else _read(path)
    since = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
    today = datetime.date.today().isoformat()
    return _sum_rows(text, since, today, strict=strict)


def range_totals(vault: Path | None = None, start: datetime.date | None = None,
                 end: datetime.date | None = None, *, strict: bool = False,
                 create: bool = False):
    """Суммы за явный диапазон дат включительно (пн–вс для обзора недели)."""
    if start is None or end is None:
        raise ValueError("нужны обе границы диапазона")
    path = _path(vault)
    text = _ensure(path) if create else _read(path)
    return _sum_rows(text, start.isoformat(), end.isoformat(), strict=strict)
