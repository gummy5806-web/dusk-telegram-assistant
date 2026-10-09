#!/usr/bin/env python3
"""Этап 2Б: связь «конспект ↔ задача применения», наблюдение и обзор.

Что здесь живёт:

* разметка новой пары 2Б (`duskApply: 2b`) и служебный блок связи в конспекте;
* карточка применения: предложенный критерий (гипотеза) и три поля, которые
  пользователь заполняет обычным текстом в Obsidian;
* чтение наблюдения из карточки и детерминированный обзор для страницы статуса.

Чего здесь нет и не должно быть: модели, сети, второй базы, реестров и
массовой миграции. Обзор — это чтение файлов и арифметика, без выводов о
причинности: положительный или отрицательный результат остаётся текстом
пользователя, а статус `done` означает только выполненное действие.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import frontmatter
import weekly_plan

MARK = "duskApply: 2b"
PAIR_FIELD = "duskPair"
NOTE_MARK = "duskApply: 2b"          # маркер конспекта новой пары
MAX_CRITERION_CHARS = 300
NO_DATA = "нет данных"
SECTION_TITLE = "## 🧪 Применение советов"
CRITERION_HEADING = "## Критерий наблюдения (гипотеза)"
TRIED_HEADING = "## Что попробовал"
OBSERVED_HEADING = "## Что наблюдал"
CONCLUSION_HEADING = "## Мой вывод"
NOTE_BLOCK_MARK = "<!-- dusk-apply -->"
TASKS_DIR = "TaskNotes/Tasks"

CRITERION_FALLBACK = (
    "что станет видно, если шаг «{action}» выполнен. "
    "Признак сформулируй сам — это гипотеза, а не обещание результата"
)

_HEADINGS = (CRITERION_HEADING, TRIED_HEADING, OBSERVED_HEADING, CONCLUSION_HEADING)


def is_application_card(text: str) -> bool:
    """Новая карточка пары 2Б: только она попадает в обзор и получает блок связи."""
    return frontmatter.get(text, "duskApply") == "2b"


def pair_of(text: str) -> str:
    """Идентификатор пары. Пустая строка — пары 2Б нет (legacy)."""
    value = frontmatter.get(text, PAIR_FIELD).strip()
    return value


def new_pair_id() -> str:
    import uuid
    return uuid.uuid4().hex[:16]


def sanitise_criterion(value: str) -> str:
    """Приводит модельный критерий к одному короткому тексту без разметки.

    Критерий — предложение, а не разметка карточки: заголовки, списки и наши
    служебные заголовки полей из него удаляются. Если после этого текста не
    осталось, критерий считается неподходящим.
    """
    text = (value or "").replace("\r", "\n")
    # Code fence — служебная разметка ответа модели, а не содержание критерия.
    # Маркеры убираются до склейки строк: иначе критерий начинался бы с ``` и
    # парсер считал бы все следующие поля кодом, пряча наблюдение пользователя.
    text = re.sub(r"`{2,}", " ", text)
    text = re.sub(r"~{2,}", " ", text)
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
            continue
        if stripped in _HEADINGS:
            continue
        lines.append(stripped)
    cleaned = " ".join(lines)
    cleaned = cleaned.replace("`", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for heading in _HEADINGS:
        cleaned = cleaned.replace(heading, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:;")
    if not cleaned or len(cleaned) < 8:
        return ""
    return cleaned[:MAX_CRITERION_CHARS].strip()


def criterion_for(action: str, model_criterion: str = "") -> str:
    """Критерий наблюдения: из модельного ответа, иначе осторожная формулировка.

    Ничего не выдумываем: ни оффера, ни канала, ни чисел, ни нормы активности.
    """
    text = sanitise_criterion(model_criterion)
    if text:
        return text
    return CRITERION_FALLBACK.format(action=(action or "шаг").strip())


def render_task_body(action: str, criterion: str, note_rel: str) -> str:
    """Тело карточки применения: критерий-гипотеза и три поля пользователя."""
    title = action.strip() or "применить из видео"
    rel = note_rel[:-3] if note_rel.endswith(".md") else note_rel
    stem = Path(note_rel).stem
    return (
        f"# {title}\n\n"
        # Первая ссылка — полный путь от корня валта: именно она однозначна.
        f"Из видео: [[{rel}]]\n"
        f"_(имя конспекта: [[{stem}]])_\n\n"
        f"{CRITERION_HEADING}\n\n{criterion.strip()} — гипотеза.\n\n"
        f"{TRIED_HEADING}\n\n{NO_DATA}\n\n"
        f"{OBSERVED_HEADING}\n\n{NO_DATA}\n\n"
        f"{CONCLUSION_HEADING}\n\n{NO_DATA}\n"
    )


def _headings(text: str) -> list:
    """Заголовки `## …` вне блоков кода: (смещение, длина строки, заголовок)."""
    out, offset, fenced = [], 0, False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
        elif not fenced and stripped.startswith("## "):
            out.append((offset, len(line), stripped))
        offset += len(line)
    return out


def _clean(value: str) -> str:
    """Значение поля одной строкой. Пустое или шаблонное — «нет данных»."""
    text = (value or "").strip()
    if not text or text == NO_DATA:
        return NO_DATA
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = " ".join(lines)
    return joined if joined else NO_DATA


def read_application(task_text: str) -> dict:
    """Что в карточке: критерий, попытка, наблюдение, вывод пользователя.

    Заголовки читаются в каноническом порядке и только вне блоков кода: похожий
    текст внутри критерия, примера или code fence не становится фактическим
    наблюдением пользователя.
    """
    headings = _headings(task_text)
    canonical = (CRITERION_HEADING, TRIED_HEADING, OBSERVED_HEADING, CONCLUSION_HEADING)
    values = {heading: "" for heading in canonical}
    index = 0
    for position, (offset, length, title) in enumerate(headings):
        if title not in canonical:
            continue                      # чужой заголовок не наше поле
        order = canonical.index(title)
        if order < index:
            continue                      # повтор уже прочитанного поля
        index = order + 1
        end = headings[position + 1][0] if position + 1 < len(headings) else len(task_text)
        values[title] = task_text[offset + length:end]
    observed = _clean(values[OBSERVED_HEADING])
    conclusion = _clean(values[CONCLUSION_HEADING])
    return {
        "criterion": _clean(values[CRITERION_HEADING]),
        "tried": _clean(values[TRIED_HEADING]),
        "observed": observed,
        "observed_state": observed,
        "conclusion": conclusion,
        "has_observation": observed != NO_DATA,
        "has_conclusion": conclusion != NO_DATA,
    }


# Служебный блок связи имеет ровно известную форму: маркер, заголовок раздела,
# пустая строка и одна строка со ссылкой на задачу. Только такой span мы имеем
# право заменять; всё, что после него, — текст пользователя и неприкосновенно.
_BLOCK_PATTERN = re.compile(
    re.escape(NOTE_BLOCK_MARK) + r"[ \t]*\r?\n"
    + re.escape(SECTION_TITLE) + r"[ \t]*\r?\n"
    + r"[ \t]*\r?\n"
    + r"- Задача: \[\[[^\]\r\n]+\]\][ \t]*\r?\n?"
)


def _block_span(note_text: str):
    """Границы известного служебного блока `(начало, конец)` или None.

    Неизвестная, повреждённая или неоднозначная разметка — `ValueError`, чтобы
    вызывающий отказался от записи, а не потерял пользовательский текст.
    """
    count = note_text.count(NOTE_BLOCK_MARK)
    if count == 0:
        return None
    if count > 1:
        raise ValueError("служебный блок встречается в конспекте больше одного раза")
    match = _BLOCK_PATTERN.search(note_text)
    if match is None:
        raise ValueError("разметка служебного блока не распознана")
    return match.start(), match.end()


def replace_note_block(note_text: str, task_rel: str) -> str:
    """Ставит служебный блок, сохраняя текст пользователя побайтно.

    Заменяется ровно известный span блока, а не «всё после маркера»: prefix и
    suffix (включая CRLF и завершающие переводы строк, а также текст, дописанный
    пользователем после публикации) остаются дословно. Неизвестная разметка —
    отказ без записи.
    """
    block = note_block(task_rel)
    span = _block_span(note_text)
    if span is None:
        prefix = note_text
        separator = "" if prefix.endswith(("\n", "\r")) else "\n"
        return prefix + separator + block
    start, end = span
    return note_text[:start] + block + note_text[end:]


def note_block(task_rel: str) -> str:
    """Служебный блок связи в конспекте: только полный путь от корня валта."""
    rel = task_rel[:-3] if task_rel.endswith(".md") else task_rel
    return f"{NOTE_BLOCK_MARK}\n{SECTION_TITLE}\n\n- Задача: [[{rel}]]\n"


def has_note_block(note_text: str, task_rel: str) -> bool:
    """Есть ли в конспекте именно служебный блок со ссылкой на эту задачу.

    Проверяется span блока, а не произвольная ссылка где-то в тексте заметки.
    Повреждённая/неоднозначная разметка блоком не считается.
    """
    rel = task_rel[:-3] if task_rel.endswith(".md") else task_rel
    try:
        span = _block_span(note_text)
    except ValueError:
        return False
    if span is None:
        return False
    start, end = span
    return f"[[{rel}]]" in note_text[start:end]


def task_links_criterion(task_text: str, note_rel: str) -> bool:
    """Задача ссылается ровно на этот конспект (полный путь в `source`)."""
    return frontmatter.get(task_text, "source").strip() == note_rel


def tasks_root(vault: Path, *, strict: bool = False):
    """Каталог карточек задач, лежащий внутри валта.

    Symlink самого каталога или любого предка наружу отклоняется: иначе запись и
    чтение уходили бы за пределы валта. При `strict` — исключение, иначе None.
    """
    root = Path(vault) / TASKS_DIR
    if not _inside(root, Path(vault)):
        if strict:
            raise ValueError("каталог задач вне валта")
        return None
    return root


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _week_bounds(today: datetime.date):
    start, end = weekly_plan.week_bounds(today)
    return datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)


def _completed_date(task_text: str):
    return weekly_plan.parse_task_date(frontmatter.get(task_text, "completedDate"))


def _source_identity(vault: Path, task_text: str) -> tuple:
    """(подтверждён ли источник, относительный путь).

    Совпадение пути и существование файла идентичностью не являются: конспект
    обязан быть нашей карточкой 2Б с той же идентичностью пары. Иначе конспект
    удалён, заменён ручным файлом или это чужая заметка.
    """
    rel = frontmatter.get(task_text, "source").strip()
    if not rel:
        return False, ""
    path = Path(vault) / rel
    try:
        if not _inside(path, vault) or not path.is_file():
            return False, ""
        note_text = path.read_text(encoding="utf-8")
    except OSError:
        return False, ""
    if not is_application_card(note_text):
        return False, ""
    task_pair, note_pair = pair_of(task_text), pair_of(note_text)
    if not task_pair or task_pair != note_pair:
        return False, ""
    return True, rel


def application_rows(vault: Path, today: datetime.date | None = None) -> dict:
    """Детерминированный обзор применения.

    * **Завершения периода** — по строгому `completedDate` внутри текущей недели
      (пн–вс, Asia/Almaty) и не позже сегодня; дата создания значения не имеет,
      поэтому закрытая на этой неделе старая карточка остаётся в обзоре.
    * **Активные сейчас** — открытые пары 2Б независимо от даты создания: они не
      исчезают при смене недели.
    * Старые завершения вне периода и будущие даты выполнения не начисляют.
    * Наблюдение — текущее состояние карточки, а не выдуманная дата записи.
    """
    vault = Path(vault)
    day = today or weekly_plan.today_local()
    week_start, week_end = _week_bounds(day)
    closed_upper = min(week_end, day)
    root = tasks_root(vault)
    rows = []
    counts = {"total": 0, "done": 0, "active": 0, "observed": 0, "no_data": 0}
    if root is not None and root.is_dir():
        for path in sorted(root.glob("*.md")):
            if not _inside(path, root):
                continue                      # symlink карточки наружу
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not is_application_card(text):
                continue
            status = frontmatter.get(text, "status")
            completed = _completed_date(text)
            done = (status == "done" and completed is not None
                    and week_start <= completed <= closed_upper)
            active = status != "done"
            # Закрытие будущей датой и закрытие без даты выполнением не считаются,
            # но и не прячутся: это видимая ошибка данных, а не пропажа карточки.
            future = bool(status == "done" and completed is not None and completed > day)
            undated = bool(status == "done" and completed is None)
            if not (done or active or future or undated):
                continue                      # завершение вне периода
            application = read_application(text)
            source_ok, note_rel = _source_identity(vault, text)
            rows.append({
                "task": path.name,
                "task_rel": path.relative_to(vault).as_posix(),
                "task_link": path.relative_to(vault).with_suffix("").as_posix(),
                "action": frontmatter.get(text, "title") or path.stem,
                "status": status,
                "pair": pair_of(text),
                "done": done,
                "active": active,
                "future": future,
                "undated": undated,
                "completed": completed.isoformat() if completed else "",
                "observed": application["observed"],
                "observed_state": application["observed_state"],
                "has_observation": application["has_observation"],
                "conclusion": application["conclusion"],
                "has_conclusion": application["has_conclusion"],
                "source_rel": note_rel,
                "source_ok": source_ok,
                "source_link": note_rel[:-3] if (source_ok and note_rel.endswith(".md")) else None,
            })
            counts["total"] += 1
            counts["done"] += 1 if done else 0
            counts["active"] += 1 if active else 0
            counts["observed"] += 1 if application["has_observation"] else 0
            counts["no_data"] += 1 if not application["has_observation"] else 0
    period = f"{week_start.isoformat()}–{week_end.isoformat()}"
    if counts["total"]:
        summary = (f"Завершения периода {period}: {counts['done']}. "
                   f"Активные сейчас: {counts['active']}. "
                   f"Наблюдение записано: {counts['observed']}, "
                   f"без данных: {counts['no_data']}.")
    else:
        summary = (f"Данных нет: за {period} нет завершений и нет активных "
                   "карточек применения.")
    return {"period": {"start": week_start.isoformat(), "end": week_end.isoformat()},
            "rows": rows, "counts": counts, "summary": summary}


def _cell(value: str) -> str:
    """Значение для ячейки: вертикальная черта не должна ломать таблицу."""
    text = (value or NO_DATA).replace("|", "\\|")
    text = " ".join(text.split())
    return text or NO_DATA


def render_section(vault: Path, today: datetime.date | None = None) -> list:
    """Блок для страницы статуса: без модели и без выводов о причинности."""
    review = application_rows(vault, today=today)
    lines = [SECTION_TITLE, "", f"_{review['summary']}_", ""]
    if not review["rows"]:
        lines += ["> Завершения считаются по строгому `completedDate` за период "
                  "(пн–вс, Asia/Almaty), активные пары показываются отдельно.", ""]
        return lines
    lines += ["| действие | наблюдение | карточка | источник совета |", "|---|---|---|---|"]
    ordered = ([row for row in review["rows"] if row["done"]]
               + [row for row in review["rows"] if row["active"]]
               + [row for row in review["rows"] if row["future"]]
               + [row for row in review["rows"] if row["undated"]])
    for row in ordered:
        if row["done"]:
            state = "✅ выполнено" + (f" ({row['completed']})" if row["completed"] else "")
        elif row["future"]:
            state = f"⚠️ дата закрытия в будущем ({row['completed']}) — не выполнено"
        elif row["undated"]:
            state = "⚠️ закрыто без даты — не выполнено"
        else:
            state = "⬜ активно сейчас"
        observed = row["observed_state"] if row["has_observation"] else NO_DATA
        card = f"[[{row['task_link']}]]"
        source = f"[[{row['source_link']}]]" if row["source_ok"] else "источник не найден"
        lines.append(f"| {_cell(state)} | {_cell(observed)} | {card} | {source} |")
    lines += ["",
              "> Завершения — по строгому `completedDate` за период (пн–вс, "
              "Asia/Almaty); активные пары показаны отдельно и не исчезают при смене "
              "недели. Это наблюдения пользователя, а не доказательство причинности.", ""]
    for row in ordered:
        if row["has_conclusion"]:
            lines.append(f"- [[{row['task_link']}]] — вывод: {_cell(row['conclusion'])}")
    lines.append("")
    return lines
