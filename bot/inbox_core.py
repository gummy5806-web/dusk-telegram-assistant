#!/usr/bin/env python3
"""Единственная реализация формата инбокса.

Её используют все входы захвата:
  - Mac:   Raycast-скрипты capture-inbox.sh / capture-private.sh
  - VPS:   bot/vault_ops.py -> append_to_inbox()

Держим логику в одном месте намеренно: раньше формат был продублирован
в bash и в Python, реализации разъехались, и это дало баги (второй инбокс,
битый frontmatter). Один вход — одна функция.

Модуль НИЧЕГО не знает про git: захват обязан быть мгновенным и без
побочных эффектов. Коммиты делает отдельный агент.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

HEADINGS = {
    "thought": "## 💭 Мысли и идеи",
    "task": "## ✅ Задачи",
    "link": "## 🔗 Ссылки и материалы",
}

DEFAULT_BODY = (
    "# 📥 Inbox\n\n"
    "## 💭 Мысли и идеи\n\n"
    "## ✅ Задачи\n\n"
    "## 🔗 Ссылки и материалы\n"
)

DEFAULT_PRIVATE_BODY = (
    "# 🔒 Private\n\n"
    "> В git не попадает (папка в .gitignore).\n"
    "> Если запись пришла через Telegram — она прошла через Telegram и VPS.\n"
    "> Разбор — вручную или локальной моделью.\n\n"
    "## 💭 Мысли и идеи\n\n"
    "## ✅ Задачи\n\n"
    "## 🔗 Ссылки и материалы\n"
)


def format_line(text: str, kind: str = "thought", moment: datetime | None = None) -> str:
    """Строка инбокса. Формат одинаков для всех входов."""
    stamp = (moment or datetime.now()).strftime("%H:%M")
    prefix = "- [ ] " if kind == "task" else "- "
    return f"{prefix}**{stamp}** {text.strip()}"


def append_line(
    inbox_path: str | Path,
    text: str,
    kind: str = "thought",
    moment: datetime | None = None,
    default_body: str = DEFAULT_BODY,
) -> str:
    """Добавляет строку в конец нужного раздела. Возвращает добавленную строку.

    Записи идут в конец раздела намеренно (FIFO): инбокс разбирают сверху вниз,
    поэтому старые записи должны оказаться выше новых.
    """
    text = (text or "").strip()
    if not text:
        return ""

    path = Path(inbox_path)
    heading = HEADINGS.get(kind, HEADINGS["thought"])
    line = format_line(text, kind, moment)
    return _insert(path, line, heading, default_body)


def append_raw(inbox_path, line: str, kind: str = "thought",
               default_body: str = DEFAULT_BODY) -> str:
    """Вставляет уже готовую строку, НЕ переставляя её метку времени.

    Нужно при переносе записи между инбоксами: время, когда мысль пришла,
    важнее времени, когда её разобрали.
    """
    line = (line or "").strip()
    if not line:
        return ""
    path = Path(inbox_path)
    heading = HEADINGS.get(kind, HEADINGS["thought"])
    return _insert(path, line, heading, default_body)


def _insert(path: Path, line: str, heading: str, default_body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(default_body, encoding="utf-8")

    lines = path.read_text(encoding="utf-8").splitlines()

    try:
        idx = next(i for i, l in enumerate(lines) if l.strip() == heading)
    except StopIteration:
        lines += ["", heading]
        idx = len(lines) - 1

    insert_at = idx + 1
    while insert_at < len(lines) and not lines[insert_at].startswith("## "):
        insert_at += 1
    while insert_at - 1 > idx and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    lines.insert(insert_at, line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return line


def vault_root() -> Path:
    """Корень валта: рядом с папкой bot/."""
    return Path(__file__).resolve().parent.parent


def inbox_path(private: bool = False) -> Path:
    """Публичный инбокс — в git. Приватный — в .gitignore, в историю не попадает."""
    root = vault_root()
    if private:
        return root / "Private" / "00 Private.md"
    # по правилам валта (CLAUDE.md) инбокс живёт в ZETA/FLEETING —
    # оттуда его видят дашборды HUB/Mail Box и Homepage
    return root / "ZETA" / "FLEETING" / "00 Инбокс.md"


def _main() -> int:
    """CLI для Raycast. Специально совместим с Python 3.9 (/usr/bin/python3)."""
    parser = argparse.ArgumentParser(description="Захват одной строки в инбокс")
    parser.add_argument("--text", required=True)
    parser.add_argument("--kind", default="thought", choices=sorted(HEADINGS))
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    line = append_line(
        inbox_path(args.private),
        args.text,
        args.kind,
        default_body=DEFAULT_PRIVATE_BODY if args.private else DEFAULT_BODY,
    )

    if not args.quiet:
        if not line:
            print("пусто — ничего не записал")
        else:
            where = "🔒 Private (вне git)" if args.private else "🧠 ZETA/FLEETING"
            section = HEADINGS.get(args.kind, HEADINGS["thought"]).replace("## ", "")
            print(f"записано → {where} · {section}")

    return 0 if line else 1


if __name__ == "__main__":
    raise SystemExit(_main())