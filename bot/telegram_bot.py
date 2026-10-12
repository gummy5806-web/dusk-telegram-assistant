import os
import re
import asyncio
import contextlib
import tempfile
import logging
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

load_dotenv(Path(__file__).parent.parent / ".env")
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# httpx пишет в лог полный URL запроса, а в нём лежит токен бота:
#   POST https://api.telegram.org/bot<TOKEN>/getUpdates
# Это утечка секрета в файл лога. Гасим транспортные логи до предупреждений —
# свои сообщения бот по-прежнему пишет через logger выше.
for noisy in ("httpx", "httpcore", "telegram.ext.Application",
              "telegram.ext.Updater", "telegram.request"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

import learn
import ask
import market
import task_ui as ui
import assistant_router
import personal_search
import resource_ui
import queue_store
import speech_to_text
import dispatch as _dispatch
import runtime_store
import thought_feed
import focus
import focus_ui
import focus_scheduler
import reminder_nlp
import reminder_ui
import reminder_scheduler
import reminders
import personal_summary
import thought_digest
import thought_digest_jobs
from thought_digest_delivery import DeliveryLedger
from vault_ops import (
    create_fleeting_note, append_to_inbox,
    get_pending_tasks, git_sync, git_push
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER = int(os.getenv("TELEGRAM_USER_ID", "0"))

# --- Кнопки ---------------------------------------------------------------
# Подписи живут в чистом `task_ui`: их же проверяют юнит-тесты, и они же
# сравниваются с текстом входящего сообщения. Меняешь подпись — меняешь одну
# константу, и обработчик не перестаёт срабатывать.
BTN_LEFT = "📋 Что осталось"          # старая кнопка: остаётся рабочим fallback
BTN_INBOX = "📥 Разобрать инбокс"     # старая кнопка: остаётся рабочим fallback
BTN_REPORT = ui.BTN_STATUS
BTN_HEALTH = ui.BTN_HEALTH


MENU_REMINDER = "🔔 Напомнить"
MENU_ACTIONS = (
    ((ui.BTN_THOUGHT, "m:thought"), (ui.BTN_THOUGHTS, "m:thoughts")),
    ((ui.BTN_NEW_TASK, "m:new"), (ui.BTN_TASKS, "m:tasks")),
    ((MENU_REMINDER, "m:remind"), (ui.BTN_STATUS, "m:status")),
    (("💡 Разбор мыслей", "m:digest"), (ui.BTN_MATERIAL, "m:material")),
    (("🔖 Ресурсы", "m:resources"),),
)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data) for label, data in row]
        for row in MENU_ACTIONS
    ])


MAIN_KEYBOARD = main_keyboard()


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(ui.BTN_CANCEL, callback_data="x")]]
    )


def auth_check(update: Update) -> bool:
    user = update.effective_user
    return ui.authorized(user.id if user else None, ALLOWED_USER)


def _vault() -> Path:
    return Path(os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))


def _now() -> datetime:
    return datetime.now(timezone.utc)


_STORE = None


def _runtime_store():
    """Ленивое операционное состояние. Недоступная база — не падение бота.

    Ошибка инициализации явно логируется по типу; хаб мыслей при этом всё равно
    рисуется, просто без фильтра скрытых и без cooldown.
    """
    global _STORE
    if _STORE is None:
        try:
            _STORE = runtime_store.RuntimeStore(runtime_store.default_db_path())
        except runtime_store.RuntimeStoreError as error:
            logger.error("runtime store unavailable: %s", type(error).__name__)
            return None
    return _STORE


def _inline(view: ui.View):
    rows = [
        [InlineKeyboardButton(label, url=data[4:]) if data.startswith("url:")
         else InlineKeyboardButton(label, callback_data=data) for label, data in row]
        for row in view.rows
    ]
    if rows:
        rows.append([InlineKeyboardButton("🏠 Меню", callback_data="m:home")])
    return InlineKeyboardMarkup(rows) if rows else None


def _notification_markup(notification):
    rows = [[InlineKeyboardButton(label, callback_data=data)
             for label, data in row] for row in notification.buttons]
    return InlineKeyboardMarkup(rows) if rows else None


def _card_view(state, token, now=None) -> ui.View:
    """Карточка задачи с актуальной фокус- и reminder-частью."""
    reminder_view = reminder_ui.card_reminder(state, _vault(), token, now=now)
    focus_view = focus_ui.card_focus(
        state, _vault(), _runtime_store(), token, now=now)
    return ui.render_card(state, _vault(), token, focus=focus_view,
                          reminder=reminder_view)


def _wake_focus(context) -> None:
    """Разбудить scheduler после start/extend/stop/done без ожидания тика."""
    application = getattr(context, "application", None)
    bot_data = getattr(application, "bot_data", None)
    wake = bot_data.get("focus_wake") if bot_data is not None else None
    if wake is not None:
        wake.set()


def _wake_reminder(context) -> None:
    """Разбудить reminder scheduler после create/snooze/remove/done."""
    application = getattr(context, "application", None)
    bot_data = getattr(application, "bot_data", None)
    wake = bot_data.get("reminder_wake") if bot_data is not None else None
    if wake is not None:
        wake.set()


def _synced(message: str) -> None:
    """Существующий git-persistence path с нейтральным сообщением коммита.

    Сначала `git_sync()` (pull --rebase --autostash): VPS может отставать от
    remote, и push без rebase упал бы. Затем `git_push(message)`.
    """
    try:
        git_sync()
        git_push(message)
    except Exception as error:  # ошибка синхронизации не ломает ответ человеку
        logger.error("git sync failed: %s", type(error).__name__)


def _sync_task() -> None:
    _synced(ui.SYNC_MESSAGES["task"])


def _sync_status() -> None:
    _synced(ui.SYNC_MESSAGES["status"])


def _sync_note() -> None:
    _synced(ui.SYNC_MESSAGES["note"])


def _sync_video() -> None:
    _synced(ui.SYNC_MESSAGES["video"])


def _sync_focus() -> None:
    _synced(ui.SYNC_MESSAGES["focus"])


def _sync_reminder() -> None:
    _synced(ui.SYNC_MESSAGES["reminder"])


def _page(raw: str) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def health_text() -> str:
    """Тот же renderer, что у команды «здоровье» в dispatch: только чтение."""
    import health
    return health.report(_vault())[:4000]


def _related_hint(text: str) -> str:
    """Одна действительно похожая старая мысль после захвата. Иначе пусто.

    Никакой модели и сети: только детерминированное пересечение значимых слов,
    ограниченное суточным cooldown в runtime store. Без доступного состояния
    cooldown проверить нельзя, поэтому лучше промолчать, чем показывать похожую
    мысль на каждом захвате.
    """
    store = _runtime_store()
    if store is None:
        return ""
    try:
        thought = thought_feed.related_thought(
            _vault(), text, store=store, now=_now())
    except runtime_store.RuntimeStoreError as error:
        logger.warning("related thought failed: %s", type(error).__name__)
        return ""
    if thought is None:
        return ""
    return f"💡 Похоже на старую мысль: {thought.title}"


