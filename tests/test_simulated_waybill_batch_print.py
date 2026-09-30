import hashlib
import json
import threading
import unittest
from unittest import mock

import jst_auto_print_app as app
import jst_print_shadow_plan as planner


def _planner_order(*, waybill="") -> dict:
    return {
        "o_id": "41001",
        "io_id": "51001",
        "created": "2026-08-27 10:00:00",
        "io_date": "2026-08-27 10:00:00",
        "shop_name": "模拟店铺",
        "wms_co_id": planner.WAREHOUSE_ID,
        "status": "WaitConfirm",
        "logistics_company": planner.TARGET_CARRIER_NAME,
        "lc_id": planner.TARGET_CARRIER_ID,
        "weight": 1,
        "l_id": waybill,
        "is_print_express": False,
        "remark": "",
        "labels": "",
        "items": [
            {
                "ioi_id": "line-51001",
                "i_id": "product-51001",
                "sku_id": "sku-51001",
                "name": "模拟商品",
                "qty": 1,
                "unit": "件",
            }
        ],
    }


def _job(index: int, *, step_index: int = 0) -> dict:
    o_id = str(710000 + index)
    io_id = str(910000 + index)
    return {
        "o_id": o_id,
        "io_id": io_id,
        "status": "PENDING",
        "step_index": step_index,
        "plan": {
            "o_id": o_id,
            "io_id": io_id,
            "claim_token": f"claim-{index}-" + "x" * 24,
            "steps": ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
            "outbound_identity_unique": True,
            "current_carrier_id": app.SOURCE_CARRIER_ID,
            "current_carrier": app.SOURCE_CARRIER_NAME,
            "privacy_required": False,
            "items": [
                {
                    "line_key": f"line-{index}",
                    "product_id": f"product-{index}",
                    "sku_id": f"sku-{index}",
                    "sku_name": "模拟商品",
                    "qty": 1.0,
                    "unit": "件",
                }
            ],
            "source_item_count": 1,
        },
    }


class _SimulationStore:
    def __init__(self, jobs: list[dict]):
        self.jobs = {
            (str(job["o_id"]), str(job["io_id"])): job for job in jobs
        }
        self.events: list[tuple[str, str, str]] = []
        self.event_details: dict[tuple[str, str, str], dict] = {}
        self.completed_claims: list[tuple[str, str, str]] = []
        self.sku_rows: list[tuple[str, str]] = []

    def update_job(self, o_id, io_id, *, step_index, status, **_kwargs):
        job = self.jobs[(str(o_id), str(io_id))]
        job["step_index"] = int(step_index)
        job["status"] = str(status)
        return True

    def mark_batch_running(self, jobs):
        if not all(str(job.get("status")) == "PREPARING" for job in jobs):
            return False
        for job in jobs:
            job["status"] = "RUNNING"
        return True

    def has_event(self, event_type, o_id, io_id):
        return (str(event_type), str(o_id), str(io_id)) in self.events

    def latest_event_detail(self, event_type, o_id, io_id):
        return self.event_details.get(
            (str(event_type), str(o_id), str(io_id))
        )

    def record_sku_outbound(self, readback):
        self.sku_rows.append((str(readback["o_id"]), str(readback["io_id"])))
        return len(readback.get("items") or [])


class _SimulationPlanner:
    def __init__(self, jobs: list[dict]):
        self.states = {
            (str(job["o_id"]), str(job["io_id"])): {
                "waybill": int(job["step_index"]) >= 1,
                "printed": False,
            }
            for job in jobs
        }
        self.inspect_calls: list[list[tuple[str, str, str]]] = []
        self.before_click_mutation = None

    def inspect_batch(self, credentials):
        self.inspect_calls.append(list(credentials))
        if self.before_click_mutation is not None and len(self.inspect_calls) == 2:
            self.before_click_mutation(self.states)
        return [self._readback(o_id, io_id) for o_id, io_id, _token in credentials]

    def _readback(self, o_id: str, io_id: str) -> dict:
        state = self.states[(str(o_id), str(io_id))]
        suffix = str(io_id)[-4:] if state["waybill"] else ""
        fingerprint = (
            hashlib.sha256(str(io_id).encode("utf-8")).hexdigest()
            if state["waybill"]
            else ""
        )
        return {
            "found": True,
            "o_id": str(o_id),
            "io_id": str(io_id),
            "status": "WaitConfirm",
            "carrier_id": app.SOURCE_CARRIER_ID,
            "carrier_name": app.SOURCE_CARRIER_NAME,
            "privacy_required": False,
            "privacy_source": "remark",
            "has_waybill": state["waybill"],
            "waybill_suffix": suffix,
            "waybill_fingerprint": fingerprint,
            "has_print_action": state["printed"],
            "has_ship_action": False,
            "action_history_complete": True,
            "items": [
                {
                    "line_key": f"line-{int(o_id) - 710000}",
                    "product_id": f"product-{int(o_id) - 710000}",
                    "sku_id": f"sku-{int(o_id) - 710000}",
                    "sku_name": "模拟商品",
                    "qty": 1.0,
                    "unit": "件",
                }
            ],
            "source_item_count": 1,
        }


