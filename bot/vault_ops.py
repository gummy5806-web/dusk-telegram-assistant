import json
import os
import re
import subprocess
import textwrap
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

import frontmatter
import inbox_core

VAULT_PATH = os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault"))

def git_sync():
    # --autostash: в рабочем дереве бывают незакоммиченные правки (кэши плагинов,
    # свежий захват). Без него pull --rebase падает с "cannot pull with rebase" —
    # именно эта ошибка месяцами ломала синхронизацию на Mac.
    subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=VAULT_PATH, capture_output=True)

def git_push(message: str):
    subprocess.run(["git", "add", "-A"], cwd=VAULT_PATH, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=VAULT_PATH, capture_output=True)
    subprocess.run(["git", "push"], cwd=VAULT_PATH, capture_output=True)

# --- Единый инбокс ---
# Формат строки живёт в inbox_core: тот же модуль вызывают Raycast-скрипты на Mac.
# Приватный инбокс лежит в Private/ (в .gitignore) — в git-историю он не попадает.
INBOX_PATH = Path(VAULT_PATH) / "ZETA" / "FLEETING" / "00 Инбокс.md"
PRIVATE_INBOX_PATH = Path(VAULT_PATH) / "Private" / "00 Private.md"

# Нейтральное сообщение коммита: ни пользовательского текста, ни его первого
# фрагмента в истории git.
INBOX_COMMIT_MESSAGE = "inbox: новая запись"

def append_to_inbox(text: str, kind: str = "thought", private: bool = False) -> str:
    """Одна строка в инбокс. Приватное — в Private/, который не попадает в git."""
    # Приватный маршрут и так вне git: никакого tracked sync для него нет.
    if not private:
        git_sync()

    target = PRIVATE_INBOX_PATH if private else INBOX_PATH
    body = inbox_core.DEFAULT_PRIVATE_BODY if private else inbox_core.DEFAULT_BODY
    line = inbox_core.append_line(target, text, kind, default_body=body)

    if line and not private:
        # Статичное сообщение: текст записи в историю коммитов не попадает.
        git_push(INBOX_COMMIT_MESSAGE)
    return line

def create_fleeting_note(title: str, content: str) -> str:
    git_sync()
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
    safe_title = "".join(c for c in title if c.isalnum() or c in " -_\u0430-\u044f\u0410-\u042f\u0451\u0401").strip()[:50]
    filename = f"{safe_title}.md"
    filepath = Path(VAULT_PATH) / "ZETA" / "FLEETING" / filename
    note = f"""---
connections:
tags:
  - fleeting_note
type: fleeting_note
created: {timestamp}
---

{content}
"""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(note, encoding="utf-8")
    git_push(f"fleeting: {safe_title}")
    return str(filepath)

def create_task(title: str, priority: str = "normal", due_date: str = "") -> str:
    git_sync()
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
    today = datetime.now().strftime("%Y-%m-%d")
    safe_title = "".join(c for c in title if c.isalnum() or c in " -_\u0430-\u044f\u0410-\u042f\u0451\u0401").strip()[:50]
    filename = f"{safe_title}.md"
    filepath = Path(VAULT_PATH) / "TaskNotes" / "Tasks" / filename
    due_block = f"due: {due_date}\n" if due_date else ""
    safe_yaml_title = title.replace('"', "'")
    note = f"""---
title: "{safe_yaml_title}"
status: open
priority: {priority}
created: {timestamp}
scheduled: {today}
{due_block}tags:
  - task
---

# {title}
"""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(note, encoding="utf-8")
    git_push(f"task: {safe_title} [{priority}]")
    return str(filepath)

def _human_minutes(raw):
    """120 -> '2 ч', 45 -> '45 мин'. Молча возвращает пусто, если оценки нет."""
    try:
        n = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n < 60:
        return f"{n} мин"
    hours, minutes = divmod(n, 60)
    return f"{hours} ч" if minutes == 0 else f"{hours} ч {minutes} мин"


def _clean_title(content, fallback):
    """Заголовок из frontmatter, а не имя файла.

    Имя файла обрезано до 50 символов и может содержать мусор от старых
    автоматизаций ('mark stylebackground ...'). В frontmatter лежит нормальный
    заголовок, поэтому берём его.
    """
    title = frontmatter.get(content, "title") or fallback
    title = re.sub(r"\s+", " ", title)
    return title if title else fallback


