#!/usr/bin/env python3
"""Local, bounded semantic retrieval over public Dusk-owned records only.

The SQLite database holds opaque source identifiers, content hashes and vectors.
It deliberately never stores Markdown, titles, excerpts, queries or credentials.
Every returned record is rebuilt from the current vault, so deleted, completed and
changed files cannot be surfaced from a stale vector cache.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import resources
import task_metadata
import thought_feed
import thought_mutations
import memory_history


DEFAULT_MODEL = "gemini-embedding-001"
DEFAULT_ABSOLUTE_FLOOR = 0.68
MAX_SOURCES = 1000
MAX_SOURCE_CHARS = 2000
MAX_NEW = 50
MAX_VECTOR_DIMS = 8192
_DONE_STATUSES = frozenset(("done", "complete", "completed", "closed", "cancelled", "canceled"))


@dataclass(frozen=True)
class _Source:
    kind: str
    identity: str
    path: str
    title: str
    excerpt: str
    text: str
    order: int
    content_hash: str = ""

    @property
    def text_hash(self) -> str:
        return self.content_hash or hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _clip(value: object, limit: int = MAX_SOURCE_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _valid_vector(value: object) -> bool:
    """Only finite, non-zero numeric vectors may enter or leave the cache."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_VECTOR_DIMS:
        return False
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        return False
    numbers = [float(item) for item in value]
    return all(math.isfinite(item) for item in numbers) and math.fsum(item * item for item in numbers) > 0.0


def _vector(value: object) -> list[float] | None:
    if not _valid_vector(value):
        return None
    return [float(item) for item in value]


def _source_text(parts: Iterable[object]) -> str:
    return _clip("\n".join(str(part or "") for part in parts))


def _source_text_and_hash(parts: Iterable[object]) -> tuple[str, str]:
    """Bound vector input but retain a hash of the complete current evidence."""
    full = " ".join("\n".join(str(part or "") for part in parts).split())
    return _clip(full), hashlib.sha256(full.encode("utf-8")).hexdigest()


def _thought_sources(vault: Path) -> list[_Source]:
    """Use thought_feed's allowlisted parsers; no title/context is embedded."""
    found: list[_Source] = []
    try:
        inbox = list(reversed(thought_feed._inbox_thoughts(vault)))
        structured = thought_feed._sorted_recent(thought_feed._structured_thoughts(vault))
    except (OSError, ValueError):
        return found
    for order, item in enumerate(inbox + structured):
        try:
            if memory_history.is_past(vault, item.identity):
                continue
            # This is the editable thought field. It deliberately excludes a
            # structured note's generated heading, context and conclusions.
            full = thought_mutations.full_text(vault, item.identity)
        except (OSError, ValueError, memory_history.HistoryError):
            # History cannot be verified: fail closed rather than surface a
            # thought that may have been explicitly superseded.
            continue
        text, content_hash = _source_text_and_hash((full or item.excerpt,))
        if text:
            found.append(_Source("thought", item.identity, item.path, _clip(item.title, 300),
                                 _clip(full or item.excerpt, 500), text, order, content_hash))
    return found


def _resource_sources(vault: Path, start: int) -> list[_Source]:
    found: list[_Source] = []
    try:
        records = resources.list_resources(vault)
    except (OSError, ValueError):
        return found
    for offset, item in enumerate(records):
        text, content_hash = _source_text_and_hash((item.title, item.description, item.url))
        if text:
            found.append(_Source("resource", item.id, item.path, _clip(item.title, 300),
                                 _clip(item.description or item.url, 500), text, start + offset,
                                 content_hash))
    return found


def _task_sources(vault: Path, start: int) -> list[_Source]:
    found: list[_Source] = []
    try:
        paths = task_metadata.direct_task_paths(vault, MAX_SOURCES)
    except (OSError, ValueError, task_metadata.TaskMetadataError):
        return found
    for offset, path in enumerate(paths):
        try:
            task = task_metadata.read_task(vault, path)
        except (OSError, ValueError, task_metadata.TaskMetadataError):
            continue
        if task.status.strip().casefold() in _DONE_STATUSES:
            continue
        try:
            state = task.path.lstat()
            if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode) or state.st_size > task_metadata.MAX_TASK_BYTES:
                continue
            task.path.resolve().relative_to(vault.resolve())
            raw = task.path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            continue
        # The task title and markdown body are user-owned task evidence.  The
        # parsed metadata call above establishes that this is a valid direct card.
        body = thought_feed._strip_frontmatter(raw)
        title = _clip(task.title or path.stem, 300)
        text, content_hash = _source_text_and_hash((task.title, body))
        if text:
            found.append(_Source("task", task.relpath, task.relpath, title, title, text,
                                 start + offset, content_hash))
    return found


