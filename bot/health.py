#!/usr/bin/env python3
"""Read-only health-check: только чтение, короткий bounded отчёт.

Зачем отдельный модуль. Пользователю нужна одна честная команда «здоровье»,
которая отвечает на вопрос «работает ли система», ничего не меняя. Здесь нет
ни модели, ни сети, ни git-мутаций, ни запуска/остановки сервисов, ни записи
файлов. Каждый probe изолирован: ошибка одного даёт `unknown`, а не падение
всего отчёта.

Что проверяется:

* **Очередь обучения** — число pending-ссылок в `Учеба/очередь.md`. Файла нет —
  честный ноль; файл повреждён/не читается — `unknown`.
* **Шесть известных LaunchAgents** — настроен ли plist, загружен ли агент,
  последнее доступное завершение и время последней активности по mtime
  **известных** log-файлов. Содержимое логов не читается.
* **Sync** — dirty-файлы и ahead/behind относительно **локально известного**
  upstream. Сеть не используется: fetch не выполняется, и это явно написано.
* **Версия кода** — git HEAD (если доступен) и детерминированный digest
  выбранных core-файлов. Набор считается полным или digest не выдаётся.
* **Интерфейс** — executable внутри `Dusk.app`, реальная конфигурация статусов
  плагина TaskNotes (`.obsidian/plugins/tasknotes/data.json`) и отдельно
  `columnOrder` доски. Конфигурация хоткеев читается только безопасным
  read-only способом, иначе `unknown`.

Чего модуль не делает и не должен делать: `launchctl print` (показывает
унаследованное окружение), чтение `.env`/environment/credential-файлов,
содержимого логов, `git fetch/pull/push/add/commit`, сеть, API, LLM, `osascript`
с изменениями, запуск/остановку сервисов и любую запись. В отчёт не попадают
raw exception/stdout/stderr и пути с потенциальными секретами.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path

import queue_store

MAX_REPORT_CHARS = 4000
MAX_LINE_CHARS = 240

QUEUE_PATH = "Учеба/очередь.md"
BASE_PATH = "Board/Задачи.base"
# Реальную конфигурацию статусов держит плагин TaskNotes, а не доска.
TASKNOTES_CONFIG = ".obsidian/plugins/tasknotes/data.json"

EXPECTED_STATUSES = ("open", "next", "in-progress", "done")
EXPECTED_LABELS = {
    "open": "📥 Список",
    "next": "➡️ Дальше",
    "in-progress": "🔥 В работе",
    "done": "✅ Готово",
}

# Ровно шесть известных агентов Dusk. Другие plist-файлы не перечисляются,
# чтобы отчёт не превращался в инвентаризацию чужого окружения.
LAUNCH_AGENTS = (
    "com.dusk.triage",
    "com.dusk.learn",
    "com.dusk.permanent",
    "com.dusk.status",
    "com.dusk.vault.sync",
    "com.dusk.vault.autopush",
)

# Известные log-файлы: только метаданные (mtime), содержимое не читается.
AGENT_LOGS = {
    "com.dusk.triage": (
        "/tmp/dusk-triage.log", "/tmp/dusk-triage-out.log", "/tmp/dusk-triage-err.log"),
    "com.dusk.learn": ("/tmp/dusk-learn.log",),
    "com.dusk.permanent": ("/tmp/dusk-permanent.log",),
    "com.dusk.status": ("/tmp/dusk-status.log",),
    "com.dusk.vault.sync": ("/tmp/dusk-sync.log", "/tmp/dusk-sync-error.log"),
    "com.dusk.vault.autopush": ("/tmp/dusk-push.log", "/tmp/dusk-push-error.log"),
}

# Digest считается по содержимому, поэтому не зависит от mtime и порядка ФС.
CORE_FILES = (
    "bot/dispatch.py",
    "bot/health.py",
    "bot/weekly_review.py",
    "bot/market.py",
    "bot/learning_cycle.py",
    "bot/queue_store.py",
    "bot/weekly_plan.py",
)

DUSK_APP_CANDIDATES = (
    Path.home() / "Applications" / "Dusk.app",
    Path("/Applications/Dusk.app"),
)
SHKDRC = Path.home() / ".skhdrc"
SKHD_MARKERS = ("dusk://capture", "dusk://private", "dusk://ask")

_LABEL_RE = re.compile(r'"Label"\s*=\s*"([^"]+)"')
_LAST_EXIT_RE = re.compile(r'"LastExitStatus"\s*=\s*(-?\d+)')
_COLUMN_ORDER_RE = re.compile(r'"note\.status"\s*:\s*(\[[^\]]*\])')


# ── процесс ─────────────────────────────────────────────────────────────────

def _run(argv, timeout=5):
    """Единственная точка вызова внешних команд. Никогда не использует shell.

    Возвращает `CompletedProcess` или `None` при недоступности команды. Сырой
    stdout никуда не печатается: probes разбирают только whitelisted поля.
    """
    try:
        return subprocess.run(
            list(argv), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _inside(path, root) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def _read_json_bounded(path, limit: int = 262144):
    """Читает небольшой JSON или `None`. Содержимое наружу не выводится."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) > limit:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _limit_line(text: str) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= MAX_LINE_CHARS:
        return text
    return text[:MAX_LINE_CHARS - 1].rstrip() + "…"


