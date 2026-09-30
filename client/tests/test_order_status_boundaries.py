import hashlib
import threading
import unittest
from unittest import mock

import jst_auto_print_app as app


def _plan(o_id="6790120", io_id="13541841"):
    return {
        "o_id": o_id,
        "io_id": io_id,
        "claim_token": "x" * 32,
        "steps": ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
        "outbound_identity_unique": True,
    }


def _job(o_id="6790120", io_id="13541841"):
    return {
        "o_id": o_id,
        "io_id": io_id,
        "status": "PENDING",
        "step_index": 0,
        "plan": _plan(o_id, io_id),
    }


def _readback(status, o_id="6790120", io_id="13541841"):
    return {
        "found": True,
        "o_id": o_id,
        "io_id": io_id,
        "status": status,
        "has_waybill": True,
        "waybill_suffix": str(io_id)[-4:],
        "waybill_fingerprint": hashlib.sha256(
            str(io_id).encode("utf-8")
        ).hexdigest(),
        "has_print_action": False,
        "has_ship_action": False,
    }


class _TerminalStore:
    def __init__(self, *, print_ok=False):
        self.print_ok = print_ok
        self.updates = []
        self.sku_calls = 0

    def has_event(self, event_type, o_id, io_id):
        return self.print_ok and event_type == "PRINT_OK"

    def record_sku_outbound(self, readback):
        self.sku_calls += 1
        return 2

    def update_job(self, o_id, io_id, **fields):
        self.updates.append((o_id, io_id, fields))
        return True


class _BatchStore(_TerminalStore):
    def mark_batch_running(self, jobs):
        return True