def _sources(vault: Path) -> tuple[list[_Source], int]:
    """Return only public Dusk collections, newest thought sources first."""
    thoughts = _thought_sources(vault)
    records = thoughts + _resource_sources(vault, len(thoughts))
    records += _task_sources(vault, len(records))
    total = len(records)
    return records[:MAX_SOURCES], max(0, total - MAX_SOURCES)


def _default_db_path() -> Path:
    raw = os.getenv("DUSK_SEMANTIC_DB")
    return Path(raw).expanduser() if raw else Path("~/.local/share/dusk/semantic.sqlite3").expanduser()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS vectors ("
        "vault_key TEXT NOT NULL, model TEXT NOT NULL, dims INTEGER NOT NULL, "
        "kind TEXT NOT NULL, identity TEXT NOT NULL, path TEXT NOT NULL, "
        "text_hash TEXT NOT NULL, vector TEXT NOT NULL, "
        "PRIMARY KEY (vault_key, model, dims, kind, identity))")
    connection.execute("CREATE INDEX IF NOT EXISTS vectors_source ON vectors(vault_key, kind, identity)")
    return connection


def _vault_key(vault: Path) -> str:
    return hashlib.sha256(str(vault.absolute()).encode("utf-8")).hexdigest()


def _cached(connection: sqlite3.Connection, vault_key: str, model: str, dimensions: int,
            source: _Source) -> list[float] | None:
    rows = connection.execute(
        "SELECT vector FROM vectors WHERE vault_key=? AND model=? AND dims=? AND kind=? AND identity=? AND text_hash=?",
        (vault_key, model, dimensions, source.kind, source.identity, source.text_hash)).fetchall()
    for (raw,) in rows:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        vector = _vector(value)
        if vector is not None:
            return vector
    return None


def _cached_any_dimensions(connection: sqlite3.Connection, vault_key: str, model: str,
                           source: _Source) -> list[float] | None:
    """Find a valid current document vector before a query establishes dimensions."""
    rows = connection.execute(
        "SELECT vector FROM vectors WHERE vault_key=? AND model=? AND kind=? AND identity=? AND text_hash=?",
        (vault_key, model, source.kind, source.identity, source.text_hash)).fetchall()
    for (raw,) in rows:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        vector = _vector(value)
        if vector is not None:
            return vector
    return None


def _prune(connection: sqlite3.Connection, vault_key: str, sources: list[_Source]) -> None:
    current = {(item.kind, item.identity, item.text_hash) for item in sources}
    rows = connection.execute("SELECT kind, identity, text_hash FROM vectors WHERE vault_key=?", (vault_key,)).fetchall()
    stale = [(kind, identity, text_hash) for kind, identity, text_hash in rows
             if (kind, identity, text_hash) not in current]
    if stale:
        connection.executemany(
            "DELETE FROM vectors WHERE vault_key=? AND kind=? AND identity=? AND text_hash=?",
            [(vault_key, kind, identity, text_hash) for kind, identity, text_hash in stale])


def _lexical_shared(source: _Source, query: str) -> int:
    words = {part for part in query.casefold().split() if len(part) > 2}
    return sum(word in source.text.casefold() for word in words)


def _index_order(missing: list[_Source], query: str) -> list[_Source]:
    """Lexical-near records first, with round-robin fairness across kinds."""
    kind_order = {"thought": 0, "resource": 1, "task": 2}
    positions: dict[str, int] = {kind: 0 for kind in kind_order}
    decorated = []
    for source in sorted(missing, key=lambda item: (item.kind, item.order, item.identity)):
        position = positions[source.kind]
        positions[source.kind] += 1
        decorated.append((-_lexical_shared(source, query), position,
                          kind_order[source.kind], source.order, source))
    return [item[-1] for item in sorted(decorated, key=lambda item: item[:-1])]


