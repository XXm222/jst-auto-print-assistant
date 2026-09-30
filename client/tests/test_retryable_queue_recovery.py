import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import jst_auto_print_app as app


def _plan(
    o_id="6797258",
    io_id="13547312",
    *,
    token="x" * 32,
    carrier_id=app.TARGET_CARRIER_ID,
    carrier_name=app.TARGET_CARRIER_NAME,
):
    return {
        "o_id": o_id,
        "io_id": io_id,
        "claim_token": token,
        "steps": ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
        "current_carrier_id": carrier_id,
        "current_carrier": carrier_name,
        "outbound_identity_unique": True,
    }


def _job(**kwargs):
    plan = _plan(**kwargs)
    return {
        "o_id": plan["o_id"],
        "io_id": plan["io_id"],
        "status": "PENDING",
        "step_index": 0,
        "plan": plan,
    }


class RetryableEventStoreTests(unittest.TestCase):
    def test_released_row_is_not_excluded_and_accepts_fresh_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = app.EventStore(root / "events.sqlite3", root / "events.jsonl")
            first = _plan(token="a" * 32)
            self.assertTrue(store.save_job(first))
            self.assertTrue(
                store.update_job(
                    first["o_id"],
                    first["io_id"],
                    step_index=0,
                    status="RELEASED_STATUS_CHANGED",
                )
            )
            self.assertNotIn(
                {"o_id": first["o_id"], "io_id": first["io_id"]},
                store.excluded_pairs(),
            )

            second = _plan(token="b" * 32)
            self.assertTrue(store.save_job(second))
            restored = store.get_job(first["o_id"], first["io_id"])
            self.assertEqual(restored["status"], "PENDING")
            self.assertEqual(restored["step_index"], 0)
            self.assertEqual(restored["plan"]["claim_token"], "b" * 32)

    def test_real_terminal_row_still_cannot_be_reactivated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = app.EventStore(root / "events.sqlite3", root / "events.jsonl")
            plan = _plan()
            self.assertTrue(store.save_job(plan))
            store.update_job(
                plan["o_id"], plan["io_id"], step_index=2, status="COMPLETED"
            )
            self.assertFalse(store.save_job(_plan(token="b" * 32)))
            self.assertEqual(
                store.get_job(plan["o_id"], plan["io_id"])["status"],
                "COMPLETED",
            )


class RetryableEngineTests(unittest.TestCase):
    def _engine(self):
        engine = object.__new__(app.AutomationEngine)
        engine.events = []
        engine._event = lambda *args, **kwargs: engine.events.append((args, kwargs))
        return engine

    def test_profile_split_preserves_current_paper_and_defers_old_paper(self):
        current = _job()
        old = _job(
            o_id="6797538",
            io_id="13547305",
            carrier_id=app.SOURCE_CARRIER_ID,
            carrier_name=app.SOURCE_CARRIER_NAME,
        )
        matching, mismatched = app.AutomationEngine._split_active_jobs_for_profile(
            [old, current], app.TARGET_CARRIER_ID
        )
        self.assertEqual(matching, [current])
        self.assertEqual(mismatched, [old])

    def test_preclick_release_is_audited_and_does_not_exclude(self):
        class Store:
            def __init__(self):
                self.updates = []

            def update_job(self, o_id, io_id, **fields):
                self.updates.append((o_id, io_id, fields))
                return True

            def exclude_job(self, *_args):
                raise AssertionError("retryable release must not permanently exclude")

        class Planner:
            def __init__(self):
                self.releases = []

            def release(self, *identity):
                self.releases.append(identity)

        engine = self._engine()
        engine.store = Store()
        planner = Planner()
        job = _job()
        engine._release_retryable_job(
            planner, job, "RELEASED_STATUS_CHANGED", "订单状态已变化：Confirmed"
        )

        self.assertEqual(
            planner.releases,
            [(job["o_id"], job["io_id"], "x" * 32)],
        )
        self.assertEqual(
            engine.store.updates[0][2]["status"], "RELEASED_STATUS_CHANGED"
        )
        self.assertIn("继续下一单", engine.events[0][0][2])

    def test_lost_preclick_lease_remains_retryable(self):
        class Store:
            def __init__(self):
                self.updates = []

            def update_job(self, o_id, io_id, **fields):
                self.updates.append((o_id, io_id, fields))
                return True

            def exclude_job(self, *_args):
                raise AssertionError("pre-click lease loss must not be excluded")

        engine = self._engine()
        engine.store = Store()
        engine._retire_lost_lease(_job(), "租约已失效")
        self.assertEqual(engine.store.updates[0][2]["status"], "RELEASED_LEASE_LOST")

    def test_running_job_recovers_even_when_operator_selected_other_paper(self):
        job = _job(
            carrier_id=app.SOURCE_CARRIER_ID,
            carrier_name=app.SOURCE_CARRIER_NAME,
        )
        job["status"] = "RUNNING"
        engine = self._engine()
        engine.store = object()
        engine._settings = lambda: app.Settings(
            allow_write=True,
            allow_print=True,
            print_profile=app.TARGET_CARRIER_ID,
        )
        engine._renew_step = lambda *_args: None
        engine._inspect = lambda *_args: {
            "found": True,
            "o_id": job["o_id"],
            "io_id": job["io_id"],
            "status": "WaitConfirm",
            "has_ship_action": False,
        }
        engine._require_job_identity = lambda *_args: None
        engine._recover_running_print = mock.Mock()
        engine._process_job(object(), job)
        engine._recover_running_print.assert_called_once()


class LiveReadonlyProbeTests(unittest.TestCase):
    def test_probe_performs_three_reads_and_zero_business_writes(self):
        class Browser:
            def __init__(self, _settings):
                self.calls = 0
                self.closed = False

            def is_healthy(self):
                return True

            def _call_page(self, method, args, **kwargs):
                self.calls += 1
                self.assert_read = (method, args, kwargs)
                return json.dumps({"datas": [{"o_id": "1"}], "dp": {"DataCount": 1}})

            def close(self):
                self.closed = True

        class Planner:
            def __init__(self, _settings, _workstation):
                pass

            def ping(self):
                return {"ok": True}

        settings = app.Settings(
            api_url="https://example.com/jst-print-api/v1",
            api_token="x" * 40,
        )
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "probe.json"
            with mock.patch.object(app, "load_settings", return_value=settings), mock.patch.object(
                app, "load_workstation_id", return_value="ws-12345678"
            ), mock.patch.object(app, "PlannerClient", Planner), mock.patch.object(
                app, "JSTNativeBrowser", Browser
            ):
                code = app.live_readonly_probe(output)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(payload["native_fetch_reads"], 3)
        self.assertEqual(payload["row_counts"], [1, 1, 1])
        self.assertEqual(payload["business_writes"], 0)
        self.assertEqual(payload["probe"], "PASS")


if __name__ == "__main__":
    unittest.main()
