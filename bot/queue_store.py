#!/usr/bin/env python3
"""Надёжная очередь: нормализация ссылок, атомарная запись, блокировка.

Зачем отдельный модуль: очередь `Учеба/очередь.md` — единственное место, где
Dusk может потерять пользовательскую ссылку. Раньше это и происходило:
`process_queue` удалял ВСЕ строки с URL, включая те, что не удалось разобрать,
и те, что добавили во время обработки.

Здесь собраны три вещи, которые нужны, чтобы этого не было:

* `normalize_url` — одна ссылка в разных формах (youtu.be, shorts, с
  таймкодом) считается одной и той же записью;
* `atomic_write` — файл заменяется целиком или не меняется вовсе;
* `queue_lock` — два процесса не разбирают одну запись одновременно.

Модуль ничего не знает про yt-dlp, модель и заметки: только про файл очереди.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Параметры, которые не меняют само видео или страницу.
TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = frozenset({
    "fbclid", "gclid", "si", "feature", "ref", "ref_src", "igshid", "mc_cid", "mc_eid",
})

YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")


def _is_host(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def normalize_url(url: str) -> str:
    """Каноническая форма ссылки для сравнения записей между собой.

    YouTube: любые формы (`watch?v=ID`, `youtu.be/ID`, `/shorts/ID`,
    `/embed/ID`, `/live/ID`) дают `https://www.youtube.com/watch?v=ID`,
    а лишние параметры (плейлист, таймкод, метки) отбрасываются.
    Прочие ссылки: убираем фрагмент и метки, схему и хост приводим к нижнему
    регистру. Тело ссылки не переписываем, чтобы не сломать чужие адреса.
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return url

    if any(_is_host(host, d) for d in ("youtube.com", "youtu.be")):
        video_id = ""
        if _is_host(host, "youtu.be"):
            video_id = parts.path.strip("/").split("/")[0]
        else:
            path = parts.path.rstrip("/")
            if path in ("/watch", ""):
                query = dict(parse_qsl(parts.query))
                video_id = query.get("v", "")
            elif path.startswith(("/shorts/", "/embed/", "/live/", "/v/")):
                video_id = path.split("/")[2] if len(path.split("/")) > 2 else ""
        if _YOUTUBE_ID.match(video_id or ""):
            return f"https://www.youtube.com/watch?v={video_id}"
        # нестандартная ссылка на YouTube — оставляем как есть, но без меток
        return f"{scheme}://{host}{parts.path}"

    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in TRACKING_KEYS and not k.startswith(TRACKING_PREFIXES)
    ]
    return urlunsplit((scheme, host, parts.path, urlencode(query), ""))


def is_video_url(url: str) -> bool:
    """YouTube/Vimeo — то, что уходит в разбор видео."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(_is_host(host, d) for d in ("youtube.com", "youtu.be", "vimeo.com"))


def atomic_write(path: Path, text: str) -> None:
    """Записывает файл целиком: либо новое содержимое, либо старое.

    Обрыв в момент записи не оставляет половину файла — очередь остаётся
    читаемой в любом случае.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
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


def _lock_path(kind: str, path: Path) -> Path:
    key = hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"dusk-{kind}-{os.getuid()}-{key}.lock"


@contextmanager
def path_lock(path: Path):
    """Короткая взаимная блокировка файла очереди: чтение, append, подтверждение.

    Держать её на медленном шаге (yt-dlp, модель) нельзя: тогда настоящий
    enqueue ждёт окончания разбора. Медленная часть идёт под `processor_lock`.

    Локальная блокировка не защищает от одновременной записи через Git на
    другом хосте (Mac и VPS) — этот остаточный риск описан в отчёте.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_path("queue", path).open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# Совместимое имя: очередь — частный случай блокировки по пути.
queue_lock = path_lock


@contextmanager
def processor_lock(path: Path):
    """Только один разбор очереди одновременно. Неблокирующий.

    Даёт True первому и False остальным, поэтому второй процесс не висит на
    ожидании, а честно сообщает, что разбор уже идёт.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_path("process", path).open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


ENTRY_PREFIX = "- http"


def queue_entries(text: str) -> list[dict]:
    """Записи очереди в порядке файла.

    Возвращает именно снимок строк: подтверждать можно только те записи,
    которые были прочитаны в начале обработки. Строку, добавленную позже,
    такой снимок не содержит, и она не потеряется.
    """
    entries = []
    for index, raw in enumerate((text or "").splitlines()):
        stripped = raw.strip()
        if stripped.startswith(ENTRY_PREFIX):
            url = stripped[len("-"):].strip()
            entries.append({"index": index, "raw": raw, "url": url,
                            "key": normalize_url(url)})
    return entries


def remove_entries(text: str, entries: list[dict]) -> str:
    """Убирает ровно эти строки (по позиции и содержимому), остальное не трогает.

    Позиция проверяется вместе с содержимым: если файл успел измениться,
    строка не удаляется, а остаётся в очереди — потерять ссылку хуже, чем
    разобрать её второй раз (повтор идемпотентен).
    """
    lines = (text or "").splitlines()
    for entry in sorted(entries, key=lambda e: e["index"], reverse=True):
        index = entry["index"]
        if 0 <= index < len(lines) and lines[index] == entry["raw"]:
            del lines[index]
    return "\n".join(lines).rstrip() + "\n" if lines else ""