class _SimulationBrowser:
    def __init__(self, planner_double: _SimulationPlanner):
        self.planner = planner_double
        self.waybill_clicks = 0
        self.print_clicks = 0
        self.raise_after_print_commit = False
        self.print_success_limit = None

    @staticmethod
    def _run_guards(kwargs):
        kwargs["before_click"]()
        kwargs["mark_running"]()
        kwargs["final_guard"]()

    def get_waybill_batch(self, identities, **kwargs):
        self._run_guards(kwargs)
        self.waybill_clicks += 1
        for identity in identities:
            self.planner.states[tuple(identity)]["waybill"] = True

    def print_express_batch(self, identities, **kwargs):
        self._run_guards(kwargs)
        self.print_clicks += 1
        selected = identities
        if self.print_success_limit is not None:
            selected = identities[: self.print_success_limit]
        for identity in selected:
            self.planner.states[tuple(identity)]["printed"] = True
        if self.raise_after_print_commit:
            raise RuntimeError("模拟按钮点击后页面响应丢失")


def _simulation_engine(jobs: list[dict], planner_double: _SimulationPlanner):
    store = _SimulationStore(jobs)
    browser = _SimulationBrowser(planner_double)
    engine = object.__new__(app.AutomationEngine)
    engine.store = store
    engine.action_lock = threading.RLock()
    engine.dom_lookup_retries = {}
    engine._settings = lambda: app.Settings(
        allow_write=True,
        allow_print=True,
        print_profile=app.SOURCE_CARRIER_ID,
    )
    engine._require_operator_permission = lambda: None
    engine._browser_for = lambda _settings: browser
    engine._validate_items_unchanged = lambda *_args: None
    engine._validate_live_route = lambda *_args: None
    engine._preflight = lambda *_args: None

    def validate_print(_plan, readback, _o_id, _io_id):
        if readback.get("has_waybill") is not True or not readback.get(
            "waybill_suffix"
        ):
            raise app.PermanentJobError("模拟打印前没有可识别运单号")

    engine._validate_final_print_readback = validate_print
    engine._complete_claim = lambda _planner, plan, reason: store.completed_claims.append(
        (str(plan["o_id"]), str(plan["io_id"]), str(reason))
    )

    def event(_level, event_type, _message, *, o_id="", io_id="", **kwargs):
        key = (str(event_type), str(o_id), str(io_id))
        store.events.append(key)
        detail = kwargs.get("detail")
        if isinstance(detail, dict):
            store.event_details[key] = dict(detail)

    engine._event = event
    return engine, store, browser


