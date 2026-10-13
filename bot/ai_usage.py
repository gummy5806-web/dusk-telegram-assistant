#!/usr/bin/env python3
"""Local aggregate-only accounting for Dusk AI calls.

The ledger intentionally contains no prompts, responses, user identifiers, or
credentials.  Costs are estimates, not an invoice: the sole configured tariff
is the standard Gemini 2.5 Flash text tariff published by Google on 2026-10-14.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import queue_store


ALMATY = ZoneInfo("Asia/Almaty")
UTC = dt.timezone.utc
SUMMARY_REL = Path("SYSTEM/AI_USAGE/summary.json")
PRICING_SOURCE_URL = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"
PRICING_VERIFIED_ON = "2026-10-14"
MAX_PREVIOUS_DAYS = 31
MAX_PUBLISHED_BYTES = 32_768


@dataclass(frozen=True)
class TokenTariff:
    """Published standard text token rates in USD per million tokens."""

    input_per_million: float
    output_per_million: float
    source_url: str
    verified_on: str


@dataclass(frozen=True)
class CharacterTariff:
    """Published embedding rate in USD per thousand billable characters."""

    per_thousand_characters: float
    source_url: str
    verified_on: str


GEMINI_25_FLASH = TokenTariff(
    input_per_million=0.30,
    output_per_million=2.50,
    source_url=PRICING_SOURCE_URL,
    verified_on=PRICING_VERIFIED_ON,
)
GEMINI_EMBEDDING_001 = CharacterTariff(
    per_thousand_characters=0.00015,
    source_url=PRICING_SOURCE_URL,
    verified_on=PRICING_VERIFIED_ON,
)


def _database_path() -> Path:
    configured = os.getenv("DUSK_AI_USAGE_DB")
    return Path(configured).expanduser() if configured else Path.home() / ".local/share/dusk/ai-usage.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ai_usage_events (
            call_id TEXT PRIMARY KEY,
            occurred_at_utc TEXT NOT NULL,
            local_day TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            kind TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            thinking_tokens INTEGER NOT NULL,
            estimated INTEGER NOT NULL,
            status TEXT NOT NULL,
            error_type TEXT NOT NULL DEFAULT '',
            billable_characters INTEGER NOT NULL DEFAULT 0
        )"""
    )
    # The initial ledger schema shipped without character accounting.  Preserve
    # its rows when upgrading rather than recreating the local database.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_usage_events)")}
    if "billable_characters" not in columns:
        try:
            connection.execute(
                "ALTER TABLE ai_usage_events ADD COLUMN billable_characters INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            # A concurrent process may have completed the same migration.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_usage_events)")}
            if "billable_characters" not in columns:
                raise
    return connection


def _now(value=None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(UTC)
    if not isinstance(value, dt.datetime):
        raise TypeError("now must be an aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _nonnegative(value, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a non-negative integer") from error
    if number < 0 or number != value:
        raise ValueError(f"{name} must be a non-negative integer")
    return number


def _text(value, name: str, limit: int = 120) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit or "\x00" in result:
        raise ValueError(f"{name} is invalid")
    return result


def _error_type(value) -> str:
    if value is None:
        return ""
    result = str(value).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,119}", result):
        raise ValueError("error_type is invalid")
    return result


def _call_id(value) -> str:
    if value is None:
        return uuid.uuid4().hex
    result = str(value).lower()
    if not re.fullmatch(r"[a-f0-9]{32}", result):
        raise ValueError("call_id must be a 32-character lowercase hexadecimal UUID")
    return result


