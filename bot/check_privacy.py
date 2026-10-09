#!/usr/bin/env python3
"""Не даёт личному и секретам утечь в git.

Зачем: правило «приватное не коммитить» бесполезно, если оно держится на памяти.
Этот скрипт превращает правило в отказ: пока утечка возможна, коммит не проходит.

Запуск вручную:      python3 bot/check_privacy.py
Как git-хук:         bot/install_hooks.sh   (хуки не синхронизируются через git)

Совместим с Python 3.9 (/usr/bin/python3), аннотации не используются.
"""

import re
import subprocess
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent

# Папки, содержимое которых по определению приватно и не должно попадать в git
PRIVATE_PREFIXES = ("Private/",)

# Признак, который triage обязан не оставлять в отслеживаемых заметках
PRIVATE_FRONTMATTER = re.compile(r"^\s*private:\s*true\s*$", re.MULTILINE)

SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "API-ключ (sk-...)"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "GitHub token (ghp_...)"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "GitHub fine-grained token"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "Telegram bot token"),
    (re.compile(r"AIza[A-Za-z0-9_-]{30,}"), "Google API key"),
]

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".py", ".sh", ".json", ".base",
                 ".canvas", ".yml", ".yaml", ".toml", ".env", ".css", ".js"}
MAX_BYTES = 2_000_000  # минифицированные бандлы не читаем целиком


def git(*args):
    res = subprocess.run(["git"] + list(args), cwd=VAULT,
                         capture_output=True, text=True)
    return res.stdout


def tracked_files():
    return [line for line in git("ls-files").splitlines() if line.strip()]


def check_staged_private(files, problems):
    for path in files:
        if any(path.startswith(prefix) for prefix in PRIVATE_PREFIXES):
            problems.append(
                "ПРИВАТНОЕ В GIT: %s — файл отслеживается, хотя должен жить вне git" % path
            )


def check_private_frontmatter(problems):
    # быстрый поиск по индексу, без чтения всех файлов
    hits = git("grep", "-l", "-E", r"^private:[[:space:]]*true", "--", ".")
    for path in [p for p in hits.splitlines() if p.strip()]:
        problems.append(
            "ПРИВАТНАЯ ЗАМЕТКА В GIT: %s — личное должно лежать в Private/, а не в TaskNotes" % path
        )


def check_secrets(files, problems):
    for rel in files:
        path = VAULT / rel
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(text):
                problems.append("СЕКРЕТ В GIT: %s — похоже на %s" % (rel, label))
                break


def main():
    files = tracked_files()
    problems = []

    check_staged_private(files, problems)
    check_private_frontmatter(problems)
    check_secrets(files, problems)

    if problems:
        print("Проверка приватности: НАЙДЕНЫ ПРОБЛЕМЫ (%d)\n" % len(problems))
        for p in problems:
            print("  • " + p)
        print("\nКоммит заблокирован. Личное — в Private/ (он вне git), секреты — в .env.")
        return 1

    print("Проверка приватности: чисто (%d файлов в git)" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())