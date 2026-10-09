#!/usr/bin/env python3
"""Недельный план: сохранённые обязательства конкретных карточек.

Проблема, которую это решает. Раньше «выполнение плана недели» считалось так:
взять все карточки с `scheduled >= сегодня − 7 дней` и поделить закрытые на все.
Верхней границы не было, поэтому будущая карточка (хоть 2030 год) попадала в
прошлую неделю, перенос даты менял показатель, а автоматически собранный сегодня
набор назывался «планом», хотя пользователь его не согласовывал.

Здесь план — это сохранённый файл (`Учеба/план недели.md`): конкретная неделя,
конкретные обязательства, момент фиксации и **устойчивая идентичность карточки**
(`duskTaskId`, UUID в шапке), зафиксированная в момент freeze. Поэтому:

* перенос даты карточки не меняет процент;
* переименование файла и заголовка обязательство не теряет (карточка узнаётся по ID);
* новая одноимённая карточка **не исполняет** старое обязательство;
* неоднозначность и пропажи показываются явно, а не молча улучшают процент.

Нечитаемый или неполный план не считается зафиксированным: метрика говорит
«не зафиксирован» с причиной и не показывает ложный процент.

Ручная фиксация (единственный способ создать план — осознанное действие):

    python3 bot/weekly_plan.py freeze "разобраться с парсером" "написать парсер новостей"
    python3 bot/weekly_plan.py show

Read-only расчёт (status.py) этот файл только читает и никогда не создаёт.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

import frontmatter
import queue_store

TZ_NAME = "Asia/Almaty"
PLAN_PATH = "Учеба/план недели.md"
PLAN_TYPE = "weekly_plan"
TASKS_DIR = "TaskNotes/Tasks"

# Состояния обязательства в отчёте.
DONE = "done"
OPEN = "open"
MISSING = "missing"
AMBIGUOUS = "ambiguous"
UNVERIFIED = "unverified"
FUTURE = "future"


def _tzinfo():
    """Зона Алматы. Без tzdata — фиксированное +05:00 (переходов там нет)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(TZ_NAME)
    except Exception:  # pragma: no cover - зависит от системной базы зон
        return datetime.timezone(datetime.timedelta(hours=5), TZ_NAME)


def to_almaty(moment: datetime.datetime | None = None) -> datetime.datetime:
    """Единственная точка приведения времени к зоне Алматы.

    Семантика входа:
    * `None` — текущий момент (UTC → Алматы);
    * aware datetime — приводится к Алматы (часовой пояс учитывается);
    * naive datetime — считается **местным временем Алматы** (его так
      конструируют вызовы на Mac: `datetime.now()` в зоне владельца).
      Если момент пришёл из другого пояса, передавай aware-значение.
    """
    if moment is None:
        return datetime.datetime.now(datetime.timezone.utc).astimezone(_tzinfo())
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_tzinfo())
    return moment.astimezone(_tzinfo())


def now_local(moment: datetime.datetime | None = None) -> datetime.datetime:
    return to_almaty(moment)


def today_local(moment: datetime.datetime | None = None) -> datetime.date:
    return to_almaty(moment).date()