def _clear_inputs(state):
    ui.cancel(state)
    reminder_ui.clear_custom(state)
    ui.clear_thought_edit(state)
    ui.clear_thought_delete(state)
    state.pop("menu_reminder_input", None)
    state.pop("digest_edit_return", None)
    resource_ui.clear(state)
    state.pop("resource_add", None)
    state.pop("resource_search", None)


async def _remove_legacy_keyboard(update, context):
    if not context.user_data.get("inline_menu_ready"):
        await update.effective_message.reply_text(
            "Кнопки теперь прямо в сообщениях. Пиши текстом или голосом.",
            reply_markup=ReplyKeyboardRemove(), disable_notification=True)
        context.user_data["inline_menu_ready"] = True


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    _clear_inputs(context.user_data)
    await _remove_legacy_keyboard(update, context)
    await update.effective_message.reply_text("Что сделать?", reply_markup=MAIN_KEYBOARD)


def _reminder_picker(state, page=0):
    view = ui.render_list(state, _vault(), page=page, root=(page == 0))
    rows = []
    for row in view.rows:
        converted = []
        for label, data in row:
            if data.startswith("t:"):
                converted.append(("🔔 " + label, "r:d:" + data[2:]))
            elif data.startswith("l:"):
                converted.append((label, "m:rp:" + data[2:]))
        if converted:
            rows.append(converted)
    rows.append([("➕ Новое напоминание", "m:remindnew")])
    return ui.View("🔔 Выбери задачу — затем время.\n\n" + view.text, rows)


async def _handle_menu_callback(update, context, data):
    query = update.callback_query
    state = context.user_data
    action = data[2:]
    _clear_inputs(state)
    modes = {"thought": ui.MODE_NOTE, "new": ui.MODE_TASK, "material": ui.MODE_VIDEO}
    if action in modes:
        prompt = ui.begin(state, modes[action])
        await ui.safe_edit(query, prompt, cancel_keyboard())
        return
    if action == "home":
        await ui.safe_edit(query, "Что сделать?", MAIN_KEYBOARD)
        return
    if action == "status":
        text = await asyncio.to_thread(_personal_status_text)
        await ui.safe_edit(query, text, MAIN_KEYBOARD)
        return
    if action == "digest":
        view = _digest_period_view()
    elif action == "resources":
        view = await asyncio.to_thread(resource_ui.list_view, _vault())
    elif action == "tasks":
        view = await asyncio.to_thread(ui.render_list, state, _vault(), root=True)
    elif action == "thoughts":
        view = await asyncio.to_thread(ui.render_thoughts, state, _vault(),
                                     store=_runtime_store(), now=_now())
    elif action == "remind":
        view = ui.View("🔔 О чём напомнить?", [
            [("➕ Новое напоминание", "m:remindnew")],
            [("📋 К существующей задаче", "m:remindtasks")],
        ])
    elif action == "remindnew":
        state["menu_reminder_input"] = True
        await ui.safe_edit(query,
            "🔔 Напиши, о чём и когда напомнить, текстом или голосом.\n"
            "Например: завтра в 9 отправить отчёт.", cancel_keyboard())
        return
    elif action == "remindtasks" or action.startswith("rp:"):
        page = _page(action[3:]) if action.startswith("rp:") else 0
        view = await asyncio.to_thread(_reminder_picker, state, page)
    else:
        await ui.safe_edit(query, "Кнопка устарела. Выбери действие.", MAIN_KEYBOARD)
        return
    await ui.safe_edit(query, view.text, _inline(view) or MAIN_KEYBOARD)


_DIGEST_PERIODS = {"today": "сегодня", "yesterday": "вчера", "week": "за неделю", "recent": "последние мысли"}


def _digest_period_view():
    return ui.View("💡 Разбор мыслей\n\nСоберу, что ты хотел, связанные идеи, следующий шаг и вопросы.\nВыбери период. Выводы — предложения; записи сами не меняются.", [
        [("Сегодня", "dg:p:today"), ("Вчера", "dg:p:yesterday")],
        [("За неделю", "dg:p:week"), ("Последние", "dg:p:recent")],
    ])


def _sync_digest(job=None):
    vault = _vault()
    if job is not None:
        path = vault / "SYSTEM/THOUGHT_DIGEST/requests" / (job["id"] + ".json")
        if not thought_digest_jobs.sync_file(vault, path, message="thought digest: request"):
            raise RuntimeError("Digest synchronization unavailable")
        return
    # Retry a locally saved request if its first push failed, then fetch results.
    requests = thought_digest_jobs.requests(vault, limit=2000)
    if not requests:
        return
    for request in requests:
        if thought_digest_jobs.get_result(vault, request["id"]) is None:
            path = vault / "SYSTEM/THOUGHT_DIGEST/requests" / (request["id"] + ".json")
            if not thought_digest_jobs.sync_file(vault, path, message="thought digest: request"):
                raise RuntimeError("Digest synchronization unavailable")
            break
    else:
        import subprocess
        completed = subprocess.run(["git", "pull", "--rebase", "--autostash", "--quiet"],
                                   cwd=vault, capture_output=True, timeout=45)
        if completed.returncode:
            raise RuntimeError("Digest result fetch unavailable")


async def _request_digest(update, context, period):
    if period not in _DIGEST_PERIODS:
        await update.effective_message.reply_text("Выбери период разбора.", reply_markup=_inline(_digest_period_view()))
        return
    _clear_inputs(context.user_data)
    job = None
    try:
        key = "telegram:" + str(getattr(update, "update_id", ""))
        if not getattr(update, "update_id", None):
            key = ""
        job = await asyncio.to_thread(thought_digest_jobs.enqueue, _vault(), period, key=key, now=_now())
        await asyncio.to_thread(_sync_digest, job)
        text = ("💡 Разбираю мысли: " + _DIGEST_PERIODS[period] + ".\n"
                "Ответ пришлю сюда. Обычно это занимает пару минут; если Mac спит — после пробуждения.\n"
                "Разбор ничего не удаляет и не создаёт задач автоматически.")
    except Exception as error:
        logger.warning("digest request failed: %s", type(error).__name__)
        text = ("Запрос сохранён, но пока не удалось передать его на Mac. Повторю автоматически." if job
                else "Не удалось поставить разбор в очередь. Записи сохранены; попробуй ещё раз.")
    wake = context.bot_data.get("digest_wake")
    if wake is not None:
        wake.set()
    query = getattr(update, "callback_query", None)
    if query is not None and (query.data or "").startswith("dg:"):
        await ui.safe_edit(query, text, MAIN_KEYBOARD)
    else:
        await update.effective_message.reply_text(text, reply_markup=MAIN_KEYBOARD)