def _empty(coverage: dict, error: str = "") -> dict:
    return {"hits": [], "coverage": coverage, "error": error[:1000]}


def _hit(source: _Source, score: float) -> dict:
    return {"kind": source.kind, "identity": source.identity, "path": source.path,
            "title": source.title, "excerpt": source.excerpt,
            "content_hash": source.text_hash, "score": float(score)}


def _cosine(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right):
        return None
    denom = math.sqrt(math.fsum(value * value for value in left) * math.fsum(value * value for value in right))
    if not math.isfinite(denom) or denom <= 0:
        return None
    value = math.fsum(a * b for a, b in zip(left, right)) / denom
    if not math.isfinite(value) or value < -1.0000001 or value > 1.0000001:
        return None
    return min(1.0, max(-1.0, value))


def index(vault, embed: Callable[[str, str], object], model: str = DEFAULT_MODEL,
          db_path=None, max_new: int = 20) -> dict:
    """Boundedly pre-index current public sources without embedding a query.

    The worker can call this once per tick after serving a queued request.  A
    fresh source is embedded at most once; cache hits require no provider call.
    """
    empty_coverage = {"sources": 0, "indexed": 0, "new": 0, "truncated": 0, "partial": False}
    if not callable(embed) or not isinstance(model, str) or not model or len(model) > 200:
        return {**empty_coverage, "error": "invalid embedding configuration"}
    try:
        new_limit = max(0, min(MAX_NEW, int(max_new)))
    except (TypeError, ValueError):
        return {**empty_coverage, "error": "invalid index limit"}
    root = Path(vault)
    sources, truncated = _sources(root)
    coverage = {"sources": len(sources), "indexed": 0, "new": 0,
                "truncated": truncated, "partial": bool(truncated)}
    try:
        connection = _connect(Path(db_path).expanduser() if db_path else _default_db_path())
    except (OSError, sqlite3.Error) as error:
        return {**coverage, "error": f"semantic cache unavailable ({type(error).__name__})"}
    try:
        key = _vault_key(root)
        _prune(connection, key, sources)
        missing: list[_Source] = []
        for source in sources:
            if _cached_any_dimensions(connection, key, model, source) is None:
                missing.append(source)
            else:
                coverage["indexed"] += 1
        failures = 0
        for source in _index_order(missing, "")[:new_limit]:
            try:
                value = _vector(embed(source.text, "RETRIEVAL_DOCUMENT"))
            except Exception:
                failures += 1
                continue
            if value is None:
                failures += 1
                continue
            connection.execute(
                "INSERT OR REPLACE INTO vectors(vault_key,model,dims,kind,identity,path,text_hash,vector) VALUES(?,?,?,?,?,?,?,?)",
                (key, model, len(value), source.kind, source.identity, source.path, source.text_hash,
                 json.dumps(value, separators=(",", ":"))))
            coverage["indexed"] += 1
            coverage["new"] += 1
        connection.commit()
        if len(missing) > coverage["new"]:
            coverage["partial"] = True
        error = f"document embeddings failed for {failures} source(s)" if failures else ""
        return {**coverage, "error": error}
    except (sqlite3.Error, OSError, ValueError) as error:
        return {**coverage, "error": f"semantic index failed ({type(error).__name__})"}
    finally:
        connection.close()


