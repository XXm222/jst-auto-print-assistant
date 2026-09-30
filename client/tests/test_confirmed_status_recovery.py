"""Regression for the 2026-09-06 RUNNING / Confirmed restart loop."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jst_auto_print_app as app


class ConfirmedStatusRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.store = app.EventStore(root / "events.sqlite3", root / "events.jsonl")
        self.engine = app.AutomationEngine(
            app.Settings(print_profile=app.SOURCE_CARRIER_ID),
            self.store, lambda _event: None, lambda _status: None,
        )
        self.engine.run_event.set()
        self.engine._browser_for = mock.Mock(
            side_effect=AssertionError("status recovery must not open a browser")
        )
        self.planner = mock.Mock()
        self.planner.inspect.return_value = self.readback()

    @staticmethod
    def readback(status="Confirmed", **fields):
        return {
            "found": True, "o_id": "6807385", "io_id": "13554097",
            "status": status, "has_ship_action": False,
            "has_print_action": False, **fields,
        }

    def job(self, step="PRINT_EXPRESS", status="RUNNING", io_id="13554097"):
        plan = {
            "o_id": "6807385", "io_id": io_id, "claim_token": "x" * 32,
            "steps": [step, "STOP_BEFORE_PRESHIP"],
            "outbound_identity_unique": True,
        }
        self.assertTrue(self.store.save_job(plan))
        self.store.update_job(plan["o_id"], io_id, step_index=0, status=status)
        return self.store.get_job(plan["o_id"], io_id)

    def assert_retired(self, expected):
        self.assertEqual(self.store.get_job("6807385", "13554097")["status"], expected)
        self.planner.complete.assert_called_once_with(
            "6807385", "13554097", "x" * 32, "UNCERTAIN_ACTION"
        )
        self.planner.release.assert_not_called()
        self.assertFalse(self.store.has_event("PRINT_OK", "6807385", "13554097"))
        self.engine._browser_for.assert_not_called()
        self.assertFalse(self.store.active_jobs())
        self.assertIn(
            {"o_id": "6807385", "io_id": "13554097"}, self.store.excluded_pairs()
        )

    def test_restart_of_running_print_retires_uncertain_without_print_or_sku(self):
        job = self.job()
        with mock.patch.object(self.store, "record_sku_outbound") as sku:
            self.engine._process_job(self.planner, job)
            sku.assert_not_called()
        self.assert_retired("SKIPPED_UNCERTAIN_PRINT")
        # A fresh process using the same database cannot resume or reclaim it.
        reopened = app.EventStore(self.store.db_path, self.store.jsonl_path)
        self.assertIsNone(reopened.next_job())
        self.assertFalse(reopened.save_job(job["plan"]))

    def test_running_get_waybill_is_not_released_for_replay(self):
        self.engine._process_job(self.planner, self.job("GET_WAYBILL"))
        self.assert_retired("SKIPPED_UNCERTAIN_WRITE")

    def test_running_reset_is_not_released_for_replay(self):
        step = f"RESET_CARRIER_AND_GET_WAYBILL:{app.SOURCE_CARRIER_ID}:{app.SOURCE_CARRIER_NAME}"
        self.engine._process_job(self.planner, self.job(step))
        self.assert_retired("SKIPPED_UNCERTAIN_WRITE")

    def test_confirmed_with_shipping_history_is_still_uncertain_not_shipped(self):
        self.planner.inspect.return_value = self.readback(has_ship_action=True)
        self.engine._process_job(self.planner, self.job())
        self.assert_retired("SKIPPED_UNCERTAIN_PRINT")

    def test_status_changes_during_readonly_print_poll(self):
        self.engine._recover_running_print(
            self.planner, self.job(), self.readback("WaitConfirm"),
            attempts=2, interval=0,
        )
        self.assert_retired("SKIPPED_UNCERTAIN_PRINT")

    def test_status_changes_during_readonly_write_poll(self):
        self.engine._recover_running_write(
            self.planner, self.job("GET_WAYBILL"), self.readback("WaitConfirm"),
            "GET_WAYBILL", attempts=2, interval=0,
        )
        self.assert_retired("SKIPPED_UNCERTAIN_WRITE")

    def test_preclick_confirmed_releases_and_can_be_claimed_again(self):
        job = self.job(status="PENDING")
        self.planner.inspect.return_value = self.readback(has_ship_action=True)
        self.engine._process_job(self.planner, job)
        self.planner.release.assert_called_once_with("6807385", "13554097", "x" * 32)
        self.planner.complete.assert_not_called()
        self.assertEqual(
            self.store.get_job("6807385", "13554097")["status"], "RELEASED_STATUS_CHANGED"
        )
        self.assertEqual(self.store.excluded_pairs(), [])
        self.assertTrue(self.store.save_job(job["plan"]))
        self.engine._browser_for.assert_not_called()

    def test_remote_failure_preserves_running_and_never_reports_retired(self):
        self.planner.complete.side_effect = app.TransientAPIError("complete unavailable")
        with self.assertRaises(app.TransientAPIError):
            self.engine._process_job(self.planner, self.job())
        self.assertEqual(self.store.get_job("6807385", "13554097")["status"], "RUNNING")
        self.assertFalse(self.store.has_event("SKIPPED_UNCERTAIN_PRINT", "6807385", "13554097"))

    def test_local_write_failure_does_not_claim_success_and_retry_is_idempotent(self):
        job = self.job()
        with mock.patch.object(self.store, "update_job", return_value=False):
            with self.assertRaises(app.SafetyStop):
                self.engine._process_job(self.planner, job)
        self.assertEqual(self.store.get_job("6807385", "13554097")["status"], "RUNNING")
        self.assertFalse(self.store.has_event("SKIPPED_UNCERTAIN_PRINT", "6807385", "13554097"))
        self.engine._process_job(self.planner, job)
        self.assertEqual(self.store.get_job("6807385", "13554097")["status"], "SKIPPED_UNCERTAIN_PRINT")

    def test_wrong_outbound_identity_cannot_retire_order(self):
        self.planner.inspect.return_value = self.readback(io_id="13554098")
        with self.assertRaises(app.PermanentJobError):
            self.engine._process_job(self.planner, self.job())
        self.planner.complete.assert_not_called()
        self.assertEqual(self.store.get_job("6807385", "13554097")["status"], "RUNNING")

    def test_confirmed_is_not_a_terminal_status_or_permission_to_print(self):
        for status in ("Confirmed", " confirmed ", "CONFIRMED"):
            with self.subTest(status=status):
                self.assertFalse(self.engine._is_allowlisted_terminal_status(self.readback(status)))
                with self.assertRaises(app.UnknownOrderStatus):
                    self.engine._require_waitconfirm_status(self.readback(status))

    def test_unknown_and_similar_statuses_still_stop(self):
        job = self.job()
        for status in ("Confirming", "ConfirmedExtra", "Confirmed\nSent", "", None, True):
            with self.subTest(status=status):
                self.planner.inspect.return_value = self.readback(status)
                with self.assertRaises(app.UnknownOrderStatus):
                    self.engine._process_job(self.planner, job)
                self.planner.complete.assert_not_called()
                self.assertEqual(self.store.get_job("6807385", "13554097")["status"], "RUNNING")

    def test_worker_continues_to_next_exact_order_after_historical_running_job(self):
        self.job()
        self.job(io_id="13554098")
        self.planner.inspect.side_effect = lambda o_id, io_id, _token: self.readback(io_id=io_id)

        def complete(*_args):
            if self.planner.complete.call_count == 2:
                self.engine.stop_event.set()

        self.planner.complete.side_effect = complete
        # Any unexpected pause fails immediately instead of hanging this test.
        self.engine._wait_until_running = lambda: self.engine.run_event.is_set()
        with mock.patch.object(app, "PlannerClient", return_value=self.planner):
            self.engine._worker()
        self.assertEqual(
            [call.args[:2] for call in self.planner.complete.call_args_list],
            [("6807385", "13554097"), ("6807385", "13554098")],
        )
        self.assertFalse(self.store.active_jobs())
        self.engine._browser_for.assert_not_called()


if __name__ == "__main__":
    unittest.main()
