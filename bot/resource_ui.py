"""Pure Telegram-facing views for local personal resources.

The transport owns Telegram objects and input modes.  This module owns only
short callback protocols, resource mutations, and the state required to make
an old button fail closed.
"""

from __future__ import annotations

import re
import textwrap
import logging

import personal_search
import resources
import task_ui as ui

PAGE_SIZE = 10
MAX_VIEW_CHARS = 3900
MAX_SEARCH_RESULTS = 8
RESOURCE_EDIT_KEY = "resource_edit"
RESOURCE_DELETE_KEY = "resource_delete"
logger = logging.getLogger(__name__)


def capture(vault, text: str, sync=None) -> ui.View:
    """Save every link in one message, with no fetch, scrape, or model call."""
    try:
        urls = resources.extract_urls(text)
        if not urls:
            return _error("Не вижу ссылку. Пришли http:// или https:// адрес.")
        description = _description_without_urls(text, urls)
        results = []
        failure = ""
        for url in urls:
            try:
                results.append(resources.save(vault, url, description))
            except (resources.ResourceError, OSError) as error:
                failure = _storage_message(error, "сохранить")
                break
    except (resources.ResourceError, ValueError, OSError) as error:
        return _error(_storage_message(error, "сохранить"))
    created = sum(result.created for result in results)
    if created and sync is not None:
        sync()
    if failure:
        return ui.View(_fit(f"🔖 Сохранено: {created}. Дальше не получилось: {failure}",
                            MAX_VIEW_CHARS), _capture_rows(results), stale=True)
    if len(results) == 1:
        view = _open_from_record(results[0].resource)
        prefix = "🔖 Сохранено." if created else "🔖 Уже сохранено."
        view.text = _fit(prefix + "\n\n" + view.text, MAX_VIEW_CHARS)
        return view
    if created:
        plural = "ресурс" if created == 1 else "ресурсов"
        return ui.View(f"🔖 Сохранено: {created} {plural}.", _capture_rows(results))
    return ui.View("🔖 Эти ссылки уже сохранены.", _capture_rows(results))


def list_view(vault, page: int = 0) -> ui.View:
    records = resources.list_resources(vault)
    if not records:
        return ui.View("🔖 Ресурсов пока нет. Пришли ссылку и пару слов, зачем она.",
                       [[("➕ Добавить", "rs:a"), ("🔎 Найти", "rs:s")]])
    return _records_view(records, page, "🔖 Ресурсы")


def search_view(vault, query: str, page: int = 0) -> ui.View:
    terms = _terms(query)
    if not terms:
        return ui.View("🔎 Напиши, что искать среди ресурсов.",
                       [[("⬅️ Ресурсы", "rs:l:0")]], stale=True)
    records = [item for item in resources.list_resources(vault)
               if all(_matches(item, term) for term in terms)][:MAX_SEARCH_RESULTS]
    if not records:
        return ui.View("🔎 В ресурсах ничего не нашёл.",
                       [[("⬅️ Ресурсы", "rs:l:0")]])
    return _records_view(records, 0, "🔎 Результаты поиска", include_search=False)


def callback(vault, state: dict, data: str, sync=None) -> ui.View:
    """Perform a resource callback. Unknown or stale data never mutates files."""
    parts = str(data or "").split(":")
    if len(parts) < 2 or parts[0] != "rs":
        return _error("⚠️ Кнопка ресурса устарела.")
    action = parts[1]
    try:
        if action == "l" and len(parts) == 3:
            return list_view(vault, _page(parts[2]))
        if action == "o" and len(parts) == 3:
            _clear_delete_for(state, parts[2])
            return _open(vault, parts[2])
        if action == "e" and len(parts) == 4:
            return _begin_edit(vault, state, parts[2], parts[3])
        if action == "d" and len(parts) == 4:
            return _confirm_delete(vault, state, parts[2], parts[3])
        if action == "z" and len(parts) == 4:
            return _delete(vault, state, parts[2], parts[3], sync)
        if action == "u" and len(parts) == 3:
            result = resources.restore(vault, parts[2])
            if result.mutated and sync is not None:
                sync()
            return _mutation_view(result)
    except (resources.ResourceError, ValueError, OSError) as error:
        return _error(_storage_message(error, "открыть"))
    return _error("⚠️ Кнопка ресурса устарела.")


