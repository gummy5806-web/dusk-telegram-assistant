#!/usr/bin/env python3
"""Этап 5: минимальный интерфейс, корневой недельный обзор и read-only health.

Всё на временных валтах. Модель, сеть и git-мутации запрещены. Проверяются
маршрутизация `здоровье`, изоляция probes, защита от утечек, путь корневого
обзора и синтетический UAT источник → применение → наблюдение → обзор.
"""

from __future__ import annotations

import ast
import datetime
import glob
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import dispatch
import health
import learning_cycle as lc
import llm
import market
import queue_store
import vault_ops
import weekly_plan
import weekly_review as wr


def forbidden(*args, **kwargs):
    raise AssertionError("Внешний вызов (модель/сеть/git) запрещён в тестах этапа 5")


llm.ask = forbidden
llm._project = forbidden
vault_ops.git_push = forbidden
vault_ops.git_sync = forbidden

ROOT = Path(__file__).resolve().parent.parent
BASE_SHA256 = "7b86958815b413bbd3b42f6d9bd2bcd2019307c2b13c1d7779ced2e4da294bbf"
DAY = datetime.date(2026, 10, 11)          # воскресенье недели 2026-10-05…11
NOTE_DIR = "ZETA/LITERATURE/Видео/🔎note summary"
SECRET = "s3cr3t-token-value"