def _digest_result_view(job_id, result):
    text = thought_digest.render(result)
    rows = []
    for source in result.get("snapshot", {}).get("sources", [])[:45]:
        source_id = source.get("id", "")
        if not re.fullmatch(r"s[1-9][0-9]{0,2}", source_id):
            continue
        label = source_id[1:] + ". " + " ".join(source.get("title", "Мысль").split())[:42]
        rows.append([(label, "dg:o:" + job_id + ":" + source_id)])
    rows.append([("💡 Другой период", "m:digest")])
    return ui.View(text, rows)


def _digest_return_view(view, job_id):
    if not re.fullmatch(r"[a-f0-9]{24}", job_id or ""):
        return view
    return ui.View(view.text, [list(row) for row in view.rows] + [
        [("⬅️ К этому разбору", "dg:r:" + job_id)]], stale=view.stale)


def _bind_digest_message(state, message, job_id):
    message_id = getattr(message, "message_id", None)
    if type(message_id) is not int or message_id <= 0:
        return
    messages = state.setdefault("digest_messages", {})
    messages[str(message_id)] = job_id
    while len(messages) > 64:
        del messages[next(iter(messages))]


def _message_digest(state, message):
    # Inline markup survives a bot restart; the bounded map also supports
    # messages returned by transports without their markup populated.
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", ()) or ():
        for button in row:
            match = re.fullmatch(r"dg:r:([a-f0-9]{24})", button.callback_data or "")
            if match:
                return match.group(1)
    return state.get("digest_messages", {}).get(str(getattr(message, "message_id", None)), "")


async def _handle_digest_callback(update, context, data):
    _clear_inputs(context.user_data)
    if data.startswith("dg:p:"):
        await _request_digest(update, context, data[5:])
        return
    match = re.fullmatch(r"dg:r:([a-f0-9]{24})", data)
    if match:
        saved = await asyncio.to_thread(thought_digest_jobs.get_result, _vault(), match.group(1))
        if saved:
            view = _digest_result_view(match.group(1), saved["result"])
            context.user_data.get("digest_messages", {}).pop(
                str(getattr(update.callback_query.message, "message_id", None)), None)
            await ui.safe_edit(update.callback_query, view.text, _inline(view))
            return
    match = re.fullmatch(r"dg:o:([a-f0-9]{24}):(s[1-9][0-9]{0,2})", data)
    if match:
        saved = await asyncio.to_thread(thought_digest_jobs.get_result, _vault(), match.group(1))
        sources = saved.get("result", {}).get("snapshot", {}).get("sources", []) if saved else []
        source = next((source for source in sources if source.get("id") == match.group(2)), None)
        if source:
            token = ui.issue_thought_token(context.user_data, source["identity"])
            view = await asyncio.to_thread(ui.render_thought, context.user_data, _vault(), _runtime_store(), token, now=_now())
            view = _digest_return_view(view, match.group(1))
            message = await update.callback_query.message.reply_text(view.text, reply_markup=_inline(view))
            _bind_digest_message(context.user_data, message, match.group(1))
            return
    await ui.safe_edit(update.callback_query, "Этот разбор недоступен. Открой новый.", MAIN_KEYBOARD)


async def _digest_tick(application, ledger):
    requests = await asyncio.to_thread(thought_digest_jobs.requests, _vault(), limit=2000)
    unsent = [request for request in requests if not await asyncio.to_thread(ledger.contains, request["id"])]
    if not unsent:
        return
    await asyncio.to_thread(_sync_digest)
    for request in unsent[:5]:
        saved = await asyncio.to_thread(thought_digest_jobs.get_result, _vault(), request["id"])
        if saved is None:
            continue
        view = _digest_result_view(request["id"], saved["result"])
        await application.bot.send_message(chat_id=ALLOWED_USER, text=view.text,
                                           reply_markup=_inline(view), disable_notification=False)
        await asyncio.to_thread(ledger.mark, request["id"])


async def _digest_loop(application, stop, wake):
    # Separate local DB: durable acknowledgements, no personal text and no change
    # to the existing focus/reminder runtime schema.
    ledger = DeliveryLedger(runtime_store.default_db_path().with_name("thought-digest-delivery.sqlite3"))
    while not stop.is_set():
        try:
            await asyncio.wait_for(wake.wait(), timeout=60)
        except asyncio.TimeoutError:
            logger.debug("digest poll interval")
        wake.clear()
        if stop.is_set():
            break
        try:
            await _digest_tick(application, ledger)
        except Exception as error:
            logger.warning("digest delivery deferred: %s", type(error).__name__)


async def _maybe_menu_reminder(update, context, text):
    if not context.user_data.get("menu_reminder_input"):
        return False
    candidate = text if reminder_nlp.parse_intent(text, now=_now()).kind != "none" else "напомни " + text
    parsed = reminder_nlp.parse_intent(candidate, now=_now())
    if parsed.kind != "ok":
        await update.message.reply_text(
            "⚠️ " + (parsed.error or reminder_nlp.NO_TIME), reply_markup=cancel_keyboard())
        return True
    result = await asyncio.to_thread(reminder_ui.natural_action, context.user_data,
        _vault(), _runtime_store(), parsed.intent, f"tg:{update.update_id}", _now())
    if result.sync_required:
        await asyncio.to_thread(_sync_reminder)
    if not result.view.stale and not result.view.text.startswith("⚠️"):
        context.user_data.pop("menu_reminder_input", None)
    _wake_reminder(context)
    await update.message.reply_text(result.view.text,
                                   reply_markup=_inline(result.view) or MAIN_KEYBOARD)
    return True


async def _reply_task_submission(update, context, result):
    if result.ok and result.path:
        path = Path(result.path)
        try:
            relative = path.relative_to(_vault()).as_posix() if path.is_absolute() else path.as_posix()
        except ValueError:
            relative = ""
        if relative:
            token = ui.issue_token(context.user_data, str(path if path.is_absolute() else _vault() / path))
            view = await asyncio.to_thread(_card_view, context.user_data, token, _now())
            await update.message.reply_text(result.text + "\n\n" + view.text,
                                           reply_markup=_inline(view) or MAIN_KEYBOARD)
            return
    await update.message.reply_text(result.text, reply_markup=_keyboard(ui.keyboard_for(result)))


async def _reply_saved_thought(update, context, text, path="", line="", digest_job=""):
    view = await asyncio.to_thread(ui.saved_thought_view, context.user_data, _vault(), path=path, line=line)
    view = _digest_return_view(view, digest_job)
    message = await update.message.reply_text(text, reply_markup=_inline(view) or MAIN_KEYBOARD)
    if digest_job:
        _bind_digest_message(context.user_data, message, digest_job)


