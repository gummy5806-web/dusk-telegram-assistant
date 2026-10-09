#!/usr/bin/env python3
"""Один мозг — два входа.

Диспетчер принимает одну строку и решает сам, что с ней делать:

    !текст / личное:    → Private/00 Private.md (вне git: в истории репозитория его нет)
    день: X / новая строка Y → задачи сразу на доску, status: next + nextStep
    ?текст              → поиск по ОБЕИМ валтам (my dusk + research-brain)
    +контент 60         → счётчик Rule of 100 в Рынок.md
    ссылка на видео     → очередь на конспект (Mac разберёт сам)
    явное действие      → задача сразу на канбан
    мысль / ссылка      → заметка сразу в ZETA/FLEETING_DAILY

Его вызывают: окно по хоткею (dusk-capture) и Telegram-бот.

Что верно про приватность, а что нет:
- локальный захват (панель, Raycast) не делает сетевых вызовов вовсе;
- захват через Telegram проходит через серверы Telegram и через VPS;
- фоновые шаги (разбор инбокса, конспект видео) вызывают модель, настроенную
  в валте (`LLM_PROVIDER` / `LLM_BASE_URL`). Записи из Private/ в эти шаги не
  попадают, но «локально» — это про маршрут, а не про любую модель вообще.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import fcntl
import hashlib
import tempfile
from contextlib import contextmanager
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

VAULT = Path(os.getenv("VAULT_PATH", "~/Documents/obsidian/my dusk"))
BOT = Path(__file__).parent
sys.path.insert(0, str(BOT))

import inbox_core  # noqa: E402
import ask  # noqa: E402
import market  # noqa: E402
import frontmatter  # noqa: E402
import health  # noqa: E402
import queue_store  # noqa: E402
import weekly_plan  # noqa: E402
from capture_privacy import looks_private  # noqa: E402

INBOX = VAULT / "ZETA" / "FLEETING" / "00 Инбокс.md"
PRIVATE = VAULT / "Private" / "00 Private.md"


@dataclass(frozen=True)
class WriteResult:
    """Результат явной записи: текст ответа, факт мутации и приватность.

    Нужен бизнес-слою, чтобы не угадывать мутацию по тексту ответа: neutral sync
    запускается только при `mutated and not private`.
    """

    text: str
    mutated: bool = False
    kind: str = ""
    private: bool = False


def _to_private(text: str) -> str:
    inbox_core.append_line(PRIVATE, text, "thought",
                           default_body=inbox_core.DEFAULT_PRIVATE_BODY)
    return "🔒 Личное записано — в git не попадёт"


def _advanced_uri_enabled() -> bool:
    plugins_path = VAULT / ".obsidian" / "community-plugins.json"
    try:
        plugins = json.loads(plugins_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(plugins, list) and "obsidian-advanced-uri" in plugins


def _obsidian_running() -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-x", "Obsidian"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _write_via_obsidian(path: Path, body: str) -> bool:
    """Create through app.vault so Bases receives the task immediately."""
    if not _advanced_uri_enabled() or not _obsidian_running():
        return False

    try:
        relative_path = path.relative_to(VAULT).as_posix()
    except ValueError:
        return False

    query = urlencode(
        {
            "vault": VAULT.name,
            "filepath": relative_path,
            "data": body,
            "openmode": "silent",
        },
        quote_via=quote,
    )
    try:
        subprocess.run(
            ["/usr/bin/open", "-g", f"obsidian://adv-uri?{query}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def _write_task(path: Path, body: str) -> bool:
    if path.exists():
        return False
    if _write_via_obsidian(path, body):
        return True

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return False
    return True


def _task_items(raw: str) -> list[str]:
    # Dictation adds punctuation naturally. Only a real line break separates tasks.
    items = [line.strip(" -•\t") for line in raw.splitlines()]
    return list(dict.fromkeys(item for item in items if len(item) >= 2))


def _task_body(title: str, ts: str, today: str, source: str = "пакет дня") -> str:
    yaml_title = json.dumps(title, ensure_ascii=False)
    return "\n".join([
        "---",
        "title: " + yaml_title,
        "status: next",
        "priority: normal",
        "nextStep: " + yaml_title,
        "created: " + ts,
        "scheduled: " + today,
        "tags:",
        "  - task",
        "source: " + json.dumps(source, ensure_ascii=False),
        "---",
        "",
        "# " + title,
        "",
    ])


def _create_task_exact_core(title: str, now=None, vault=None, source: str = "пакет дня"):
    """Тело создания задачи без блокировки: вызывается под `_capture_lock`."""
    vault = Path(vault) if vault is not None else VAULT
    moment = weekly_plan.to_almaty(now)
    ts = moment.strftime("%Y-%m-%dT%H:%M")
    today = moment.strftime("%Y-%m-%d")
    safe = "".join(c for c in title if c.isalnum() or c in " -_" or "\u0400" <= c <= "\u04ff").strip()[:50]
    safe = safe or "Задача"
    base = vault / "TaskNotes" / "Tasks" / (safe + ".md")
    body = _task_body(title, ts, today, source=source)

    def _same_task(path):
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return "# " + title in existing.splitlines()

    counter = 0
    path = base
    while counter < 1000:
        if path.exists():
            if _same_task(path):
                return path, False
        elif _write_task(path, body):
            return path, True
        elif path.exists() and _same_task(path):
            # Гонка: конкурент создал ровно эту карточку между проверкой и
            # записью. Проигравший не уходит в «Название 1.md», а признаёт её.
            return path, False
        counter += 1
        path = base.with_name(f"{safe} {counter}.md")
    return None, False


def create_task_exact(title: str, now=None, vault=None, source: str = "пакет дня"):
    """Создаёт ровно одну задачу в `next` под общим lock. Возвращает `(path, created)`.

    `path is None` — запись не удалась; `created is False` при живом path —
    такая задача уже есть. Точный путь нужен кнопочному UX: статус меняется по
    карточке, а не поиском по заголовку. Lock сериализует конкурентов, чтобы два
    одновременных запроса не дали `Название.md` и `Название 1.md`.
    """
    with _capture_lock(vault):
        return _create_task_exact_core(title, now=now, vault=vault, source=source)


def _daily_tasks_count(raw: str):
    """`(ответ, сколько создано)`. Вызывается под lock."""
    items = _task_items(raw)
    if not items:
        return "Пусто. Каждая задача должна быть на отдельной строке", 0

    # Один aware-момент владельца на весь пакет: процесс может стоять в UTC,
    # но дата задачи — дата Алматы.
    moment = weekly_plan.to_almaty()
    made = []
    for title in items:
        _path, created = _create_task_exact_core(title, now=moment)
        if created:
            made.append(title)
    if not made:
        return "Такие задачи уже есть — ничего не добавил", 0
    head = "Создано задач: " + str(len(made)) + " → канбан · Дальше"
    if len(made) < len(items):
        head += f" · уже есть: {len(items) - len(made)}"
    return head + "\n" + "\n".join("• " + m[:50] for m in made), len(made)


class TaskMessage(str):
    """Строка-ответ о задачах + число реально созданных карточек.

    Остаётся строкой, чтобы старые вызовы `_daily_tasks` не менялись, но несёт
    `created` для neutral sync в Telegram.
    """

    created = 0


def _daily_tasks(raw: str) -> str:
    text, created = _daily_tasks_count(raw)
    message = TaskMessage(text)
    message.created = created
    return message


def _counter_result(text: str):
    """`(ответ, была_ли_запись)`. Вызывается под lock."""
    parsed = market.parse_command(text)
    if not parsed:
        return "", False
    key, amount = parsed
    try:
        label, now, target, unit = market.bump(key, amount)
    except (KeyError, TypeError, ValueError):
        return "", False
    unit_txt = f" {unit}" if unit not in ("шт",) else ""
    head = f"📊 {label}: +{amount:g}{unit_txt} · сегодня {now:g}{unit_txt}"
    if target:
        # Недельная цель сравнивается только с недельным прогрессом, не с днём.
        try:
            progress = market.week_progress(key)
        except market.MarketError:
            progress = None
        if progress is not None:
            return (head + f". За неделю пн→сегодня: {progress['total']:g} "
                    f"из {target:g}{unit_txt}"), True
    return head, True


def _counter(text: str) -> str:
    return _counter_result(text)[0]


def _video_result(url: str):
    """`(ответ, была_ли_запись)`. Повторная ссылка не мутирует очередь."""
    import learn  # тяжёлый импорт — только когда реально нужна ссылка
    outcome = learn.enqueue(url)
    if outcome == "уже в очереди":
        return "Видео уже в очереди на конспект", False
    return "Видео добавлено в очередь на конспект", True


def _video(url: str) -> str:
    return _video_result(url)[0]


@dataclass(frozen=True)
class CaptureRoute:
    kind: str
    text: str
    command: str = ""


ACTION_WORDS = frozenset("""
сделать создать написать записать проверить починить исправить
добавить убрать удалить купить заказать оплатить позвонить отправить
ответить опубликовать выпустить снять записаться сходить поехать найти
изучить прочитать посмотреть подготовить настроить установить обновить
собрать перенести загрузить выложить разобрать обсудить
запустить закрыть закончить завершить провести согласовать оформить
продумать придумать разработать внедрить протестировать отредактировать
забрать забронировать вернуть открыть подключить отключить поменять
решить уточнить отправиться скачать сохранить составить спланировать
сделай создай напиши запиши проверь почини исправь добавь убери удали
купи закажи оплати позвони отправь ответь опубликуй подготовь настрой
установи обнови собери перенеси загрузи выложи разбери запусти заверши
прочитай посмотри изучи согласуй оформи
разобраться поработать доделать спарсить выгрузить залить поставить
поговорить продолжить подумать перенастроить
разберись доделай поставь выгрузи залей поговори продолжи
""".split())


def _is_action(text: str) -> bool:
    text = text.strip(" -•\t").lower()
    if "?" in text or re.match(r"^(?:не\s+(?:надо|нужно|хочу)|нужно\s+ли)\b", text):
        return False
    if re.match(r"^(?:надо|нужно|необходимо|не забыть)\s+\S", text):
        return True
    first = re.match(r"[а-яё]+", text)
    return bool(first and first.group(0) in ACTION_WORDS)


# Префиксы, которые Telegram обязан отдать общему dispatcher, а не своим путям.
_SHARED_PREFIX_RE = re.compile(
    r"^\s*(?:задача|задачи|todo|мысль|идея|заметка)\s*:",
    re.IGNORECASE,
)


def shared_prefix(text: str) -> bool:
    """Чистый predicate префиксов общего маршрутизатора (без I/O)."""
    return bool(_SHARED_PREFIX_RE.match(text or ""))


def resolve_capture(text: str) -> CaptureRoute:
    """One routing decision shared by preview and saving; never performs I/O."""
    text = (text or "").strip()
    if not text:
        return CaptureRoute("empty", "")

    # «здоровье» и «health» — read-only проверка. Проверяется до `looks_private`:
    # иначе слово «здоровье» уводило бы команду в Private.
    if re.fullmatch(r"(?:здоровье|health)", text, re.IGNORECASE):
        return CaptureRoute("health", "")

    patterns = (
        ("private", r"^(?:!(?!\[\[)|личное\s*[:\-]?)\s*(.+)$"),
        ("task", r"^(?:день|задачи на день|сегодня|задача|задачи|todo)\s*:\s*(.+)$"),
        ("note", r"^(?:мысль|идея|заметка)\s*:\s*(.+)$"),
        ("search", r"^(?:\?|спроси\s*[:\-]?|найди\s*[:\-]?)\s*(.+)$"),
    )
    for kind, pattern in patterns:
        match = re.match(pattern, text, re.IGNORECASE | re.DOTALL)
        if match and match.group(1).strip():
            payload = match.group(1).strip()
            if kind in ("note", "task") and looks_private(payload):
                kind = "private"
            return CaptureRoute(kind, payload)

    match = re.match(r"^(дальше|в работу|готово|список|next|todo)\s+(.+)$", text, re.IGNORECASE)
    if match:
        return CaptureRoute("advance", match.group(2).strip(), match.group(1).lower())
    if text.startswith("+"):
        try:
            counter = market.parse_command(text)
        except ValueError:
            counter = None
        if counter:
            return CaptureRoute("counter", text)
    if looks_private(text):
        return CaptureRoute("private", text)

    match = re.search(r"https?://[^\s<>]+", text)
    if match:
        url = match.group(0).rstrip(".,;!?)\"'")
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        if any(host == domain or host.endswith("." + domain)
               for domain in ("youtube.com", "youtu.be", "vimeo.com")):
            return CaptureRoute("video", url)
        return CaptureRoute("link", text)
    lines = [line for line in text.splitlines() if line.strip(" -•\t")]
    if lines and all(_is_action(line) for line in lines):
        return CaptureRoute("task", text)
    return CaptureRoute("note", text)


def preview(text: str) -> str:
    route = resolve_capture(text)
    labels = {
        "empty": "Пустая запись",
        "private": "Личное → Private · вне git",
        "task": "Задача → канбан · Дальше",
        "note": "Заметка → ZETA/FLEETING_DAILY",
        "link": "Ссылка → ZETA/FLEETING_DAILY",
        "search": "Поиск → Dusk и research-brain",
        "advance": "Статус задачи → канбан",
        "counter": "Счётчик → Рынок",
        "video": "Видео → очередь на конспект",
        "health": "Здоровье → read-only проверка",
    }
    if route.kind == "task":
        count = len(_task_items(route.text))
        if count > 1:
            return f"Задачи: {count} → канбан · Дальше"
    return labels[route.kind]


def save_note_result(text: str) -> WriteResult:
    """Заметка из явного режима: маршрут заметки без префикса.

    Личное по-прежнему пишется в Private (существующий безопасный маршрут), но
    помечается `private=True`, чтобы вызывающий не делал tracked git sync.
    """
    text = (text or "").strip()
    if not text:
        return WriteResult("Пустая заметка", mutated=False, kind="note")
    with _capture_lock():
        if looks_private(text):
            return WriteResult(_to_private(text), mutated=True,
                               kind="private", private=True)
        return WriteResult(_to_note(text, "note"), mutated=True, kind="note")


def save_note(text: str) -> str:
    return save_note_result(text).text


def save_video_result(url: str) -> WriteResult:
    """Видео из явного режима. Повторная ссылка не считается мутацией."""
    url = (url or "").strip()
    if not url:
        return WriteResult("Пустая ссылка", mutated=False, kind="video")
    with _capture_lock():
        text, changed = _video_result(url)
    return WriteResult(text, mutated=changed, kind="video")


def save_video(url: str) -> str:
    return save_video_result(url).text


def _to_note(text: str, kind: str = "note") -> str:
    import zettel
    path = zettel.make_fleeting(
        text=text,
        title=text.splitlines()[0],
        source="Dusk",
        vault=VAULT,
        writer=_write_task,
    )
    label = "Ссылка" if kind == "link" else "Заметка"
    return f"{label} сохранена → ZETA/FLEETING_DAILY\n{path.stem}"


@contextmanager
def _capture_lock(vault=None):
    target = Path(vault) if vault is not None else VAULT
    key = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()[:24]
    path = Path(tempfile.gettempdir()) / f"dusk-capture-{os.getuid()}-{key}.lock"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass(frozen=True)
class DispatchResult:
    """Ответ и факт реальной записи. Нужен Telegram-слою для neutral sync."""

    text: str
    mutated: bool = False
    kind: str = ""
    private: bool = False


def dispatch(text: str) -> str:
    """Единая точка входа. Возвращает ответ для человека. Без git-побочек."""
    return dispatch_detailed(text).text


def dispatch_detailed(text: str) -> DispatchResult:
    """То же, что `dispatch`, но сообщает, была ли реальная мутация.

    Модуль остаётся push-free: сам `dispatch` не делает git-операций. Решение о
    neutral sync принимает вызывающий (Telegram), поэтому локальный Caps+N
    сохраняет прежнюю семантику.
    """
    route = resolve_capture(text)
    with _capture_lock(VAULT):
        return _dispatch_route(route)


def _dispatch_route(route: CaptureRoute) -> DispatchResult:
    if route.kind == "empty":
        return DispatchResult("Пустая строка", kind="empty")
    if route.kind == "private":
        # Приватный маршрут не попадает в tracked-коммит: mutated=False.
        return DispatchResult(_to_private(route.text), kind="private", private=True)
    if route.kind == "task":
        text = _daily_tasks(route.text)
        return DispatchResult(text, mutated=bool(getattr(text, "created", 0)), kind="task")
    if route.kind == "advance":
        text, changed = _advance_task_core(route.command, route.text)
        return DispatchResult(text, mutated=changed, kind="advance")
    if route.kind == "search":
        return DispatchResult(ask.answer(route.text), kind="search")
    if route.kind == "counter":
        text, changed = _counter_result(route.text)
        return DispatchResult(text, mutated=changed, kind="counter")
    if route.kind == "video":
        text, changed = _video_result(route.text)
        return DispatchResult(text, mutated=changed, kind="video")
    if route.kind == "health":
        return DispatchResult(health.report(VAULT), kind="health")
    return DispatchResult(_to_note(route.text, route.kind), mutated=True, kind=route.kind)

# --- продвижение задач без мыши --------------------------------------------
# «дальше опубликовать» → задача в ➡️ Дальше. Работает из панели и бота:
# та же команда, что и драг карточки, только с клавиатуры.
ADVANCE = {"дальше": "next", "в работу": "in-progress", "готово": "done",
           "список": "open", "next": "next", "todo": "next"}


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


# Работу с шапкой держит общий модуль: метаданные читаются только из первой
# валидной шапки, тело заметки остаётся текстом.
def _frontmatter_span(text: str):
    """Внутренняя часть первой шапки как (start, end) или None."""
    body = frontmatter.region(text)
    if not body:
        return None
    return 4, 4 + len(body)


def _frontmatter_get(text: str, name: str) -> str:
    return frontmatter.get(text, name)


def _frontmatter_set(text: str, name: str, value):
    return frontmatter.set_field(text, name, value)


def _completion_stamp(now=None):
    """Даты в формате TaskNotes и в зоне владельца.

    `completedDate` — только день, `dateModified` — ISO со смещением. Момент
    приводится к Asia/Almaty единой функцией `weekly_plan.to_almaty`: сервер
    может стоять в UTC, но дата закрытия — это дата владельца. Naive datetime
    трактуется как местное время Алматы (см. `weekly_plan.to_almaty`).
    """
    moment = weekly_plan.to_almaty(now)
    return moment.strftime("%Y-%m-%d"), moment.isoformat(timespec="milliseconds")


def _advance_task_core(command: str, query: str, now=None):
    """`(сообщение, было_ли_изменение)`. Вызывается под lock."""
    target = ADVANCE[command]
    tasks_dir = VAULT / "TaskNotes" / "Tasks"
    if not tasks_dir.is_dir():
        return "нет папки задач", False
    q = _norm(query)
    if not q:
        return "укажи название: " + command + " опубликовать", False

    matches = []
    for f in sorted(tasks_dir.glob("*.md")):
        n = _norm(f.stem)
        if not n:
            continue
        if q in n or n in q or (len(q) > 4 and q[:5] in n):
            matches.append(f)

    if not matches:
        # послабее: пересечение слов
        words = {query.lower().split()[0]} if query else set()
        for f in tasks_dir.glob("*.md"):
            if words and any(w in f.stem.lower() for w in words):
                matches.append(f)

    if not matches:
        return "не нашёл задачу с «" + query[:30] + "»", False
    if len(matches) > 1:
        names = [m.stem for m in matches[:5]]
        return ("нашёл несколько, уточни:" + chr(10)
                + chr(10).join("• " + n for n in names)), False

    changed, message = _set_task_status_core(matches[0], target, now=now, vault=VAULT)
    return message, changed


def advance_task(command: str, query: str, now=None) -> str:
    """Меняет статус задачи по названию (нечётко). Везде: панель и бот.

    Правила даты завершения:
    * новый переход в done **всегда** ставит текущую дату, в том числе если в
      шапке осталась устаревшая дата от прошлого закрытия;
    * повтор, когда статус уже done (в том числе `status: "done"`), не меняет
      ничего: историю не переписываем и дату не выдумываем;
    * переоткрытие удаляет completedDate только из шапки.
    """
    with _capture_lock(VAULT):
        return _advance_task_core(command, query, now=now)[0]


def _resolve_task_path(path, vault=None):
    """Только непосредственный `.md` внутри `TaskNotes/Tasks`, без symlink.

    Boundary держит сам public helper: вызывающий не может случайно изменить
    заметку с frontmatter вне папки задач.
    """
    vault = Path(vault) if vault is not None else VAULT
    candidate = Path(path)
    if candidate.is_symlink():
        return None
    try:
        tasks_dir = (vault / "TaskNotes" / "Tasks").resolve()
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.suffix.lower() != ".md":
        return None
    if resolved.parent != tasks_dir:
        return None
    if not resolved.is_file():
        return None
    return resolved


def _set_task_status_core(path, target: str, now=None, vault=None):
    """Точная смена статуса без блокировки. Возвращает `(changed, сообщение)`."""
    if target not in set(ADVANCE.values()):
        return False, "Неизвестный статус — ничего не изменил"

    f = _resolve_task_path(path, vault)
    if f is None:
        return False, "⚠️ Задача не найдена — обнови список"

    try:
        text = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False, "⚠️ Задача не найдена — обнови список"

    if not frontmatter.has_valid(text):
        return (False, "⚠️ «" + f.stem[:40] + "»: нет корректной шапки — статус не изменён "
                "(файл оставлен как есть)")

    old = frontmatter.get(text, "status") or "—"
    if target == old:
        # Уже в этом статусе: ничего не пишем, дату завершения не выдумываем.
        return False, "«" + f.stem[:40] + "» уже " + old

    day, stamp = _completion_stamp(now)
    updated, ok = frontmatter.set_field(text, "status", target)
    if not ok:
        return False, "⚠️ «" + f.stem[:40] + "»: шапка не прочитана — статус не изменён"

    if target == "done":
        # Новый переход: текущая дата заменяет устаревшую из шапки.
        updated, _ = frontmatter.set_field(updated, "completedDate", day)
    elif old == "done":
        # Переоткрытие: дата уходит из шапки целиком.
        updated, _ = frontmatter.set_field(updated, "completedDate", None)

    updated, ok = frontmatter.set_field(updated, "dateModified", stamp)
    if not ok:
        return False, "⚠️ «" + f.stem[:40] + "»: не смог записать dateModified"

    if updated != text:
        queue_store.atomic_write(f, updated)
    return True, "✅ «" + f.stem[:40] + "»: " + old + " → " + target


def set_task_status(path, target: str, now=None, vault=None):
    """Меняет статус по точному пути под общим lock. Возвращает `(changed, сообщение)`.

    Точная идентичность нужна кнопкам: две задачи с одинаковым заголовком — это
    две разные карточки, и fuzzy-поиск здесь недопустим. Boundary и блокировка
    живут здесь, а не у вызывающего.
    """
    with _capture_lock(vault):
        return _set_task_status_core(path, target, now=now, vault=vault)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--private", action="store_true", help="сразу в личное, без префикса")
    ap.add_argument("--preview", action="store_true", help="маршрут без записи и изменения файлов")
    args = ap.parse_args()
    if args.preview:
        print(preview("!" + args.text if args.private else args.text))
    elif args.private:
        with _capture_lock():
            print(_to_private(args.text.strip()) if args.text.strip() else "Пустая строка")
    else:
        print(dispatch(args.text))