MARKET_TEXT = (
    "# 💰 Рынок\n\n## Оффер и канал\n\n"
    "- **Оффер:** не задан\n- **Кому:** не задано\n"
    "- **Цена:** не задана\n- **Канал:** не выбран\n\n"
    "## Дни\n\n"
    "| дата | Контент | Публикации | Аутрич | Лиды | Разговоры | Офферы | Продажи | Деньги |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def launchctl_stdout(label, last_exit=None, pid=None, extra=""):
    lines = ["{", f'\t"Label" = "{label}";']
    if last_exit is not None:
        lines.append(f'\t"LastExitStatus" = {last_exit};')
    if pid is not None:
        lines.append(f'\t"PID" = {pid};')
    if extra:
        lines.append(extra)
    lines.append("};")
    return "\n".join(lines) + "\n"


class Stage5Base(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        (self.vault / "TaskNotes" / "Tasks").mkdir(parents=True)
        (self.vault / "Учеба").mkdir(parents=True)
        (self.vault / NOTE_DIR).mkdir(parents=True)
        (self.vault / market.MARKET_PATH).write_text(MARKET_TEXT, encoding="utf-8")

    def write_market(self, rows=()) -> None:
        text = MARKET_TEXT
        for row in rows:
            text += "| " + " | ".join(str(cell) for cell in row) + " |\n"
        (self.vault / market.MARKET_PATH).write_text(text, encoding="utf-8")

    def write_pair(self, *, name="Шаг", pair="pair0001", status_="open",
                   completed="", observed=None, conclusion=None):
        note_rel = f"{NOTE_DIR}/Разбор.md"
        (self.vault / note_rel).write_text(
            "---\n"
            "reference: https://youtu.be/aaaaaaaaaaa\n"
            "generator: dusk\n"
            f"{lc.MARK}\n{lc.PAIR_FIELD}: {pair}\n"
            "---\n\n# Разбор\n", encoding="utf-8")
        task = (
            "---\n"
            f'title: "{name}"\nstatus: {status_}\ncreated: 2026-10-06T10:00\n'
            + (f"completedDate: {completed}\n" if completed else "")
            + f'source: "{note_rel}"\n{lc.MARK}\n{lc.PAIR_FIELD}: {pair}\n'
            "---\n\n"
            f"# {name}\n\n"
            f"{lc.CRITERION_HEADING}\n\nвидно результат — гипотеза\n\n"
            f"{lc.TRIED_HEADING}\n\nнет данных\n\n"
            f"{lc.OBSERVED_HEADING}\n\n{observed or lc.NO_DATA}\n\n"
            f"{lc.CONCLUSION_HEADING}\n\n{conclusion or lc.NO_DATA}\n"
        )
        (self.vault / "TaskNotes" / "Tasks" / f"{name}.md").write_text(task, encoding="utf-8")
        return note_rel


# ── 1. маршрутизация и отсутствие записи ────────────────────────────────────

class RoutingTests(unittest.TestCase):
    def test_health_is_routed_before_privacy(self) -> None:
        self.assertEqual(dispatch.resolve_capture("здоровье").kind, "health")
        self.assertEqual(dispatch.resolve_capture("health").kind, "health")
        self.assertEqual(dispatch.resolve_capture("ЗДОРОВЬЕ").kind, "health")

    def test_preview_mentions_health(self) -> None:
        self.assertIn("Здоровье", dispatch.preview("здоровье"))

    def test_dispatch_health_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            with patch.object(dispatch, "VAULT", vault), \
                    patch.object(health, "report", return_value="HEALTH-OK"):
                self.assertEqual(dispatch.dispatch("здоровье"), "HEALTH-OK")
            self.assertEqual(list(vault.iterdir()), [], "health не пишет файлы")

    def test_private_prefix_still_wins(self) -> None:
        self.assertEqual(dispatch.resolve_capture("!здоровье").kind, "private")


# ── 2. очередь обучения ─────────────────────────────────────────────────────

class QueueProbeTests(unittest.TestCase):
    def test_missing_file_is_honest_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(health.queue_pending(Path(directory)), 0)

    def test_valid_queue_counts_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / health.QUEUE_PATH
            path.parent.mkdir(parents=True)
            queue_store.atomic_write(
                path, "# header\n\n- https://youtu.be/a\n- https://youtu.be/b\n")
            self.assertEqual(health.queue_pending(Path(directory)), 2)

    def test_corrupt_queue_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / health.QUEUE_PATH
            path.parent.mkdir(parents=True)
            path.write_bytes(b"\xff\xfe- https://youtu.be/a\n")
            self.assertIsNone(health.queue_pending(Path(directory)))


# ── 3. LaunchAgents ─────────────────────────────────────────────────────────

class LaunchAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.agents_dir = Path(directory.name)

    def _runner(self, stdout):
        return lambda argv, timeout=None: result(0, stdout)

    def test_missing_plist_and_not_loaded(self) -> None:
        with patch.object(health, "_run", self._runner('Could not find service "x"')):
            state = health.agent_status("com.dusk.triage", agents_dir=self.agents_dir)
        self.assertFalse(state["configured"])
        self.assertFalse(state["loaded"])
        self.assertIsNone(state["last_exit"])

    def test_not_loaded_but_configured(self) -> None:
        (self.agents_dir / "com.dusk.triage.plist").write_text("<plist/>", encoding="utf-8")
        with patch.object(health, "_run", self._runner('Could not find service "x"')):
            state = health.agent_status("com.dusk.triage", agents_dir=self.agents_dir)
        self.assertTrue(state["configured"])
        self.assertFalse(state["loaded"])

    def test_last_exit_success_and_nonzero(self) -> None:
        (self.agents_dir / "com.dusk.triage.plist").write_text("<plist/>", encoding="utf-8")
        with patch.object(health, "_run", self._runner(launchctl_stdout("com.dusk.triage", 0))):
            state = health.agent_status("com.dusk.triage", agents_dir=self.agents_dir)
        self.assertTrue(state["loaded"])
        self.assertEqual(state["last_exit"], 0)

        with patch.object(health, "_run", self._runner(launchctl_stdout("com.dusk.triage", 78))):
            state = health.agent_status("com.dusk.triage", agents_dir=self.agents_dir)
        self.assertEqual(state["last_exit"], 78)

    def test_missing_exit_status_is_unknown(self) -> None:
        (self.agents_dir / "com.dusk.triage.plist").write_text("<plist/>", encoding="utf-8")
        with patch.object(health, "_run", self._runner(launchctl_stdout("com.dusk.triage"))):
            state = health.agent_status("com.dusk.triage", agents_dir=self.agents_dir)
        self.assertTrue(state["loaded"])
        self.assertIsNone(state["last_exit"])

    def test_launchctl_unavailable_is_unknown(self) -> None:
        with patch.object(health, "_run", return_value=None):
            state = health.agent_status("com.dusk.triage", agents_dir=self.agents_dir)
        self.assertIsNone(state["loaded"])

    def test_last_activity_comes_from_log_mtime_not_now(self) -> None:
        log = self.agents_dir / "triage.log"
        log.write_text("", encoding="utf-8")
        stamp = datetime.datetime(2026, 10, 1, 12, 30).timestamp()
        os.utime(log, (stamp, stamp))
        with patch.dict(health.AGENT_LOGS, {"com.dusk.triage": (str(log),)}):
            activity = health.last_activity(health.AGENT_LOGS["com.dusk.triage"])
        self.assertEqual(activity, datetime.datetime.fromtimestamp(stamp))

    def test_missing_log_time_is_not_now(self) -> None:
        with patch.dict(health.AGENT_LOGS, {"com.dusk.triage": ()}):
            self.assertIsNone(health.last_activity(()))
        line = health._render_agent({
            "label": "com.dusk.triage", "configured": True, "loaded": True,
            "last_exit": 0, "last_activity": None,
        })
        self.assertIn("активность: unknown", line)

    def test_raw_launchctl_stdout_and_secret_never_leak(self) -> None:
        stdout = launchctl_stdout("com.dusk.triage", 0, extra=f'\t"Secret" = "{SECRET}";')
        git = _GitRunner()
        def runner(argv, timeout=None):
            if "launchctl" in argv:
                return result(0, stdout)
            return git(argv, timeout=timeout)

        with patch.object(health, "_run", runner), \
                patch.dict(health.AGENT_LOGS, {}), \
                patch.object(health, "DUSK_APP_CANDIDATES", ()), \
                patch.object(health, "SHKDRC", self.agents_dir / "missing"):
            text = health.report(self.agents_dir / "vault")
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Secret", text)
        self.assertNotIn("Could not find", text)
        self.assertLessEqual(len(text), health.MAX_REPORT_CHARS)

    def test_probe_failure_isolated_to_unknown(self) -> None:
        with patch.object(health, "agent_status", side_effect=RuntimeError("boom")), \
                patch.object(health, "sync_status", side_effect=RuntimeError("boom")), \
                patch.object(health, "code_digest", side_effect=RuntimeError("boom")), \
                patch.object(health, "tasknotes_state", side_effect=RuntimeError("boom")), \
                patch.object(health, "DUSK_APP_CANDIDATES", ()), \
                patch.object(health, "SHKDRC", Path("/nonexistent-skhdrc")):
            text = health.report("/tmp/vault")
        self.assertIn("Очередь обучения", text)
        self.assertIn("unknown", text)
        self.assertLessEqual(len(text), health.MAX_REPORT_CHARS)

    def test_no_launchctl_print_used(self) -> None:
        seen = []
        def runner(argv, timeout=None):
            seen.append(list(argv))
            return result(0, launchctl_stdout("com.dusk.triage", 0))
        with patch.object(health, "_run", runner):
            health.agent_status("com.dusk.triage", agents_dir=self.agents_dir)
        self.assertTrue(all("print" not in argv for argv in seen))


# ── 4. sync (локальный upstream, без сети) ──────────────────────────────────

class _GitRunner:
    """Симулятор read-only git. Никакие мутации не допускаются."""

    FORBIDDEN = ("fetch", "pull", "push", "add", "commit", "checkout", "reset")

    def __init__(self, *, dirty=0, upstream="origin/master", ahead=0, behind=0,
                 head="abc1234", fail_status=False, fail_upstream=False,
                 fail_counts=False):
        self.dirty = dirty
        self.upstream = upstream
        self.ahead = ahead
        self.behind = behind
        self.head = head
        self.fail_status = fail_status
        self.fail_upstream = fail_upstream
        self.fail_counts = fail_counts
        self.commands = []

    def __call__(self, argv, timeout=None):
        argv = list(argv)
        self.commands.append(argv)
        for token in argv:
            if token in self.FORBIDDEN:
                raise AssertionError(f"git-мутация запрещена: {argv}")
        if "status" in argv and "--porcelain" in argv:
            if self.fail_status:
                return result(128, "", "fail")
            lines = "".join(" M file%d\n" % i for i in range(self.dirty))
            return result(0, lines)
        if "@{upstream}" in argv:
            if self.upstream is None or self.fail_upstream:
                return result(128, "", "no upstream")
            return result(0, self.upstream + "\n")
        if "rev-list" in argv:
            if self.fail_counts:
                return result(128, "", "fail")
            return result(0, f"{self.behind}\t{self.ahead}\n")
        if "rev-parse" in argv and "HEAD" in argv:
            if self.head is None:
                return result(128, "", "no head")
            return result(0, self.head + "\n")
        return result(1, "", "")


class SyncProbeTests(unittest.TestCase):
    def test_clean_repo(self) -> None:
        runner = _GitRunner(dirty=0, ahead=0, behind=0)
        with patch.object(health, "_run", runner):
            state = health.sync_status("/tmp/vault")
        self.assertEqual(state["dirty"], 0)
        self.assertEqual(state["upstream"], "origin/master")
        self.assertEqual((state["ahead"], state["behind"]), (0, 0))

    def test_dirty_ahead_behind(self) -> None:
        runner = _GitRunner(dirty=3, ahead=2, behind=1)
        with patch.object(health, "_run", runner):
            state = health.sync_status("/tmp/vault")
        self.assertEqual(state["dirty"], 3)
        self.assertEqual((state["ahead"], state["behind"]), (2, 1))

    def test_no_upstream_is_unknown_not_ok(self) -> None:
        runner = _GitRunner(upstream=None)
        with patch.object(health, "_run", runner):
            state = health.sync_status("/tmp/vault")
        self.assertIsNone(state["upstream"])
        self.assertIsNone(state["ahead"])
        self.assertIsNone(state["behind"])

    def test_git_failure_is_unknown(self) -> None:
        runner = _GitRunner(fail_status=True, fail_counts=True, head=None)
        with patch.object(health, "_run", runner):
            state = health.sync_status("/tmp/vault")
        self.assertIsNone(state["dirty"])
        self.assertIsNone(state["ahead"])
        self.assertIsNone(state["head"])

    def test_report_states_no_fetch_and_no_mutations(self) -> None:
        runner = _GitRunner(dirty=1, ahead=1, behind=0)
        with patch.object(health, "_run", runner), \
                patch.object(health, "DUSK_APP_CANDIDATES", ()), \
                patch.object(health, "SHKDRC", Path("/nonexistent-skhdrc")):
            text = health.report("/tmp/vault")
        self.assertIn("fetch не выполнялся", text)
        self.assertIn("ahead 1, behind 0", text)
        for argv in runner.commands:
            for token in _GitRunner.FORBIDDEN:
                self.assertNotIn(token, argv)


# ── 5. digest кода ──────────────────────────────────────────────────────────

class VersionTests(unittest.TestCase):
    def _make_code(self, vault: Path, content="x = 1\n"):
        for rel in ("bot/dispatch.py", "bot/health.py", "bot/weekly_review.py"):
            path = vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def test_digest_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            self._make_code(vault)
            files = ("bot/dispatch.py", "bot/health.py", "bot/weekly_review.py")
            first = health.code_digest(vault, files)
            second = health.code_digest(vault, files)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 16)

    def test_digest_changes_with_content_not_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            self._make_code(vault)
            files = ("bot/dispatch.py", "bot/health.py", "bot/weekly_review.py")
            before = health.code_digest(vault, files)
            target = vault / "bot/health.py"
            stamp = datetime.datetime(2030, 1, 1).timestamp()
            os.utime(target, (stamp, stamp))
            self.assertEqual(health.code_digest(vault, files), before,
                             "mtime не влияет на digest")
            target.write_text("x = 2\n", encoding="utf-8")
            self.assertNotEqual(health.code_digest(vault, files), before)

    def test_digest_missing_files_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(health.code_digest(Path(directory)))

    def test_partial_core_set_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / "bot").mkdir()
            (vault / "bot" / "one.py").write_text("x = 1\n", encoding="utf-8")
            self.assertIsNone(
                health.code_digest(vault, files=("bot/one.py", "bot/missing.py")))
            self.assertIsNone(
                health.code_digest(vault, files=("bot/one.py", "../escape.py")))

    def test_symlink_outside_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                tempfile.TemporaryDirectory() as outside:
            vault = Path(directory)
            (vault / "bot").mkdir()
            target = Path(outside) / "outside.py"
            target.write_text("x = 1\n", encoding="utf-8")
            (vault / "bot" / "link.py").symlink_to(target)
            self.assertIsNone(health.code_digest(vault, files=("bot/link.py",)))