async def _save_resource(update, context, text):
    view = await asyncio.to_thread(resource_ui.capture, _vault(), text, sync=_sync_note)
    if not view.stale:
        _clear_inputs(context.user_data)
    await update.message.reply_text(view.text, reply_markup=_inline(view) or MAIN_KEYBOARD)


async def _handle_resource_callback(update, context, data):
    state = context.user_data
    if not data.startswith("rs:z:"):
        _clear_inputs(state)
    if data == "rs:a":
        state["resource_add"] = True
        view = ui.View("🔖 Пришли ссылку и, если хочешь, пояснение: зачем она тебе.",
                       [[("Отмена", "m:resources")]])
    elif data == "rs:s":
        state["resource_search"] = True
        view = ui.View("🔎 Что найти среди сохранённых ресурсов? Напиши обычными словами.",
                       [[("Отмена", "m:resources")]])
    else:
        view = await asyncio.to_thread(resource_ui.callback, _vault(), state, data, sync=_sync_note)
    await ui.safe_edit(update.callback_query, view.text, _inline(view) or MAIN_KEYBOARD)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        await update.message.reply_text("Доступ запрещён.")
        return
    _clear_inputs(context.user_data)
    await _remove_legacy_keyboard(update, context)
    await update.message.reply_text(
        "Привет. Пиши обычными словами — можно текстом или голосом.\n"
        "Кнопки под этим сообщением. Нажми нужную, затем пришли только содержание.\n\n"
        "💭 Мысль — сохранить мысль заметкой.\n"
        f"{ui.BTN_THOUGHTS} — Inbox, последние и забытые мысли; внутри есть поиск.\n"
        "➕ Задача — создать ровно одну задачу (следующее — название).\n"
        "📋 Задачи — открыть список и менять статус кнопками.\n"
        "📋 Задачи → ⏱ Фокус — запустить блок 15/25/45/60/90 мин.\n"
        "🎓 Материал — видео в очередь на конспект, сайт в сохранённые ресурсы.\n"
        "🔖 Ресурсы — сайты и инструменты: открыть, найти, исправить или удалить.\n"
        "📊 Статус — показать сводку.\n\n"
        "Просто текст без кнопки я сохраню в Inbox. Дальше мысль можно разобрать "
        "в «🧠 Мысли» или превратить в задачу кнопкой. «создай задачу …» создаст "
        "задачу. «что я думал про …» найдёт в Dusk. Вопрос про автора или термин "
        "дополнительно посмотрит агентские конспекты Research Brain.\n\n"
        "🔔 Напомнить есть в этом меню и в карточке задачи. Фраза тоже работает: "
        "«напомни завтра в 9 отправить отчёт».",
        reply_markup=MAIN_KEYBOARD
    )


def _keyboard(screen: str):
    return cancel_keyboard() if screen == ui.SCREEN_CANCEL else MAIN_KEYBOARD


async def _show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Новый корневой список инвалидирует токены предыдущего списка.
    view = ui.render_list(context.user_data, _vault(), root=True)
    await update.message.reply_text(view.text, reply_markup=_inline(view))


async def _show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сводка собирается на момент нажатия из локальных фактов. Модель/сеть не
    # вызываются, файл не пишется; блокирующее чтение — не в event loop.
    text = await asyncio.to_thread(_personal_status_text)
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


def _personal_status_text() -> str:
    summary = personal_summary.collect(_vault(), _runtime_store(), _now())
    return personal_summary.render(summary)


