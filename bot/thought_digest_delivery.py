"""Local delivery acknowledgements. Personal content never enters this ledger."""
import re
import sqlite3
from pathlib import Path


class DeliveryLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            with connection:
                connection.execute('CREATE TABLE IF NOT EXISTS delivered (id TEXT PRIMARY KEY, sent TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        finally:
            connection.close()

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=10)

    @staticmethod
    def _validate(job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{24}', job_id):
            raise ValueError('Invalid digest ID')

    def contains(self, job_id):
        self._validate(job_id)
        connection = self._connect()
        try:
            return connection.execute('SELECT 1 FROM delivered WHERE id = ?', (job_id,)).fetchone() is not None
        finally:
            connection.close()

    def mark(self, job_id):
        self._validate(job_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute('INSERT OR IGNORE INTO delivered (id) VALUES (?)', (job_id,))
        finally:
            connection.close()