# ── 6. документация и байтовая сохранность доски ────────────────────────────

class DocsTests(unittest.TestCase):
    def docs(self):
        return "\n".join([
            (ROOT / "Пульт.md").read_text(encoding="utf-8"),
            (ROOT / "Board" / "Начать здесь.md").read_text(encoding="utf-8"),
        ])

    def test_real_shortcuts_and_statuses(self) -> None:
        text = self.docs()
        for token in ("Caps+N", "Caps+P", "Caps+A"):
            self.assertIn(token, text)
        for token in ("open", "next", "in-progress", "done",
                      "📥 Список", "➡️ Дальше", "🔥 В работе", "✅ Готово"):
            self.assertIn(token, text)

    def test_no_stale_hotkeys_statuses_or_norm(self) -> None:
        text = self.docs()
        for stale in ("⌃⌥Space", "⌃⌥⇧Space", "doing", "waiting", "inbox",
                      "Rule of 100", "100 минут"):
            self.assertNotIn(stale, text, f"устаревшее обещание: {stale}")

    def test_weekly_review_is_documented_as_existing(self) -> None:
        text = self.docs()
        self.assertIn("Недельный обзор.md", text)
        self.assertNotIn("Недельного обзора нет", text)

    def test_pult_does_not_promise_comma_split(self) -> None:
        pult = (ROOT / "Пульт.md").read_text(encoding="utf-8")
        self.assertEqual(len(dispatch._task_items("снять видео, позвонить")), 1)
        self.assertNotIn("день: снять видео, позвонить", pult)
        self.assertIn("каждая — с новой строки", " ".join(pult.split()))

    def test_pult_calls_search_and_review_deterministic(self) -> None:
        compact = " ".join(
            (ROOT / "Пульт.md").read_text(encoding="utf-8").split())
        self.assertNotIn(
            "Разбор ссылок на видео, поиск по базе и сборка статуса "
            "запускаются на Mac и используют модель", compact)
        self.assertIn("Детерминировано, без модели и сети", compact)

    def test_base_file_is_bytes_identical(self) -> None:
        digest = hashlib.sha256(
            (ROOT / "Board" / "Задачи.base").read_bytes()).hexdigest()
        self.assertEqual(digest, BASE_SHA256)