def get_pending_tasks(limit: int = 12) -> str:
    """Список незакрытых задач, пригодный для чтения с телефона.

    Группируем по времени, а не по внутреннему полю priority: 'normal'
    у большинства задач, и повторять его в каждой строке — шум.
    """
    git_sync()
    vault = Path(VAULT_PATH)
    rows = []

    tn_dir = vault / "TaskNotes" / "Tasks"
    if tn_dir.exists():
        for f in sorted(tn_dir.glob("*.md")):
            content = f.read_text(encoding="utf-8")
            status = priority = est = ""
            for line in content.split("\n"):
                if line.startswith("status:"):
                    status = line.split(":", 1)[1].strip()
                elif line.startswith("priority:"):
                    priority = line.split(":", 1)[1].strip()
                elif line.startswith("timeEstimate:"):
                    est = line.split(":", 1)[1].strip()
            if not status or status == "done":
                continue
            title = _clean_title(content, f.stem)
            # обрезка по границе слова: резать посреди слова на телефоне выглядит как мусор
            title = textwrap.shorten(title, width=52, placeholder="…") or title[:52]
            rows.append({
                "title": title,
                "priority": priority or "normal",
                "est": est,
                "minutes": int(float(est)) if est.replace(".", "", 1).isdigit() else 0,
            })

    inbox = vault / "ZETA" / "FLEETING" / "00 Инбокс.md"
    unprocessed = 0
    if inbox.exists():
        unprocessed = len([l for l in inbox.read_text(encoding="utf-8").splitlines() if l.startswith("- ")])

    total = len(rows)
    out = [f"📋 Осталось задач: {total}"]

    if unprocessed:
        word = "запись" if unprocessed % 10 == 1 and unprocessed % 100 != 11 else (
            "записи" if unprocessed % 10 in (2, 3, 4) and unprocessed % 100 not in (12, 13, 14) else "записей")
        out.append(f"📥 В инбоксе: {unprocessed} {word} — нажать «Разобрать инбокс»")

    def line(r):
        duration = _human_minutes(r["minutes"])
        return f"• {r['title']}" + (f" — {duration}" if duration else "")

    hot = [r for r in rows if r["priority"] == "high"]
    rest = [r for r in rows if r["priority"] != "high"]

    quick = sorted([r for r in rest if 0 < r["minutes"] <= 30], key=lambda r: r["minutes"])
    medium = sorted([r for r in rest if 30 < r["minutes"] <= 60], key=lambda r: r["minutes"])
    long_ = sorted([r for r in rest if r["minutes"] > 60], key=lambda r: -r["minutes"])
    unknown = [r for r in rest if not r["minutes"]]

    shown = 0

    def section(head, items):
        nonlocal shown
        if not items or shown >= limit:
            return
        out.append("")
        out.append(head)
        for r in items:
            if shown >= limit:
                return
            out.append(line(r))
            shown += 1

    section("🔥 Сначала это", hot[:3])
    section("⚡ До получаса — закрыть быстро", quick[:4])
    section("⏳ Час и больше", long_[:3])
    section("🕐 Полчаса — час", medium[:3])
    section("❔ Без оценки", unknown[:2])

    hidden = total - shown
    if hidden > 0:
        out.append("")
        out.append(f"…и ещё {hidden} — весь список в борде «Задачи.base»")

    return "\n".join(out)


def run_claude_code(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "--print", "-p", prompt],
            cwd=VAULT_PATH,
            capture_output=True,
            text=True,
            timeout=300
        )
        return result.stdout or result.stderr or "No output"
    except subprocess.TimeoutExpired:
        return "Request timed out (5 min limit)"
    except Exception as e:
        return f"Error: {str(e)}"

def run_llm(prompt: str, timeout: int = 180) -> str:
    """Запрос к любой OpenAI-совместимой модели. Только стандартная библиотека.

    Настраивается через .env валта, поэтому модель можно менять без правки кода:
        LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
        LLM_API_KEY=...
        LLM_MODEL=qwen-plus

    Специально без SDK: у бота на VPS нет ни claude-авторизации, ни Vertex,
    а лишняя зависимость — ещё одна вещь, которая может сломаться.
    """
    base = (os.getenv("LLM_BASE_URL") or "").rstrip("/")
    key = os.getenv("LLM_API_KEY") or ""
    model = os.getenv("LLM_MODEL") or "qwen-plus"

    if not base or not key:
        return ("Модель не настроена. Добавь в .env валта LLM_BASE_URL, "
                "LLM_API_KEY и LLM_MODEL — после этого разбор заработает.")

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode("utf-8")

    req = urllib.request.Request(
        base + "/chat/completions",
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", errors="ignore")
        return f"Ошибка модели HTTP {e.code}: {detail}"
    except Exception as e:
        return f"Ошибка модели ({type(e).__name__}): {str(e)[:200]}"

def get_daily_summary() -> str:
    """Итог дня БЕЗ модели: только свои данные и арифметика.

    Раньше это уходило в стороннюю LLM. Считать закрытые задачи и остаток
    можно локально — тогда модель для этого не вызывается вовсе.
    """
    git_sync()
    vault = Path(VAULT_PATH)
    tn_dir = vault / "TaskNotes" / "Tasks"
    today = datetime.now().strftime("%Y-%m-%d")

    closed_today, open_rows = [], []
    order = {"high": 0, "normal": 1, "low": 2}

    if tn_dir.exists():
        for f in sorted(tn_dir.glob("*.md")):
            content = f.read_text(encoding="utf-8")
            status = priority = completed = due = est = ""
            for line in content.split("\n"):
                if line.startswith("status:"):
                    status = line.split(":", 1)[1].strip()
                elif line.startswith("priority:"):
                    priority = line.split(":", 1)[1].strip()
                elif line.startswith("completedDate:"):
                    completed = line.split(":", 1)[1].strip()
                elif line.startswith("due:"):
                    due = line.split(":", 1)[1].strip()
                elif line.startswith("timeEstimate:"):
                    est = line.split(":", 1)[1].strip()
            title = _clean_title(content, f.stem)
            if status == "done":
                if completed.startswith(today):
                    closed_today.append(title)
                continue
            minutes = int(float(est)) if est.replace(".", "", 1).isdigit() else 0
            overdue = bool(due) and due < today
            open_rows.append((order.get(priority or "normal", 1), overdue, -minutes, priority or "normal", title, minutes))

    open_rows.sort()
    out = [f"📊 Итог за {today}"]
    out.append("")
    out.append(f"✅ Закрыто сегодня: {len(closed_today)}")
    for title in closed_today[:5]:
        out.append(f"• {title[:60]}")
    out.append("")
    out.append(f"⏳ Осталось: {len(open_rows)}")
    top = open_rows[:3]
    if top:
        out.append("")
        out.append("🎯 Топ-3 на завтра")
        for _, overdue, neg, priority, title, minutes in top:
            mark = " (просрочено)" if overdue else ""
            dur = f" — {_human_minutes(minutes)}" if minutes else ""
            out.append(f"• {title[:58]}{dur}{mark}")
    return "\n".join(out)
