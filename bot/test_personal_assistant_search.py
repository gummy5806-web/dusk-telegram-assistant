#!/usr/bin/env python3
"""Dusk-first локальный поиск: только личные места, без утечек и падений.

Изолированный временный валт: ни сети, ни модели, ни индекса, ни git.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import personal_search as ps

MARKER = "секретмаркерzz9"


class SearchBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)

    def write(self, relative: str, body: str) -> Path:
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def write_bytes(self, relative: str, body: bytes) -> Path:
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def inbox(self, *lines: str) -> None:
        body = "# 📥 Inbox\n\n## 💭 Мысли и идеи\n\n"
        body += "\n".join(f"- **10:00** {line}" for line in lines)
        body += "\n\n## ✅ Задачи\n\n## 🔗 Ссылки и материалы\n"
        self.write("ZETA/FLEETING/00 Инбокс.md", body)

    def fleeting(self, name: str, title: str, text: str) -> Path:
        return self.write(
            f"ZETA/FLEETING_DAILY/{name}.md",
            "---\ntype: fleeting\ntags: [zeta/fleeting]\n"
            "created: 2026-10-09 10:00\n---\n\n"
            f"# {title}\n\n## Мысль\n- {text}\n",
        )

    def task(self, name: str, title: str, status: str = "next") -> Path:
        return self.write(
            f"TaskNotes/Tasks/{name}.md",
            "---\n"
            f'title: "{title}"\n'
            f"status: {status}\n"
            "created: 2026-10-09T09:00\n"
            "---\n\n"
            f"# {title}\n",
        )


class ExtendedCoverageTests(SearchBase):
    def test_finds_sticky_note(self) -> None:
        self.write("STICKY/идея про оффер.md", "# идея\nподумал про оффер")
        hits = ps.search("оффер", vault=self.vault)
        self.assertIn("sticky", {hit.kind for hit in hits})

    def test_finds_omi_memory(self) -> None:
        self.write("Omi/Memories.md", "# Memories\n- запомнил про оффер")
        hits = ps.search("оффер", vault=self.vault)
        self.assertIn("memory", {hit.kind for hit in hits})

    def test_finds_para_resources(self) -> None:
        self.write("PARA/RESOURCES/книги.md", "# Книги\nоффер и продажи")
        hits = ps.search("оффер", vault=self.vault)
        self.assertIn("resource", {hit.kind for hit in hits})

    def test_finds_para_resources_hobby_variant(self) -> None:
        self.write("PARA/RESOURCES хобби и заметки по ним/оффер.md",
                   "# оффер\nпро оффер")
        hits = ps.search("оффер", vault=self.vault)
        self.assertIn("resource", {hit.kind for hit in hits})


class ExtendedIsolationTests(SearchBase):
    def test_bot_obsidian_credentials_do_not_leak(self) -> None:
        self.write("bot/secret.md", f"# bot\n{MARKER}\n")
        self.write(".obsidian/secret.md", f"# obsidian\n{MARKER}\n")
        self.write(".smart-env/secret.md", f"# smart\n{MARKER}\n")
        self.write("credentials.md", f"# credentials\n{MARKER}\n")
        self.write("PARA/WORKSTATION/secret.md", f"# workstation\n{MARKER}\n")
        hits = ps.search(MARKER, vault=self.vault)
        self.assertEqual(hits, [], "bot/.obsidian/credentials не читаются")

    def test_allowlist_stays_narrow(self) -> None:
        # STICKY/Omi/PARA.RESOURCES разрешены, но весь vault не сканируется.
        self.write("STICKY/s.md", f"# s\n{MARKER}\n")
        self.assertTrue(ps.search(MARKER, vault=self.vault),
                        "новые личные разделы должны находиться")


class RenderLabelTests(SearchBase):
    def test_new_kinds_have_russian_labels(self) -> None:
        self.write("STICKY/оффер.md", "# оффер\nоффер")
        self.write("Omi/Memories.md", "# память\nоффер")
        self.write("PARA/RESOURCES/оффер.md", "# оффер\nоффер")
        hits = ps.search("оффер", vault=self.vault)
        text = ps.render(hits)
        self.assertIn("Стикер", text)
        self.assertIn("Omi", text)
        self.assertIn("Ресурс", text)

    def test_combined_keeps_both_headers_with_long_personal(self) -> None:
        for number in range(15):
            self.fleeting(f"2026-10-0{number % 9} — Аудит {number}",
                          f"Аудит {number}",
                          "аудит " + "очень длинный текст " * 40)
        hits = ps.search("аудит", vault=self.vault)
        self.assertTrue(hits)
        research = "research-fragment " * 300
        text = ps.render_combined(hits, research)
        self.assertLess(len(text), 4000)
        self.assertIn("Из твоего Dusk", text)
        self.assertIn("Из агентских конспектов", text)
        self.assertIn("research-fragment", text,
                      "часть Research Brain обязана остаться видимой")


class RecallTests(SearchBase):
    def test_finds_inbox_thought_without_exact_endings(self) -> None:
        self.inbox("размышлял про аудит воронки")
        hits = ps.search("аудита", vault=self.vault)
        self.assertTrue(hits, "мысль из Inbox должна находиться по стему")
        self.assertEqual(hits[0].kind, "inbox")
        self.assertIn("аудит", hits[0].excerpt.lower())

    def test_finds_fleeting_note_without_exact_endings(self) -> None:
        self.fleeting("2026-10-09 — Идея", "Идея про лендинг",
                      "начал продажи через лендинг")
        hits = ps.search("продажу", vault=self.vault)
        self.assertTrue(hits, "заметка должна находиться по стему")
        self.assertEqual(hits[0].kind, "fleeting")

    def test_finds_task_and_exposes_status_metadata(self) -> None:
        self.task("Проверить воронку", "Проверить воронку", status="next")
        hits = ps.search("воронк", vault=self.vault)
        self.assertTrue(hits)
        hit = hits[0]
        self.assertEqual(hit.kind, "task")
        self.assertEqual(hit.status, "next")
        self.assertIn("Проверить воронку", hit.title)

    def test_paths_are_vault_relative(self) -> None:
        self.fleeting("2026-10-09 — Оффер", "Оффер", "подумал про оффер")
        hits = ps.search("оффер", vault=self.vault)
        self.assertTrue(hits)
        self.assertFalse(hits[0].path.startswith("/"))
        self.assertIn("ZETA", hits[0].path)

    def test_exact_phrase_title_outranks_body_mention(self) -> None:
        self.fleeting("2026-10-09 — Аудит", "Аудит",
                      "сегодня прошёл аудит и всё ок")
        self.fleeting("2026-10-08 — Другое", "Другое",
                      "про аудит поговорили вчера вечером")
        hits = ps.search("аудит", vault=self.vault)
        self.assertGreaterEqual(len(hits), 2)
        self.assertEqual(hits[0].title, "Аудит")


class IsolationTests(SearchBase):
    def test_private_and_research_and_system_do_not_leak(self) -> None:
        self.write("Private/00 Private.md", f"# Private\n- {MARKER}\n")
        self.write("research-brain/raw/leak.md", f"# leak\n{MARKER}\n")
        self.write("SYSTEM/leak.md", f"# leak\n{MARKER}\n")
        self.write("sessions/2026-10-09 leak.md", f"# leak\n{MARKER}\n")
        self.write("ZETA/LITERATURE Изучение,конспект/leak.md", f"# leak\n{MARKER}\n")
        hits = ps.search(MARKER, vault=self.vault)
        self.assertEqual(hits, [], "личный поиск не читает Private/Research/систему")

    def test_external_symlink_is_ignored(self) -> None:
        outside = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8")
        self.addCleanup(Path(outside.name).unlink)
        outside.write(f"# outside\n{MARKER}\n")
        outside.close()
        link = self.vault / "ZETA" / "FLEETING_DAILY" / "link.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside.name)

        hits = ps.search(MARKER, vault=self.vault)
        self.assertEqual(hits, [], "symlink за пределы валта не читается")

    def test_oversized_binary_and_broken_files_are_skipped(self) -> None:
        huge = ("x" * 400) + f" {MARKER} " + ("y" * (ps.MAX_FILE_BYTES + 100))
        self.write("ZETA/FLEETING_DAILY/huge.md", huge)
        self.write_bytes("ZETA/FLEETING_DAILY/binary.md",
                         b"\x00\xff\xfe" + MARKER.encode("utf-8"))
        self.write_bytes("ZETA/FLEETING_DAILY/broken.md",
                         b"valid start \xff\xfe " + MARKER.encode("utf-8"))

        hits = ps.search(MARKER, vault=self.vault)
        self.assertEqual(hits, [], "битые и огромные файлы пропускаются")

    def test_broken_file_does_not_abort_good_search(self) -> None:
        self.write_bytes("ZETA/FLEETING_DAILY/broken.md", b"\xff\xfe\x00")
        self.fleeting("2026-10-09 — Воронка", "Воронка", "проверить воронку")
        hits = ps.search("воронку", vault=self.vault)
        self.assertTrue(hits, "битый файл не ломает поиск по остальным")


class BoundsTests(SearchBase):
    def test_empty_and_stopword_only_do_not_dump_vault(self) -> None:
        self.inbox("важная мысль про оффер")
        self.fleeting("2026-10-09 — Оффер", "Оффер", "продажи и оффер")
        self.assertEqual(ps.search("", vault=self.vault), [])
        self.assertEqual(ps.search("   ", vault=self.vault), [])
        self.assertEqual(ps.search("что и как", vault=self.vault), [])

    def test_result_count_is_bounded(self) -> None:
        for number in range(15):
            self.fleeting(f"2026-10-0{number % 9} — Аудит {number}",
                          f"Аудит {number}", f"аудит номер {number}")
        hits = ps.search("аудит", vault=self.vault, limit=5)
        self.assertLessEqual(len(hits), 5)
        default_hits = ps.search("аудит", vault=self.vault)
        self.assertLessEqual(len(default_hits), ps.MAX_RESULTS)


class RenderTests(SearchBase):
    def test_render_labels_personal_source(self) -> None:
        self.fleeting("2026-10-09 — Оффер", "Оффер", "мысль про оффер")
        hits = ps.search("оффер", vault=self.vault)
        text = ps.render(hits)
        self.assertIn("🧠 Из твоего Dusk", text)

    def test_render_is_under_telegram_limit(self) -> None:
        for number in range(15):
            self.fleeting(f"2026-10-0{number % 9} — Аудит {number}",
                          f"Аудит {number}",
                          f"аудит номер {number} " + "длинный текст " * 20)
        hits = ps.search("аудит", vault=self.vault)
        self.assertLess(len(ps.render(hits)), 4000)

    def test_render_empty_does_not_dump_vault(self) -> None:
        text = ps.render([])
        self.assertIn("Из твоего Dusk", text)
        self.assertNotIn("ZETA/FLEETING", text)

    def test_combined_puts_personal_section_first(self) -> None:
        self.fleeting("2026-10-09 — Оффер", "Оффер", "мысль про оффер")
        hits = ps.search("оффер", vault=self.vault)
        text = ps.render_combined(hits, "research fragment")
        self.assertIn("🧠 Из твоего Dusk", text)
        self.assertIn("📚 Из агентских конспектов", text)
        self.assertLess(text.index("Из твоего Dusk"),
                        text.index("Из агентских конспектов"))


if __name__ == "__main__":
    unittest.main()