# ── 6a. TaskNotes plugin config vs board ────────────────────────────────────

class TaskNotesPluginTests(Stage5Base):
    def _valid_statuses(self):
        return [
            {"id": "open", "value": "open", "label": "📥 Список", "isCompleted": False},
            {"id": "next", "value": "next", "label": "➡️ Дальше", "isCompleted": False},
            {"id": "in-progress", "value": "in-progress", "label": "🔥 В работе", "isCompleted": False},
            {"id": "done", "value": "done", "label": "✅ Готово", "isCompleted": True},
        ]

    def _write_config(self, statuses) -> None:
        path = self.vault / health.TASKNOTES_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"customStatuses": statuses}), encoding="utf-8")

    def _write_board(self) -> None:
        path = self.vault / health.BASE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'columnOrder: \'{"note.status":["open","next","in-progress","done"]}\'\n',
            encoding="utf-8")

    def test_valid_plugin_config(self) -> None:
        self._write_config(self._valid_statuses())
        self.assertEqual(health.tasknotes_statuses(self.vault), health.EXPECTED_STATUSES)
        self.assertEqual(health.tasknotes_state(self.vault)["status"], "configured")

    def test_missing_plugin_config_is_unknown(self) -> None:
        self.assertIsNone(health.tasknotes_statuses(self.vault))
        self.assertEqual(health.tasknotes_state(self.vault)["status"], "unknown")

    def test_partial_plugin_config_is_not_4_of_4(self) -> None:
        statuses = self._valid_statuses()
        self._write_config([statuses[0], statuses[3]])
        self.assertIsNone(health.tasknotes_statuses(self.vault))
        self.assertEqual(health.tasknotes_state(self.vault)["count"], 2)

    def test_board_alone_does_not_prove_plugin_statuses(self) -> None:
        self._write_board()
        self.assertIsNone(health.tasknotes_statuses(self.vault),
                          "валидный columnOrder не доказывает статусы плагина")
        self.assertEqual(health.board_statuses(self.vault), health.EXPECTED_STATUSES)

    def test_wrong_label_is_mismatch(self) -> None:
        statuses = self._valid_statuses()
        statuses[1]["label"] = "Дальше"
        self._write_config(statuses)
        self.assertIsNone(health.tasknotes_statuses(self.vault))

    def test_wrong_is_completed_is_mismatch(self) -> None:
        statuses = self._valid_statuses()
        statuses[3]["isCompleted"] = False
        self._write_config(statuses)
        self.assertIsNone(health.tasknotes_statuses(self.vault))

    def test_duplicate_status_is_mismatch(self) -> None:
        statuses = self._valid_statuses()
        statuses.append(dict(statuses[0]))
        self._write_config(statuses)
        self.assertIsNone(health.tasknotes_statuses(self.vault))
        self.assertEqual(health.tasknotes_state(self.vault)["status"], "mismatch")

    def test_corrupt_json_is_unknown(self) -> None:
        path = self.vault / health.TASKNOTES_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe not json")
        self.assertIsNone(health.tasknotes_statuses(self.vault))

    def test_report_shows_both_tasknotes_and_board(self) -> None:
        self._write_config(self._valid_statuses())
        self._write_board()
        with patch.object(health, "_run", _GitRunner()), \
                patch.dict(health.AGENT_LOGS, {}), \
                patch.object(health, "DUSK_APP_CANDIDATES", ()), \
                patch.object(health, "SHKDRC", Path("/nonexistent-skhdrc")):
            text = health.report(self.vault)
        self.assertIn("TaskNotes 4/4", text)
        self.assertIn("board 4/4", text)

    def test_report_partial_plugin_never_says_4_of_4(self) -> None:
        statuses = self._valid_statuses()
        self._write_config([statuses[0], statuses[3]])
        self._write_board()
        with patch.object(health, "_run", _GitRunner()), \
                patch.dict(health.AGENT_LOGS, {}), \
                patch.object(health, "DUSK_APP_CANDIDATES", ()), \
                patch.object(health, "SHKDRC", Path("/nonexistent-skhdrc")):
            text = health.report(self.vault)
        self.assertIn("TaskNotes 2/4", text)
        self.assertIn("board 4/4", text)
        self.assertNotIn("TaskNotes 4/4", text)


