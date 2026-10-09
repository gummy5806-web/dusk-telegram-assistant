#!/usr/bin/env python3
"""Приватный захват: содержимое не попадает в метаданные коммита.

Проверяется только синтетический маркер — реальные приватные записи
пользователя тесты не читают.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vault_ops

SECRET = "СЕКРЕТ-МАРКЕР-9f3a1"


class PrivateMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.messages = []

        for name, value in (
            ("PRIVATE_INBOX_PATH", self.vault / "Private" / "00 Private.md"),
            ("INBOX_PATH", self.vault / "ZETA" / "FLEETING" / "00 Инбокс.md"),
        ):
            patcher = patch.object(vault_ops, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        patcher = patch.object(vault_ops, "git_sync", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

        patcher = patch.object(vault_ops, "git_push",
                               lambda message: self.messages.append(message))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_private_text_is_not_in_commit_message(self) -> None:
        vault_ops.append_to_inbox(SECRET, private=True)

        self.assertEqual(self.messages, [],
                         "приватный маршрут не делает tracked sync вовсе")

    def test_private_text_is_still_saved(self) -> None:
        vault_ops.append_to_inbox(SECRET, private=True)
        saved = (self.vault / "Private" / "00 Private.md").read_text(encoding="utf-8")
        self.assertIn(SECRET, saved, "сама запись обязана сохраниться")

    def test_no_created_name_contains_secret(self) -> None:
        vault_ops.append_to_inbox(SECRET, private=True)
        names = [p.name for p in self.vault.rglob("*")]
        for name in names:
            self.assertNotIn(SECRET, name, f"имя файла не должно содержать приватный текст: {name}")

    def test_public_capture_message_is_neutral(self) -> None:
        """Публичный захват коммитится статичным нейтральным сообщением."""
        vault_ops.append_to_inbox("опубликовать разбор воронки", private=False)
        self.assertEqual(len(self.messages), 1)
        message = self.messages[0]
        self.assertEqual(message, vault_ops.INBOX_COMMIT_MESSAGE)
        self.assertNotIn("опубликовать", message)
        self.assertNotIn("воронки", message)


if __name__ == "__main__":
    unittest.main()
