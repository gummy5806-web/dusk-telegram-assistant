#!/usr/bin/env python3
"""Immutable local-to-Mac semantic-search request and result records.

Synchronization is intentionally owned by the transport layer.  This module
only creates and processes validated files under SYSTEM/SEMANTIC_SEARCH.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


QUEUE_ROOT = "SYSTEM/SEMANTIC_SEARCH"
MAX_JSON_BYTES = 512 * 1024
MAX_HISTORY = 2000
MAX_PENDING = 20
MAX_QUERY_CHARS = 160
_JOB_ID = re.compile(r"^[0-9a-f]{24}$")
_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class QueueSafetyError(ValueError):
    """Queue path is malformed or crosses a symbolic link."""


def _timestamp(value: dt.datetime | None = None) -> str:
    value = value or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.isoformat()


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _safe_dir(vault: Path | str, *parts: str, create: bool = False) -> Path:
    root = Path(vault).absolute()
    try:
        state = root.lstat()
    except FileNotFoundError as error:
        raise QueueSafetyError("vault does not exist") from error
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
        raise QueueSafetyError("vault must be a regular directory")
    current = root
    for part in parts:
        if not part or Path(part).name != part:
            raise QueueSafetyError("unsafe queue path")
        current = current / part
        try:
            state = current.lstat()
        except FileNotFoundError:
            if not create:
                return root.joinpath(*parts)
            current.mkdir(mode=0o700)
            state = current.lstat()
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
            raise QueueSafetyError("queue path must not use symlinks")
    return current


def _queue_dir(vault: Path | str, group: str, create: bool = False) -> Path:
    if group not in {"requests", "results"}:
        raise QueueSafetyError("unknown queue group")
    return _safe_dir(vault, "SYSTEM", "SEMANTIC_SEARCH", group, create=create)


def _file_path(vault: Path | str, group: str, job_id: str, create: bool = False) -> Path:
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError("invalid job id")
    return _queue_dir(vault, group, create=create) / f"{job_id}.json"


def request_path(vault: Path | str, job_id: str) -> Path:
    return _file_path(vault, "requests", job_id)


def result_path(vault: Path | str, job_id: str) -> Path:
    return _file_path(vault, "results", job_id)


def _read_json(path: Path) -> dict | None:
    try:
        state = path.lstat()
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
            return None
        if state.st_size < 2 or state.st_size > MAX_JSON_BYTES:
            return None
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _valid_query(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value.strip()) <= MAX_QUERY_CHARS and "\x00" not in value


def _valid_request(data: object, job_id: str | None = None) -> bool:
    return (isinstance(data, dict) and set(data) == {"id", "query", "created"}
            and isinstance(data.get("id"), str) and _JOB_ID.fullmatch(data["id"]) is not None
            and (job_id is None or data["id"] == job_id) and _valid_query(data.get("query"))
            and _is_timestamp(data.get("created")))


def _valid_hit(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"kind", "identity", "path", "title", "excerpt", "content_hash", "score"}:
        return False
    if value["kind"] not in {"thought", "resource", "task"}:
        return False
    if not all(isinstance(value[key], str) and len(value[key]) <= 2000
               for key in ("identity", "path", "title", "excerpt", "content_hash")):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", value["content_hash"]):
        return False
    if isinstance(value["score"], bool) or not isinstance(value["score"], (int, float)):
        return False
    try:
        score = float(value["score"])
    except (TypeError, ValueError):
        return False
    return math.isfinite(score) and -1.0 <= score <= 1.0


def _valid_search_result(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"hits", "coverage", "error"}:
        return False
    if not (isinstance(value["hits"], list) and len(value["hits"]) <= 50
            and all(_valid_hit(hit) for hit in value["hits"])
            and isinstance(value["coverage"], dict) and len(value["coverage"]) <= 32
            and isinstance(value["error"], str) and len(value["error"]) <= 1000):
        return False
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= MAX_JSON_BYTES
    except (TypeError, ValueError):
        return False


def _valid_result(data: object, job_id: str | None = None) -> bool:
    return (isinstance(data, dict) and set(data) == {"id", "query", "created", "completed", "result"}
            and isinstance(data.get("id"), str) and _JOB_ID.fullmatch(data["id"]) is not None
            and (job_id is None or data["id"] == job_id) and _valid_query(data.get("query"))
            and _is_timestamp(data.get("created")) and _is_timestamp(data.get("completed"))
            and _valid_search_result(data.get("result")))


def _write_new(path: Path, value: dict) -> bool:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ValueError("queue record exceeds size limit")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


@contextmanager
def _worker_lock(vault: Path | str) -> Iterator[None]:
    digest = hashlib.sha256(str(Path(vault).absolute()).encode("utf-8")).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / f"dusk-semantic-search-{digest}.lock"
    key = str(lock_path)
    with _LOCK_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        try:
            if lock_path.exists() and stat.S_ISLNK(lock_path.lstat().st_mode):
                raise QueueSafetyError("unsafe local lock")
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as error:
            raise QueueSafetyError("cannot open local lock") from error
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _all_requests(vault: Path | str) -> list[dict]:
    try:
        directory = _queue_dir(vault, "requests")
        if not directory.exists():
            return []
    except QueueSafetyError:
        return []
    records = []
    for path in sorted(directory.glob("*.json"))[:MAX_HISTORY]:
        data = _read_json(path)
        if _JOB_ID.fullmatch(path.stem) and _valid_request(data, path.stem):
            records.append(data)
    return sorted(records, key=lambda item: (item["created"], item["id"]), reverse=True)


def requests(vault: Path | str, limit: int = MAX_HISTORY) -> list[dict]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY:
        raise ValueError(f"limit must be between 1 and {MAX_HISTORY}")
    return _all_requests(vault)[:limit]


def get_request(vault: Path | str, job_id: str) -> dict | None:
    try:
        data = _read_json(request_path(vault, job_id))
    except (QueueSafetyError, ValueError):
        return None
    return data if _valid_request(data, job_id) else None


def get_result(vault: Path | str, job_id: str) -> dict | None:
    try:
        data = _read_json(result_path(vault, job_id))
    except (QueueSafetyError, ValueError):
        return None
    request = get_request(vault, job_id)
    if not _valid_result(data, job_id) or request is None:
        return None
    if data["created"] != request["created"] or data["query"] != request["query"]:
        return None
    # `created` belongs to the immutable on-disk envelope used for request /
    # result correlation.  The polling API exposes only the specified result.
    return {key: data[key] for key in ("id", "query", "completed", "result")}


def _pending(vault: Path | str) -> list[dict]:
    return sorted((job for job in _all_requests(vault) if get_result(vault, job["id"]) is None),
                  key=lambda item: (item["created"], item["id"]))


def enqueue(vault: Path | str, query: str, now: dt.datetime | None = None) -> dict:
    if not _valid_query(query):
        raise ValueError(f"query must contain 1 to {MAX_QUERY_CHARS} characters")
    with _worker_lock(vault):
        existing = _all_requests(vault)
        if len(existing) >= MAX_HISTORY:
            raise QueueSafetyError("semantic queue record capacity reached")
        if len(_pending(vault)) >= MAX_PENDING:
            raise QueueSafetyError("pending semantic queue is full")
        job = {"id": secrets.token_hex(12), "query": query.strip(), "created": _timestamp(now)}
        path = _file_path(vault, "requests", job["id"], create=True)
        if _write_new(path, job):
            return job
        raise QueueSafetyError("request id collision")


def _error_result(error: str) -> dict:
    return {"hits": [], "coverage": {"sources": 0, "partial": False}, "error": error[:1000]}


def process_one(vault: Path | str, search: Callable[[Path | str, str], dict] | None = None,
                now: dt.datetime | None = None) -> str | None:
    """Persist exactly one result. A result file means the request is never retried."""
    with _worker_lock(vault):
        job = None
        for candidate in _pending(vault)[:MAX_PENDING]:
            path = _file_path(vault, "results", candidate["id"])
            if not os.path.lexists(path):
                job = candidate
                break
        if job is None:
            return None
        try:
            if search is None:
                raise RuntimeError("semantic search callback unavailable")
            value = search(vault, job["query"])
            result = value if _valid_search_result(value) else _error_result("invalid semantic search result")
        except Exception as error:
            result = _error_result(f"semantic search failed ({type(error).__name__})")
        record = {"id": job["id"], "query": job["query"], "created": job["created"],
                  "completed": _timestamp(now), "result": result}
        path = _file_path(vault, "results", job["id"], create=True)
        if not _write_new(path, record) and get_result(vault, job["id"]) is None:
            raise QueueSafetyError("result file is invalid or occupied")
        return job["id"]