# ── 6a2. shared Telegram prefixes ───────────────────────────────────────────

class SharedPrefixTests(unittest.TestCase):
    def test_shared_prefixes(self) -> None:
        for text in ("задача: купить", "задачи: купить", "todo: купить",
                     "мысль: идея", "идея: идея", "заметка: идея",
                     "ЗАДАЧА: купить"):
            with self.subTest(text=text):
                self.assertTrue(dispatch.shared_prefix(text))

    def test_non_shared_prefixes(self) -> None:
        for text in ("задачи на день: купить", "день: купить", "!личное",
                     "?вопрос", "дальше задача", "+контент 60", "просто текст"):
            with self.subTest(text=text):
                self.assertFalse(dispatch.shared_prefix(text))

    def test_shared_prefixes_route_to_task_or_note(self) -> None:
        self.assertEqual(dispatch.resolve_capture("задача: купить").kind, "task")
        self.assertEqual(dispatch.resolve_capture("todo: купить").kind, "task")
        self.assertEqual(dispatch.resolve_capture("мысль: идея").kind, "note")
        self.assertEqual(dispatch.resolve_capture("заметка: идея").kind, "note")

    def test_telegram_routes_shared_prefixes_through_dispatcher(self) -> None:
        source = (ROOT / "bot" / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("shared_prefix", source)
        self.assertRegex(source, r"\w+\.dispatch_detailed\(text\)")


# ── 6b. статический интерфейс и AST ─────────────────────────────────────────

class StaticInterfaceTests(unittest.TestCase):
    def test_telegram_uses_shared_health_renderer(self) -> None:
        source = (ROOT / "bot" / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("BTN_HEALTH", source)
        self.assertIn('CommandHandler("health"', source)
        self.assertIn("health.report", source)

    def test_interface_doc_lists_three_surfaces(self) -> None:
        text = (ROOT / "bot" / "INTERFACE.md").read_text(encoding="utf-8")
        for surface in ("Пульт.md", "Board/Задачи.base", "Недельный обзор.md"):
            self.assertIn(surface, text)
        self.assertIn("здоровье", text)

    def test_all_bot_sources_parse(self) -> None:
        files = sorted(glob.glob(str(ROOT / "bot" / "*.py")))
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=path):
                ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)


