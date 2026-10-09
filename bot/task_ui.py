#!/usr/bin/env python3
"""Кнопочный Telegram-UX: чистые правила, отдельно от Telegram I/O.

Модуль не знает про `telegram`, сеть и git. Он умеет:

* вести режим ожидания ввода (`➕ Задача` → следующее сообщение = название);
* валидировать название задачи, заметку и ссылку на видео;
* читать открытые задачи и рисовать список/карточку с короткими токенами;
* точно менять статус выбранной карточки (не fuzzy-поиском по заголовку)
  и сообщать, было ли реальное изменение — только тогда вызывается `sync`.

Так это и тестируется: без токена бота, без сети, с внедрёнными `sync`.
Тонкий слой `telegram_bot.py` только превращает `View` в кнопки Telegram.
"""

from __future__ import annotations

import re
import secrets
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter
import queue_store
import thought_feed

# ── Подписи главной клавиатуры ────────────────────────────────────────────
# Цели пользователя, а не синтаксис. Старые подписи остаются константами-алиасами:
# клавиатура у клиента могла не обновиться, и старая кнопка обязана работать.

BTN_THOUGHT = "💭 Мысль"
BTN_THOUGHTS = "🧠 Мысли"
BTN_SEARCH = "🔎 Найти"         # легаси-алиас старой кнопки поиска
BTN_NEW_TASK = "➕ Задача"
BTN_TASKS = "📋 Задачи"
BTN_MATERIAL = "🎓 Материал"
BTN_STATUS = "📊 Статус"
BTN_HEALTH = "🩺 Здоровье"          # доступно как /health, но не на главной клавиатуре
BTN_CANCEL = "✖️ Отмена"

# Легаси-алиасы: старые Telegram-клавиатуры продолжают срабатывать.
BTN_NOTE = "📝 Заметка"
BTN_VIDEO = "🎓 Видео"

MAIN_BUTTONS = (
    BTN_THOUGHT, BTN_THOUGHTS,
    BTN_NEW_TASK, BTN_TASKS,
    BTN_MATERIAL, BTN_STATUS,
)

MODE_TASK = "task"
MODE_NOTE = "note"
MODE_VIDEO = "video"
MODE_SEARCH = "search"

MODE_PROMPTS = {
    MODE_TASK: "Напиши название задачи — одним сообщением.",
    MODE_NOTE: "Напиши заметку — сохраню как есть.",
    MODE_VIDEO: "Пришли ссылку на материал (YouTube или Vimeo).",
    MODE_SEARCH: "Напиши, что найти в Dusk — обычными словами.",
}

MAX_TITLE_LEN = 90
PAGE_SIZE = 6
MAX_TOKENS = 200

MODE_KEY = "task_mode"
PAGE_KEY = "task_page"
TOKEN_KEY = "task_tokens"
GEN_KEY = "task_generation"

STATUS_LABELS = {
    "open": "📥 Список",
    "next": "➡️ Дальше",
    "in-progress": "🔥 В работе",
    "done": "✅ Готово",
}

STATUS_ICONS = {"open": "📥", "next": "➡️", "in-progress": "🔥", "done": "✅"}
STATUS_ORDER = {"in-progress": 0, "next": 1, "open": 2}
STATUS_TARGETS = frozenset(("open", "next", "in-progress", "done"))

# Нейтральные сообщения коммита: ни пользовательского текста, ни заголовка.
SYNC_MESSAGES = {
    "task": "telegram: задача создана",
    "status": "telegram: статус задачи изменён",
    "note": "telegram: заметка",
    "video": "telegram: видео в очередь",
    "counter": "telegram: счётчик рынка",
    "inbox": "telegram: запись в инбокс",
    "focus": "telegram: фокус",
    "reminder": "telegram: напоминание",
}

# Куда ведёт ответ: в режим ожидания с «Отмена» или в главное меню.
SCREEN_MAIN = "main"
SCREEN_CANCEL = "cancel"