# ── очередь обучения ────────────────────────────────────────────────────────

def queue_pending(vault) -> int | None:
    """Число pending-ссылок или `None`=unknown при повреждении/ошибке.

    Отсутствие файла — честный ноль: очередь пуста, терять нечего.
    """
    path = Path(vault) / QUEUE_PATH
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        return len(queue_store.queue_entries(text))
    except Exception:  # noqa: BLE001 — повреждение очереди не должно ронять отчёт
        return None


# ── LaunchAgents ────────────────────────────────────────────────────────────

def last_activity(log_paths):
    """Последний mtime среди известных log-файлов или `None`.

    Читается только metadata через `stat`, содержимое логов не открывается.
    Отсутствие времени честно остаётся `None`, а не «только что».
    """
    latest = None
    for raw in log_paths or ():
        try:
            info = Path(raw).stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        moment = datetime.fromtimestamp(info.st_mtime)
        if latest is None or moment > latest:
            latest = moment
    return latest


def agent_status(label: str, agents_dir=None) -> dict:
    """Статус одного известного агента. Никаких сырых строк в результате."""
    directory = Path(agents_dir) if agents_dir is not None \
        else Path.home() / "Library" / "LaunchAgents"
    plist = directory / f"{label}.plist"
    try:
        configured = plist.is_file()
    except OSError:
        configured = False

    loaded = None
    last_exit = None
    result = _run(["/bin/launchctl", "list", label], timeout=3)
    if result is not None:
        out = result.stdout or ""
        match = _LABEL_RE.search(out)
        if match and match.group(1) == label:
            loaded = True
            exit_match = _LAST_EXIT_RE.search(out)
            if exit_match:
                try:
                    last_exit = int(exit_match.group(1))
                except ValueError:
                    last_exit = None
        else:
            loaded = False

    return {
        "label": label,
        "configured": configured,
        "loaded": loaded,
        "last_exit": last_exit,
        "last_activity": last_activity(AGENT_LOGS.get(label, ())),
    }


# ── sync (только локально известный upstream) ───────────────────────────────

def git_head(vault) -> str | None:
    result = _run(["/usr/bin/git", "-C", str(vault), "rev-parse", "--short", "HEAD"],
                  timeout=5)
    if result is None or result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def sync_status(vault) -> dict:
    """dirty/ahead/behind относительно локально известного upstream.

    Никакой сети: команды только читают локальное состояние репозитория.
    Отсутствие upstream — `None` (unknown), а не «всё синхронизировано».
    """
    vault = Path(vault)
    status = {"dirty": None, "upstream": None, "ahead": None, "behind": None,
              "head": None}

    dirty = _run(["/usr/bin/git", "-C", str(vault), "status", "--porcelain"],
                 timeout=5)
    if dirty is not None and dirty.returncode == 0:
        status["dirty"] = sum(
            1 for line in (dirty.stdout or "").splitlines() if line.strip())

    upstream = _run([
        "/usr/bin/git", "-C", str(vault), "rev-parse",
        "--abbrev-ref", "--symbolic-full-name", "@{upstream}",
    ], timeout=5)
    if upstream is not None and upstream.returncode == 0:
        name = (upstream.stdout or "").strip()
        if name:
            status["upstream"] = name

    if status["upstream"]:
        counts = _run([
            "/usr/bin/git", "-C", str(vault), "rev-list",
            "--left-right", "--count", "@{upstream}...HEAD",
        ], timeout=5)
        if counts is not None and counts.returncode == 0:
            parts = (counts.stdout or "").split()
            if len(parts) == 2:
                try:
                    status["behind"], status["ahead"] = int(parts[0]), int(parts[1])
                except ValueError:
                    pass

    status["head"] = git_head(vault)
    return status