class OrderStatusBoundaryTests(unittest.TestCase):
    def test_exact_terminal_allowlist_normalizes_case_and_outer_whitespace(self):
        for value, kind in (
            ("Sent", "SENT"),
            (" sent ", "SENT"),
            ("Delete", "DELETED"),
            (" DELETE ", "DELETED"),
        ):
            with self.subTest(value=value):
                readback = {"status": value}
                self.assertEqual(app.AutomationEngine._terminal_status_kind(readback), kind)
                self.assertTrue(
                    app.AutomationEngine._is_allowlisted_terminal_status(readback)
                )

    def test_similar_unknown_or_non_text_statuses_remain_fail_closed(self):
        for value in (
            "Deleted",
            "DeleteOrder",
            "Delete\nSent",
            "取消",
            "WaitConfirm",
            "",
            None,
            1,
            True,
            {"value": "Delete"},
        ):
            with self.subTest(value=value):
                self.assertIsNone(
                    app.AutomationEngine._terminal_status_kind({"status": value})
                )
                self.assertFalse(
                    app.AutomationEngine._is_allowlisted_terminal_status(
                        {"status": value}
                    )
                )

    def test_waitconfirm_accepts_normalized_value_and_routes_terminals(self):
        for value in ("WaitConfirm", " waitconfirm ", "WAITCONFIRM"):
            with self.subTest(value=value):
                app.AutomationEngine._require_waitconfirm_status({"status": value})

        for value in ("Sent", "Delete"):
            with self.subTest(value=value):
                with self.assertRaises(app.ExternalTerminalStatusDetected):
                    app.AutomationEngine._require_waitconfirm_status(
                        {"status": value}
                    )

        for value in ("Deleted", "Cancelled", "Confirming"):
            with self.subTest(value=value):
                with self.assertRaises(app.UnknownOrderStatus):
                    app.AutomationEngine._require_waitconfirm_status(
                        {"status": value}
                    )

    def test_delete_is_completed_without_browser_action_or_sku_count(self):
        engine = object.__new__(app.AutomationEngine)
        engine.store = _TerminalStore(print_ok=True)
        completed = []
        events = []
        engine._complete_claim = lambda planner, plan, reason: completed.append(
            (plan["o_id"], plan["io_id"], reason)
        )
        engine._event = lambda *args, **kwargs: events.append((args, kwargs))

        engine._skip_terminal_status_job(
            object(), _job(), _readback(" Delete ")
        )

        self.assertEqual(completed, [("6790120", "13541841", "TERMINAL")])
        self.assertEqual(engine.store.sku_calls, 0)
        self.assertEqual(
            engine.store.updates,
            [
                (
                    "6790120",
                    "13541841",
                    {"step_index": 0, "status": "SKIPPED_DELETED"},
                )
            ],
        )
        self.assertEqual(events[0][0][1], "SKIPPED_DELETED")
        self.assertIn("继续下一单", events[0][0][2])

    def test_sent_keeps_existing_print_ok_sku_recovery(self):
        engine = object.__new__(app.AutomationEngine)
        engine.store = _TerminalStore(print_ok=True)
        engine._complete_claim = lambda *_args: None
        engine._validate_items_unchanged = lambda *_args: None
        engine._event = lambda *_args, **_kwargs: None

        engine._skip_terminal_status_job(object(), _job(), _readback("Sent"))

        self.assertEqual(engine.store.sku_calls, 1)
        self.assertEqual(
            engine.store.updates[0][2]["status"], "SKIPPED_TERMINAL_STATUS"
        )

    def test_delete_does_not_change_local_state_when_remote_complete_fails(self):
        engine = object.__new__(app.AutomationEngine)
        engine.store = _TerminalStore()
        engine._complete_claim = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("complete unavailable")
        )
        engine._event = lambda *_args, **_kwargs: None

        with self.assertRaisesRegex(RuntimeError, "complete unavailable"):
            engine._skip_terminal_status_job(
                object(), _job(), _readback("Delete")
            )

        self.assertEqual(engine.store.updates, [])
        self.assertEqual(engine.store.sku_calls, 0)

    def test_process_job_delete_never_opens_browser_for_pending_or_running(self):
        for local_status in ("PENDING", "RUNNING"):
            with self.subTest(local_status=local_status):
                engine = object.__new__(app.AutomationEngine)
                engine.store = _TerminalStore()
                engine._renew_step = lambda *_args: None
                engine._settings = lambda: app.Settings(
                    allow_write=True,
                    allow_print=True,
                    print_profile=app.SOURCE_CARRIER_ID,
                )
                engine._inspect = lambda *_args: _readback("Delete")
                engine._complete_claim = lambda *_args: None
                engine._event = lambda *_args, **_kwargs: None
                engine._browser_for = lambda *_args: self.fail(
                    "Delete terminal must not open or operate the browser"
                )
                job = _job()
                job["status"] = local_status

                engine._process_job(object(), job)

                self.assertEqual(
                    engine.store.updates[0][2]["status"], "SKIPPED_DELETED"
                )

    def test_batch_post_click_delete_settles_one_order_and_continues(self):
        engine = object.__new__(app.AutomationEngine)
        engine.store = _BatchStore()
        engine.action_lock = threading.RLock()
        engine.dom_lookup_retries = {}
        engine._settings = lambda: app.Settings(
            allow_write=True,
            allow_print=True,
            print_profile=app.SOURCE_CARRIER_ID,
        )
        engine._renew_step = lambda *_args: None
        engine._inspect = lambda _planner, plan: _readback(
            "WaitConfirm", plan["o_id"], plan["io_id"]
        )
        engine._validate_items_unchanged = lambda *_args: None
        engine._validate_final_print_readback = lambda *_args: None
        engine._event = lambda *_args, **_kwargs: None
        engine._require_operator_permission = lambda: None

        class Browser:
            def print_express_batch(self, identities, **kwargs):
                kwargs["before_click"]()
                kwargs["mark_running"]()
                kwargs["final_guard"]()

        engine._browser_for = lambda _settings: Browser()
        first = _job("6790120", "13541841")
        second = _job("6790195", "13541843")
        poll_results = iter(
            [
                app.ExternalTerminalStatusDetected(
                    _readback("Delete", "6790120", "13541841")
                ),
                _readback("WaitConfirm", "6790195", "13541843"),
            ]
        )

        def poll(*_args, **_kwargs):
            result = next(poll_results)
            if isinstance(result, Exception):
                raise result
            return result

        engine._poll_readback = poll
        retired = []
        completed = []
        engine._skip_terminal_status_job = (
            lambda _planner, job, readback: retired.append(
                (job["o_id"], readback["status"])
            )
        )
        engine._complete_printed_job = (
            lambda _planner, job, readback, next_index: completed.append(
                (job["o_id"], readback["status"], next_index)
            )
        )

        with mock.patch.object(app, "print_service_online", return_value=True):
            engine._process_print_batch(object(), [first, second])

        self.assertEqual(retired, [("6790120", "Delete")])
        self.assertEqual(completed, [("6790195", "WaitConfirm", 2)])


if __name__ == "__main__":
    unittest.main()
