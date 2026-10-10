import tempfile
import unittest
from pathlib import Path


class DigestDeliveryTests(unittest.TestCase):
    def test_durable_ack_deduplicates_and_reopens(self):
        import thought_digest_delivery as delivery
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'delivery.sqlite3'
            ledger=delivery.DeliveryLedger(path)
            self.assertFalse(ledger.contains('a'*24))
            ledger.mark('a'*24)
            self.assertTrue(delivery.DeliveryLedger(path).contains('a'*24))

    def test_invalid_job_cannot_be_marked(self):
        import thought_digest_delivery as delivery
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError): delivery.DeliveryLedger(Path(directory)/'db').mark('../bad')
