#!/usr/bin/env python3
"""Immutable, file-backed handoff for thought digest requests and results."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


QUEUE_ROOT = "SYSTEM/THOUGHT_DIGEST"
MAX_JSON_BYTES = 512 * 1024
PERIODS = frozenset(("today", "yesterday", "week", "recent"))
_JOB_ID = re.compile(r"^[0-9a-f]{24}$")
_SOURCE_ID = re.compile(r"^s[1-9][0-9]{0,2}$")
_SECTION_KINDS = frozenset(("intent", "connections", "next", "reflect", "defer"))
_MAX_QUEUE_FILES = 2000
_MAX_PENDING = 20
_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCK_STATE = threading.local()


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
        root_stat = root.lstat()
    except FileNotFoundError as error:
        raise QueueSafetyError("vault does not exist") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise QueueSafetyError("vault must be a regular directory")
    current = root
    for index, part in enumerate(parts):
        if not part or Path(part).name != part:
            raise QueueSafetyError("unsafe queue path")
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            if not create:
                return root.joinpath(*parts)
            current.mkdir(mode=0o700)
            current_stat = current.lstat()
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise QueueSafetyError("queue path must not use symlinks")
    return current


def _queue_dir(vault: Path | str, name: str, create: bool = False) -> Path:
    return _safe_dir(vault, "SYSTEM", "THOUGHT_DIGEST", name, create=create)


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
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            return None
        if file_stat.st_size < 2 or file_stat.st_size > MAX_JSON_BYTES:
            return None
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _valid_request(data: object, job_id: str | None = None) -> bool:
    if not isinstance(data, dict) or set(data) != {"id", "period", "created"}:
        return False
    return (isinstance(data["id"], str) and _JOB_ID.fullmatch(data["id"]) is not None
            and (job_id is None or data["id"] == job_id)
            and isinstance(data["period"], str) and data["period"] in PERIODS
            and _is_timestamp(data["created"]))


def _public_path(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        return False
    if "\\" in value or value.startswith("/") or "\x00" in value:
        return False
    pieces = value.split("/")
    return (len(pieces) >= 2 and pieces[0] == "ZETA"
            and all(piece not in {"", ".", ".."} for piece in pieces))


def _valid_snapshot(snapshot: object) -> bool:
    if not isinstance(snapshot, dict):
        return False
    required = {"period", "label", "tasks", "coverage", "sources"}
    if set(snapshot) != required or not isinstance(snapshot.get("period"), str) or snapshot.get("period") not in PERIODS:
        return False
    if not isinstance(snapshot["label"], str) or not isinstance(snapshot["tasks"], list):
        return False
    if not isinstance(snapshot["coverage"], dict) or not isinstance(snapshot["sources"], list):
        return False
    coverage = snapshot["coverage"]
    if set(coverage) != {"undated_inbox", "omitted", "truncated"} or any(
            not isinstance(coverage[key], int) or coverage[key] < 0 for key in coverage):
        return False
    if len(snapshot["sources"]) > 45:
        return False
    if len(snapshot["tasks"]) > 20:
        return False
    for task in snapshot["tasks"]:
        if (not isinstance(task, dict) or set(task) != {"title", "status", "due"}
                or any(not isinstance(task[key], str) for key in task)
                or not 1 <= len(task["title"]) <= 120
                or len(task["status"]) > 40 or len(task["due"]) > 40):
            return False
    source_ids = set()
    for source in snapshot["sources"]:
        if not isinstance(source, dict):
            return False
        required_source = {"id", "identity", "path", "title", "text", "date", "role"}
        if set(source) != required_source or any(not isinstance(source[key], str) for key in required_source):
            return False
        if (not _SOURCE_ID.fullmatch(source["id"]) or source["id"] in source_ids
                or not re.fullmatch(r"[^#\x00]{1,240}#[0-9a-f]{12}", source["identity"])
                or not _public_path(source["path"])
                or not 1 <= len(source["title"]) <= 300
                or not 1 <= len(source["text"]) <= 24_000
                or source["role"] not in {"primary", "context"}
                or (source["date"] != "UNKNOWN DAY" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source["date"]))):
            return False
        source_ids.add(source["id"])
    return True


def _valid_result_payload(payload: object) -> bool:
    if not isinstance(payload, dict) or set(payload) != {"snapshot", "sections", "error"}:
        return False
    if not _valid_snapshot(payload["snapshot"]) or not isinstance(payload["sections"], list):
        return False
    if not isinstance(payload["error"], str) or len(payload["error"]) > 1000:
        return False
    known = {source["id"] for source in payload["snapshot"]["sources"]}
    if len(payload["sections"]) > 5:
        return False
    section_kinds = set()
    for section in payload["sections"]:
        if not isinstance(section, dict) or set(section) != {"kind", "items"}:
            return False
        if (not isinstance(section["kind"], str) or section["kind"] not in _SECTION_KINDS
                or section["kind"] in section_kinds or not isinstance(section["items"], list)
                or len(section["items"]) > 2):
            return False
        section_kinds.add(section["kind"])
        for item in section["items"]:
            if not isinstance(item, dict) or set(item) != {"text", "source_ids"}:
                return False
            if (not isinstance(item["text"], str) or not 1 <= len(item["text"]) <= 1200
                    or not isinstance(item["source_ids"], list) or not 1 <= len(item["source_ids"]) <= 5
                    or len(set(item["source_ids"])) != len(item["source_ids"])):
                return False
            if any(not isinstance(source_id, str) or source_id not in known
                   for source_id in item["source_ids"]):
                return False
    return True


def _valid_result(data: object, job_id: str | None = None) -> bool:
    return (isinstance(data, dict) and set(data) == {"id", "created", "completed", "result"}
            and isinstance(data["id"], str) and _JOB_ID.fullmatch(data["id"]) is not None
            and (job_id is None or data["id"] == job_id)
            and _is_timestamp(data["created"]) and _is_timestamp(data["completed"])
            and _valid_result_payload(data["result"]))


def _write_new(path: Path, value: dict) -> bool:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ValueError("queue record exceeds size limit")
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path, follow_symlinks=False)
        except FileExistsError:
            return False
    except BaseException:
        raise
    finally:
        temp_path.unlink(missing_ok=True)
    return True


@contextmanager
def worker_lock(vault: Path | str) -> Iterator[None]:
    """Cross-process lock stored outside the synced vault."""
    digest = hashlib.sha256(str(Path(vault).absolute()).encode("utf-8")).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / f"dusk-thought-digest-{digest}.lock"
    key = str(lock_path)
    with _LOCK_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    thread_lock.acquire()
    held = getattr(_LOCK_STATE, "held", {})
    if key in held:
        held[key]["depth"] += 1
        _LOCK_STATE.held = held
        try:
            yield
        finally:
            held[key]["depth"] -= 1
            thread_lock.release()
        return
    try:
        if lock_path.exists() and stat.S_ISLNK(lock_path.lstat().st_mode):
            raise QueueSafetyError("unsafe local lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except (OSError, QueueSafetyError) as error:
        thread_lock.release()
        raise QueueSafetyError("cannot open local lock") from error
    handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    locked = False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
        held[key] = {"depth": 1, "handle": handle}
        _LOCK_STATE.held = held
        yield
    finally:
        state = held.get(key)
        if state is not None:
            state["depth"] -= 1
            if state["depth"] == 0:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                held.pop(key, None)
                handle.close()
        elif not handle.closed:
            if locked:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        thread_lock.release()


def enqueue(vault: Path | str, period: str, key: str = "", now: dt.datetime | None = None) -> dict:
    if period not in PERIODS:
        raise ValueError("unknown digest period")
    if not isinstance(key, str) or len(key) > 512:
        raise ValueError("invalid idempotency key")
    job_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] if key else secrets.token_hex(12)
    with worker_lock(vault):
        path = _file_path(vault, "requests", job_id, create=True)
        existing = _read_json(path)
        if existing is not None:
            if _valid_request(existing, job_id) and existing["period"] == period:
                return existing
            raise QueueSafetyError("request id is already occupied")
        existing_requests = all_requests(vault, limit=_MAX_QUEUE_FILES)
        if len(existing_requests) >= _MAX_QUEUE_FILES:
            raise QueueSafetyError("digest queue record capacity reached")
        if len(pending(vault, limit=_MAX_PENDING)) >= _MAX_PENDING:
            raise QueueSafetyError("pending digest queue is full")
        job = {"id": job_id, "period": period, "created": _timestamp(now)}
        if _write_new(path, job):
            return job
        raced = _read_json(path)
        if _valid_request(raced, job_id) and raced["period"] == period:
            return raced
        raise QueueSafetyError("request id is already occupied")


def get_request(vault: Path | str, job_id: str) -> dict | None:
    try:
        data = _read_json(_file_path(vault, "requests", job_id))
    except (QueueSafetyError, ValueError):
        return None
    return data if _valid_request(data, job_id) else None


def get_result(vault: Path | str, job_id: str) -> dict | None:
    try:
        data = _read_json(_file_path(vault, "results", job_id))
    except (QueueSafetyError, ValueError):
        return None
    request = get_request(vault, job_id)
    if not _valid_result(data, job_id) or request is None:
        return None
    if data["created"] != request["created"] or data["result"]["snapshot"]["period"] != request["period"]:
        return None
    return data


def all_requests(vault: Path | str, limit: int = _MAX_QUEUE_FILES) -> list[dict]:
    """Validated request records, newest first, including completed jobs."""
    if not isinstance(limit, int) or not 1 <= limit <= _MAX_QUEUE_FILES:
        raise ValueError(f"limit must be between 1 and {_MAX_QUEUE_FILES}")
    try:
        directory = _queue_dir(vault, "requests")
        if not directory.exists():
            return []
    except QueueSafetyError:
        return []
    found = []
    for path in sorted(directory.glob("*.json"))[:_MAX_QUEUE_FILES]:
        job_id = path.stem
        data = _read_json(path)
        if _JOB_ID.fullmatch(job_id) and _valid_request(data, job_id):
            found.append(data)
    return sorted(found, key=lambda item: (item["created"], item["id"]), reverse=True)[:limit]


def requests(vault: Path | str, limit: int = 100) -> list[dict]:
    """Validated newest-first polling view, bounded to queue capacity."""
    if not isinstance(limit, int) or not 1 <= limit <= _MAX_QUEUE_FILES:
        raise ValueError(f"limit must be between 1 and {_MAX_QUEUE_FILES}")
    return all_requests(vault, limit=limit)


def pending(vault: Path | str, limit: int = 10) -> list[dict]:
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    jobs = [job for job in all_requests(vault, limit=_MAX_QUEUE_FILES)
            if get_result(vault, job["id"]) is None]
    return sorted(jobs, key=lambda item: (item["created"], item["id"]))[:limit]


def _error_result(snapshot: dict, error: str) -> dict:
    return {"snapshot": snapshot, "sections": [], "error": error[:1000]}


def process_one(vault: Path | str, generate: Callable[[dict], dict] | None = None,
                now: dt.datetime | None = None) -> str:
    """Generate exactly one result while holding the local worker lock."""
    with worker_lock(vault):
        job = None
        for candidate in pending(vault, limit=_MAX_PENDING):
            candidate_path = _file_path(vault, "results", candidate["id"])
            # A malformed immutable result must not be overwritten, but must not
            # starve every later valid request either.
            if os.path.lexists(candidate_path):
                continue
            job = candidate
            break
        if job is None:
            return ""
        try:
            import thought_digest
            requested_at = dt.datetime.fromisoformat(job["created"].replace("Z", "+00:00"))
            snapshot = thought_digest.collect(vault, job["period"], now=requested_at)
            producer = generate or thought_digest.generate
            result = producer(snapshot)
            if not _valid_result_payload(result):
                result = _error_result(snapshot, "Некорректный ответ обработчика")
        except Exception as error:
            snapshot = {"period": job["period"], "label": "", "tasks": [],
                        "coverage": {"undated_inbox": 0, "omitted": 0, "truncated": 0}, "sources": []}
            result = _error_result(snapshot, f"Ошибка обработки ({type(error).__name__})")
        record = {"id": job["id"], "created": job["created"], "completed": _timestamp(now),
                  "result": result}
        path = _file_path(vault, "results", job["id"], create=True)
        if not _write_new(path, record) and get_result(vault, job["id"]) is None:
            raise QueueSafetyError("result file is invalid or occupied")
        return job["id"]


def _git(command: list[str], vault: Path | str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(command, cwd=Path(vault), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=45, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def sync_file(vault: Path | str, path: Path | str,
              message: str = "thought digest: request/result") -> bool:
    """Sync one validated immutable queue file without staging unrelated changes."""
    try:
        target = Path(path).absolute()
        root = Path(vault).absolute()
        relative = target.relative_to(root)
        parts = relative.parts
        if len(parts) != 4 or parts[:2] != ("SYSTEM", "THOUGHT_DIGEST") or parts[2] not in {"requests", "results"}:
            return False
        job_id = Path(parts[3]).stem
        if Path(parts[3]).suffix != ".json" or not _JOB_ID.fullmatch(job_id):
            return False
        expected = _file_path(vault, parts[2], job_id)
        if expected != target:
            return False
        record = get_request(vault, job_id) if parts[2] == "requests" else get_result(vault, job_id)
        if record is None:
            return False
    except (ValueError, QueueSafetyError):
        return False
    pull = _git(["git", "pull", "--rebase", "--autostash"], vault)
    if pull is None or pull.returncode != 0:
        return False
    added = _git(["git", "add", "--", str(target)], vault)
    if added is None or added.returncode != 0:
        return False
    changed = _git(["git", "diff", "--cached", "--quiet", "--", str(target)], vault)
    if changed is None or changed.returncode not in (0, 1):
        return False
    if changed.returncode == 1:
        committed = _git(["git", "commit", "-m", message, "--", str(target)], vault)
        if committed is None or committed.returncode != 0:
            return False
    pushed = _git(["git", "push"], vault)
    return pushed is not None and pushed.returncode == 0