# ── версия кода ─────────────────────────────────────────────────────────────

def code_digest(vault, files=CORE_FILES) -> str | None:
    """Детерминированный digest core-файлов — только если набор полон.

    Fail closed: если хотя бы один файл отсутствует, не является обычным файлом
    внутри валта, читается через symlink наружу или недоступен — возвращается
    `None`. Частичный набор не является версией всего кода.
    """
    vault = Path(vault)
    parts = []
    for rel in files:
        path = vault / rel
        if not _inside(path, vault):
            return None
        if path.is_symlink() and not _inside(path.resolve(), vault):
            return None
        try:
            if not path.is_file():
                return None
            data = path.read_bytes()
        except OSError:
            return None
        parts.append((rel, hashlib.sha256(data).digest()))
    if not parts:
        return None
    builder = hashlib.sha256()
    for rel, digest in parts:
        builder.update(rel.encode("utf-8"))
        builder.update(b"\0")
        builder.update(digest)
    return builder.hexdigest()[:16]


# ── интерфейс ───────────────────────────────────────────────────────────────

def dusk_app_path(candidates=None) -> str | None:
    """Имя найденного `Dusk.app` (без полного пути) или `None`."""
    for app in candidates if candidates is not None else DUSK_APP_CANDIDATES:
        macos = Path(app) / "Contents" / "MacOS"
        try:
            if not macos.is_dir():
                continue
            for child in macos.iterdir():
                if child.is_file() and os.access(child, os.X_OK):
                    return Path(app).name
        except OSError:
            continue
    return None