_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_START = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def parse_task_date(value: str):
    """Единственная строгая точка разбора даты задачи.

    Принимает **полную** ISO-дату (`2026-10-06`) или **полный** ISO-timestamp
    (`2026-10-04T20:00:00+00:00`). Timestamp приводится к Asia/Almaty, потому что
    день закрытия — день владельца, а сервер может стоять в UTC.

    Отвергает всё остальное: `2026-10-06invalid`, `2026-13-01`, `2026-10-06T99:00`,
    пустую строку, а также значения нестрокового типа (`123`, `true`, список,
    объект). Приведение через `str()` недопустимо: оно превращало бы мусор в
    «дату». Неизвестную дату нельзя подменять сегодняшней.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if _DATE_ONLY.fullmatch(raw):
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            return None
    if not _TIME_START.match(raw):
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        moment = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return to_almaty(moment).date()


def week_bounds(day: datetime.date | None = None) -> tuple[str, str]:
    """Понедельник и воскресенье недели, к которой относится день.

    Обе границы обязательны и включительны: неделя — закрытый интервал.
    """
    day = day or today_local()
    start = day - datetime.timedelta(days=day.weekday())
    end = start + datetime.timedelta(days=6)
    return start.isoformat(), end.isoformat()


def plan_path(vault: Path) -> Path:
    return Path(vault) / PLAN_PATH


# ── чтение и проверка плана ────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> dict:
    """Разбор шапки без внешних зависимостей: `ключ: значение` и список."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    data: dict = {}
    current_list = None
    for raw in text[4:end + 1].splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_list is not None:
            item = line[4:].strip()
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                item = item.strip('"').strip("'")
            data.setdefault(current_list, []).append(item)
            continue
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            if value == "":
                data[key] = []
                current_list = key
                continue
            current_list = None
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                data[key] = value.strip('"').strip("'")
    return data


def _text_field(data: dict, name: str) -> str:
    """Строковое поле из шапки плана или пустая строка.

    Тип проверяется явно: `week_start: 123`, список или объект — это не дата,
    и план должен быть отвергнут с причиной, а не уронить расчёт исключением.
    """
    value = data.get(name)
    return value if isinstance(value, str) else ""


def load_plan(vault: Path) -> tuple:
    """Читает и проверяет план. Возвращает (план | None, причина отказа).

    Проверяется всё, от чего зависит честность процента: тип файла, зона,
    реальные границы недели (пн–вс), момент фиксации с часовым поясом и то,
    что `tasks` — непустой список корректных записей. Молчаливое принятие
    «почти правильного» плана здесь дороже отказа: на его основе считается
    отчётность.
    """
    path = plan_path(vault)
    if not path.exists():
        return None, "план не зафиксирован"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, "план не читается"
    data = _parse_frontmatter(text)
    if not data:
        return None, "у плана нет корректной шапки"

    if _text_field(data, "type").strip() != PLAN_TYPE:
        return None, f"у плана неверный type (нужен {PLAN_TYPE})"

    timezone = _text_field(data, "timezone").strip()
    if timezone != TZ_NAME:
        return None, f"нужна зона {TZ_NAME}, а в плане «{timezone or 'пусто'}»"

    start = parse_task_date(_text_field(data, "week_start"))
    end = parse_task_date(_text_field(data, "week_end"))
    if start is None or end is None:
        return None, "границы недели не даты"
    if start.weekday() != 0 or end != start + datetime.timedelta(days=6):
        return None, "границы недели не понедельник–воскресенье"

    frozen_raw = _text_field(data, "frozen").strip()
    try:
        frozen = datetime.datetime.fromisoformat(frozen_raw)
    except ValueError:
        return None, "момент фиксации не читается"
    if frozen.tzinfo is None:
        return None, "у момента фиксации нет часового пояса"

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return None, "поле tasks должно быть непустым списком"

    entries = []
    for item in raw_tasks:
        if isinstance(item, str):
            title = item.strip()
            if not title:
                return None, "в плане есть запись без названия"
            entries.append({"title": title, "stem": "", "created": "", "id": ""})
        elif isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            if not title:
                return None, "в плане есть запись без названия"
            entries.append({"title": title,
                            "stem": str(item.get("stem") or "").strip(),
                            "created": str(item.get("created") or "").strip(),
                            "id": str(item.get("id") or "").strip()})
        else:
            return None, "запись плана не строка и не объект"
    if not entries:
        return None, "поле tasks должно быть непустым списком"

    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "timezone": timezone,
        "frozen": frozen_raw,
        "tasks": entries,
        "path": path,
    }, ""