def edit_pending(state: dict) -> bool:
    value = state.get(RESOURCE_EDIT_KEY) if isinstance(state, dict) else None
    return (isinstance(value, tuple) and len(value) == 2
            and all(isinstance(part, str) and part for part in value))


def clear_edit(state: dict) -> None:
    if isinstance(state, dict):
        state.pop(RESOURCE_EDIT_KEY, None)


def clear(state: dict) -> None:
    """Cancel pending resource input and a destructive confirmation."""
    if isinstance(state, dict):
        state.pop(RESOURCE_EDIT_KEY, None)
        state.pop(RESOURCE_DELETE_KEY, None)


def submit_edit(vault, state: dict, text: str, sync=None) -> ui.View:
    pending = state.get(RESOURCE_EDIT_KEY) if isinstance(state, dict) else None
    if not edit_pending(state):
        return _error("⚠️ Режим исправления устарел.")
    identifier, version = pending
    try:
        result = resources.update(vault, identifier, version, text)
    except (resources.ResourceError, OSError) as error:
        return _error(_storage_message(error, "исправить"))
    if not result.ok:
        return _mutation_view(result)
    clear_edit(state)
    if result.mutated and sync is not None:
        sync()
    view = _open(vault, identifier)
    if result.mutated:
        view.text = _fit(result.message + "\n\n" + view.text, MAX_VIEW_CHARS)
    return view


def _open(vault, identifier: str) -> ui.View:
    record = resources.load(vault, identifier)
    if record is None:
        return _error("⚠️ Ресурс устарел — открой список заново.")
    title = _fit(record.title, 500)
    description = _fit(record.description, 2000)
    lines = ["🔖 Ресурс", "", title]
    if description and description != title:
        lines.extend(["", description])
    lines.extend(["", record.url])
    return ui.View(_fit("\n".join(lines), MAX_VIEW_CHARS), [
        [("↗️ Открыть", "url:" + record.url)],
        [("✏️ Исправить", f"rs:e:{record.id}:{record.version}"),
         ("🗑 Удалить", f"rs:d:{record.id}:{record.version}")],
        [("⬅️ Ресурсы", "rs:l:0")],
        [("🕘 История", "mh:h:r:" + record.id)],
    ])


def _begin_edit(vault, state: dict, identifier: str, version: str) -> ui.View:
    record = resources.load(vault, identifier)
    if record is None or record.version != version:
        return _error("⚠️ Ресурс изменился — открой его заново.")
    state[RESOURCE_EDIT_KEY] = (identifier, version)
    shown = record.description or record.title
    return ui.View(_fit("✏️ Исправление ресурса\n\nТекущий текст:\n" + shown +
                        "\n\nПришли новое описание одним сообщением.", MAX_VIEW_CHARS),
                   [[("⬅️ Отмена", f"rs:o:{identifier}")]])


def _confirm_delete(vault, state: dict, identifier: str, version: str) -> ui.View:
    record = resources.load(vault, identifier)
    if record is None or record.version != version:
        return _error("⚠️ Ресурс изменился — открой его заново.")
    state[RESOURCE_DELETE_KEY] = (identifier, version)
    return ui.View("🗑 Удалить ресурс? Он попадёт в корзину, откуда его можно восстановить.",
                   [[("🗑 Удалить", f"rs:z:{identifier}:{version}"),
                     ("⬅️ Отмена", f"rs:o:{identifier}")]])


def _delete(vault, state: dict, identifier: str, version: str, sync) -> ui.View:
    if state.get(RESOURCE_DELETE_KEY) != (identifier, version):
        return _error("⚠️ Сначала подтверди удаление ресурса.")
    try:
        result = resources.delete(vault, identifier, version)
    except (resources.ResourceError, OSError) as error:
        return _error(_storage_message(error, "удалить"))
    state.pop(RESOURCE_DELETE_KEY, None)
    if result.mutated and sync is not None:
        sync()
    return _mutation_view(result)


def _mutation_view(result: resources.Mutation) -> ui.View:
    if not result.ok:
        return ui.View(result.message, [[("⬅️ Ресурсы", "rs:l:0")]], stale=result.stale)
    if result.trash_id:
        return ui.View(result.message,
                       [[("↩️ Восстановить", f"rs:u:{result.trash_id}"),
                         ("⬅️ Ресурсы", "rs:l:0")]])
    if result.resource is not None:
        view = _open_from_record(result.resource)
        if result.mutated:
            view.text = _fit(result.message + "\n\n" + view.text, MAX_VIEW_CHARS)
        return view
    return ui.View(result.message, [[("⬅️ Ресурсы", "rs:l:0")]])