def search(vault, query, embed: Callable[[str, str], object], model: str = DEFAULT_MODEL,
           db_path=None, absolute_floor: float = DEFAULT_ABSOLUTE_FLOOR, relative_floor: float = 0.10,
           limit: int = 6, max_new: int = MAX_NEW) -> dict:
    """Search current public sources; embedding failures remain explicit and empty."""
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_SOURCE_CHARS:
        return _empty({"sources": 0, "indexed": 0, "new": 0, "truncated": 0, "partial": False}, "invalid query")
    if not callable(embed) or not isinstance(model, str) or not model or len(model) > 200:
        return _empty({"sources": 0, "indexed": 0, "new": 0, "truncated": 0, "partial": False}, "invalid embedding configuration")
    try:
        bound = max(1, min(50, int(limit)))
        new_limit = max(0, min(MAX_NEW, int(max_new)))
        absolute = float(absolute_floor)
        relative = float(relative_floor)
    except (TypeError, ValueError):
        return _empty({"sources": 0, "indexed": 0, "new": 0, "truncated": 0, "partial": False}, "invalid search limits")
    if not all(math.isfinite(item) for item in (absolute, relative)) or absolute < -1 or absolute > 1 or relative < 0:
        return _empty({"sources": 0, "indexed": 0, "new": 0, "truncated": 0, "partial": False}, "invalid score floors")
    root = Path(vault)
    sources, truncated = _sources(root)
    coverage = {"sources": len(sources), "indexed": 0, "new": 0, "truncated": truncated, "partial": bool(truncated)}
    if not sources:
        return _empty(coverage)
    try:
        query_vector = _vector(embed(_clip(query), "RETRIEVAL_QUERY"))
    except Exception as error:
        return _empty(coverage, f"query embedding failed ({type(error).__name__})")
    if query_vector is None:
        return _empty(coverage, "query embedding returned an invalid vector")
    try:
        connection = _connect(Path(db_path).expanduser() if db_path else _default_db_path())
    except (OSError, sqlite3.Error) as error:
        return _empty(coverage, f"semantic cache unavailable ({type(error).__name__})")
    try:
        key = _vault_key(root)
        _prune(connection, key, sources)
        vectors: dict[tuple[str, str], list[float]] = {}
        missing: list[_Source] = []
        for source in sources:
            cached = _cached(connection, key, model, len(query_vector), source)
            if cached is not None:
                vectors[(source.kind, source.identity)] = cached
            else:
                missing.append(source)
        for source in _index_order(missing, query)[:new_limit]:
            try:
                value = _vector(embed(source.text, "RETRIEVAL_DOCUMENT"))
            except Exception:
                continue
            if value is None:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO vectors(vault_key,model,dims,kind,identity,path,text_hash,vector) VALUES(?,?,?,?,?,?,?,?)",
                (key, model, len(value), source.kind, source.identity, source.path, source.text_hash,
                 json.dumps(value, separators=(",", ":"))))
            vectors[(source.kind, source.identity)] = value
            coverage["new"] += 1
        connection.commit()
        coverage["indexed"] = len(vectors)
        if len(missing) > coverage["new"]:
            coverage["partial"] = True
        ranked: list[tuple[float, _Source]] = []
        for source in sources:
            vector = vectors.get((source.kind, source.identity))
            if vector is None:
                continue
            score = _cosine(query_vector, vector)
            if score is not None:
                ranked.append((score, source))
        if not ranked:
            return _empty(coverage)
        ranked.sort(key=lambda pair: (-pair[0], pair[1].order, pair[1].identity))
        # `relative_floor` is a cosine-distance window, not a score ratio:
        # with its default 0.10, candidates must be within 0.10 of the best.
        threshold = max(absolute, ranked[0][0] - relative)
        current = {(source.kind, source.identity, source.path, source.text_hash) for source in _sources(root)[0]}
        hits = []
        for score, source in ranked:
            # Verify against a just-collected hash before exposing a hit.
            if score < threshold or (source.kind, source.identity, source.path, source.text_hash) not in current:
                continue
            hit = _hit(source, score)
            if validate_hit(root, hit):
                hits.append(hit)
            if len(hits) >= bound:
                break
        return {"hits": hits, "coverage": coverage, "error": ""}
    except (sqlite3.Error, OSError, ValueError) as error:
        return _empty(coverage, f"semantic search failed ({type(error).__name__})")
    finally:
        connection.close()


def validate_hit(vault, hit) -> bool:
    """Return whether a public hit still identifies an allowed current source."""
    if not isinstance(hit, dict) or hit.get("kind") not in {"thought", "resource", "task"}:
        return False
    for source in _sources(Path(vault))[0]:
        if (source.kind == hit.get("kind") and source.identity == hit.get("identity")
                and source.path == hit.get("path") and source.title == hit.get("title")
                and source.excerpt == hit.get("excerpt") and source.text_hash == hit.get("content_hash")):
            return True
    return False