def record_call(*, provider, model, kind="generation", input_tokens=0,
                output_tokens=0, thinking_tokens=0, estimated=False,
                status="ok", call_id=None, now=None, error_type=None,
                billable_characters=0) -> dict:
    """Persist one idempotent, content-free call event and return that event.

    ``call_id`` is generated per attempted provider request.  Retrying a
    durable caller with the same id is safe: the first event wins.
    """
    happened = _now(now)
    event = {
        "call_id": _call_id(call_id),
        "occurred_at_utc": happened.isoformat(),
        "day": happened.astimezone(ALMATY).date().isoformat(),
        "provider": _text(provider, "provider"),
        "model": _text(model, "model"),
        "kind": _text(kind, "kind"),
        "input_tokens": _nonnegative(input_tokens, "input_tokens"),
        "output_tokens": _nonnegative(output_tokens, "output_tokens"),
        "thinking_tokens": _nonnegative(thinking_tokens, "thinking_tokens"),
        "billable_characters": _nonnegative(billable_characters, "billable_characters"),
        "estimated": bool(estimated),
        "status": _text(status, "status"),
        "error_type": _error_type(error_type),
    }
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT OR IGNORE INTO ai_usage_events
            (call_id, occurred_at_utc, local_day, provider, model, kind,
             input_tokens, output_tokens, thinking_tokens, estimated, status, error_type, billable_characters)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event["call_id"], event["occurred_at_utc"], event["day"], event["provider"],
             event["model"], event["kind"], event["input_tokens"], event["output_tokens"],
             event["thinking_tokens"], int(event["estimated"]), event["status"],
             event["error_type"], event["billable_characters"]),
        )
        row = connection.execute(
            """SELECT call_id, occurred_at_utc, local_day, provider, model, kind,
                      input_tokens, output_tokens, thinking_tokens, estimated, status, error_type, billable_characters
                 FROM ai_usage_events WHERE call_id = ?""", (event["call_id"],)
        ).fetchone()
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return _row_event(row)


def _row_event(row) -> dict:
    return {
        "call_id": row[0], "occurred_at_utc": row[1], "day": row[2],
        "provider": row[3], "model": row[4], "kind": row[5],
        "input_tokens": row[6], "output_tokens": row[7], "thinking_tokens": row[8],
        "estimated": bool(row[9]), "status": row[10], "error_type": row[11],
        "billable_characters": row[12],
    }


def _tariff(provider: str, model: str, kind: str) -> TokenTariff | CharacterTariff | None:
    if kind == "generation" and provider.casefold() == "vertex" and model.casefold() == "gemini-2.5-flash":
        return GEMINI_25_FLASH
    if kind == "embedding" and provider.casefold() == "vertex" and model.casefold() == "gemini-embedding-001":
        return GEMINI_EMBEDDING_001
    return None


def _day_summary(day: str, rows) -> dict:
    calls = len(rows)
    input_tokens = sum(row[6] for row in rows)
    output_tokens = sum(row[7] for row in rows)
    thinking_tokens = sum(row[8] for row in rows)
    billable_characters = sum(row[12] for row in rows)
    known_cost = 0.0
    cost_unknown = False
    for row in rows:
        tariff = _tariff(row[3], row[4], row[5])
        if row[10] != "ok" or row[9] or tariff is None:
            cost_unknown = True
            continue
        if isinstance(tariff, TokenTariff):
            known_cost += ((row[6] * tariff.input_per_million)
                           + ((row[7] + row[8]) * tariff.output_per_million)) / 1_000_000
        elif row[12] > 0:
            known_cost += row[12] * tariff.per_thousand_characters / 1_000
        else:
            # Google bills this embedding model by billable characters, not
            # its response token metadata.  Never substitute a token estimate.
            cost_unknown = True
    return {
        "date": day,
        "calls": calls,
        "failed_calls": sum(row[10] != "ok" for row in rows),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "billable_characters": billable_characters,
        # candidates and thought tokens are independent usage fields; do not
        # replace this with provider total_token_count, which may overlap them.
        "tokens": input_tokens + output_tokens + thinking_tokens,
        "estimated_cost_usd": round(known_cost, 10),
        "cost_unknown": cost_unknown,
    }


