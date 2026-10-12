import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import resource_ui
import resources


def callbacks(view):
    return [data for row in view.rows for _label, data in row]


class ResourceUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        self.state = {}

    def add(self, suffix, description="полезный генератор документов"):
        return resources.save(self.vault, f"https://tools.example/{suffix}", description).resource

    def test_capture_multiple_links_writes_every_link_once_and_syncs_once(self):
        sync = Mock()
        view = resource_ui.capture(
            self.vault,
            "генераторы: https://one.example/x и https://two.example/y",
            sync=sync,
        )
        self.assertIn("Сохранено", view.text)
        self.assertEqual(len(resources.list_resources(self.vault)), 2)
        sync.assert_called_once_with()

    def test_single_capture_returns_its_card_and_duplicate_preserves_description(self):
        first = resource_ui.capture(
            self.vault, "Генератор документов https://one.example/tool")
        self.assertIn("Генератор документов", first.text)
        self.assertIn("url:https://one.example/tool", callbacks(first))
        item = resources.list_resources(self.vault)[0]
        repeated = resource_ui.capture(self.vault, "https://one.example/tool")
        self.assertIn("Уже сохранено", repeated.text)
        self.assertEqual(resources.load(self.vault, item.id).description,
                         "Генератор документов")

    def test_list_paginates_and_open_callback_has_short_id(self):
        for index in range(12):
            self.add(str(index), f"Полезный ресурс {index}")
        view = resource_ui.list_view(self.vault)
        data = callbacks(view)
        self.assertIn("rs:l:1", data)
        self.assertTrue(any(item.startswith("rs:o:") for item in data))
        self.assertTrue(all(len(item.encode("utf-8")) <= 64 for item in data))

    def test_edit_delete_restore_and_stale_buttons_never_overwrite_external_change(self):
        item = self.add("one", "Старое описание")
        version = item.version
        edit = resource_ui.callback(self.vault, self.state, f"rs:e:{item.id}:{version}")
        self.assertTrue(resource_ui.edit_pending(self.state))
        self.assertIn("Текущий текст", edit.text)
        changed = resource_ui.submit_edit(self.vault, self.state, "Новое описание")
        self.assertIn("исправлен", changed.text)
        current = resources.load(self.vault, item.id)
        self.assertEqual(current.description, "Новое описание")

        old_version = current.version
        confirm = resource_ui.callback(self.vault, self.state,
                                       f"rs:d:{item.id}:{old_version}")
        self.assertIn("Удалить", confirm.text)
        path = resources.resource_path(self.vault, item.id)
        path.write_text(path.read_text(encoding="utf-8") + "\nвнешняя правка\n",
                        encoding="utf-8")
        stale = resource_ui.callback(self.vault, self.state,
                                    f"rs:z:{item.id}:{old_version}")
        self.assertTrue(stale.stale)
        self.assertTrue(path.exists())

        fresh = resources.load(self.vault, item.id)
        resource_ui.callback(self.vault, self.state, f"rs:d:{item.id}:{fresh.version}")
        deleted = resource_ui.callback(self.vault, self.state, f"rs:z:{item.id}:{fresh.version}")
        self.assertIn("восстановить", deleted.text)
        trash = next(data.split(":", 2)[2] for data in callbacks(deleted)
                     if data.startswith("rs:u:"))
        restored = resource_ui.callback(self.vault, self.state, f"rs:u:{trash}")
        self.assertIn("восстановлен", restored.text)
        self.assertIsNotNone(resources.load(self.vault, item.id))

    def test_search_and_clear_edit_are_bounded(self):
        item = self.add("uni", "Генератор документов американских университетов")
        found = resource_ui.search_view(self.vault, "документов университетов")
        self.assertIn(item.id, " ".join(callbacks(found)))
        natural = resource_ui.search_view(self.vault, "тот генератор документов")
        self.assertIn(item.id, " ".join(callbacks(natural)))
        declension = resource_ui.search_view(self.vault, "генератор университеты")
        self.assertIn(item.id, " ".join(callbacks(declension)))
        resource_ui.callback(self.vault, self.state, f"rs:e:{item.id}:{item.version}")
        resource_ui.clear_edit(self.state)
        self.assertFalse(resource_ui.edit_pending(self.state))

    def test_restore_never_overwrites_a_resource_recreated_after_delete(self):
        item = self.add("recreate", "Первая версия")
        resource_ui.callback(self.vault, self.state, f"rs:d:{item.id}:{item.version}")
        deleted = resource_ui.callback(self.vault, self.state, f"rs:z:{item.id}:{item.version}")
        trash = next(data.split(":", 2)[2] for data in callbacks(deleted)
                     if data.startswith("rs:u:"))
        recreated = resources.save(self.vault, item.url, "Новая версия").resource
        self.assertEqual(recreated.description, "Новая версия")
        restored = resource_ui.callback(self.vault, self.state, f"rs:u:{trash}")
        self.assertTrue(restored.stale)
        self.assertEqual(resources.load(self.vault, item.id).description, "Новая версия")

    def test_malformed_navigation_callback_fails_closed(self):
        view = resource_ui.callback(self.vault, self.state, "rs:l:not-a-page")
        self.assertTrue(view.stale)
        self.assertIn("устарела", view.text)

    def test_storage_errors_are_visible_and_partial_capture_syncs_saved_records(self):
        original = resources.save
        calls = 0

        def save_once_then_fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return original(*args, **kwargs)

        sync = Mock()
        with patch.object(resources, "save", side_effect=save_once_then_fail):
            partial = resource_ui.capture(
                self.vault, "https://one.example/x https://two.example/y", sync=sync)
        self.assertTrue(partial.stale)
        self.assertIn("Сохранено: 1", partial.text)
        sync.assert_called_once_with()

        item = resources.list_resources(self.vault)[0]
        with patch.object(resources, "update", side_effect=OSError("disk full")):
            self.state[resource_ui.RESOURCE_EDIT_KEY] = (item.id, item.version)
            failed_edit = resource_ui.submit_edit(self.vault, self.state, "новое")
        self.assertTrue(failed_edit.stale)
        self.assertTrue(resource_ui.edit_pending(self.state))

        with patch.object(resources, "load", side_effect=OSError("disk full")):
            failed_callback = resource_ui.callback(self.vault, self.state, f"rs:o:{item.id}")
        self.assertTrue(failed_callback.stale)


if __name__ == "__main__":
    unittest.main()
