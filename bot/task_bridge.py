#!/usr/bin/env python3
"""Task Bridge: Scans daily notes for new tasks and creates TaskNotes files."""

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

VAULT_PATH = os.getenv("VAULT_PATH", os.path.expanduser("~/dusk-vault"))
DAILY_DIR = Path(VAULT_PATH) / "DAILY" / "DAILY"
TASKNOTES_DIR = Path(VAULT_PATH) / "TaskNotes" / "Tasks"

# Template/habit tasks to skip
SKIP_PATTERNS = [
    "утреца", "английский", "голос", "zeland", "keho", "манифест",
    "python --", "content in shift", "content in", "new item",
    "отметь если", "06:20", "06:30"
]

def git_sync():
    subprocess.run(["git", "pull", "--rebase"], cwd=VAULT_PATH, capture_output=True)

def git_push(message):
    subprocess.run(["git", "add", "-A"], cwd=VAULT_PATH, capture_output=True)
    result = subprocess.run(["git", "commit", "-m", message], cwd=VAULT_PATH, capture_output=True, text=True)
    if result.returncode == 0:
        subprocess.run(["git", "push"], cwd=VAULT_PATH, capture_output=True)
        return True
    return False

def extract_tasks(filepath):
    """Extract incomplete tasks from a daily note."""
    tasks = []
    content = filepath.read_text(encoding="utf-8")
    for line in content.split("\n"):
        line_stripped = line.strip()
        # Match incomplete tasks
        match = re.match(r'^-\s*\[\s\]\s*(.+)$', line_stripped)
        if not match:
            continue

        task_text = match.group(1).strip()

        # Remove task plugin emoji dates and tags for cleaner title
        clean_text = re.sub(r'[\U0001F4C5\U0001F5D3\u2B73\U0001F6EB]\s*\d{4}-\d{2}-\d{2}', '', task_text).strip()
        clean_text = re.sub(r'#\S+', '', clean_text).strip()

        # Skip if too short
        if len(clean_text) < 5:
            continue

        # Skip template/habit tasks
        if any(pattern in clean_text.lower() for pattern in SKIP_PATTERNS):
            continue

        tasks.append({
            "text": clean_text,
            "original": task_text,
            "source": filepath.name
        })

    return tasks

def task_exists(title):
    """Check if a TaskNotes file with similar title already exists."""
    if not TASKNOTES_DIR.exists():
        return False

    title_lower = title.lower().strip()
    for f in TASKNOTES_DIR.glob("*.md"):
        content = f.read_text(encoding="utf-8")
        # Check title in frontmatter
        for line in content.split("\n"):
            if line.startswith("title:"):
                existing_title = line.split(":", 1)[1].strip().strip('"').strip("'").lower()
                if existing_title == title_lower:
                    return True
                # Fuzzy match - if 80% of words match
                existing_words = set(existing_title.split())
                new_words = set(title_lower.split())
                if existing_words and new_words:
                    overlap = len(existing_words & new_words) / max(len(existing_words), len(new_words))
                    if overlap > 0.8:
                        return True
                break
    return False

def create_tasknote(task):
    """Create a TaskNotes file for a task."""
    TASKNOTES_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
    today = datetime.now().strftime("%Y-%m-%d")

    # Create safe filename - keep alphanumeric, spaces, hyphens, underscores, and Cyrillic
    safe_title = "".join(c for c in task["text"] if c.isalnum() or c in " -_" or ('\u0400' <= c <= '\u04ff')).strip()[:50]
    if not safe_title:
        safe_title = f"task-{datetime.now().strftime('%H%M%S')}"

    filename = f"{safe_title}.md"
    filepath = TASKNOTES_DIR / filename

    # Avoid overwriting
    counter = 1
    while filepath.exists():
        filepath = TASKNOTES_DIR / f"{safe_title} {counter}.md"
        counter += 1

    source_note = task["source"].replace(".md", "")

    note_content = f"""---
title: "{task['text']}"
status: open
priority: normal
created: {timestamp}
scheduled: {today}
tags:
  - task
  - from-daily
source: "DAILY/DAILY/{task['source']}"
---

# {task['text']}

Captured from daily note: [[{source_note}]]
"""

    filepath.write_text(note_content, encoding="utf-8")
    return filepath.name

def main():
    git_sync()

    if not DAILY_DIR.exists():
        print("DAILY/DAILY not found")
        return

    created = []

    for daily_file in sorted(DAILY_DIR.glob("*.md")):
        tasks = extract_tasks(daily_file)
        for task in tasks:
            if not task_exists(task["text"]):
                filename = create_tasknote(task)
                created.append(f"{filename} (from {task['source']})")
                print(f"Created: {filename}")

    if created:
        git_push(f"task-bridge: created {len(created)} TaskNotes from daily notes")
        print(f"\nTotal: {len(created)} new tasks created")
    else:
        print("No new tasks found")

if __name__ == "__main__":
    main()