class SimulatedWaybillAndBatchPrintTests(unittest.TestCase):
    def test_real_planner_detects_waybill_and_only_exposes_last_four(self):
        full_waybill = "7530012345670466"
        row = _planner_order(waybill=full_waybill)

        readback = planner._found_inspect_result(
            row, [], True, generated_at="2026-08-27T03:00:00Z"
        )
        plan = planner.make_plan(row, [], action_history_complete=True)

        self.assertTrue(readback["has_waybill"])
        self.assertEqual(readback["waybill_suffix"], "0466")
        self.assertEqual(
            readback["waybill_fingerprint"],
            hashlib.sha256(full_waybill.encode("utf-8")).hexdigest(),
        )
        same_suffix = planner._found_inspect_result(
            _planner_order(waybill="9900000000000466"),
            [],
            True,
            generated_at="2026-08-27T03:00:00Z",
        )
        self.assertEqual(same_suffix["waybill_suffix"], "0466")
        self.assertNotEqual(
            same_suffix["waybill_fingerprint"],
            readback["waybill_fingerprint"],
        )
        self.assertNotIn(full_waybill, json.dumps(readback, ensure_ascii=False))
        self.assertTrue(plan.has_waybill)
        self.assertEqual(plan.steps, ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"])

    def test_real_planner_treats_empty_or_whitespace_waybill_as_missing(self):
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                row = _planner_order(waybill=raw)
                readback = planner._found_inspect_result(
                    row, [], True, generated_at="2026-08-27T03:00:00Z"
                )
                self.assertFalse(readback["has_waybill"])
                self.assertEqual(readback["waybill_suffix"], "")
                self.assertEqual(readback["waybill_fingerprint"], "")

    def test_ten_orders_take_numbers_then_print_with_one_click_per_batch(self):
        jobs = [_job(index) for index in range(10)]
        planner_double = _SimulationPlanner(jobs)
        engine, store, browser = _simulation_engine(jobs, planner_double)

        with mock.patch.object(app, "print_service_online", return_value=True):
            engine._process_waybill_batch(planner_double, jobs)

            self.assertEqual(browser.waybill_clicks, 1)
            self.assertEqual(
                [(job["step_index"], job["status"]) for job in jobs],
                [(1, "PENDING")] * 10,
            )
            waybill_events = [event for event in store.events if event[0] == "WAYBILL_OK"]
            self.assertEqual(len(waybill_events), 10)

            engine._process_print_batch(planner_double, jobs)

        self.assertEqual(browser.print_clicks, 1)
        self.assertEqual(
            [(job["step_index"], job["status"]) for job in jobs],
            [(3, "COMPLETED")] * 10,
        )
        self.assertEqual(len(store.completed_claims), 10)
        self.assertEqual({reason for *_pair, reason in store.completed_claims}, {"PRINTED"})
        self.assertEqual(len(store.sku_rows), 10)

    def test_batch_print_accepts_2_9_10_and_clicks_exactly_once(self):
        for size in (2, 9, 10):
            with self.subTest(size=size):
                jobs = [_job(index, step_index=1) for index in range(size)]
                planner_double = _SimulationPlanner(jobs)
                engine, _store, browser = _simulation_engine(jobs, planner_double)

                with mock.patch.object(app, "print_service_online", return_value=True):
                    engine._process_print_batch(planner_double, jobs)

                self.assertEqual(browser.print_clicks, 1)
                self.assertTrue(all(job["status"] == "COMPLETED" for job in jobs))

    def test_large_queues_are_partitioned_into_10_order_click_groups(self):
        expected = {
            0: [],
            1: [1],
            10: [10],
            11: [10, 1],
            20: [10, 10],
            21: [10, 10, 1],
            25: [10, 10, 5],
        }
        for total, expected_sizes in expected.items():
            with self.subTest(total=total):
                pending = [_job(index, step_index=1) for index in range(total)]
                actual_sizes = []
                while pending:
                    batch = app.AutomationEngine._next_print_batch(pending)
                    actual_sizes.append(len(batch))
                    pending = pending[len(batch) :]
                self.assertEqual(actual_sizes, expected_sizes)

    def test_twenty_five_ready_orders_use_exactly_three_real_batch_calls(self):
        jobs = [_job(index, step_index=1) for index in range(25)]
        planner_double = _SimulationPlanner(jobs)
        engine, _store, browser = _simulation_engine(jobs, planner_double)
        pending = list(jobs)

        with mock.patch.object(app, "print_service_online", return_value=True):
            while pending:
                batch = app.AutomationEngine._next_print_batch(pending)
                engine._process_print_batch(planner_double, batch)
                pending = pending[len(batch) :]

        self.assertEqual(browser.print_clicks, 3)
        self.assertTrue(all(job["status"] == "COMPLETED" for job in jobs))

    def test_partial_post_click_confirmation_settles_only_proven_orders(self):
        jobs = [_job(index, step_index=1) for index in range(3)]
        planner_double = _SimulationPlanner(jobs)
        engine, _store, browser = _simulation_engine(jobs, planner_double)
        browser.print_success_limit = 2

        with mock.patch.object(app, "print_service_online", return_value=True):
            engine._process_print_batch(planner_double, jobs)

        self.assertEqual(browser.print_clicks, 1)
        self.assertEqual(
            [(job["step_index"], job["status"]) for job in jobs],
            [(3, "COMPLETED"), (3, "COMPLETED"), (1, "RUNNING")],
        )

    def test_batch_methods_reject_0_1_11_before_any_click(self):
        for method_name in ("_process_waybill_batch", "_process_print_batch"):
            for size in (0, 1, 11):
                with self.subTest(method=method_name, size=size):
                    jobs = [_job(index) for index in range(size)]
                    planner_double = _SimulationPlanner(jobs)
                    engine, _store, browser = _simulation_engine(jobs, planner_double)

                    with self.assertRaises(app.SafetyStop):
                        getattr(engine, method_name)(planner_double, jobs)

                    self.assertEqual(browser.waybill_clicks, 0)
                    self.assertEqual(browser.print_clicks, 0)

    def test_waybill_disappearing_at_final_guard_causes_zero_print_clicks(self):
        jobs = [_job(index, step_index=1) for index in range(2)]
        planner_double = _SimulationPlanner(jobs)
        changed_pair = (str(jobs[1]["o_id"]), str(jobs[1]["io_id"]))
        planner_double.before_click_mutation = lambda states: states[changed_pair].update(
            waybill=False
        )
        engine, _store, browser = _simulation_engine(jobs, planner_double)
        processed_singly = []
        engine._process_job = lambda _planner, job: processed_singly.append(
            (str(job["o_id"]), str(job["io_id"]))
        )

        with mock.patch.object(app, "print_service_online", return_value=True):
            engine._process_print_batch(planner_double, jobs)

        self.assertEqual(browser.print_clicks, 0)
        self.assertEqual(processed_singly, [changed_pair])
        self.assertTrue(all(job["status"] == "PENDING" for job in jobs))

    def test_exception_after_committed_click_is_uncertain_and_never_reclicked(self):
        jobs = [_job(index, step_index=1) for index in range(2)]
        planner_double = _SimulationPlanner(jobs)
        engine, _store, browser = _simulation_engine(jobs, planner_double)
        browser.raise_after_print_commit = True

        with mock.patch.object(app, "print_service_online", return_value=True):
            with self.assertRaises(app.CommittedBatchUncertain):
                engine._process_print_batch(planner_double, jobs)

        self.assertEqual(browser.print_clicks, 1)
        self.assertTrue(all(job["status"] == "RUNNING" for job in jobs))

    def test_committed_print_with_lost_callback_recovers_print_ok_and_sku(self):
        jobs = [_job(index, step_index=1) for index in range(2)]
        planner_double = _SimulationPlanner(jobs)
        engine, store, browser = _simulation_engine(jobs, planner_double)
        browser.raise_after_print_commit = True

        with mock.patch.object(app, "print_service_online", return_value=True):
            with self.assertRaises(app.CommittedBatchUncertain):
                engine._process_print_batch(planner_double, jobs)

        engine._renew_step = lambda *_args: None
        engine._inspect = lambda _planner, plan: planner_double._readback(
            str(plan["o_id"]), str(plan["io_id"])
        )
        for job in jobs:
            engine._process_job(planner_double, job)

        self.assertEqual(browser.print_clicks, 1)
        self.assertEqual(
            [(job["step_index"], job["status"]) for job in jobs],
            [(3, "COMPLETED"), (3, "COMPLETED")],
        )
        self.assertEqual(len(store.sku_rows), 2)
        self.assertEqual(
            len([event for event in store.events if event[0] == "PRINT_OK"]),
            2,
        )

    def test_committed_print_recovery_rejects_changed_waybill_fingerprint(self):
        jobs = [_job(index, step_index=1) for index in range(2)]
        planner_double = _SimulationPlanner(jobs)
        engine, store, browser = _simulation_engine(jobs, planner_double)
        browser.raise_after_print_commit = True

        with mock.patch.object(app, "print_service_online", return_value=True):
            with self.assertRaises(app.CommittedBatchUncertain):
                engine._process_print_batch(planner_double, jobs)

        engine._renew_step = lambda *_args: None

        def changed_readback(_planner, plan):
            value = planner_double._readback(str(plan["o_id"]), str(plan["io_id"]))
            value["waybill_fingerprint"] = "b" * 64
            return value

        engine._inspect = changed_readback
        with self.assertRaisesRegex(
            app.CompletionProofError, "完整运单指纹与本地提交凭据不一致"
        ):
            engine._process_job(planner_double, jobs[0])

        self.assertEqual(browser.print_clicks, 1)
        self.assertEqual(jobs[0]["status"], "RUNNING")
        self.assertEqual(store.sku_rows, [])


if __name__ == "__main__":
    unittest.main()