async def _show_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сознательно НЕ зовём модель: личное не уходит сторонним API.
    # Разбор делает Mac через свой Vertex — там, где свой GCP.
    inbox = _vault() / "ZETA" / "FLEETING" / "00 Инбокс.md"
    lines = [l for l in inbox.read_text(encoding="utf-8").splitlines() if l.startswith("- ")] if inbox.exists() else []
    if not lines:
        await update.message.reply_text("📥 Инбокс пуст.", reply_markup=MAIN_KEYBOARD)
        return

    def _strip_stamp(line):
        return re.sub(r"^- \*\*\d{2}:\d{2}\*\*\s*", "", line)[:60]

    preview = "\n".join("• " + _strip_stamp(l) for l in lines[:8])
    more = f"\n…и ещё {len(lines) - 8}" if len(lines) > 8 else ""
    await update.message.reply_text(
        f"📥 В инбоксе {len(lines)}:\n{preview}{more}\n\n"
        "Разбор сделает Mac через твой Vertex (свой GCP). "
        "Если Mac спит — разберётся, когда проснётся.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    text = update.message.text
    state = context.user_data
    await _remove_legacy_keyboard(update, context)
    if (text or "").strip().casefold() in ("меню", "menu"):
        await handle_menu(update, context)
        return
    if text == "🔖 Ресурсы":
        _clear_inputs(state)
        view = await asyncio.to_thread(resource_ui.list_view, _vault())
        await update.message.reply_text(view.text, reply_markup=_inline(view))
        return
    if text == MENU_REMINDER:
        _clear_inputs(state)
        state["menu_reminder_input"] = True
        await update.message.reply_text("🔔 Напиши, о чём и когда напомнить. Например: завтра в 9 отправить отчёт.",
                                        reply_markup=cancel_keyboard())
        return

    # Main buttons и Отмена сбрасывают custom-reminder ввод, обычный текст — нет.
    if text in ui.MAIN_BUTTONS or text in (
            ui.BTN_SEARCH, ui.BTN_NOTE, ui.BTN_VIDEO, BTN_LEFT, BTN_INBOX,
            ui.BTN_CANCEL, "отмена", "cancel", "📊 Отчёт за день"):
        _clear_inputs(state)

    # --- Главная клавиатура: кнопки важнее режима, ими из него выходят. -----
    if text == ui.BTN_THOUGHT or text == ui.BTN_NOTE:
        await update.message.reply_text(ui.begin(state, ui.MODE_NOTE),
                                        reply_markup=cancel_keyboard())
        return

    if text == ui.BTN_THOUGHTS:
        ui.cancel(state)
        view = ui.render_thoughts(state, _vault(), store=_runtime_store(), now=_now())
        await update.message.reply_text(view.text, reply_markup=_inline(view))
        return

    if text == ui.BTN_SEARCH:
        # Легаси-алиас: старая клавиатура у клиента продолжает работать.
        await update.message.reply_text(ui.begin(state, ui.MODE_SEARCH),
                                        reply_markup=cancel_keyboard())
        return

    if text == ui.BTN_NEW_TASK:
        await update.message.reply_text(ui.begin(state, ui.MODE_TASK),
                                        reply_markup=cancel_keyboard())
        return

    if text == ui.BTN_TASKS:
        ui.cancel(state)
        await _show_tasks(update, context)
        return

    if text == ui.BTN_MATERIAL or text == ui.BTN_VIDEO:
        await update.message.reply_text(ui.begin(state, ui.MODE_VIDEO),
                                        reply_markup=cancel_keyboard())
        return

    if text in (ui.BTN_STATUS, "📊 Отчёт за день"):
        ui.cancel(state)
        await _show_status(update, context)
        return

    if text == ui.BTN_HEALTH or (text or "").strip().lower() in ("здоровье", "health"):
        ui.cancel(state)
        await update.message.reply_text(health_text(), reply_markup=MAIN_KEYBOARD)
        return

    # Старые кнопки продолжают работать, даже если клавиатура уже обновилась.
    if text == BTN_LEFT:
        ui.cancel(state)
        await update.message.reply_text(get_pending_tasks(), reply_markup=MAIN_KEYBOARD)
        return

    if text == BTN_INBOX:
        ui.cancel(state)
        await _show_inbox(update, context)
        return

    if (text or "").strip() in (ui.BTN_CANCEL, "отмена", "cancel"):
        ui.cancel(state)
        await update.message.reply_text("Отменено.", reply_markup=MAIN_KEYBOARD)
        return

    # Всё остальное — общий content-путь (тот же, что у голоса).
    await handle_content(update, context, text, origin="text")


async def _maybe_reminder_custom(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE,
                                 text: str) -> bool:
    """«Другое время»: следующее сообщение — время напоминания."""
    state = context.user_data
    if not reminder_ui.custom_pending(state):
        return False
    result = await asyncio.to_thread(
        reminder_ui.submit_custom, state, _vault(), _runtime_store(), text,
        f"tg:{update.update_id}", _now())
    if result.sync_required:
        await asyncio.to_thread(_sync_reminder)
    _wake_reminder(context)
    await update.message.reply_text(
        result.view.text, reply_markup=_inline(result.view) or MAIN_KEYBOARD)
    return True


async def _maybe_reminder_intent(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE,
                                 text: str) -> bool:
    """Typed: none=fall through, error/missing=error reply, ok=create."""
    parsed = reminder_nlp.parse_intent(text, now=_now())
    if parsed.kind == "none":
        return False
    if parsed.kind in ("error", "missing_subject"):
        await update.message.reply_text(f"⚠️ {parsed.error}",
                                        reply_markup=MAIN_KEYBOARD)
        return True
    result = await asyncio.to_thread(
        reminder_ui.natural_action, context.user_data, _vault(),
        _runtime_store(), parsed.intent, f"tg:{update.update_id}", _now())
    if result.sync_required:
        await asyncio.to_thread(_sync_reminder)
    _wake_reminder(context)
    await update.message.reply_text(
        result.view.text, reply_markup=_inline(result.view) or MAIN_KEYBOARD)
    return True


async def handle_content(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         text: str, origin: str = "text") -> None:
    """Единый путь содержания: режимы кнопок, затем свободный Dusk-first роутер.

    Его вызывают и `handle_text`, и `handle_voice`: расшифрованный голос ничем не
    отличается от текста, а `origin` нужен только для честного ответа.
    """
    text = (text or "").strip()
    state = context.user_data

    if await _maybe_menu_reminder(update, context, text):
        return
    if resource_ui.edit_pending(state):
        view = await asyncio.to_thread(resource_ui.submit_edit, _vault(), state, text, sync=_sync_note)
        await update.message.reply_text(view.text, reply_markup=_inline(view) or MAIN_KEYBOARD)
        return
    if state.get("resource_search"):
        state.pop("resource_search", None)
        view = await asyncio.to_thread(resource_ui.search_view, _vault(), text)
        await update.message.reply_text(view.text, reply_markup=_inline(view) or MAIN_KEYBOARD)
        return
    if state.get("resource_add"):
        await _save_resource(update, context, text)
        return
    if ui.thought_edit_pending(state):
        digest_job = state.get("digest_edit_return", "")
        result = await asyncio.to_thread(ui.submit_thought_edit, state, _vault(), text, sync=_sync_note)
        if result.ok:
            state.pop("digest_edit_return", None)
            await _reply_saved_thought(update, context, result.text, path=result.path, digest_job=digest_job)
        else:
            view = _digest_return_view(ui.View(result.text, [[("Отмена", "x")]]), digest_job)
            message = await update.message.reply_text(result.text, reply_markup=_inline(view))
            if digest_job:
                _bind_digest_message(state, message, digest_job)
        return
    if await _maybe_reminder_custom(update, context, text):
        return

    def search_answer(subject: str, research: bool) -> str:
        hits = personal_search.search(subject, vault=_vault())
        if research:
            return personal_search.render_combined(hits, ask.answer(subject))
        return personal_search.render(hits)

    # --- Режимы ожидания: следующее сообщение — это содержание. -------------
    current = ui.mode(state)
    if current == ui.MODE_TASK:
        result = await asyncio.to_thread(ui.submit_task, state, text, vault=_vault(), sync=_sync_task)
        await _reply_task_submission(update, context, result)
        return

    if current == ui.MODE_NOTE:
        result = await asyncio.to_thread(ui.submit_note, state, text, sync=_sync_note)
        if result.ok:
            await _reply_saved_thought(update, context, result.text, path=result.path)
        else:
            await update.message.reply_text(result.text, reply_markup=cancel_keyboard())
        return

    if current == ui.MODE_VIDEO:
        links = re.findall(r"https?://[^\s<>]+", text, re.IGNORECASE)
        if len(links) > 1 and any(queue_store.is_video_url(link) for link in links):
            await update.message.reply_text("Пришли видео отдельным сообщением, а сайты — следующим. Здесь ничего не сохранил.",
                                            reply_markup=cancel_keyboard())
            return
        if re.search(r"https?://", text) and ui.classify_video(text)[1]:
            await _save_resource(update, context, text)
            return
        result = ui.submit_video(state, text, sync=_sync_video)
        await update.message.reply_text(
            result.text, reply_markup=_keyboard(ui.keyboard_for(result)))
        return

    if current == ui.MODE_SEARCH:
        ui.cancel(state)
        probe = assistant_router.route(text)
        await update.message.reply_text(
            search_answer(probe.subject or text, probe.research),
            reply_markup=MAIN_KEYBOARD)
        return

    period = thought_digest.parse_request(text)
    if period is not None:
        await _request_digest(update, context, period)
        return

    # --- Совместимость: старые явные префиксы и power-user команды. ---------
    # Всё, что меняет файлы, идёт через общий dispatch и получает neutral sync
    # ровно один раз при реальной не-private мутации.
    if _dispatch.shared_prefix(text):
        result = _dispatch.dispatch_detailed(text)
        ui.mutation_sync(result, _synced)
        await update.message.reply_text(result.text, reply_markup=MAIN_KEYBOARD)
        return

    # Явное личное: "!текст" или "личное: текст" → Private/, который вне git.
    priv = re.match(r"^\s*(?:!|личное\s*[:\-]?)\s*(.+)$", text,
                    re.IGNORECASE | re.DOTALL)
    if priv and priv.group(1).strip():
        append_to_inbox(priv.group(1).strip(), "thought", private=True)
        await update.message.reply_text("🔒 Записано в Private — в git не попадёт.",
                                        reply_markup=MAIN_KEYBOARD)
        return

    # продвижение задач без мыши: "дальше опубликовать", "готово рил"
    adv = re.match(r"^(дальше|в работу|готово|список|next|todo)\s+(.+)$",
                   text, re.IGNORECASE)
    if adv and adv.group(2).strip():
        result = _dispatch.dispatch_detailed(text)
        ui.mutation_sync(result, _synced)
        await update.message.reply_text(result.text, reply_markup=MAIN_KEYBOARD)
        return

    # Пакет задач на день: "день: ..." — общий dispatch, без своего дубля.
    day = re.match(r"^\s*(?:день|задачи на день|сегодня)\s*:\s*(.+)$", text,
                   re.IGNORECASE | re.DOTALL)
    if day and day.group(1).strip():
        result = _dispatch.dispatch_detailed(text)
        ui.mutation_sync(result, _synced)
        await update.message.reply_text(result.text, reply_markup=MAIN_KEYBOARD)
        return

    # Ссылка на видео → в очередь на разбор (общий dispatch + neutral sync).
    if any(queue_store.is_video_url(match.group(0))
           for match in re.finditer(r"https?://[^\s<>]+", text, re.IGNORECASE)):
        if len(re.findall(r"https?://[^\s<>]+", text, re.IGNORECASE)) > 1:
            await update.message.reply_text("Пришли видео отдельным сообщением, а сайты — следующим. Здесь ничего не сохранил.",
                                            reply_markup=MAIN_KEYBOARD)
            return
        result = _dispatch.dispatch_detailed(text)
        ui.mutation_sync(result, _synced)
        await update.message.reply_text(result.text, reply_markup=MAIN_KEYBOARD)
        return

    # Счётчик рынка: "+контент 60", "+рил", "+аутрич 20", "+продажа 5000".
    if market.parse_command(text):
        result = _dispatch.dispatch_detailed(text)
        if result.text:
            ui.mutation_sync(result, _synced)
            await update.message.reply_text(result.text, reply_markup=MAIN_KEYBOARD)
            return

    # --- Свободный Dusk-first роутер. ---------------------------------------
    if re.fullmatch(r"(?:покажи\s+)?(?:мои\s+)?(?:ресурсы|сохранённые ссылки|сохраненные ссылки|сайты)", text, re.IGNORECASE):
        view = await asyncio.to_thread(resource_ui.list_view, _vault())
        await update.message.reply_text(view.text, reply_markup=_inline(view))
        return
    if await _maybe_reminder_intent(update, context, text):
        return
    route = assistant_router.route(text)
    if route.kind == assistant_router.KIND_TASK:
        result = await asyncio.to_thread(ui.submit_task, state, route.subject, vault=_vault(), sync=_sync_task)
        await _reply_task_submission(update, context, result)
        return

    if route.kind == assistant_router.KIND_PERSONAL_SEARCH:
        view = await asyncio.to_thread(resource_ui.search_view, _vault(), route.subject or text)
        await update.message.reply_text(
            search_answer(route.subject or text, False), reply_markup=_inline(view) or MAIN_KEYBOARD)
        return

    if route.kind == assistant_router.KIND_KNOWLEDGE_SEARCH:
        await update.message.reply_text(
            search_answer(route.subject or text, True), reply_markup=MAIN_KEYBOARD)
        return

    # Сайты и инструменты сохраняются отдельно от мыслей без запроса модели.
    if re.search(r"https?://", text, re.IGNORECASE):
        await _save_resource(update, context, text)
        return

    # Всё неоднозначное безопасно сохраняется в Dusk Inbox.
    line = await asyncio.to_thread(append_to_inbox, " ".join(text.split()), "thought")
    if line:
        answer = "✅ Записал в Inbox. Потом можно открыть кнопкой «🧠 Мысли»."
        hint = await asyncio.to_thread(_related_hint, text)
        if hint:
            answer += "\n\n" + hint
        await _reply_saved_thought(update, context, answer, line=line)
    else:
        await update.message.reply_text("Пустая запись — ничего не сохранил.",
                                        reply_markup=MAIN_KEYBOARD)


async def _handle_thought_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   data: str) -> None:
    """Callbacks хаба `🧠 Мысли`: только короткие токены, без путей и текста."""
    query = update.callback_query
    state = context.user_data
    store = _runtime_store()
    now = _now()
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    token = parts[2] if len(parts) > 2 else ""
    digest_job = _message_digest(state, query.message)
    if action not in {"o", "c", "b", "n", "x", "e", "d", "z", "u", "t"}:
        digest_job = ""
        state.get("digest_messages", {}).pop(str(getattr(query.message, "message_id", None)), None)
    if action != "z":
        _clear_inputs(state)

    if action == "i":
        view = ui.render_thoughts(state, _vault(), store=store, now=now,
                                  section=ui.THOUGHT_SECTION_INBOX)
    elif action == "r":
        view = ui.render_thoughts(state, _vault(), store=store, now=now,
                                  section=ui.THOUGHT_SECTION_RECENT)
    elif action == "w":
        view = ui.remember_thought(state, _vault(), store=store, now=now)
    elif action == "o":
        view = ui.render_thought(state, _vault(), store, token, now=now)
    elif action == "a":
        view = ui.render_thoughts(state, _vault(), store=store, now=now, section="done")
    elif action == "c":
        view = await asyncio.to_thread(ui.complete_thought, state, _vault(), token, sync=_sync_note)
    elif action == "b":
        view = await asyncio.to_thread(ui.reopen_thought, state, _vault(), token, sync=_sync_note)
    elif action == "n":
        view = ui.next_thought(state, _vault(), store, token, now=now)
    elif action == "x":
        view = ui.hide_thought(state, store, token, now=now)
    elif action == "e":
        _clear_inputs(state)
        view = await asyncio.to_thread(ui.begin_thought_edit, state, _vault(), token)
        if digest_job and ui.thought_edit_pending(state):
            state["digest_edit_return"] = digest_job
    elif action == "d":
        _clear_inputs(state)
        view = await asyncio.to_thread(ui.confirm_thought_delete, state, _vault(), token)
    elif action == "z":
        view = await asyncio.to_thread(ui.delete_thought, state, _vault(), token, sync=_sync_note)
    elif action == "u":
        view = await asyncio.to_thread(ui.restore_thought, state, _vault(), token, sync=_sync_note)
    elif action == "t":
        result = await asyncio.to_thread(ui.task_from_thought, state, _vault(), token,
                                         sync=_sync_task, now=now)
        view = ui.render_thought(state, _vault(), store, token, now=now)
        if view.stale:
            view = ui.View(result.text, [[("⬅️ Назад", ui.THOUGHT_HUB)]], stale=True)
        else:
            view = ui.View(result.text + "\n\n" + view.text, view.rows)
    else:
        view = ui.render_thoughts(state, _vault(), store=store, now=now)
    view = _digest_return_view(view, digest_job)
    await ui.safe_edit(query, view.text, _inline(view))