def _open_from_record(record: resources.Resource) -> ui.View:
    lines = ["🔖 Ресурс", "", _fit(record.title, 500)]
    if record.description and record.description != record.title:
        lines.extend(["", _fit(record.description, 2000)])
    lines.extend(["", record.url])
    return ui.View(_fit("\n".join(lines), MAX_VIEW_CHARS), [
        [("↗️ Открыть", "url:" + record.url)],
        [("✏️ Исправить", f"rs:e:{record.id}:{record.version}"),
         ("🗑 Удалить", f"rs:d:{record.id}:{record.version}")],
        [("⬅️ Ресурсы", "rs:l:0")],
        [("🕘 История", "mh:h:r:" + record.id)],
    ])


def _records_view(records: list[resources.Resource], page: int, heading: str,
                  include_search: bool = True) -> ui.View:
    pages = max(1, (len(records) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    chunk = records[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    lines = [heading, ""]
    rows = []
    for index, record in enumerate(chunk, start=page * PAGE_SIZE + 1):
        label = textwrap.shorten(record.title, width=40, placeholder="…") or "Ссылка"
        lines.append(f"{index}. {label}")
        rows.append([(f"{index}. {label}", f"rs:o:{record.id}")])
    if pages > 1:
        nav = []
        if page:
            nav.append(("◀️", f"rs:l:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", f"rs:l:{page}"))
        if page + 1 < pages:
            nav.append(("▶️", f"rs:l:{page + 1}"))
        rows.append(nav)
    rows.append([("➕ Добавить", "rs:a"), ("🔎 Найти", "rs:s")] if include_search
                else [("⬅️ Ресурсы", "rs:l:0")])
    return ui.View(_fit("\n".join(lines), MAX_VIEW_CHARS), rows)


def _capture_rows(results) -> list:
    rows = []
    for index, result in enumerate(results, start=1):
        label = textwrap.shorten(result.resource.title, width=34, placeholder="…") or "Ссылка"
        rows.append([(f"{index}. {label}", f"rs:o:{result.resource.id}")])
    rows.append([("🔖 Ресурсы", "rs:l:0")])
    return rows


def _description_without_urls(text: str, urls: list[str]) -> str:
    value = str(text or "")
    for url in urls:
        value = value.replace(url, " ")
    return re.sub(r"\s+", " ", value).strip(" —–-,:;")


def _terms(value: str) -> list[str]:
    fillers = frozenset(("найди", "поищи", "покажи", "напомни", "тот", "ту",
                         "эта", "этот", "это", "мою", "мой", "моя", "ссылку",
                         "ссылка", "ресурс", "полезный", "полезную"))
    return [word for word in personal_search._terms(str(value or ""))
            if word not in fillers][:12]


def _haystack(record: resources.Resource) -> str:
    return " ".join((record.title, record.description, record.url)).lower()


def _matches(record: resources.Resource, term: str) -> bool:
    needle = personal_search._stem(term)
    if not needle:
        return False
    words = re.findall(r"[а-яёa-z0-9]+", _haystack(record))
    return any(needle in personal_search._stem(word) for word in words)


def _page(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise resources.ResourceError("⚠️ Страница ресурсов устарела.") from error
    if number < 0 or number > 999:
        raise resources.ResourceError("⚠️ Страница ресурсов устарела.")
    return number


def _clear_delete_for(state: dict, identifier: str) -> None:
    pending = state.get(RESOURCE_DELETE_KEY) if isinstance(state, dict) else None
    if isinstance(pending, tuple) and pending and pending[0] == identifier:
        state.pop(RESOURCE_DELETE_KEY, None)


def _error(message: str) -> ui.View:
    return ui.View(_fit(message, MAX_VIEW_CHARS), [[("⬅️ Ресурсы", "rs:l:0")]], stale=True)


def _fit(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _storage_message(error: Exception, action: str) -> str:
    if isinstance(error, OSError):
        logger.warning("resource %s failed: %s", action, type(error).__name__)
        return f"⚠️ Не удалось {action} ресурс. Попробуй ещё раз."
    return str(error)