# ── Авторизация и режимы ────────────────────────────────────────────────────

def authorized(user_id, allowed_user) -> bool:
    """Pure-проверка доступа: тот же смысл, что `auth_check` в боте."""
    return user_id is not None and user_id == allowed_user


def mode(state) -> str:
    value = state.get(MODE_KEY) if isinstance(state, dict) else ""
    return value if value in MODE_PROMPTS else ""


def begin(state, mode_name: str) -> str:
    """Переводит только этого пользователя в режим ожидания ввода."""
    if mode_name not in MODE_PROMPTS:
        mode_name = MODE_TASK
    state[MODE_KEY] = mode_name
    return MODE_PROMPTS[mode_name]


def cancel(state) -> None:
    if isinstance(state, dict):
        state.pop(MODE_KEY, None)


# Совместимое имя: сброс режима.
clear = cancel


# ── Валидация ввода ─────────────────────────────────────────────────────────

def validate_title(raw: str):
    """(название, ошибка). Пустое/служебное/слишком длинное — ошибка."""
    title = re.sub(r"\s+", " ", (raw or "").strip())
    if not title:
        return "", "Пустое название — напиши задачу текстом или нажми «Отмена»."
    if title in MAIN_BUTTONS:
        return "", "Это кнопка, а не название. Напиши название задачи."
    if title.startswith("/"):
        return "", "Команды не становятся задачами. Напиши название задачи."
    if len(title) > MAX_TITLE_LEN:
        return "", (f"Слишком длинное название: {len(title)} символов, "
                    f"максимум {MAX_TITLE_LEN}.")
    if any(ord(ch) < 32 for ch in title):
        return "", "В названии есть непечатаемые символы. Напиши текстом."
    return title, ""


def classify_video(raw: str):
    """(ссылка, ошибка) для видеорежима. Только YouTube и Vimeo."""
    match = re.search(r"https?://[^\s<>]+", raw or "")
    if not match:
        return "", "Не вижу ссылку. Пришли ссылку YouTube или Vimeo или нажми «Отмена»."
    url = match.group(0).rstrip(".,;!?)\"'")
    if not queue_store.is_video_url(url):
        return "", ("Поддерживаются только YouTube и Vimeo. "
                    "Пришли такую ссылку или нажми «Отмена».")
    return url, ""


# ── Чтение карточек ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TaskCard:
    relpath: str
    title: str
    status: str
    next_step: str
    estimate: str
    priority: str


@dataclass
class View:
    text: str
    rows: list = field(default_factory=list)
    stale: bool = False


@dataclass
class Submission:
    ok: bool
    text: str
    synced: bool
    path: str = ""


def keyboard_for(result: Submission) -> str:
    """Ошибка ввода оставляет «✖️ Отмена», успех возвращает главное меню."""
    return SCREEN_MAIN if result.ok else SCREEN_CANCEL


def neutral_kind(kind: str) -> str:
    """Смысл мутации для нейтрального сообщения коммита."""
    return {"advance": "status", "link": "note"}.get(kind or "", kind or "")


def mutation_sync(result, sync) -> bool:
    """Вызывает `sync(message)` ровно один раз при реальной не-private мутации.

    Duplicate/not-found/already/error и private-маршрут не синхронизируются:
    push не вызывается без настоящей записи и никогда — с приватными данными.
    """
    if not getattr(result, "mutated", False) or getattr(result, "private", False):
        return False
    message = SYNC_MESSAGES.get(neutral_kind(getattr(result, "kind", "")))
    if not message:
        return False
    if sync is not None:
        sync(message)
    return True


NOT_MODIFIED_MARKER = "Message is not modified"

def _is_message_not_modified(error) -> bool:
    """Повторный идентичный edit PTB просит не считать ошибкой."""
    return (type(error).__name__ == "BadRequest"
            and NOT_MODIFIED_MARKER in str(error))