async def _handle_focus_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                data: str) -> None:
    """Callbacks фокуса `q:`: короткие токены, без путей и пользовательского текста."""
    query = update.callback_query
    state = context.user_data
    store = _runtime_store()
    now = _now()
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    args = parts[2:]

    if action == "b" and args:
        view = _card_view(state, args[0], now=now)
        await ui.safe_edit(query, view.text, _inline(view))
        return
    if action == "d" and args:
        result = focus_ui.begin_duration_menu(state, _vault(), args[0], now=now)
    elif action == "s" and len(args) == 2:
        result = focus_ui.start_from_menu(state, _vault(), store, args[0],
                                          args[1], now=now)
    elif action == "e" and args:
        result = focus_ui.extend_from_token(state, _vault(), store, args[0], now=now)
    elif action == "x" and args:
        result = focus_ui.stop_active(state, _vault(), store, args[0], now=now)
    elif action == "g" and args:
        result = focus_ui.done_from_expiry(state, _vault(), store, args[0], now=now)
    elif action == "E" and args:
        result = focus_ui.extend_from_expiry(state, _vault(), store, args[0],
                                             data, now=now)
    elif action == "z" and args:
        result = focus_ui.dismiss_expiry(state, _vault(), store, args[0], now=now)
    else:
        result = focus_ui.stale_action()

    if result.sync_required:
        await asyncio.to_thread(_sync_focus)
    _wake_focus(context)
    await ui.safe_edit(query, result.view.text, _inline(result.view))


