import tempfile
import unittest
from pathlib import Path

import personal_search
import resources


class ResourceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)

    def test_save_preserves_real_url_and_deduplicates_normalized_url(self):
        first = resources.save(
            self.vault,
            "https://Example.com:443/tool?school=abc#about",
            "Генератор документов для университетов",
        )
        second = resources.save(
            self.vault,
            "https://example.com/tool?school=abc#other",
            "",
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.resource.id, second.resource.id)
        self.assertEqual(second.resource.url,
                         "https://Example.com:443/tool?school=abc#about")
        self.assertEqual(second.resource.description,
                         "Генератор документов для университетов")
        self.assertEqual(len(list(resources.resource_dir(self.vault).glob("*.md"))), 1)

    def test_extract_urls_requires_valid_http_links_and_keeps_each_link(self):
        urls = resources.extract_urls(
            "сравнить https://a.example/a и https://b.example/b?x=1")
        self.assertEqual(urls, ["https://a.example/a", "https://b.example/b?x=1"])
        for bad in ("ftp://example.com/file", "https://user:pass@example.com/x"):
            with self.assertRaises(ValueError):
                resources.extract_urls(bad)
        self.assertEqual(resources.extract_urls("без ссылки"), [])

    def test_extract_urls_removes_markdown_wrapper_parenthesis_but_keeps_balanced_url(self):
        wrapped = resources.extract_urls("[инструмент](https://example.com/tool)")
        balanced = resources.extract_urls("https://example.com/Function_(math)")
        self.assertEqual(wrapped, ["https://example.com/tool"])
        self.assertEqual(balanced, ["https://example.com/Function_(math)"])

    def test_saved_resource_is_found_by_personal_search(self):
        resources.save(self.vault, "https://docs.example.edu/generate",
                       "Генератор документов американских университетов")
        hits = personal_search.search("генератор университетов", vault=self.vault)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "resource")
        self.assertIn("Генератор", hits[0].title)

    def test_rejects_external_symlink_in_resource_folder(self):
        external = self.vault.parent / (self.vault.name + "-outside")
        external.mkdir()
        parent = self.vault / "PARA" / "RESOURCES"
        parent.mkdir(parents=True)
        (parent / "Сохранённые ссылки").symlink_to(external, target_is_directory=True)
        with self.assertRaises(resources.ResourceError):
            resources.save(self.vault, "https://safe.example/x", "тест")
        self.assertEqual(list(external.iterdir()), [])

    def test_parent_symlink_cannot_create_resource_directories_outside_vault(self):
        external = self.vault.parent / (self.vault.name + "-parent-outside")
        external.mkdir()
        (self.vault / "PARA").symlink_to(external, target_is_directory=True)
        with self.assertRaises(resources.ResourceError):
            resources.save(self.vault, "https://safe.example/x", "тест")
        self.assertFalse((external / "RESOURCES").exists())

    def test_corrupt_or_traversal_record_is_never_loaded_or_overwritten(self):
        directory = resources.resource_dir(self.vault)
        corrupt = directory / ("a" * 24 + ".md")
        corrupt.write_text("---\nid: \"" + "a" * 24 + "\"\nurl: \"https://user:pass@example.com\"\n---\n",
                           encoding="utf-8")
        self.assertEqual(resources.list_resources(self.vault), [])
        self.assertIsNone(resources.load(self.vault, "../" + "a" * 24))
        self.assertEqual(corrupt.read_text(encoding="utf-8").splitlines()[0], "---")

    def test_stale_restore_does_not_create_a_trash_directory(self):
        result = resources.restore(self.vault, "a" * 24)
        self.assertTrue(result.stale)
        self.assertFalse((self.vault / "SYSTEM").exists())


if __name__ == "__main__":
    unittest.main()