async def safe_edit(query, text, reply_markup=None) -> bool:
    """`edit_message_text`, терпящий только benign «Message is not modified».

    Иначе повторный тап по той же кнопке превращал бы карточку в ошибку.
    Любой другой `BadRequest` или исключение пробрасывается наружу.
    """
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception as error:
        if _is_message_not_modified(error):
            return False
        raise
    return True


def _human_minutes(raw) -> str:
    try:
        number = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    if number < 60:
        return f"{number} мин"
    hours, minutes = divmod(number, 60)
    return f"{hours} ч" if minutes == 0 else f"{hours} ч {minutes} мин"


def _task_title(text: str, fallback: str) -> str:
    title = re.sub(r"\s+", " ", frontmatter.get(text, "title") or "").strip()
    return title or fallback


def scan_open_tasks(vault) -> list:
    """Все незакрытые задачи. Заголовок берётся из шапки, идентичность — путь."""
    tasks_dir = Path(vault) / "TaskNotes" / "Tasks"
    if not tasks_dir.is_dir():
        return []
    cards = []
    for path in sorted(tasks_dir.glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        status = frontmatter.get(text, "status")
        if status == "done":
            continue
        cards.append(TaskCard(
            relpath=str(path),
            title=_task_title(text, path.stem),
            status=status,
            next_step=frontmatter.get(text, "nextStep"),
            estimate=frontmatter.get(text, "timeEstimate"),
            priority=frontmatter.get(text, "priority"),
        ))
    cards.sort(key=lambda card: (STATUS_ORDER.get(card.status or "open", 3),
                                 card.title.lower(), card.relpath))
    return cards


# ── Короткие токены вместо путей ────────────────────────────────────────────

def _new_token() -> str:
    """8 hex-символов: непредсказуемо для чужого пользователя, коротко для callback."""
    return secrets.token_hex(4)


def _token_store(state) -> dict:
    store = state.get(TOKEN_KEY)
    if not isinstance(store, dict) or "by_token" not in store or "by_path" not in store:
        store = {"by_token": {}, "by_path": {}, "gen": int(state.get(GEN_KEY, 0))}
        state[TOKEN_KEY] = store
    return store


def _new_generation(state) -> int:
    """Новый корневой список обесценивает токены предыдущей генерации."""
    generation = int(state.get(GEN_KEY, 0)) + 1
    state[GEN_KEY] = generation
    state[TOKEN_KEY] = {"by_token": {}, "by_path": {}, "gen": generation}
    return generation


def issue_token(state, path) -> str:
    store = _token_store(state)
    key = str(path)
    existing = store["by_path"].get(key)
    if existing:
        return existing
    token = _new_token()
    while token in store["by_token"]:
        token = _new_token()
    store["by_token"][token] = key
    store["by_path"][key] = token
    while len(store["by_token"]) > MAX_TOKENS:
        oldest = next(iter(store["by_token"]))
        store["by_token"].pop(oldest, None)
        store["by_path"] = {k: v for k, v in store["by_path"].items() if v != oldest}
    return token


def resolve_token(state, token) -> str:
    store = _token_store(state)
    return store["by_token"].get((token or "").strip()) or ""


# ── Хаб мыслей: короткие токены на identity, не на путь ─────────────────────
# callback_data для мыслей тоже несёт только 8 hex-символов: ни пути, ни текста
# заметки там нет. В памяти на токен отображается identity (путь + хеш),
# поэтому рестарт или чужой тап дают stale, а не чтение произвольного файла.

THOUGHT_SECTION_OVERVIEW = "overview"
THOUGHT_SECTION_INBOX = "inbox"
THOUGHT_SECTION_RECENT = "recent"

THOUGHT_TOKEN_KEY = "thought_tokens"
THOUGHT_LIST_KEY = "thought_list"
THOUGHT_CURSOR_KEY = "thought_cursor"

THOUGHT_HUB = "f:h"
THOUGHT_INBOX = "f:i"
THOUGHT_RECENT = "f:r"
THOUGHT_REMEMBER = "f:w"
THOUGHT_SEARCH = "f:s"

THOUGHT_LIST_SIZE = 8
THOUGHT_TASK_SOURCE = "Telegram · из мысли"


def _thought_store(state) -> dict:
    store = state.get(THOUGHT_TOKEN_KEY)
    if (not isinstance(store, dict) or "by_token" not in store
            or "by_identity" not in store):
        store = {"by_token": {}, "by_identity": {}}
        state[THOUGHT_TOKEN_KEY] = store
    return store


def issue_thought_token(state, identity) -> str:
    store = _thought_store(state)
    key = str(identity)
    existing = store["by_identity"].get(key)
    if existing:
        return existing
    token = _new_token()
    while token in store["by_token"]:
        token = _new_token()
    store["by_token"][token] = key
    store["by_identity"][key] = token
    while len(store["by_token"]) > MAX_TOKENS:
        oldest = next(iter(store["by_token"]))
        store["by_token"].pop(oldest, None)
        store["by_identity"] = {k: v for k, v in store["by_identity"].items()
                                if v != oldest}
    return token


def resolve_thought_token(state, token) -> str:
    store = _thought_store(state)
    return store["by_token"].get((token or "").strip()) or ""


def _stale_thought() -> "View":
    return View("⚠️ Мысль устарела — открой «🧠 Мысли» заново.",
                [[("⬅️ Назад", THOUGHT_HUB)]], stale=True)


def _remember_position(state, identity: str) -> None:
    identities = state.get(THOUGHT_LIST_KEY)
    if not isinstance(identities, list) or identity not in identities:
        identities = [identity]
        state[THOUGHT_LIST_KEY] = identities
    state[THOUGHT_CURSOR_KEY] = identities.index(identity)


def _thought_view(state, thought) -> "View":
    token = issue_thought_token(state, thought.identity)
    _remember_position(state, thought.identity)
    rows = [
        [("➡️ В задачу", f"f:t:{token}"), ("Следующая", f"f:n:{token}")],
        [("Скрыть на 7 дней", f"f:x:{token}"), ("⬅️ Назад", THOUGHT_HUB)],
    ]
    return View(thought_feed.render_thought(thought), rows)


def _hub_rows() -> list:
    return [
        [("📥 Inbox", THOUGHT_INBOX), ("🆕 Последние", THOUGHT_RECENT)],
        [("💡 Вспомнить", THOUGHT_REMEMBER), ("🔎 Найти", THOUGHT_SEARCH)],
    ]


def render_thoughts(state, vault, store=None, now=None,
                   section: str = THOUGHT_SECTION_OVERVIEW) -> "View":
    """Один ограниченный экран хаба: обзор, Inbox или последние заметки."""
    if section == THOUGHT_SECTION_INBOX:
        items = thought_feed.inbox_thoughts(
            vault, limit=THOUGHT_LIST_SIZE)
        state[THOUGHT_LIST_KEY] = [thought.identity for thought in items]
        return View(thought_feed.render_list(items, "📥 Inbox"),
                    _section_rows(state, items))

    if section == THOUGHT_SECTION_RECENT:
        items = thought_feed.recent_thoughts(
            vault, limit=THOUGHT_LIST_SIZE)
        state[THOUGHT_LIST_KEY] = [thought.identity for thought in items]
        return View(thought_feed.render_list(items, "🆕 Последние заметки"),
                    _section_rows(state, items))

    data = thought_feed.overview(vault, store=store, now=now)
    text = thought_feed.render_overview(data)
    state[THOUGHT_LIST_KEY] = (
        [data.forgotten.identity] if data.forgotten is not None else [])
    # Обзор только ПОКАЗЫВАЕТ кандидата. Показ записывает лишь «💡 Вспомнить»,
    # иначе простое открытие хаба съедало бы забытую мысль без действия.
    return View(text, _hub_rows())


def remember_thought(state, vault, store=None, now=None) -> "View":
    """`💡 Вспомнить`: сразу открывает одну забытую мысль или честно говорит, что её нет."""
    data = thought_feed.overview(vault, store=store, now=now)
    thought = data.forgotten
    if thought is None:
        return View("💡 Пока нечего вспомнить — новых забытых мыслей нет.",
                    [[("⬅️ Назад", THOUGHT_HUB)]])
    state[THOUGHT_LIST_KEY] = [thought.identity]
    if store is not None:
        store.record_surface(thought.identity, now=now)
    return _thought_view(state, thought)


def _section_rows(state, items) -> list:
    rows = []
    for index, thought in enumerate(items):
        token = issue_thought_token(state, thought.identity)
        label = f"{index + 1}. {textwrap.shorten(thought.title, width=36, placeholder='…')}"
        rows.append([(label, f"f:o:{token}")])
    rows.append([("⬅️ Назад", THOUGHT_HUB)])
    return rows


def render_thought(state, vault, store, token, now=None) -> "View":
    """Выбранная мысль по короткому токену. Stale-токен закрывается fail-closed."""
    identity = resolve_thought_token(state, token)
    thought = thought_feed.load_thought(vault, identity) if identity else None
    if thought is None:
        return _stale_thought()
    return _thought_view(state, thought)


def next_thought(state, vault, store, token, now=None) -> "View":
    """Следующий кандидат в текущем разделе; в конце список зацикливается."""
    identity = resolve_thought_token(state, token)
    identities = state.get(THOUGHT_LIST_KEY)
    if not isinstance(identities, list) or not identities:
        return _stale_thought()
    try:
        index = identities.index(identity)
    except ValueError:
        index = int(state.get(THOUGHT_CURSOR_KEY, 0))
    next_index = (index + 1) % len(identities)
    thought = thought_feed.load_thought(vault, identities[next_index])
    if thought is None:
        return _stale_thought()
    state[THOUGHT_CURSOR_KEY] = next_index
    return _thought_view(state, thought)


def hide_thought(state, store, token, now=None) -> "View":
    """Скрыть выбранную мысль на 7 дней: только identity и cooldown в SQLite."""
    identity = resolve_thought_token(state, token)
    if not identity:
        return _stale_thought()
    if store is None:
        return View("⚠️ Состояние недоступно — не смог скрыть мысль.",
                    [[("⬅️ Назад", THOUGHT_HUB)]], stale=True)
    store.hide(identity, now=now)
    return View("🙈 Скрыл на 7 дней — неделю не покажу.",
                [[("💡 Вспомнить", THOUGHT_REMEMBER),
                  ("⬅️ Назад", THOUGHT_HUB)]])



def _safe_task_path(path, vault) -> bool:
    """Карточка резолвится только внутрь TaskNotes/Tasks и не через symlink."""
    candidate = Path(path)
    if candidate.is_symlink():
        return False
    try:
        tasks_dir = (Path(vault) / "TaskNotes" / "Tasks").resolve()
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved.parent == tasks_dir


# ── Отрисовка ───────────────────────────────────────────────────────────────

def _clamp_page(page, pages: int) -> int:
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    return max(0, min(page, pages - 1))


def _stale(state) -> View:
    page = state.get(PAGE_KEY, 0)
    return View("⚠️ Список устарел — обнови его.",
                [[("📋 Задачи", f"l:{page}")]], stale=True)


def render_list(state, vault, page=0, page_size: int = PAGE_SIZE, root: bool = False) -> View:
    if root:
        _new_generation(state)
    page_size = max(1, int(page_size or PAGE_SIZE))
    cards = scan_open_tasks(vault)
    pages = max(1, (len(cards) + page_size - 1) // page_size)
    page = _clamp_page(page, pages)
    state[PAGE_KEY] = page

    rows = []
    for card in cards[page * page_size:(page + 1) * page_size]:
        token = issue_token(state, card.relpath)
        icon = STATUS_ICONS.get(card.status or "open", "•")
        label = f"{icon} {textwrap.shorten(card.title, width=38, placeholder='…')}"
        rows.append([(label, f"t:{token}")])

    nav = []
    if page > 0:
        nav.append(("⬅️", f"l:{page - 1}"))
    if page < pages - 1:
        nav.append(("➡️", f"l:{page + 1}"))
    if nav:
        rows.append(nav)

    if not cards:
        text = "📋 Открытых задач нет. Нажми «➕ Задача», чтобы создать."
    else:
        text = f"📋 Открытых задач: {len(cards)}"
        if pages > 1:
            text += f" · стр. {page + 1}/{pages}"
        text += "\nВыбери задачу — открою карточку."
    return View(text, rows)


def render_card(state, vault, token, focus=None, reminder=None) -> View:
    path = resolve_token(state, token)
    if not path:
        return _stale(state)
    candidate = Path(path)
    if not _safe_task_path(candidate, vault) or not candidate.is_file():
        return _stale(state)
    try:
        text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _stale(state)

    status = frontmatter.get(text, "status") or "open"
    title = _task_title(text, candidate.stem)
    lines = [f"📌 {title}", "", f"Статус: {STATUS_LABELS.get(status, status)}"]

    next_step = frontmatter.get(text, "nextStep")
    if next_step:
        lines.append(f"Шаг: {textwrap.shorten(next_step, width=80, placeholder='…')}")
    estimate = _human_minutes(frontmatter.get(text, "timeEstimate"))
    if estimate:
        lines.append(f"Оценка: {estimate}")
    priority = frontmatter.get(text, "priority")
    if priority and priority != "normal":
        lines.append(f"Приоритет: {priority}")

    # Фокус-часть приходит от чистого focus_ui, reminder-часть — от reminder_ui.
    # Без них показываются кнопки по умолчанию.
    reminder_rows = []
    if reminder is not None:
        if reminder.text:
            lines.append("")
            lines.append(reminder.text)
        reminder_rows = list(reminder.rows)
    else:
        reminder_rows = [[("🔔 Напомнить", f"r:d:{token}")]]

    focus_rows = []
    if focus is not None:
        if focus.text:
            lines.append("")
            lines.append(focus.text)
        focus_rows = list(focus.rows)
    else:
        focus_rows = [[("⏱ Фокус", f"q:d:{token}")]]

    page = state.get(PAGE_KEY, 0)
    rows = reminder_rows + focus_rows + [
        [("📥 В список", f"s:{token}:open"), ("➡️ Дальше", f"s:{token}:next")],
        [("🔥 В работу", f"s:{token}:in-progress"), ("✅ Готово", f"s:{token}:done")],
        [("⬅️ Назад", f"l:{page}")],
    ]
    return View("\n".join(lines), rows)


# ── Точная мутация и синхронизация ──────────────────────────────────────────

def apply_status(state, vault, token, target, sync=None, now=None, focus=None,
                 reminder=None):
    """Меняет статус ровно выбранной карточки. Возвращает (View, changed).

    `sync` вызывается только при реальном изменении: на stale-токене, чужой
    карточке, повреждённой шапке и повторном done push не выполняется.
    """
    if target not in STATUS_TARGETS:
        return _stale(state), False
    path = resolve_token(state, token)
    if not path:
        return _stale(state), False
    candidate = Path(path)
    if not _safe_task_path(candidate, vault) or not candidate.is_file():
        return _stale(state), False

    import dispatch  # тяжёлый модуль — только когда реально меняем статус
    changed, message = dispatch.set_task_status(candidate, target, now=now, vault=vault)
    if changed and sync is not None:
        sync()

    view = render_card(state, vault, token, focus=focus, reminder=reminder)
    if view.stale:
        view = View(message, view.rows, stale=True)
    else:
        view = View(message + "\n\n" + view.text, view.rows)
    return view, changed


def submit_task(state, raw_title, vault=None, sync=None, now=None,
                source: str = "Telegram") -> Submission:
    title, error = validate_title(raw_title)
    if error:
        return Submission(False, error, False)
    import dispatch
    try:
        path, created = dispatch.create_task_exact(
            title, now=now, vault=vault, source=source)
    except Exception:
        return Submission(False, "Не смог создать задачу — попробуй ещё раз или нажми «Отмена».", False)
    if path is None:
        # Сбой записи: режим остаётся, следующий текст — новая попытка.
        return Submission(False, "Не смог создать задачу — попробуй ещё раз.", False)
    cancel(state)
    if not created:
        return Submission(True, f"Такая задача уже есть: {title}", False, str(path))
    if sync is not None:
        sync()
    return Submission(True, f"✅ Задача создана: {title}", True, str(path))


def task_from_thought(state, vault, token, sync=None, now=None) -> Submission:
    """`➡️ В задачу`: ровно одна задача из выбранной мысли.

    Исходная мысль не удаляется и не редактируется. Повторный тап по тому же
    токену не создаёт дубль: `create_task_exact` узнаёт уже существующую
    карточку и возвращает её с `created=False` — тогда sync не запускается.
    """
    identity = resolve_thought_token(state, token)
    thought = thought_feed.load_thought(vault, identity) if identity else None
    if thought is None:
        return Submission(False, "⚠️ Мысль устарела — открой «🧠 Мысли» заново.", False)
    title = thought.title[:MAX_TITLE_LEN].strip()
    if not title:
        return Submission(False, "У мысли нет названия для задачи.", False)
    import dispatch
    try:
        path, created = dispatch.create_task_exact(
            title, now=now, vault=vault, source=THOUGHT_TASK_SOURCE)
    except Exception:
        return Submission(False, "Не смог создать задачу — попробуй ещё раз.", False)
    if path is None:
        return Submission(False, "Не смог создать задачу — попробуй ещё раз.", False)
    if not created:
        return Submission(True, f"Такая задача уже есть: {title}", False, str(path))
    if sync is not None:
        sync()
    return Submission(True, f"✅ Задача создана: {title}", True, str(path))


def submit_note(state, raw_text, save=None, sync=None) -> Submission:
    text = (raw_text or "").strip()
    if not text:
        return Submission(False, "Пустая заметка — ничего не записал.", False)
    if save is None:
        import dispatch
        save = dispatch.save_note_result
    try:
        result = save(text)
    except Exception:
        return Submission(False, "Не смог записать заметку — попробуй ещё раз или нажми «Отмена».", False)
    cancel(state)
    message = getattr(result, "text", None) or "Заметка сохранена"
    if getattr(result, "private", False):
        # Приватный маршрут никогда не запускает tracked git sync.
        return Submission(True, message, False)
    changed = bool(getattr(result, "mutated", False))
    if changed and sync is not None:
        sync()
    return Submission(True, message, changed)


def submit_video(state, raw_text, enqueue=None, sync=None) -> Submission:
    url, error = classify_video(raw_text)
    if error:
        return Submission(False, error, False)
    if enqueue is None:
        import dispatch
        enqueue = dispatch.save_video_result
    try:
        result = enqueue(url)
    except Exception:
        return Submission(False, "Не смог поставить видео в очередь — попробуй ещё раз или нажми «Отмена».", False)
    cancel(state)
    message = getattr(result, "text", None) or "Видео добавлено в очередь"
    changed = bool(getattr(result, "mutated", False))
    if changed and sync is not None:
        sync()
    return Submission(True, message, changed)
