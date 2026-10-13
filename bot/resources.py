"""Personal links stored as small, local Markdown records.

Resources are deliberately separate from thoughts and Research Brain.  A link
is never scraped or described by a model here: only the URL and the words the
user supplied are persisted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import queue_store
import memory_history

RESOURCE_REL = "PARA/RESOURCES/Сохранённые ссылки"
TRASH_REL = "SYSTEM/RESOURCE_TRASH"
MAX_URL_CHARS = 2048
MAX_TEXT_CHARS = 4000
MAX_FILE_BYTES = 64 * 1024
MAX_CAPTURE_URLS = 20
_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_URL_RE = re.compile(r"(?:https?|ftp)://[^\s<>]+", re.I)
logger = logging.getLogger(__name__)


class ResourceError(ValueError):
    """A user-facing resource boundary error."""


@dataclass(frozen=True)
class Resource:
    id: str
    url: str
    normalized_url: str
    title: str
    description: str
    version: str
    path: str


@dataclass(frozen=True)
class SaveResult:
    resource: Resource
    created: bool


@dataclass(frozen=True)
class Mutation:
    ok: bool
    message: str
    resource: Resource | None = None
    mutated: bool = False
    trash_id: str = ""
    stale: bool = False


def extract_urls(text: str) -> list[str]:
    """Extract every HTTP(S) link, rejecting missing or unsafe URL-like input."""
    raw = str(text or "")
    candidates = []
    for match in _URL_RE.finditer(raw):
        value = _trim_url(match.group(0))
        if value:
            candidates.append(value)
    if not candidates:
        return []
    if len(candidates) > MAX_CAPTURE_URLS:
        raise ResourceError(f"За раз можно сохранить до {MAX_CAPTURE_URLS} ссылок.")
    urls = []
    for value in candidates:
        normalized = normalize_url(value)
        if not normalized:
            raise ResourceError("Ссылка некорректна или небезопасна.")
        if value.lower().startswith("ftp://"):
            raise ResourceError("Поддерживаются только ссылки http:// и https://.")
        urls.append(value)
    return urls


def normalize_url(url: str) -> str:
    """Canonical key for a HTTP(S) URL, retaining meaningful query parameters."""
    value = str(url or "").strip()
    if not value or len(value) > MAX_URL_CHARS or any(ch.isspace() for ch in value):
        raise ResourceError("Ссылка слишком длинная или содержит пробелы.")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise ResourceError("Ссылка некорректна.") from error
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in {"http", "https"}:
        raise ResourceError("Поддерживаются только ссылки http:// и https://.")
    if not host or parts.username is not None or parts.password is not None:
        raise ResourceError("Ссылка должна быть без логина и пароля.")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ResourceError("В ссылке некорректный домен.") from error
    display_host = f"[{ascii_host}]" if ":" in ascii_host and not ascii_host.startswith("[") else ascii_host
    keep_port = port is not None and not ((scheme == "http" and port == 80)
                                          or (scheme == "https" and port == 443))
    netloc = display_host + (f":{port}" if keep_port else "")
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def resource_id(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:24]


def resource_dir(vault, create: bool = True) -> Path:
    root = Path(vault)
    target = root / RESOURCE_REL
    if create:
        _mkdir_safe(root, target)
    if not _safe_dir(root, target):
        raise ResourceError("Папка ресурсов недоступна.")
    return target


def resource_path(vault, identifier: str, create: bool = True) -> Path:
    key = _checked_id(identifier)
    directory = resource_dir(vault, create=create)
    path = directory / f"{key}.md"
    if path.is_symlink() or not _inside(Path(vault), path.parent):
        raise ResourceError("Ресурс недоступен.")
    return path


def list_resources(vault) -> list[Resource]:
    try:
        directory = resource_dir(vault, create=False)
    except ResourceError:
        return []
    if not directory.exists():
        return []
    items = []
    for path in sorted(directory.glob("*.md")):
        if path.is_symlink() or not _ID_RE.fullmatch(path.stem):
            continue
        record = _read_resource(Path(vault), path)
        if record is not None:
            items.append(record)
    return sorted(items, key=lambda item: (item.title.casefold(), item.id))


def load(vault, identifier: str) -> Resource | None:
    try:
        path = resource_path(vault, identifier, create=False)
    except ResourceError:
        return None
    return _read_resource(Path(vault), path)


def save(vault, url: str, description: str = "") -> SaveResult:
    """Create an immutable-by-default record; duplicate capture never erases text."""
    original = _validate_source_url(url)
    canonical = normalize_url(original)
    identifier = resource_id(original)
    description = _clean_description(description)
    title = _title_for(original, description)
    path = resource_path(vault, identifier)
    with queue_store.queue_lock(path):
        current = _read_resource(Path(vault), path)
        if current is not None:
            return SaveResult(current, False)
        if path.exists():
            raise ResourceError("Файл ресурса повреждён — не перезаписываю его.")
        text = _render(identifier, original, canonical, title, description)
        queue_store.atomic_write(path, text)
    record = _read_resource(Path(vault), path)
    if record is None:
        raise ResourceError("Не удалось сохранить ресурс.")
    return SaveResult(record, True)


def update(vault, identifier: str, version: str, description: str) -> Mutation:
    path = resource_path(vault, identifier, create=False)
    cleaned = _clean_description(description)
    with queue_store.queue_lock(path):
        current = _read_resource(Path(vault), path)
        if current is None:
            return Mutation(False, "⚠️ Ресурс устарел — открой список заново.", stale=True)
        if not _same_version(current, version):
            return Mutation(False, "⚠️ Ресурс изменился — открой его заново.", stale=True)
        title = _title_for(current.url, cleaned)
        rewritten = _render(current.id, current.url, current.normalized_url, title, cleaned)
        if _version(rewritten) == current.version:
            return Mutation(True, "Ресурс уже содержит этот текст.", current, False)
        try:
            raw = path.read_text(encoding="utf-8")
            event_id = memory_history.prepare_change(
                vault, "resource", current.id, current.id, raw, current.id, rewritten)
        except (OSError, UnicodeDecodeError, memory_history.HistoryError):
            return Mutation(False, "⚠️ История недоступна — ресурс не изменён.")
        try:
            queue_store.atomic_write(path, rewritten)
        except OSError:
            memory_history.abort_change(vault, event_id)
            return Mutation(False, "⚠️ Не удалось исправить ресурс.")
        try:
            memory_history.commit_change(vault, event_id)
        except (OSError, memory_history.HistoryError):
            return Mutation(False, "⚠️ Ресурс исправлен, но историю не удалось подтвердить.",
                            mutated=True)
    changed = load(vault, identifier)
    if changed is None:
        raise ResourceError("Не удалось обновить ресурс.")
    return Mutation(True, "✏️ Ресурс исправлен.", changed, True)


def delete(vault, identifier: str, version: str) -> Mutation:
    root = Path(vault)
    path = resource_path(root, identifier, create=False)
    with queue_store.queue_lock(path):
        current = _read_resource(root, path)
        if current is None:
            return Mutation(False, "⚠️ Ресурс устарел — открой список заново.", stale=True)
        if not _same_version(current, version):
            return Mutation(False, "⚠️ Ресурс изменился — открой его заново.", stale=True)
        raw = path.read_text(encoding="utf-8")
        trash_id = hashlib.sha256((current.id + current.version).encode("utf-8")).hexdigest()[:24]
        trash = _trash_path(root, trash_id)
        if trash.exists():
            return Mutation(False, "⚠️ Корзина ресурса недоступна.")
        envelope = json.dumps({"id": current.id, "raw": raw}, ensure_ascii=False,
                              sort_keys=True) + "\n"
        queue_store.atomic_write(trash, envelope)
        try:
            path.unlink()
        except OSError as error:
            logger.warning("resource delete failed: %s", type(error).__name__)
            try:
                trash.unlink()
            except OSError as cleanup_error:
                logger.warning("resource trash cleanup failed: %s",
                               type(cleanup_error).__name__)
            raise
    return Mutation(True, "🗑 Ресурс удалён. Его можно восстановить.", current, True, trash_id)


def restore(vault, trash_id: str) -> Mutation:
    root = Path(vault)
    try:
        trash = _trash_path(root, trash_id, create=False)
    except ResourceError:
        return Mutation(False, "⚠️ Восстановление устарело.", stale=True)
    with queue_store.queue_lock(trash):
        try:
            raw = trash.read_text(encoding="utf-8")
            envelope = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return Mutation(False, "⚠️ Восстановление устарело.", stale=True)
        if not isinstance(envelope, dict) or not isinstance(envelope.get("id"), str) or not isinstance(envelope.get("raw"), str):
            return Mutation(False, "⚠️ Восстановление недоступно.", stale=True)
        identifier = envelope["id"]
        try:
            target = resource_path(root, identifier, create=True)
        except ResourceError:
            return Mutation(False, "⚠️ Восстановление недоступно.", stale=True)
        restored = _parse_resource(root, target, envelope["raw"])
        if restored is None or target.exists():
            return Mutation(False, "⚠️ Ресурс уже существует или корзина повреждена.", stale=True)
        with queue_store.queue_lock(target):
            if target.exists():
                return Mutation(False, "⚠️ Ресурс уже существует.", stale=True)
            queue_store.atomic_write(target, envelope["raw"])
        trash.unlink()
    return Mutation(True, "↩️ Ресурс восстановлен.", load(root, identifier), True)


def _validate_source_url(url: str) -> str:
    value = str(url or "").strip()
    normalize_url(value)
    return value


def _clean_description(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) > MAX_TEXT_CHARS:
        raise ResourceError(f"Описание длиннее {MAX_TEXT_CHARS} символов.")
    if any(ord(char) < 32 for char in text):
        raise ResourceError("Описание содержит непечатаемые символы.")
    return text


def _title_for(url: str, description: str) -> str:
    if description:
        return description[:160].rstrip()
    parts = urlsplit(url)
    fallback = ((parts.hostname or "ссылка") + (parts.path.rstrip("/") or ""))
    return fallback[:160]


def _render(identifier: str, url: str, normalized: str, title: str, description: str) -> str:
    def field(name, value):
        return f"{name}: {json.dumps(value, ensure_ascii=False)}"
    lines = ["---", field("id", identifier), field("url", url),
             field("normalized_url", normalized), field("title", title),
             field("description", description), "---", "", f"# {title}", "",
             f"URL: {url}"]
    if description:
        lines.extend(["", f"Описание: {description}"])
    return "\n".join(lines) + "\n"


def _read_resource(root: Path, path: Path) -> Resource | None:
    if not path.exists() or path.is_symlink() or not _inside(root, path):
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_resource(root, path, raw)


def _parse_resource(root: Path, path: Path, raw: str) -> Resource | None:
    if len(raw.encode("utf-8")) > MAX_FILE_BYTES or not _ID_RE.fullmatch(path.stem):
        return None
    header = _header(raw)
    if header is None:
        return None
    try:
        identifier = _json_field(header, "id")
        url = _json_field(header, "url")
        normalized = _json_field(header, "normalized_url")
        title = _json_field(header, "title")
        description = _json_field(header, "description")
    except (ResourceError, json.JSONDecodeError):
        return None
    if any(not isinstance(value, str) for value in (url, normalized, title, description)):
        return None
    if len(title) > 160 or len(description) > MAX_TEXT_CHARS:
        return None
    try:
        valid = (normalize_url(url) == normalized and resource_id(url) == identifier)
    except ResourceError:
        return None
    if identifier != path.stem or not _ID_RE.fullmatch(identifier) or not valid:
        return None
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return None
    return Resource(identifier, url, normalized, title, description, _version(raw), rel)


def _header(raw: str) -> str | None:
    if not raw.startswith("---\n"):
        return None
    end = raw.find("\n---\n", 4)
    return raw[4:end] if end != -1 else None


def _json_field(header: str, name: str) -> str:
    prefix = name + ": "
    for line in header.splitlines():
        if line.startswith(prefix):
            value = json.loads(line[len(prefix):])
            if isinstance(value, str):
                return value
            break
    raise ResourceError("Повреждённая карточка ресурса.")


def _version(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _same_version(resource: Resource, version: str) -> bool:
    return isinstance(version, str) and resource.version == version and _ID_RE.fullmatch(version) is not None


def _trash_path(root: Path, trash_id: str, create: bool = True) -> Path:
    key = _checked_id(trash_id)
    directory = root / TRASH_REL
    if create:
        _mkdir_safe(root, directory)
    elif not _safe_dir(root, directory):
        raise ResourceError("Корзина ресурсов недоступна.")
    if not _safe_dir(root, directory):
        raise ResourceError("Корзина ресурсов недоступна.")
    path = directory / f"{key}.json"
    if path.is_symlink() or not _inside(root, path.parent):
        raise ResourceError("Корзина ресурсов недоступна.")
    return path


def _checked_id(value: str) -> str:
    key = str(value or "").strip()
    if not _ID_RE.fullmatch(key):
        raise ResourceError("Некорректный ресурс.")
    return key


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _safe_dir(root: Path, path: Path) -> bool:
    if path.is_symlink() or not path.is_dir() or not _inside(root, path):
        return False
    cursor = path
    try:
        root_resolved = root.resolve()
        while cursor != root:
            if cursor.is_symlink():
                return False
            cursor = cursor.parent
        return root_resolved == root.resolve()
    except (OSError, RuntimeError):
        return False


def _mkdir_safe(root: Path, target: Path) -> None:
    """Create a fixed child path without following a pre-existing child symlink."""
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise ResourceError("Папка ресурсов недоступна.") from error
    if not root.is_dir() or not _inside(root, root):
        raise ResourceError("Папка ресурсов недоступна.")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ResourceError("Папка ресурсов недоступна.")
        if cursor.exists():
            if not cursor.is_dir() or not _inside(root, cursor):
                raise ResourceError("Папка ресурсов недоступна.")
            continue
        try:
            cursor.mkdir()
        except FileExistsError:
            if cursor.is_symlink() or not cursor.is_dir() or not _inside(root, cursor):
                raise ResourceError("Папка ресурсов недоступна.")
        except OSError as error:
            raise ResourceError("Папка ресурсов недоступна.") from error
        if cursor.is_symlink() or not _inside(root, cursor):
            raise ResourceError("Папка ресурсов недоступна.")


def _trim_url(value: str) -> str:
    """Drop prose/Markdown closing punctuation without changing balanced URL paths."""
    result = str(value or "").rstrip(".,;!?'\"")
    while result.endswith(")") and result.count("(") < result.count(")"):
        result = result[:-1]
    return result
