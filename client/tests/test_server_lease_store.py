import tempfile
import unittest
from pathlib import Path

import sys

SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER_DIR))
try:
    from jst_lease_store import ACTIVE, LeaseConflict, LeaseStore
finally:
    sys.path.remove(str(SERVER_DIR))


class LeaseStoreBatchRenewTests(unittest.TestCase):
    def test_one_workstation_can_renew_each_order_in_its_batch(self):
        now = [1_800_000_000.0]
        with tempfile.TemporaryDirectory() as directory:
            store = LeaseStore(
                Path(directory) / "leases.sqlite3",
                ttl_seconds=300,
                clock=lambda: now[0],
            )
            first = store.claim("6787909", "13539546", "ws-batch")
            second = store.claim("6788091", "13539600", "ws-batch")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)

            now[0] += 30
            renewed_first = store.renew(
                first.o_id,
                first.io_id,
                first.workstation_id,
                first.claim_token,
            )
            renewed_second = store.renew(
                second.o_id,
                second.io_id,
                second.workstation_id,
                second.claim_token,
            )

            self.assertEqual(renewed_first.state, ACTIVE)
            self.assertEqual(renewed_second.state, ACTIVE)
            self.assertEqual(renewed_first.expires_epoch, now[0] + 300)
            self.assertEqual(renewed_second.expires_epoch, now[0] + 300)

    def test_same_token_can_renew_after_operator_pause_exceeds_ttl(self):
        now = [1_800_000_000.0]
        with tempfile.TemporaryDirectory() as directory:
            store = LeaseStore(
                Path(directory) / "leases.sqlite3",
                ttl_seconds=300,
                clock=lambda: now[0],
            )
            lease = store.claim("6792528", "13543683", "ws-skip")
            self.assertIsNotNone(lease)

            now[0] += 301
            renewed = store.renew(
                lease.o_id,
                lease.io_id,
                lease.workstation_id,
                lease.claim_token,
            )
            completed = store.complete(
                lease.o_id,
                lease.io_id,
                lease.workstation_id,
                lease.claim_token,
                "OPERATOR_SKIPPED",
            )

            self.assertEqual(renewed.state, ACTIVE)
            self.assertEqual(completed.state, "COMPLETED")

    def test_old_token_cannot_skip_after_another_workstation_reclaims_order(self):
        now = [1_800_000_000.0]
        with tempfile.TemporaryDirectory() as directory:
            store = LeaseStore(
                Path(directory) / "leases.sqlite3",
                ttl_seconds=300,
                clock=lambda: now[0],
            )
            old = store.claim("6792528", "13543683", "ws-old")
            self.assertIsNotNone(old)

            now[0] += 301
            current = store.claim("6792528", "13543683", "ws-current")
            self.assertIsNotNone(current)
            self.assertNotEqual(old.claim_token, current.claim_token)

            with self.assertRaises(LeaseConflict):
                store.renew(
                    old.o_id,
                    old.io_id,
                    old.workstation_id,
                    old.claim_token,
                )
            with self.assertRaises(LeaseConflict):
                store.complete(
                    old.o_id,
                    old.io_id,
                    old.workstation_id,
                    old.claim_token,
                    "OPERATOR_SKIPPED",
                )


if __name__ == "__main__":
    unittest.main()