async def _handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   data: str) -> None:
    """Callbacks напоминаний `r:`: короткие токены, без путей и текста."""
    query = update.callback_query
    state = context.user_data
    store = _runtime_store()
    now = _now()
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    args = parts[2:]

    if action == "b" and args:
        view = await asyncio.to_thread(_card_view, state, args[0], now)
        await ui.safe_edit(query, view.text, _inline(view))
        return
    if action == "d" and args:
        result = await asyncio.to_thread(reminder_ui.begin_menu, state,
                                         _vault(), args[0])
    elif action == "s" and len(args) == 2:
        result = await asyncio.to_thread(reminder_ui.schedule_preset, state,
                                         _vault(), store, args[0], args[1], now)
    elif action == "o" and args:
        result = reminder_ui.other_time(state, args[0])
    elif action == "g" and args:
        result = await asyncio.to_thread(reminder_ui.done_action, _vault(),
                                         store, args[0], now)
    elif action == "p" and args:
        result = await asyncio.to_thread(reminder_ui.snooze_action, _vault(),
                                         store, args[0], 15, now)
    elif action == "h" and args:
        result = await asyncio.to_thread(reminder_ui.snooze_action, _vault(),
                                         store, args[0], 60, now)
    elif action == "x" and args:
        result = await asyncio.to_thread(reminder_ui.remove_action, _vault(),
                                         store, args[0], now)
    else:
        result = reminder_ui.stale_action()

    if result.sync_required:
        await asyncio.to_thread(_sync_reminder)
    _wake_reminder(context)
    await ui.safe_edit(query, result.view.text, _inline(result.view))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not auth_check(update):
        try:
            await query.answer()
        except Exception as error:
            logger.warning("callback ack failed: %s", type(error).__name__)
        return
    await query.answer()

    state = context.user_data
    data = query.data or ""
    try:
        if data.startswith("m:"):
            await _remove_legacy_keyboard(update, context)
            await _handle_menu_callback(update, context, data)
            return
        if data.startswith("dg:"):
            await _handle_digest_callback(update, context, data)
            return
        if data.startswith("rs:"):
            await _handle_resource_callback(update, context, data)
            return
        if data == "x":
            _clear_inputs(state)
            await ui.safe_edit(query, "Отменено. Что сделать?", MAIN_KEYBOARD)
            return

        if data == ui.THOUGHT_SEARCH:
            _clear_inputs(state)
            # Поиск внутри хаба входит в тот же MODE_SEARCH, что и старая кнопка.
            ui.begin(state, ui.MODE_SEARCH)
            await ui.safe_edit(query, ui.MODE_PROMPTS[ui.MODE_SEARCH],
                               cancel_keyboard())
            return

        if data.startswith("f:"):
            await _handle_thought_callback(update, context, data)
            return

        _clear_inputs(state)
        if data.startswith("q:"):
            await _handle_focus_callback(update, context, data)
            return

        if data.startswith("r:"):
            await _handle_reminder_callback(update, context, data)
            return

        if data.startswith("l:") or data.startswith("b:"):
            view = ui.render_list(state, _vault(), page=_page(data[2:]))
            await ui.safe_edit(query, view.text, _inline(view))
            return

        if data.startswith("t:"):
            view = await asyncio.to_thread(_card_view, state, data[2:], _now())
            await ui.safe_edit(query, view.text, _inline(view))
            return

        if data.startswith("s:"):
            parts = data.split(":")
            if len(parts) != 3:
                raise ValueError("bad status callback")
            _prefix, token, target = parts
            reminder_view = await asyncio.to_thread(
                reminder_ui.card_reminder, state, _vault(), token, _now())
            outcome = focus_ui.status_with_focus(
                state, _vault(), _runtime_store(), token, target, now=_now(),
                reminder=reminder_view)
            if outcome.changed:
                await asyncio.to_thread(_sync_status)
            elif outcome.stopped:
                await asyncio.to_thread(_sync_focus)
            _wake_focus(context)
            _wake_reminder(context)
            await ui.safe_edit(query, outcome.view.text, _inline(outcome.view))
            return

        view = ui.render_list(state, _vault())
        await ui.safe_edit(query, "⚠️ Неизвестная кнопка — открой список заново.",
                           _inline(view))
    except Exception as error:
        logger.error("callback error: %s", type(error).__name__)
        try:
            await ui.safe_edit(query, "⚠️ Не получилось. Открой «📋 Задачи» заново.")
        except Exception as fallback_error:
            logger.warning("callback fallback failed: %s", type(fallback_error).__name__)


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    await _remove_legacy_keyboard(update, context)
    _clear_inputs(context.user_data)
    await update.message.reply_text("Отменено.", reply_markup=MAIN_KEYBOARD)


async def handle_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    await _remove_legacy_keyboard(update, context)
    await update.message.reply_text(health_text(), reply_markup=MAIN_KEYBOARD)