def save_plan(vault: Path, tasks, day: datetime.date | None = None,
              moment: datetime.datetime | None = None) -> Path:
    """Фиксирует план на неделю. Только вручную.

    В момент фиксации каждая запись привязывается к конкретной карточке:
    выбранной карточке записывается `duskTaskId` (UUID) — устойчивая
    идентичность, по которой потом отличают переименование от подмены.
    Имя файла и `created` для этого не годятся: их легко повторить новой карточкой.
    """
    requested = [str(t).strip() for t in tasks if str(t).strip()]
    if not requested:
        raise ValueError("пустой план: перечисли обязательства")

    entries = []
    warnings = []
    for raw in requested:
        found, warning = _find_card_for_freeze(vault, raw)
        if found is None:
            # Карточка не найдена: обязательство остаётся, но связь не подтверждена.
            entries.append({"title": raw, "stem": "", "created": "", "id": ""})
            warnings.append(warning or f"«{raw}»: карточка не найдена")
            continue
        try:
            task_id = _ensure_card_id(found)
        except (ValueError, OSError) as exc:
            # Шапку записать нельзя: план всё равно фиксируем, но без ID —
            # выполнение по такой записи засчитано не будет.
            entries.append({"title": found["title"], "stem": found["stem"],
                            "created": found["created"], "id": ""})
            warnings.append(f"«{found['title']}»: {exc}")
            continue
        entries.append({"title": found["title"], "stem": found["stem"],
                        "created": found["created"], "id": task_id})

    start, end = week_bounds(day)
    frozen = to_almaty(moment).isoformat(timespec="seconds")
    lines = [
        "---",
        f"type: {PLAN_TYPE}",
        f"timezone: {TZ_NAME}",
        f"week_start: {start}",
        f"week_end: {end}",
        f"frozen: {frozen}",
        "tasks:",
    ]
    lines += [f"  - {json.dumps(e, ensure_ascii=False)}" for e in entries]
    lines += [
        "---",
        "",
        f"# План недели {start} — {end}",
        "",
        "> Зафиксирован вручную. Процент выполнения считается по этому списку,",
        "> поэтому перенос даты карточки его не меняет.",
        "",
    ]
    path = plan_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, "\n".join(lines))
    save_plan.last_warnings = warnings
    return path


def _write_atomic(path: Path, text: str) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
        delete=False,
    )
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# ── сопоставление обязательств с карточками ────────────────────────────────

def _norm(value: str) -> str:
    return "".join(c for c in (value or "").lower() if c.isalnum())


def _task_field(text: str, name: str) -> str:
    """Поле карточки только из первой валидной шапки (тело — не метаданные)."""
    return frontmatter.get(text, name)