def board_statuses(vault):
    """Четыре статуса из `columnOrder` доски или `None`=unknown.

    Отдельный probe: доска показывает колонки, но не доказывает реальную
    конфигурацию плагина TaskNotes.
    """
    try:
        text = (Path(vault) / BASE_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _COLUMN_ORDER_RE.search(text)
    if not match:
        return None
    try:
        values = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return None
    return tuple(values)


def tasknotes_state(vault) -> dict:
    """Реальная конфигурация статусов плагина TaskNotes.

    Читается только `.obsidian/plugins/tasknotes/data.json`. Требуются четыре
    точных `id`/`value`, метки `📥 Список`, `➡️ Дальше`, `🔥 В работе`,
    `✅ Готово`, `done.isCompleted=true` и `false` у остальных. Отсутствие,
    повреждение, неверный тип и дубли — `unknown`/`mismatch`, не 4/4.
    """
    data = _read_json_bounded(Path(vault) / TASKNOTES_CONFIG)
    if not isinstance(data, dict):
        return {"status": "unknown", "count": 0, "statuses": ()}
    entries = data.get("customStatuses")
    if not isinstance(entries, list):
        return {"status": "unknown", "count": 0, "statuses": ()}

    seen = set()
    valid = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ident = entry.get("id")
        value = entry.get("value")
        if not (isinstance(ident, str) and isinstance(value, str)):
            continue
        if ident != value or ident not in EXPECTED_LABELS:
            continue
        if ident in seen:
            return {"status": "mismatch", "count": 0, "statuses": ()}
        seen.add(ident)
        if entry.get("label") != EXPECTED_LABELS[ident]:
            continue
        completed = entry.get("isCompleted")
        if not isinstance(completed, bool) or completed != (ident == "done"):
            continue
        valid.add(ident)

    ordered = tuple(status for status in EXPECTED_STATUSES if status in valid)
    if len(entries) == len(EXPECTED_STATUSES) and len(ordered) == len(EXPECTED_STATUSES):
        return {"status": "configured", "count": len(ordered), "statuses": ordered}
    return {"status": "mismatch", "count": len(ordered), "statuses": ordered}


def tasknotes_statuses(vault):
    """Строгий снимок плагина: четыре статуса или `None`=unknown/mismatch."""
    state = tasknotes_state(vault)
    if state["status"] == "configured" and state["statuses"] == EXPECTED_STATUSES:
        return state["statuses"]
    return None


def shortcuts_status(path=None, limit: int = 65536) -> str:
    """Безопасная read-only проверка fallback-хоткеев в skhd, иначе `unknown`.

    Читается только начало файла конфигурации; содержимое в отчёт не попадает.
    Caps+N/P/A живут в BTT и здесь не проверяются — это честно `unknown`.
    """
    try:
        raw = Path(path if path is not None else SHKDRC).read_bytes()
    except OSError:
        return "unknown"
    text = raw[:limit].decode("utf-8", errors="ignore")
    found = [marker for marker in SKHD_MARKERS if marker in text]
    if len(found) == len(SKHD_MARKERS):
        return "fallback skhd найден"
    if found:
        return "fallback skhd частично"
    return "unknown"


# ── отчёт ───────────────────────────────────────────────────────────────────

def _render_agent(state: dict) -> str:
    configured = "configured" if state["configured"] else "missing"
    if state["loaded"] is True:
        loaded = "loaded"
    elif state["loaded"] is False:
        loaded = "not loaded"
    else:
        loaded = "loaded unknown"
    if state["last_exit"] is None:
        exit_text = "unknown"
    elif state["last_exit"] == 0:
        exit_text = "success"
    else:
        exit_text = f"exit {state['last_exit']}"
    moment = state["last_activity"]
    when = moment.strftime("%Y-%m-%d %H:%M") if moment else "unknown"
    return _limit_line(
        f"{state['label']}: {configured}, {loaded}, {exit_text}, активность: {when}")


def _render_sync(state: dict) -> list:
    dirty = "unknown" if state["dirty"] is None else str(state["dirty"])
    upstream = "upstream unknown" if state["upstream"] is None \
        else f"upstream {state['upstream']}"
    if state["ahead"] is None or state["behind"] is None:
        movement = "ahead/behind unknown"
    else:
        movement = f"ahead {state['ahead']}, behind {state['behind']}"
    return [
        _limit_line(f"Sync: dirty {dirty}, {upstream}, {movement}"),
        "  сеть: fetch не выполнялся, upstream взят локально",
    ]


def _render_interface(vault) -> list:
    app = dusk_app_path()
    notes = tasknotes_state(vault)
    board = board_statuses(vault)
    if notes["status"] == "unknown":
        notes_text = "TaskNotes unknown"
    else:
        notes_text = f"TaskNotes {notes['count']}/4"
    if board is None:
        board_text = "board unknown"
    else:
        board_have = sum(1 for status in EXPECTED_STATUSES if status in board)
        board_text = f"board {board_have}/4"
    return [_limit_line(
        f"Интерфейс: Dusk.app {'найден' if app else 'не найден'}; "
        f"{notes_text}; {board_text}; хоткеи {shortcuts_status()}")]


def report(vault=None, agents_dir=None) -> str:
    """Короткий bounded отчёт. Только чтение, каждый probe изолирован."""
    vault = Path(vault or os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault")))

    lines = ["🩺 Здоровье Dusk (только чтение)"]

    pending = queue_pending(vault)
    lines.append(_limit_line(
        "Очередь обучения: " + ("unknown" if pending is None else f"{pending} pending")))

    lines.append("LaunchAgents:")
    for label in LAUNCH_AGENTS:
        try:
            state = agent_status(label, agents_dir=agents_dir)
        except Exception:  # noqa: BLE001 — один probe не роняет отчёт
            lines.append(_limit_line(f"{label}: unknown"))
            continue
        lines.append("  " + _render_agent(state))

    try:
        sync = sync_status(vault)
    except Exception:  # noqa: BLE001
        sync = {"dirty": None, "upstream": None, "ahead": None, "behind": None,
                "head": None}
    lines.extend(_render_sync(sync))

    try:
        digest = code_digest(vault)
    except Exception:  # noqa: BLE001
        digest = None
    lines.append(_limit_line(
        "Версия кода: HEAD " + (sync["head"] or "unknown")
        + ", digest " + (digest or "unknown")))

    try:
        lines.extend(_render_interface(vault))
    except Exception:  # noqa: BLE001
        lines.append("Интерфейс: unknown")

    return "\n".join(lines)[:MAX_REPORT_CHARS]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Read-only health-check Dusk")
    parser.add_argument("--vault", default=None)
    args = parser.parse_args()
    print(report(args.vault))