# ── 7. корневой недельный обзор ─────────────────────────────────────────────

class WeeklyReviewRootTests(Stage5Base):
    def test_review_path_is_vault_root(self) -> None:
        path = wr.review_path(self.vault)
        self.assertEqual(path, self.vault / "Недельный обзор.md")
        self.assertEqual(path.parent, self.vault)

    def test_build_page_without_day_uses_single_owner_day(self) -> None:
        owner = datetime.date(2026, 10, 7)
        with patch.object(wr.weekly_plan, "today_local", return_value=owner) as clock:
            page = wr.build_page(self.vault)
        self.assertIn("Обзор недели 2026-10-05 — 2026-10-11", page)
        self.assertEqual(clock.call_count, 1,
                         "owner-day фиксируется один раз для всех секций")

    def test_generate_without_day_creates_root_review(self) -> None:
        owner = datetime.date(2026, 10, 7)
        with patch.object(wr.weekly_plan, "today_local", return_value=owner):
            ok, message = wr.generate(self.vault)
        self.assertTrue(ok, message)
        path = wr.review_path(self.vault)
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("2026-10-05", text)
        self.assertIn("2026-10-11", text)

    def test_stale_cards_never_receive_none_day(self) -> None:
        owner = datetime.date(2026, 10, 7)
        with patch.object(wr.weekly_plan, "today_local", return_value=owner), \
                patch.object(wr, "_stale_cards", wraps=wr._stale_cards) as stale:
            wr.build_page(self.vault)
        self.assertTrue(stale.called)
        self.assertIsInstance(stale.call_args.args[1], datetime.date)

    def test_generation_writes_root_and_preserves_user_zone(self) -> None:
        ok, _ = wr.generate(self.vault, DAY)
        self.assertTrue(ok)
        path = wr.review_path(self.vault)
        self.assertTrue(path.exists())
        first = path.read_text(encoding="utf-8")
        edited = first.replace("- **Фокус:** —", "- **Фокус:** мой фокус")
        path.write_text(edited, encoding="utf-8")

        ok, _ = wr.generate(self.vault, DAY)
        self.assertTrue(ok)
        second = path.read_text(encoding="utf-8")
        self.assertEqual(wr.read_decisions(second)["Фокус"], "мой фокус")
        gen_first = first.split(wr.GEN_START, 1)[1].split(wr.GEN_END, 1)[0]
        gen_second = second.split(wr.GEN_START, 1)[1].split(wr.GEN_END, 1)[0]
        self.assertEqual(gen_first, gen_second)

    def test_crlf_user_span_and_suffix_preserved(self) -> None:
        self.assertTrue(wr.generate(self.vault, DAY)[0])
        path = wr.review_path(self.vault)
        raw = path.read_text(encoding="utf-8")
        user = raw.split(wr.USER_START, 1)[1].split(wr.USER_END, 1)[0]
        user = user.replace("- **Фокус:** —",
                            "- **Фокус:** строка\n- **Фокус:** строка ещё раз")
        custom = raw.split(wr.USER_START, 1)[0] + wr.USER_START + user + wr.USER_END \
            + "\nПользовательский хвост\nПользовательский хвост\n"
        crlf = custom.replace("\n", "\r\n")
        path.write_bytes(crlf.encode("utf-8"))
        user_before = crlf.encode("utf-8").split(
            wr.USER_START.encode(), 1)[1].split(wr.USER_END.encode(), 1)[0]
        suffix_before = crlf.encode("utf-8").split(wr.USER_END.encode(), 1)[1]

        self.assertTrue(wr.generate(self.vault, DAY)[0])
        out = path.read_bytes()
        self.assertEqual(out.split(wr.USER_START.encode(), 1)[1]
                         .split(wr.USER_END.encode(), 1)[0], user_before)
        self.assertTrue(out.endswith(suffix_before))

    def test_legacy_only_fails_closed_without_touching_bytes(self) -> None:
        legacy = wr.legacy_review_path(self.vault)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("# старый обзор\n", encoding="utf-8")
        before = legacy.read_bytes()

        ok, message = wr.generate(self.vault, DAY)

        self.assertFalse(ok)
        self.assertIn("старый обзор", message)
        self.assertFalse(wr.review_path(self.vault).exists())
        self.assertEqual(legacy.read_bytes(), before)

    def test_two_reviews_fail_closed(self) -> None:
        self.assertTrue(wr.generate(self.vault, DAY)[0])
        legacy = wr.legacy_review_path(self.vault)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("# старый обзор\n", encoding="utf-8")
        root_before = wr.review_path(self.vault).read_bytes()
        legacy_before = legacy.read_bytes()

        ok, message = wr.generate(self.vault, DAY)

        self.assertFalse(ok)
        self.assertIn("два обзора", message)
        self.assertEqual(wr.review_path(self.vault).read_bytes(), root_before)
        self.assertEqual(legacy.read_bytes(), legacy_before)

    def test_build_page_refuses_legacy(self) -> None:
        legacy = wr.legacy_review_path(self.vault)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("# старый обзор\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            wr.build_page(self.vault, DAY)

    def test_build_page_rejects_root_review_symlink_outside(self) -> None:
        outside = self.vault.parent / f"{self.vault.name}-outside-review.md"
        outside.write_text(
            "PREFIX-SECRET\n"
            f"{wr.GEN_START}\nold\n{wr.GEN_END}\n"
            f"{wr.USER_START}\nuser\n{wr.USER_END}\n",
            encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        before = outside.read_bytes()
        (self.vault / "Недельный обзор.md").symlink_to(outside)

        with patch.object(wr, "generated_text", return_value="# safe\n") as gen:
            with self.assertRaises(ValueError) as caught:
                wr.build_page(self.vault)

        self.assertEqual(outside.read_bytes(), before, "внешний файл не читается и не трогается")
        self.assertNotIn("PREFIX-SECRET", str(caught.exception))
        self.assertFalse(gen.called, "containment проверяется до генерации")

    def test_generate_rejects_root_review_symlink_outside(self) -> None:
        outside = self.vault.parent / f"{self.vault.name}-outside-generation.md"
        outside.write_text("EXTERNAL\n", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        before = outside.read_bytes()
        (self.vault / "Недельный обзор.md").symlink_to(outside)

        ok, message = wr.generate(self.vault, DAY)

        self.assertFalse(ok)
        self.assertIn("вне валта", message)
        self.assertEqual(outside.read_bytes(), before)

    def test_build_page_normal_internal_review(self) -> None:
        self.assertTrue(wr.generate(self.vault, DAY)[0])
        path = wr.review_path(self.vault)
        edited = path.read_text(encoding="utf-8").replace(
            "- **Фокус:** —", "- **Фокус:** внутри валта")
        path.write_text(edited, encoding="utf-8")

        page = wr.build_page(self.vault, DAY)

        self.assertEqual(wr.read_decisions(page)["Фокус"], "внутри валта")
        self.assertIn(wr.GEN_START, page)
        self.assertIn(wr.GEN_END, page)


# ── 8. синтетический UAT ────────────────────────────────────────────────────

class UatTests(Stage5Base):
    def test_capture_advance_observation_review(self) -> None:
        # 1. Захват задачи и заметки — только локально, без модели и сети.
        with patch.object(dispatch, "VAULT", self.vault):
            task_reply = dispatch.dispatch("задача: Проверить гипотезу")
            note_reply = dispatch.dispatch("мысль: проверить гипотезу на практике")

        self.assertIn("Создано задач", task_reply)
        task_path = self.vault / "TaskNotes" / "Tasks" / "Проверить гипотезу.md"
        self.assertTrue(task_path.exists())
        self.assertIn("status: next", task_path.read_text(encoding="utf-8"))
        notes = list((self.vault / "ZETA" / "FLEETING_DAILY").glob("*.md"))
        self.assertEqual(len(notes), 1, note_reply)

        # 2. Продвижение: next → in-progress → done.
        with patch.object(dispatch, "VAULT", self.vault):
            in_work = dispatch.advance_task("в работу", "Проверить гипотезу")
            done = dispatch.advance_task(
                "готово", "Проверить гипотезу",
                now=datetime.datetime(2026, 10, 8, 12, 0))
        self.assertIn("in-progress", in_work)
        self.assertIn("done", done)
        final = task_path.read_text(encoding="utf-8")
        self.assertIn("status: done", final)
        self.assertIn("completedDate: 2026-10-08", final)

        # 3. Подтверждённая карточка 2Б с наблюдением пользователя.
        self.write_pair(
            name="Применить совет", pair="pairuat01", status_="done",
            completed="2026-10-08",
            observed="стало видно, что шаг работает",
            conclusion="оставляю в работе")

        # 4. Корневой недельный обзор видит результат.
        ok, message = wr.generate(self.vault, DAY)
        self.assertTrue(ok, message)
        page = wr.review_path(self.vault).read_text(encoding="utf-8")
        self.assertIn("Обзор недели 2026-10-05 — 2026-10-11", page)
        self.assertIn("Применить совет", page)
        self.assertIn("действие закрыто", page)
        self.assertIn("стало видно, что шаг работает", page)
        self.assertIn("оставляю в работе", page)
        self.assertIn("Недельный обзор.md", str(wr.review_path(self.vault)))


if __name__ == "__main__":
    unittest.main()
