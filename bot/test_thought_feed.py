#!/usr/bin/env python3
"""Bounded-чтение и детерминированный возврат мыслей: только чтение валта.

Никаких моделей, сети и subprocess. Разрешены ровно два источника: инбокс и
структурированные fleeting-заметки. Приватное, служебные каталоги, кэши,
symlink-выходы, огромные и бинарные файлы не читаются. Идентичность — путь плюс
хеш содержимого; дата берётся из frontmatter/имени, но никогда из mtime.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

import runtime_store
import thought_feed

NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=datetime.timezone.utc)
DAY = datetime.timedelta(days=1)


def stamp(days_ago: int) -> str:
    return (NOW.date() - DAY * days_ago).isoformat()


class VaultBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.inbox_dir = self.root / "ZETA" / "FLEETING"
        self.daily = self.root / "ZETA" / "FLEETING_DAILY"
        self.inbox_dir.mkdir(parents=True)
        self.daily.mkdir(parents=True)
        self.inbox = self.inbox_dir / "00 Инбокс.md"
        self.db = self.root / "state" / "runtime.sqlite3"
        self.store = runtime_store.RuntimeStore(self.db)
        self.addCleanup(self.store.close)

    def write_inbox(self, lines) -> None:
        body = ["# 📥 Inbox", "", "## 💭 Мысли и идеи", ""]
        body += list(lines)
        body += ["", "## ✅ Задачи", "", "## 🔗 Ссылки и материалы", ""]
        self.inbox.write_text("\n".join(body), encoding="utf-8")

    def write_note(self, name: str, days_ago: int, body: str = "",
                   created: bool = True) -> Path:
        path = self.daily / name
        stamp_line = ""
        if created:
            stamp_line = f"created: {stamp(days_ago)} 09:30\n"
        path.write_text(
            "---\ntype: fleeting_note\n" + stamp_line + "---\n\n"
            f"# {path.stem}\n\n{body}\n",
            encoding="utf-8",
        )
        return path


# ── overview и границы ──────────────────────────────────────────────────────

class OverviewTests(VaultBase):
    def test_overview_bounds_each_section(self) -> None:
        self.write_inbox(["- **0{i}:00** строка {i}".format(i=i) for i in range(1, 9)])
        for i in range(6):
            self.write_note(f"{stamp(i)} — Заметка {i}.md", i, body="тело")
        overview = thought_feed.overview(self.root, store=self.store, now=NOW)
        self.assertEqual(overview.inbox_total, 8)
        self.assertEqual(len(overview.inbox), thought_feed.MAX_INBOX_RECENT)
        self.assertEqual(len(overview.recent), thought_feed.MAX_STRUCTURED_RECENT)
        self.assertIsNotNone(overview.forgotten)

    def test_inbox_recent_is_newest_first(self) -> None:
        self.write_inbox(["- **09:00** первая", "- **10:00** вторая"])
        inbox = thought_feed.inbox_thoughts(self.root)
        self.assertEqual(inbox[0].excerpt, "вторая")
        self.assertEqual(inbox[1].excerpt, "первая")

    def test_structured_recent_is_newest_first(self) -> None:
        self.write_note(f"{stamp(5)} — Старая.md", 5)
        self.write_note(f"{stamp(1)} — Новая.md", 1)
        recent = thought_feed.recent_thoughts(self.root)
        self.assertIn("Новая", recent[0].title + recent[0].path)
        self.assertIn("Старая", recent[1].title + recent[1].path)


class InboxScopeTests(VaultBase):
    """Собираем только секцию мыслей: задачи и материалы — не мысли."""

    def mixed(self) -> None:
        self.inbox.write_text(
            "# 📥 Inbox\n\n"
            "- **08:00** строка до раздела\n\n"
            "## 💭 Мысли и идеи\n\n"
            "- **09:00** мысль про оффер\n\n"
            "## ✅ Задачи\n\n"
            "- [ ] **09:05** задача купить кабель\n\n"
            "## 🔗 Ссылки и материалы\n\n"
            "- **11:00** материал про agent loop\n",
            encoding="utf-8")

    def test_only_thought_section_is_collected(self) -> None:
        self.mixed()
        inbox = thought_feed.inbox_thoughts(self.root, limit=50)
        self.assertEqual([t.excerpt for t in inbox], ["мысль про оффер"])

    def test_count_and_overview_see_only_the_thought(self) -> None:
        self.mixed()
        overview = thought_feed.overview(self.root, store=self.store, now=NOW)
        self.assertEqual(overview.inbox_total, 1)
        self.assertEqual([t.excerpt for t in overview.inbox], ["мысль про оффер"])

    def test_lines_before_thought_heading_are_skipped(self) -> None:
        self.mixed()
        self.assertNotIn(
            "строка до раздела",
            [t.excerpt for t in thought_feed.inbox_thoughts(self.root, limit=50)])

    def test_task_and_material_lines_are_not_loadable_thoughts(self) -> None:
        self.mixed()
        for line in ("- [ ] **09:05** задача купить кабель",
                     "- **11:00** материал про agent loop"):
            identity = (f"{thought_feed.INBOX_REL}#"
                        f"{hashlib.sha1(line.encode('utf-8')).hexdigest()[:12]}")
            self.assertIsNone(thought_feed.load_thought(self.root, identity))

    def test_task_and_material_lines_are_not_related_candidates(self) -> None:
        self.mixed()
        self.assertIsNone(thought_feed.related_thought(
            self.root, "задача купить кабель", self.store, NOW))
        self.assertIsNone(thought_feed.related_thought(
            self.root, "материал про agent loop", self.store, NOW))


# ── идентичность ────────────────────────────────────────────────────────────

class IdentityTests(VaultBase):
    def test_structured_identity_is_relative_path_plus_content_hash(self) -> None:
        self.write_note(f"{stamp(3)} — Мысль.md", 3, body="один")
        thought = thought_feed.recent_thoughts(self.root)[0]
        self.assertTrue(thought.identity.startswith(
            "ZETA/FLEETING_DAILY/" + f"{stamp(3)} — Мысль.md"))
        self.assertIn("#", thought.identity)
        before = thought.identity
        self.write_note(f"{stamp(3)} — Мысль.md", 3, body="два")
        after = thought_feed.recent_thoughts(self.root)[0].identity
        self.assertNotEqual(before, after, "смена содержимого меняет identity")

    def test_inbox_identity_is_inbox_path_plus_line_hash(self) -> None:
        self.write_inbox(["- **10:00** первая", "- **10:01** вторая"])
        thoughts = thought_feed.inbox_thoughts(self.root)
        for thought in thoughts:
            self.assertTrue(thought.identity.startswith(thought_feed.INBOX_REL))
            self.assertIn("#", thought.identity)
        self.assertNotEqual(thoughts[0].identity, thoughts[1].identity)

    def test_load_thought_roundtrip_and_mismatch(self) -> None:
        self.write_note(f"{stamp(4)} — Проверка.md", 4, body="текст")
        thought = thought_feed.recent_thoughts(self.root)[0]
        loaded = thought_feed.load_thought(self.root, thought.identity)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.identity, thought.identity)
        self.assertIsNone(thought_feed.load_thought(self.root, thought.identity + "0"))

    def test_load_inbox_thought_roundtrip(self) -> None:
        self.write_inbox(["- **10:00** нужная мысль"])
        thought = thought_feed.inbox_thoughts(self.root)[0]
        loaded = thought_feed.load_thought(self.root, thought.identity)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.excerpt, "нужная мысль")


# ── даты ────────────────────────────────────────────────────────────────────

class DateTests(VaultBase):
    def test_date_comes_from_frontmatter_not_mtime(self) -> None:
        path = self.write_note(f"{stamp(40)} — Древняя.md", 40)
        os.utime(path, None)  # свежий mtime не должен попасть в дату
        thought = thought_feed.recent_thoughts(self.root, limit=50)[0]
        self.assertEqual(thought.date, NOW.date() - DAY * 40)

    def test_date_falls_back_to_filename(self) -> None:
        self.write_note(f"{stamp(7)} — Без шапки.md", 0, created=False)
        thought = thought_feed.recent_thoughts(self.root, limit=50)[0]
        self.assertEqual(thought.date, NOW.date() - DAY * 7)

    def test_date_never_uses_mtime_when_absent(self) -> None:
        path = self.daily / "Заметка без даты.md"
        path.write_text("# Заметка без даты\n\nтекст\n", encoding="utf-8")
        os.utime(path, None)
        thought = thought_feed.recent_thoughts(self.root, limit=50)[0]
        self.assertIsNone(thought.date)


# ── забытые ─────────────────────────────────────────────────────────────────

class ForgottenTests(VaultBase):
    def test_older_wins(self) -> None:
        self.write_note(f"{stamp(3)} — Трёхдневная.md", 3)
        self.write_note(f"{stamp(30)} — Месячная.md", 30)
        forgotten = thought_feed.forgotten_thought(self.root, self.store, NOW)
        self.assertIn("Месячная", forgotten.identity + forgotten.title + forgotten.path)

    def test_recent_notes_are_not_forgotten(self) -> None:
        self.write_note(f"{stamp(1)} — Вчерашняя.md", 1)
        self.assertIsNone(thought_feed.forgotten_thought(self.root, self.store, NOW))

    def test_hidden_is_excluded(self) -> None:
        old = self.write_note(f"{stamp(30)} — Месячная.md", 30)
        self.write_note(f"{stamp(4)} — Четырёхдневная.md", 4)
        target = thought_feed.recent_thoughts(self.root, limit=50)
        identity = [t for t in target if t.path.endswith(old.name)][0].identity
        self.store.hide(identity, now=NOW)
        forgotten = thought_feed.forgotten_thought(self.root, self.store, NOW)
        self.assertNotEqual(forgotten.identity, identity)

    def test_recently_surfaced_is_excluded(self) -> None:
        self.write_note(f"{stamp(30)} — Месячная.md", 30)
        self.write_note(f"{stamp(4)} — Четырёхдневная.md", 4)
        target = thought_feed.recent_thoughts(self.root, limit=50)
        identity = target[0].identity
        self.store.record_surface(identity, now=NOW - DAY)
        forgotten = thought_feed.forgotten_thought(self.root, self.store, NOW)
        self.assertNotEqual(forgotten.identity, identity)

    def test_none_is_explained_without_error(self) -> None:
        self.write_inbox(["- **10:00** свежая"])
        overview = thought_feed.overview(self.root, store=self.store, now=NOW)
        self.assertIsNone(overview.forgotten)
        text = thought_feed.render_overview(overview)
        self.assertIn("🧠", text)

    def test_ranking_is_deterministic(self) -> None:
        self.write_note(f"{stamp(10)} — Б.md", 10)
        self.write_note(f"{stamp(10)} — А.md", 10)
        first = thought_feed.forgotten_thought(self.root, self.store, NOW)
        second = thought_feed.forgotten_thought(self.root, self.store, NOW)
        self.assertEqual(first.identity, second.identity)


# ── безопасность чтения ─────────────────────────────────────────────────────

class SafetyTests(VaultBase):
    def test_symlink_escape_is_rejected(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("---\ncreated: 2020-01-01 00:00\n---\n\n# Внешняя\n", encoding="utf-8")
        link = self.daily / "Ссылка.md"
        link.symlink_to(outside)
        titles = [t.path for t in thought_feed.recent_thoughts(self.root, limit=100)]
        self.assertNotIn("ZETA/FLEETING_DAILY/Ссылка.md", titles)

    def test_private_and_service_files_are_not_read(self) -> None:
        (self.root / "Private").mkdir()
        (self.root / "Private" / "00 Private.md").write_text(
            "# Private\n\n- **10:00** тайная мысль\n", encoding="utf-8")
        (self.root / "SYSTEM").mkdir()
        (self.root / "SYSTEM" / "secret.md").write_text(
            "# secret\n", encoding="utf-8")
        self.write_note(f"{stamp(4)} — Обычная.md", 4, body="обычная")
        paths = [t.path for t in thought_feed.recent_thoughts(self.root, limit=100)]
        self.assertEqual(paths, [f"ZETA/FLEETING_DAILY/{stamp(4)} — Обычная.md"])

    def test_oversized_file_is_skipped(self) -> None:
        big = self.daily / "Огромная.md"
        big.write_text("x" * (thought_feed.MAX_FILE_BYTES + 10), encoding="utf-8")
        self.assertNotIn(
            "ZETA/FLEETING_DAILY/Огромная.md",
            [t.path for t in thought_feed.recent_thoughts(self.root, limit=100)])

    def test_binary_and_non_utf8_files_are_skipped(self) -> None:
        (self.daily / "Бинарная.md").write_bytes(b"# \x00\x01\x02")
        try:
            (self.daily / "Неutf8.md").write_bytes(b"# \xff\xfe\xfd")
        except OSError:
            pass
        paths = [t.path for t in thought_feed.recent_thoughts(self.root, limit=100)]
        self.assertNotIn("ZETA/FLEETING_DAILY/Бинарная.md", paths)
        self.assertNotIn("ZETA/FLEETING_DAILY/Неutf8.md", paths)

    def test_render_has_no_absolute_paths(self) -> None:
        self.write_inbox(["- **10:00** мысль"])
        self.write_note(f"{stamp(30)} — Старая.md", 30, body="тело")
        overview = thought_feed.overview(self.root, store=self.store, now=NOW)
        text = thought_feed.render_overview(overview)
        self.assertNotIn(str(self.root), text)
        self.assertLessEqual(len(text), thought_feed.MAX_RENDER_CHARS)


# ── связанная старая мысль после захвата ────────────────────────────────────

class RelatedTests(VaultBase):
    def test_strong_overlap_finds_older_thought(self) -> None:
        self.write_note(f"{stamp(20)} — Лендинг.md", 20,
                        body="переписать лендинг про оффер и цену")
        found = thought_feed.related_thought(
            self.root, "хочу переписать лендинг про оффер", self.store, NOW)
        self.assertIsNotNone(found)
        self.assertIn("Лендинг", found.title + found.path)

    def test_weak_overlap_finds_nothing(self) -> None:
        self.write_note(f"{stamp(20)} — Лендинг.md", 20,
                        body="переписать лендинг про оффер")
        found = thought_feed.related_thought(
            self.root, "купить кабель для монитора", self.store, NOW)
        self.assertIsNone(found)

    def test_related_respects_24h_cooldown(self) -> None:
        self.write_note(f"{stamp(20)} — Лендинг.md", 20,
                        body="переписать лендинг про оффер и цену")
        first = thought_feed.related_thought(
            self.root, "переписать лендинг про оффер", self.store, NOW)
        self.assertIsNotNone(first)
        second = thought_feed.related_thought(
            self.root, "переписать лендинг про оффер", self.store, NOW)
        self.assertIsNone(second, "повтор в течение суток запрещён")
        third = thought_feed.related_thought(
            self.root, "переписать лендинг про оффер", self.store, NOW + DAY + DAY)
        self.assertIsNotNone(third)

    def test_related_skips_the_just_captured_line(self) -> None:
        self.write_inbox(["- **10:00** переписать лендинг про оффер"])
        found = thought_feed.related_thought(
            self.root, "переписать лендинг про оффер", self.store, NOW)
        self.assertIsNone(found)

    def test_related_works_without_store(self) -> None:
        self.write_note(f"{stamp(20)} — Лендинг.md", 20,
                        body="переписать лендинг про оффер и цену")
        found = thought_feed.related_thought(
            self.root, "переписать лендинг про оффер", None, NOW)
        self.assertIsNotNone(found)

    def test_same_day_structured_note_is_not_old(self) -> None:
        self.write_note(f"{stamp(0)} — Сегодня.md", 0,
                        body="переписать лендинг про оффер и цену")
        self.assertIsNone(thought_feed.related_thought(
            self.root, "переписать лендинг про оффер", self.store, NOW),
            "сегодняшняя заметка не может быть «старой мыслью»")

    def test_undated_inbox_line_is_not_old(self) -> None:
        self.write_inbox(["- **10:00** переписать лендинг про оффер и цену"])
        self.assertIsNone(thought_feed.related_thought(
            self.root, "переписать лендинг про оффер", self.store, NOW),
            "строка Inbox без даты не может быть «старой мыслью»")

    def test_positive_only_old_structured_note_is_found(self) -> None:
        self.write_note(f"{stamp(1)} — Вчерашняя.md", 1,
                        body="переписать лендинг про оффер и цену")
        self.write_note(f"{stamp(15)} — Старая.md", 15,
                        body="переписать лендинг про оффер и цену")
        found = thought_feed.related_thought(
            self.root, "переписать лендинг про оффер", self.store, NOW)
        self.assertIsNotNone(found)
        self.assertIn("Старая", found.title + found.path)


if __name__ == "__main__":
    unittest.main()