def _all_cards(vault: Path) -> list:
    """Все карточки — по одной записи на файл.

    Именно список, а не словарь по названию: две разные карточки могут иметь
    одинаковый `title`, и раньше вторая молча вытесняла первую.
    """
    cards = []
    tasks_dir = Path(vault) / TASKS_DIR
    if not tasks_dir.is_dir():
        return cards
    for f in sorted(tasks_dir.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        cards.append({
            "title": _task_field(text, "title") or f.stem,
            "status": _task_field(text, "status"),
            "stem": f.stem,
            "created": _task_field(text, "created") or _task_field(text, "dateCreated"),
            "completed": _task_field(text, "completedDate"),
            "dusk_id": _task_field(text, "duskTaskId"),
            "path": f,
        })
    return cards


def tasks_index(vault: Path) -> dict:
    """Индекс для поиска по имени файла и однозначному заголовку.

    Неоднозначные заголовки в индекс не попадают: лучше не найти карточку и
    попросить уточнить, чем привязать обязательство к случайной из двух.
    """
    index: dict = {}
    titles: dict = {}
    for card in _all_cards(vault):
        index.setdefault(_norm(card["stem"]), card)
        titles.setdefault(_norm(card["title"]), []).append(card)
    for key, group in titles.items():
        if len(group) == 1 and key not in index:
            index[key] = group[0]
    return index


def _ensure_card_id(card: dict) -> str:
    """Возвращает `duskTaskId` карточки, создавая его при первом ручном freeze.

    ID пишется только выбранной карточке, только в шапку, атомарно и под
    локальной блокировкой; тело сохраняется дословно. Существующий ID не
    меняется, поэтому повторная фиксация ничего не сдвигает. Массовой миграции
    нет: сюда попадают только карточки, которые пользователь назвал сам.
    """
    existing = (card.get("dusk_id") or "").strip()
    if existing:
        return existing
    path = card["path"]
    new_id = str(uuid.uuid4())
    with queue_store.path_lock(path):
        text = path.read_text(encoding="utf-8")
        current = frontmatter.get(text, "duskTaskId")
        if current:
            return current
        updated, ok = frontmatter.set_field(text, "duskTaskId", new_id)
        if not ok:
            raise ValueError(f"нет корректной шапки, ID не записан: {path.name}")
        queue_store.atomic_write(path, updated)
    return new_id


def _find_card_for_freeze(vault: Path, raw: str):
    """Карточка для записи в план. Возвращает (карточка | None, предупреждение)."""
    key = _norm(raw)
    cards = _all_cards(vault)
    for card in cards:
        if _norm(card["stem"]) == key:
            return card, ""
    matches = [c for c in cards if _norm(c["title"]) == key]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, "несколько карточек с таким названием — назови файл точнее"
    return None, ""


def _state_for(entry: dict, today: datetime.date) -> str:
    """Состояние карточки: done / open / future.

    Закрытие датой из будущего выполнением не считается: это либо ошибка в
    данных, либо ручная правка, и засчитывать её в отчёт нельзя.
    """
    if entry["status"] != "done":
        return OPEN
    completed = (entry.get("completed") or "").strip()
    if completed:
        day = parse_task_date(completed)
        if day is not None and day > today:
            return FUTURE
    return DONE


def _match_card(by_id: dict, entry: dict):
    """Находит карточку обязательства по устойчивому ID.

    Возвращает `(карточка | None, вид связи, причина отказа)`.

    Идентичность — только `duskTaskId`, записанный в карточку при ручном
    `freeze`. Имя файла и `created` идентичностью не являются: карточку можно
    переименовать, а на её место положить другую с тем же именем и той же
    минутой создания. Поэтому:

    * ID найден ровно у одной карточки — это она (переименование не мешает);
    * ID найден у нескольких — неоднозначность, случайную не выбираем;
    * ID не найден — обязательство остаётся в знаменателе, выполнение не засчитываем;
    * ID в плане нет (старый строковый план) — связь не подтверждена, и новая
      одноимённая карточка это обязательство **не** исполняет.
    """
    task_id = (entry.get("id") or "").strip()
    if not task_id:
        return None, "", "план без устойчивого ID: связь не подтверждена"

    matches = by_id.get(task_id, [])
    if len(matches) == 1:
        return matches[0], "id", ""
    if len(matches) > 1:
        return None, "", "несколько карточек с одинаковым duskTaskId"
    return None, "", "карточка с зафиксированным ID не найдена"


def resolve_plan(vault: Path, plan: dict, day: datetime.date | None = None) -> dict:
    """Сопоставляет обязательства с карточками по устойчивому ID."""
    today = day or today_local()
    cards = _all_cards(vault)
    by_id: dict = {}
    for card in cards:
        if card["dusk_id"]:
            by_id.setdefault(card["dusk_id"], []).append(card)

    rows = []
    for item in plan.get("tasks", []):
        title = item.get("title") or ""
        card, identity, reason = _match_card(by_id, item)

        if card is None:
            if "несколько" in reason:
                state = AMBIGUOUS
            elif "не подтверждена" in reason:
                # Старый план без ID: обязательство живо, но засчитать его нельзя.
                state = UNVERIFIED
            else:
                state = MISSING
            rows.append({"title": title, "state": state, "identity": "",
                         "note": reason})
            continue

        state = _state_for(card, today)
        note = ""
        if state == FUTURE:
            note = f"дата закрытия в будущем ({card['completed']})"
        rows.append({"title": card["title"], "state": state, "identity": identity,
                     "status": card["status"], "stem": card["stem"], "note": note})

    total = len(rows)
    done = sum(1 for r in rows if r["state"] == DONE)
    missing = sum(1 for r in rows if r["state"] == MISSING)
    ambiguous = sum(1 for r in rows if r["state"] == AMBIGUOUS)
    future = sum(1 for r in rows if r["state"] == FUTURE)
    unverified = sum(1 for r in rows if r["state"] == UNVERIFIED)
    return {
        "rows": rows,
        "total": total,
        "done": done,
        "missing": missing,
        "ambiguous": ambiguous,
        "future": future,
        "unverified": unverified,
        "percent": round(100 * done / total) if total else None,
    }


def metric(vault: Path, day: datetime.date | None = None) -> dict:
    """Число выполнения для статуса. Никогда не создаёт и не правит план."""
    start, end = week_bounds(day)
    plan, reason = load_plan(vault)
    if plan is None:
        return {"fixed": False, "reason": reason, "week_start": start, "week_end": end,
                "percent": None, "total": 0, "done": 0, "missing": 0,
                "ambiguous": 0, "future": 0, "unverified": 0, "rows": []}
    if (plan["week_start"], plan["week_end"]) != (start, end):
        return {"fixed": False,
                "reason": f"план другой недели ({plan['week_start']} — {plan['week_end']})",
                "week_start": start, "week_end": end, "percent": None, "total": 0,
                "done": 0, "missing": 0, "ambiguous": 0, "future": 0,
                "unverified": 0, "rows": []}

    resolved = resolve_plan(vault, plan, day)
    resolved.update({"fixed": True, "reason": "", "week_start": start, "week_end": end})
    return resolved


MARKS = {DONE: "✅", OPEN: "⬜", MISSING: "❓", AMBIGUOUS: "⚠️",
         FUTURE: "⏳", UNVERIFIED: "❔"}


def _main() -> int:
    parser = argparse.ArgumentParser(description="Недельный план обязательств")
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("freeze", help="зафиксировать план на текущую неделю")
    freeze.add_argument("tasks", nargs="*", help="названия задач из TaskNotes/Tasks")
    freeze.add_argument("--vault", default=None)

    show = sub.add_parser("show", help="показать текущий план и выполнение")
    show.add_argument("--vault", default=None)

    args = parser.parse_args()
    vault = Path(args.vault or os.getenv("VAULT_PATH", "~/Documents/obsidian/my dusk"))

    if args.command == "freeze":
        if not args.tasks:
            print('Перечисли обязательства: weekly_plan.py freeze "задача 1" "задача 2"')
            return 2
        path = save_plan(vault, args.tasks)
        plan, reason = load_plan(vault)
        unresolved = [e["title"] for e in (plan or {}).get("tasks", []) if not e["id"]]
        for warning in getattr(save_plan, "last_warnings", []):
            print(f"  ⚠️ {warning}")
        print(f"План зафиксирован: {path}")
        if unresolved:
            print("  ⚠️ не нашёл карточки (идентичность не зафиксирована): "
                  + ", ".join(unresolved))
        return 0

    result = metric(vault)
    if not result["fixed"]:
        print(f"{result['reason']} (неделя {result['week_start']} — {result['week_end']})")
        return 0
    print(f"План недели {result['week_start']} — {result['week_end']}: "
          f"{result['done']}/{result['total']} = {result['percent']}%")
    for row in result["rows"]:
        line = f"  {MARKS[row['state']]} {row['title']}"
        if row.get("note"):
            line += f" — {row['note']}"
        print(line)
    for kind, label in ((MISSING, "не найдено"), (AMBIGUOUS, "неоднозначно"),
                        (FUTURE, "закрыто будущей датой"),
                        (UNVERIFIED, "связь не подтверждена (план без ID)")):
        count = sum(1 for r in result["rows"] if r["state"] == kind)
        if count:
            print(f"  {MARKS[kind]} {label}: {count} (в знаменателе остаются)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
