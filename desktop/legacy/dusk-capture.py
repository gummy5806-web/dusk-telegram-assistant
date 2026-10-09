#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dusk-capture — окно захвата по хоткею.

Одна клавиша из любой программы → это окно → строка ушла в систему.
Enter — отправить, Esc — закрыть. Больше ничего нажимать не надо.

Что можно ввести (подсказка видна прямо в окне):
    просто текст      → мысль: заметка или задача, разберётся само
    !текст            → личное, вне git
    день: X, Y, Z     → задачи сразу на доску
    ?текст            → поиск по обоим валтам
    +контент 60       → счётчик Rule of 100
    ссылка            → видео в очередь на конспект
"""

import os
import subprocess
import sys
from pathlib import Path

VAULT = os.environ.get("VAULT_PATH", "~/Documents/obsidian/my dusk")
DISPATCH = str(Path(VAULT) / "bot" / "dispatch.py")

import tkinter as tk

BG = "#1e1e2e"
FG = "#cdd6f4"
MUTED = "#6c7086"
OK = "#a6e3a1"
PRIVATE_FG = "#f38ba8"

HINT = "мысль · !личное · день: задачи · ?поиск · +контент 60 · дальше/в работу/готово X · ссылка"
HINT_PRIVATE = "ЛИЧНОЕ — уйдёт в Private, в git не попадёт"


def run_dispatcher(text: str, private: bool) -> str:
    """Отдельный процесс: падение одного модуля не роняет окно."""
    cmd = [sys.executable, DISPATCH]
    if private:
        cmd += ["--private", "--text", text]
    else:
        cmd += ["--text", text]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        out = (r.stdout or "").strip()
        return out[:400] if out else "⚠️ Пустой ответ — проверь лог"
    except subprocess.TimeoutExpired:
        return "⚠️ Долго — но строка записана, проверь позже"
    except Exception as e:  # окно не должно падать из-за диспетчера
        return "⚠️ Ошибка: " + str(e)[:200]


def show(private: bool = False):
    root = tk.Tk()
    root.title("Dusk")
    root.configure(bg=BG)
    root.attributes("-topmost", True)
    root.resizable(False, False)

    w, h = 720, 130
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() // 3) - h // 2
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.overrideredirect(False)

    fg = PRIVATE_FG if private else FG
    entry = tk.Entry(root, font=("Helvetica", 18), bg=BG, fg=fg,
                     insertbackground=fg, relief="flat", width=54,
                     highlightthickness=1, highlightbackground="#45475a")
    entry.pack(padx=18, pady=(22, 8), fill="x")

    hint = tk.Label(root, text=HINT_PRIVATE if private else HINT,
                    font=("Helvetica", 11), bg=BG, fg=MUTED, wraplength=660)
    hint.pack(padx=18, pady=(0, 6))

    result = tk.Label(root, text="", font=("Helvetica", 12), bg=BG,
                      fg=OK, wraplength=660, justify="left")
    result.pack(padx=18, pady=(0, 10), fill="x")

    if private:
        entry.insert(0, "!")

    state = {"busy": False}

    def submit(_event=None):
        if state["busy"]:
            return
        text = entry.get().strip()
        if not text:
            root.destroy()
            return
        state["busy"] = True
        out = run_dispatcher(text, private)
        result.config(text=out)
        entry.delete(0, "end")
        root.after(1500, root.destroy)

    entry.bind("<Return>", submit)
    entry.bind("<Escape>", lambda e: root.destroy())
    entry.focus_set()
    entry.icursor("end" if not private else "end")

    root.mainloop()


if __name__ == "__main__":
    show("--private" in sys.argv)