async def handle_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    await _remove_legacy_keyboard(update, context)
    context.user_data["free_mode"] = False
    await update.message.reply_text("Режим вопросов выключен — снова пишу в Inbox.", reply_markup=MAIN_KEYBOARD)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    await _remove_legacy_keyboard(update, context)
    await update.message.reply_text("🎤 Расшифровываю…")

    voice = update.message.voice or update.message.audio
    if not voice:
        await update.message.reply_text("Не смог получить аудио.")
        return

    file = await voice.get_file()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    transcript = ""
    try:
        transcript = await asyncio.to_thread(
            speech_to_text.transcribe_file, tmp_path)
    except Exception as error:
        # Не логируем и не пересказываем содержание: только тип сбоя.
        logger.error("transcription failed: %s", type(error).__name__)
        await update.message.reply_text("Не получилось расшифровать — попробуй ещё раз.")
        return
    finally:
        os.unlink(tmp_path)

    if not transcript:
        await update.message.reply_text("Речь не распозналась, попробуй ещё раз.")
        return

    # Показываем расшифровку, чтобы ошибка распознавания была видна сразу.
    await update.message.reply_text(f"🎤 «{transcript[:500]}»")
    await handle_content(update, context, transcript, origin="voice")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update):
        return
    await _remove_legacy_keyboard(update, context)
    await update.message.reply_text("📸 Смотрю скриншот…")

    photo = update.message.photo[-1]
    file = await photo.get_file()
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        caption = update.message.caption or ""
        text = re.sub(r"\s+", " ", caption).strip()
        if text:
            append_to_inbox(text, "thought")
            await update.message.reply_text(f"📸 Подпись записана в Inbox: {text[:80]}")
        else:
            await update.message.reply_text(
                "📸 Саму картинку я пока не читаю — пришли текстом или голосовым.\n"
                "(Разбор изображений подключу отдельно, если скажешь.)"
            )
    except Exception as e:
        logger.error(f"Photo processing error: {e}")
        await update.message.reply_text(f"Ошибка: {str(e)[:200]}")
    finally:
        os.unlink(tmp_path)


async def post_init(application: Application):
    """Старт: reconcile до цикла, stop/wake события, raw scheduler task.

    Не используем `Application.create_task`: его `Application.stop` ждёт, а наш
    scheduler завершается по stop-событию, а не по вечному ожиданию.
    """
    digest_stop = asyncio.Event()
    digest_wake = asyncio.Event()
    application.bot_data["digest_stop"] = digest_stop
    application.bot_data["digest_wake"] = digest_wake
    application.bot_data["digest_task"] = asyncio.create_task(_digest_loop(application, digest_stop, digest_wake))
    store = _runtime_store()
    if store is None:
        logger.error("focus scheduler skipped: runtime store unavailable")
        return
    try:
        result = focus.reconcile(_vault(), store, now=_now())
        if result.sync_required:
            await asyncio.to_thread(_sync_focus)
    except (focus.FocusError, runtime_store.RuntimeStoreError) as error:
        logger.error("focus reconcile failed: %s", type(error).__name__)

    stop = asyncio.Event()
    wake = asyncio.Event()
    application.bot_data["focus_stop"] = stop
    application.bot_data["focus_wake"] = wake

    async def deliver(notification):
        await application.bot.send_message(
            chat_id=ALLOWED_USER, text=notification.text, disable_notification=False,
            reply_markup=_notification_markup(notification))

    scheduler = focus_scheduler.FocusScheduler(
        vault=_vault(), store=store, deliver=deliver, sync=_sync_focus)
    application.bot_data["focus_scheduler"] = scheduler
    application.bot_data["focus_task"] = asyncio.create_task(
        scheduler.run(stop, wake))
    logger.info("Dusk focus scheduler started")

    try:
        await asyncio.to_thread(reminders.reconcile, _vault(), store, _now())
    except (reminders.ReminderError, runtime_store.RuntimeStoreError) as error:
        logger.error("reminder reconcile failed: %s", type(error).__name__)

    reminder_stop = asyncio.Event()
    reminder_wake = asyncio.Event()
    application.bot_data["reminder_stop"] = reminder_stop
    application.bot_data["reminder_wake"] = reminder_wake

    async def deliver_reminder(notification):
        await application.bot.send_message(
            chat_id=ALLOWED_USER, text=notification.text, disable_notification=False,
            reply_markup=_notification_markup(notification))

    reminder_sched = reminder_scheduler.ReminderScheduler(
        vault=_vault(), store=store, deliver=deliver_reminder,
        sync=_sync_reminder)
    application.bot_data["reminder_scheduler"] = reminder_sched
    application.bot_data["reminder_task"] = asyncio.create_task(
        reminder_sched.run(reminder_stop, reminder_wake))
    logger.info("Dusk reminder scheduler started")


async def post_shutdown(application: Application):
    """Остановка: разбудить и дождаться scheduler без утечки task."""
    stop = application.bot_data.get("focus_stop")
    wake = application.bot_data.get("focus_wake")
    scheduler = application.bot_data.get("focus_scheduler")
    task = application.bot_data.get("focus_task")
    if scheduler is not None and stop is not None and wake is not None:
        scheduler.stop(stop, wake)
    elif stop is not None:
        stop.set()
        if wake is not None:
            wake.set()
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            pass
    r_stop = application.bot_data.get("reminder_stop")
    r_wake = application.bot_data.get("reminder_wake")
    r_sched = application.bot_data.get("reminder_scheduler")
    r_task = application.bot_data.get("reminder_task")
    if r_sched is not None and r_stop is not None and r_wake is not None:
        r_sched.stop(r_stop, r_wake)
    elif r_stop is not None:
        r_stop.set()
        if r_wake is not None:
            r_wake.set()
    if r_task is not None:
        try:
            await asyncio.wait_for(r_task, timeout=5)
        except asyncio.TimeoutError:
            r_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await r_task
        except asyncio.CancelledError:
            pass
    digest_stop = application.bot_data.get("digest_stop")
    digest_wake = application.bot_data.get("digest_wake")
    digest_task = application.bot_data.get("digest_task")
    if digest_stop is not None:
        digest_stop.set()
    if digest_wake is not None:
        digest_wake.set()
    if digest_task is not None:
        try:
            await asyncio.wait_for(digest_task, timeout=5)
        except asyncio.TimeoutError:
            digest_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await digest_task
        except asyncio.CancelledError:
            logger.debug("digest scheduler already cancelled")
    logger.info("Dusk schedulers stopped")


def main():
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        return

    app = (Application.builder().token(TOKEN)
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", handle_menu))
    app.add_handler(CommandHandler("cancel", handle_cancel))
    app.add_handler(CommandHandler("health", handle_health))
    app.add_handler(CommandHandler("exit", handle_exit))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Dusk Brain bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