def summary(now=None) -> dict:
    """Return today plus up to 31 preceding Almaty calendar days."""
    moment = _now(now)
    current = moment.astimezone(ALMATY).date()
    earliest = (current - dt.timedelta(days=MAX_PREVIOUS_DAYS)).isoformat()
    connection = _connect()
    try:
        rows = connection.execute(
            """SELECT call_id, occurred_at_utc, local_day, provider, model, kind,
                      input_tokens, output_tokens, thinking_tokens, estimated, status, error_type, billable_characters
                 FROM ai_usage_events
                WHERE local_day >= ? AND local_day <= ? ORDER BY local_day DESC, occurred_at_utc ASC""",
            (earliest, current.isoformat()),
        ).fetchall()
    finally:
        connection.close()
    grouped = {}
    for row in rows:
        grouped.setdefault(row[2], []).append(row)
    today = _day_summary(current.isoformat(), grouped.pop(current.isoformat(), []))
    previous = [_day_summary(day, events) for day, events in sorted(grouped.items(), reverse=True)]
    return {
        "generated_at_utc": moment.isoformat(),
        "timezone": "Asia/Almaty",
        "pricing": {"label": "примерно, не точное списание", "source_url": PRICING_SOURCE_URL,
                    "verified_on": PRICING_VERIFIED_ON},
        "today": today,
        "previous_days": previous,
    }


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _mkdir_safe(root: Path, target: Path) -> None:
    if root.is_symlink() or not root.is_dir() or not _inside(root, root):
        raise ValueError("vault directory is unsafe")
    try:
        parts = target.relative_to(root).parts
    except ValueError as error:
        raise ValueError("publish path is outside vault") from error
    cursor = root
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("publish directory is unsafe")
        if not cursor.exists():
            cursor.mkdir()
        if cursor.is_symlink() or not cursor.is_dir() or not _inside(root, cursor):
            raise ValueError("publish directory is unsafe")


def _published_path(vault) -> tuple[Path, Path]:
    root = Path(vault)
    return root, root / SUMMARY_REL


def publish(vault, now=None) -> Path:
    """Atomically publish aggregate JSON to a fixed, non-symlinked vault path."""
    root, path = _published_path(vault)
    _mkdir_safe(root, path.parent)
    if path.is_symlink() or (path.exists() and (not path.is_file() or not _inside(root, path))):
        raise ValueError("published summary path is unsafe")
    value = summary(now=now)
    previous = read_published(root)
    if _same_publication(previous, value):
        return path
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_PUBLISHED_BYTES:
        raise ValueError("published summary exceeds bound")
    queue_store.atomic_write(path, text + "\n")
    return path


def _same_publication(previous, current: dict) -> bool:
    """Ignore the volatile publication timestamp when aggregate content matches."""
    if not isinstance(previous, dict):
        return False
    earlier = dict(previous)
    later = dict(current)
    earlier.pop("generated_at_utc", None)
    later.pop("generated_at_utc", None)
    return earlier == later


def read_published(vault) -> dict | None:
    """Read only a bounded JSON aggregate from the fixed safe path."""
    root, path = _published_path(vault)
    if (root.is_symlink() or path.is_symlink() or not path.is_file() or not _inside(root, path)
            or any(parent.is_symlink() for parent in (root / "SYSTEM", root / "SYSTEM" / "AI_USAGE"))):
        return None
    try:
        if path.stat().st_size > MAX_PUBLISHED_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _format_day(day: dict) -> str:
    estimate = f"${day['estimated_cost_usd']:.6f} примерно"
    unknown = "; часть расходов неизвестна" if day.get("cost_unknown") else ""
    return f"запросов {day['calls']}; токенов {day['tokens']}; {estimate}{unknown}"


def render(vault) -> str:
    """Render the published aggregate in a compact human-readable form."""
    value = read_published(vault)
    if value is None or not isinstance(value.get("today"), dict):
        return "🤖 Расход AI недоступен."
    today = value["today"]
    text = f"🤖 Расход AI за {today.get('date', '?')}: {_format_day(today)}."
    return text[:899]


def daily_notice(vault, now=None) -> tuple[str, str] | None:
    """Return a deterministic prior-Almaty-day notice only when it had calls."""
    value = read_published(vault)
    if value is None:
        return None
    previous_day = (_now(now).astimezone(ALMATY).date() - dt.timedelta(days=1)).isoformat()
    candidates = value.get("previous_days")
    if not isinstance(candidates, list):
        return None
    day = next((item for item in candidates if isinstance(item, dict) and item.get("date") == previous_day), None)
    if not day or not isinstance(day.get("calls"), int) or day["calls"] <= 0:
        return None
    notice_id = hashlib.sha256(f"dusk-ai-usage:{previous_day}".encode("utf-8")).hexdigest()[:24]
    return notice_id, f"🤖 Расход AI. За {previous_day}: {_format_day(day)}."

