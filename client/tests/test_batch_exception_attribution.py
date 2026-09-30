import copy
import hashlib
import threading
import unittest
from unittest import mock

import jst_auto_print_app as app


def _waybill_job(index: int) -> dict:
    o_id = str(6792656 + index)
    io_id = str(13544115 + index)
    return {
        "o_id": o_id,
        "io_id": io_id,
        "status": "PENDING",
        "step_index": 0,
        "plan": {
            "o_id": o_id,
            "io_id": io_id,
            "claim_token": f"token-{index}-" + "x" * 24,
            "steps": [
                f"RESET_CARRIER_AND_GET_WAYBILL:{app.SOURCE_CARRIER_ID}:"
                f"{app.SOURCE_CARRIER_NAME}",
                "PRINT_EXPRESS",
                "STOP_BEFORE_PRESHIP",
            ],
            "current_carrier_id": app.TARGET_CARRIER_ID,
            "current_carrier": app.TARGET_CARRIER_NAME,
            "privacy_required": False,
            "outbound_identity_unique": True,
            "items": [],
        },
    }


def _print_job(index: int) -> dict:
    job = _waybill_job(index)
    job["plan"]["steps"] = ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"]
    job["plan"]["current_carrier_id"] = app.SOURCE_CARRIER_ID
    job["plan"]["current_carrier"] = app.SOURCE_CARRIER_NAME
    return job


class _StateStore:
    def __init__(self, jobs):
        self.jobs = {
            (str(job["o_id"]), str(job["io_id"])): copy.deepcopy(job)
            for job in jobs
        }
        self.updates = []
        self.next_job_calls = 0

    def update_job(self, o_id, io_id, **fields):
        key = (str(o_id), str(io_id))
        job = self.jobs.get(key)
        if job is None:
            return False
        job.update(fields)
        self.updates.append((key, dict(fields)))
        return True

    def mark_batch_running(self, jobs):
        keys = [(str(job["o_id"]), str(job["io_id"])) for job in jobs]
        if any(self.jobs[key]["status"] != "PREPARING" for key in keys):
            return False
        for key in keys:
            self.jobs[key]["status"] = "RUNNING"
        return True

    def get_job(self, o_id, io_id):
        job = self.jobs.get((str(o_id), str(io_id)))
        return copy.deepcopy(job) if job is not None else None

    def has_event(self, _event_type, _o_id, _io_id):
        return False

    def next_job(self):
        self.next_job_calls += 1
        return copy.deepcopy(next(iter(self.jobs.values())))

    def active_jobs(self):
        return [
            copy.deepcopy(job)
            for job in self.jobs.values()
            if str(job.get("status")) in app.ACTIVE_JOB_STATUSES
        ]


def _base_engine(jobs) -> app.AutomationEngine:
    engine = object.__new__(app.AutomationEngine)
    engine.store = _StateStore(jobs)
    engine.action_lock = threading.RLock()
    engine.dom_lookup_retries = {}
    engine._settings = lambda: app.Settings(
        allow_write=True,
        allow_print=True,
        print_profile=app.SOURCE_CARRIER_ID,
    )
    engine._renew_step = lambda *_args: None
    engine._inspect = lambda _planner, plan: {
        "o_id": str(plan["o_id"]),
        "io_id": str(plan["io_id"]),
        "has_waybill": True,
        "waybill_suffix": str(plan["io_id"])[-4:],
        "waybill_fingerprint": hashlib.sha256(
            str(plan["io_id"]).encode("utf-8")
        ).hexdigest(),
    }
    engine._require_job_identity = lambda *_args: None
    engine._is_allowlisted_terminal_status = lambda _readback: False
    engine._require_waitconfirm_status = lambda *_args: None
    engine._validate_items_unchanged = lambda *_args: None
    engine._validate_final_print_readback = lambda *_args: None
    engine._preflight = lambda *_args: None
    engine._require_operator_permission = lambda: None
    engine._event = lambda *_args, **_kwargs: None
    return engine


class BatchExceptionAttributionTests(unittest.TestCase):
    def test_external_print_request_added_after_claim_blocks_batch_browser_click(self):
        jobs = [_print_job(0), _print_job(1)]
        engine = _base_engine(jobs)
        processed = []
        clicked = []
        engine._process_job = lambda _planner, job: processed.append(job)
        engine._validate_final_print_readback = (
            lambda _plan, readback, _o_id, _io_id: app.AutomationEngine._preflight(
                readback, "PRINT_EXPRESS", None
            )
        )

        class Planner:
            def __init__(self):
                self.calls = 0

            def inspect_batch(self, _credentials):
                self.calls += 1
                results = [
                    {
                        "found": True,
                        "o_id": job["o_id"],
                        "io_id": job["io_id"],
                        "status": "WaitConfirm",
                        "action_history_complete": True,
                        "has_print_request": False,
                        "has_print_action": False,
                        "has_ship_action": False,
                        "is_print_express": False,
                        "delivery_hold_marked": False,
                        "delivery_hold_reasons": [],
                        "has_waybill": True,
                        "waybill_suffix": str(job["io_id"])[-4:],
                        "waybill_fingerprint": hashlib.sha256(
                            str(job["io_id"]).encode("utf-8")
                        ).hexdigest(),
                    }
                    for job in jobs
                ]
                if self.calls == 2:
                    results[1]["has_print_request"] = True
                return results

        class Browser:
            def print_express_batch(self, _identities, **kwargs):
                kwargs["before_click"]()
                kwargs["mark_running"]()
                clicked.append(True)

        engine._browser_for = lambda _settings: Browser()
        with mock.patch.object(app, "print_service_online", return_value=True):
            engine._process_print_batch(Planner(), jobs)

        self.assertEqual(clicked, [])
        self.assertEqual(processed, [jobs[1]])
        self.assertTrue(
            all(
                engine.store.get_job(job["o_id"], job["io_id"])["status"]
                == "PENDING"
                for job in jobs
            )
        )

    def test_real_batch_print_reinspects_before_mark_running(self):
        jobs = [_print_job(0), _print_job(1)]
        engine = _base_engine(jobs)
        processed = []
        clicked = []
        engine._process_job = lambda _planner, job: processed.append(job)

        class Planner:
            def __init__(self):
                self.calls = []

            def inspect_batch(self, credentials):
                self.calls.append(list(credentials))
                results = [
                    {
                        "found": True,
                        "o_id": job["o_id"],
                        "io_id": job["io_id"],
                        "status": "WaitConfirm",
                        "has_ship_action": False,
                        "has_print_action": False,
                        "has_waybill": True,
                        "waybill_suffix": str(job["io_id"])[-4:],
                        "waybill_fingerprint": hashlib.sha256(
                            str(job["io_id"]).encode("utf-8")
                        ).hexdigest(),
                    }
                    for job in jobs
                ]
                if len(self.calls) == 2:
                    results[1]["has_print_action"] = True
                return results

        class Browser:
            def print_express_batch(self, _identities, **kwargs):
                kwargs["before_click"]()
                kwargs["mark_running"]()
                clicked.append(True)

        planner = Planner()
        engine._browser_for = lambda _settings: Browser()
        with mock.patch.object(app, "print_service_online", return_value=True):
            engine._process_print_batch(planner, jobs)

        self.assertEqual([len(call) for call in planner.calls], [2, 2])
        self.assertEqual(clicked, [])
        self.assertEqual(processed, [jobs[1]])
        self.assertEqual(
            [
                engine.store.get_job(job["o_id"], job["io_id"])["status"]
                for job in jobs
            ],
            ["PENDING", "PENDING"],
        )

    def test_real_batch_waybill_rechecks_live_route_before_mark_running(self):
        jobs = [_waybill_job(0), _waybill_job(1)]
        engine = _base_engine(jobs)
        processed = []
        clicked = []
        engine._process_job = lambda _planner, job: processed.append(job)

        class Planner:
            def __init__(self):
                self.calls = []

            def inspect_batch(self, credentials):
                self.calls.append(list(credentials))
                results = [
                    {
                        "found": True,
                        "o_id": job["o_id"],
                        "io_id": job["io_id"],
                        "status": "WaitConfirm",
                        "has_ship_action": False,
                        "has_print_action": False,
                        "has_waybill": False,
                        "carrier_id": app.TARGET_CARRIER_ID,
                        "carrier_name": app.TARGET_CARRIER_NAME,
                    }
                    for job in jobs
                ]
                if len(self.calls) == 2:
                    results[1]["route_changed"] = True
                return results

        def validate_route(readback, _step, _plan):
            if readback.get("route_changed"):
                raise app.PermanentJobError("实时仓库/重量/店铺路由已变化")

        class Browser:
            def reset_carrier_batch(self, _identities, *_args, **kwargs):
                kwargs["before_confirm"]()
                kwargs["mark_running"]()
                clicked.append(True)

        planner = Planner()
        engine._preflight = validate_route
        engine._browser_for = lambda _settings: Browser()
        engine._process_waybill_batch(planner, jobs)

        self.assertEqual([len(call) for call in planner.calls], [2, 2])
        self.assertEqual(clicked, [])
        self.assertEqual(processed, [jobs[1]])
        self.assertEqual(
            [
                engine.store.get_job(job["o_id"], job["io_id"])["status"]
                for job in jobs
            ],
            ["PENDING", "PENDING"],
        )

    def test_second_waybill_readback_failure_is_bound_to_second_order(self):
        jobs = [_waybill_job(0), _waybill_job(1)]
        engine = _base_engine(jobs)

        class Browser:
            def reset_carrier_batch(self, _identities, *_args, **kwargs):
                kwargs["before_confirm"]()
                kwargs["mark_running"]()
                kwargs["final_guard"]()

        engine._browser_for = lambda _settings: Browser()

        def poll(_planner, o_id, *_args, **_kwargs):
            if str(o_id) == jobs[0]["o_id"]:
                return {
                    "waybill_suffix": "0466",
                    "waybill_fingerprint": "a" * 64,
                }
            raise app.SafetyStop("第二单取号回读未通过")

        engine._poll_readback = poll

        with self.assertRaisesRegex(app.SafetyStop, "第二单") as raised:
            engine._process_waybill_batch(object(), jobs)

        active = engine._job_for_exception(jobs[0], raised.exception)
        self.assertEqual(
            (active["o_id"], active["io_id"]),
            (jobs[1]["o_id"], jobs[1]["io_id"]),
        )
        self.assertEqual(engine.store.next_job_calls, 0)
        self.assertEqual(
            engine.store.get_job(jobs[0]["o_id"], jobs[0]["io_id"])["status"],
            "PENDING",
        )
        self.assertEqual(active["status"], "RUNNING")

    def test_second_print_readback_failure_is_bound_to_second_order(self):
        jobs = [_print_job(0), _print_job(1)]
        engine = _base_engine(jobs)

        class Browser:
            def print_express_batch(self, _identities, **kwargs):
                kwargs["before_click"]()
                kwargs["mark_running"]()
                kwargs["final_guard"]()

        engine._browser_for = lambda _settings: Browser()

        def poll(_planner, o_id, *_args, **_kwargs):
            if str(o_id) == jobs[0]["o_id"]:
                return {
                    "has_print_action": True,
                    "waybill_suffix": str(jobs[0]["io_id"])[-4:],
                    "waybill_fingerprint": hashlib.sha256(
                        str(jobs[0]["io_id"]).encode("utf-8")
                    ).hexdigest(),
                }
            raise app.SafetyStop("第二单打印回读未通过")

        engine._poll_readback = poll
        engine._complete_printed_job = (
            lambda _planner, job, _readback, next_index: engine.store.update_job(
                job["o_id"],
                job["io_id"],
                step_index=next_index,
                status="COMPLETED",
            )
        )

        with mock.patch.object(app, "print_service_online", return_value=True):
            with self.assertRaisesRegex(app.SafetyStop, "第二单") as raised:
                engine._process_print_batch(object(), jobs)

        active = engine._job_for_exception(jobs[0], raised.exception)
        self.assertEqual(
            (active["o_id"], active["io_id"]),
            (jobs[1]["o_id"], jobs[1]["io_id"]),
        )
        self.assertEqual(engine.store.next_job_calls, 0)
        self.assertEqual(active["status"], "RUNNING")

    def test_second_preparation_inspect_failure_is_not_transferred_to_batch_head(self):
        jobs = [_waybill_job(0), _waybill_job(1)]
        engine = _base_engine(jobs)

        def inspect(_planner, plan):
            if str(plan["o_id"]) == jobs[1]["o_id"]:
                raise app.BackendSchemaError("第二单后台字段错误")
            return {"o_id": str(plan["o_id"]), "io_id": str(plan["io_id"])}

        engine._inspect = inspect

        with self.assertRaisesRegex(app.BackendSchemaError, "第二单") as raised:
            engine._process_waybill_batch(object(), jobs)

        active = engine._job_for_exception(jobs[0], raised.exception)
        self.assertEqual(
            (active["o_id"], active["io_id"]),
            (jobs[1]["o_id"], jobs[1]["io_id"]),
        )

    def test_worker_retires_exact_second_running_order_from_batch_exception(self):
        jobs = [_waybill_job(0), _waybill_job(1)]
        engine = _base_engine(jobs)
        engine.workstation_id = "ws-batch-exception-test"
        engine.stop_event = threading.Event()
        engine.shutdown_event = threading.Event()
        engine.run_event = threading.Event()
        engine.run_event.set()
        engine.set_status = lambda _message: None
        engine._wait_until_running = lambda: True
        engine._wait = lambda _seconds: True
        engine._disconnect_browser = lambda: None
        retired = []

        def fail_on_second(_planner, batch):
            first = batch[0]
            second = batch[1]
            engine.store.jobs[(first["o_id"], first["io_id"])]["status"] = (
                "COMPLETED"
            )
            engine.store.jobs[(second["o_id"], second["io_id"])]["status"] = (
                "RUNNING"
            )
            with engine._job_exception_scope(second):
                raise app.SafetyStop("第二单取号回读未通过")

        def retire(_planner, job, reason):
            retired.append((job["o_id"], job["io_id"], reason))
            engine.stop_event.set()
            return True

        engine._process_waybill_batch = fail_on_second
        engine._process_job = lambda _planner, _job: (_ for _ in ()).throw(
            app.SafetyStop("第二单取号回读未通过")
        )
        engine._retire_uncertain_running = retire

        with mock.patch.object(app, "PlannerClient", return_value=object()):
            engine._worker()

        self.assertEqual(
            retired,
            [
                (
                    jobs[1]["o_id"],
                    jobs[1]["io_id"],
                    "第二单取号回读未通过",
                )
            ],
        )
        self.assertEqual(engine.store.next_job_calls, 0)

    def test_committed_print_batch_never_falls_back_to_repeat_printing(self):
        jobs = [_print_job(0), _print_job(1)]
        engine = _base_engine(jobs)
        processed = []
        engine._process_job = lambda _planner, job: processed.append(job)

        class Browser:
            def print_express_batch(self, _identities, **kwargs):
                kwargs["before_click"]()
                kwargs["mark_running"]()
                raise RuntimeError("点击后页面响应丢失")

        engine._browser_for = lambda _settings: Browser()

        with mock.patch.object(app, "print_service_online", return_value=True):
            with self.assertRaisesRegex(
                app.CommittedBatchUncertain, "禁止重复打印"
            ) as raised:
                engine._process_print_batch(object(), jobs)

        self.assertFalse(hasattr(raised.exception, "_jst_job_identity"))
        self.assertEqual(processed, [])
        self.assertEqual(
            [
                engine.store.get_job(job["o_id"], job["io_id"])["status"]
                for job in jobs
            ],
            ["RUNNING", "RUNNING"],
        )

    def test_generic_post_commit_waybill_error_is_whole_batch_uncertain(self):
        jobs = [_waybill_job(0), _waybill_job(1)]
        engine = _base_engine(jobs)
        processed = []
        engine._process_job = lambda _planner, job: processed.append(job)

        class Browser:
            def reset_carrier_batch(self, _identities, *_args, **kwargs):
                kwargs["before_confirm"]()
                kwargs["mark_running"]()
                raise RuntimeError("确认后页面响应丢失")

        engine._browser_for = lambda _settings: Browser()

        with self.assertRaisesRegex(
            app.CommittedBatchUncertain, "禁止重复取号"
        ) as raised:
            engine._process_waybill_batch(object(), jobs)

        self.assertFalse(hasattr(raised.exception, "_jst_job_identity"))
        self.assertEqual(processed, [])
        self.assertEqual(
            [
                engine.store.get_job(job["o_id"], job["io_id"])["status"]
                for job in jobs
            ],
            ["RUNNING", "RUNNING"],
        )

    def test_worker_keeps_whole_committed_batch_for_read_only_recovery(self):
        jobs = [_print_job(0), _print_job(1)]
        engine = _base_engine(jobs)
        engine.workstation_id = "ws-committed-batch-test"
        engine.stop_event = threading.Event()
        engine.shutdown_event = threading.Event()
        engine.run_event = threading.Event()
        engine.run_event.set()
        engine.set_status = lambda _message: None
        engine._wait_until_running = lambda: True
        engine._disconnect_browser = lambda: None
        events = []
        recovered = []

        def event(_severity, event_type, message, **_kwargs):
            events.append((event_type, message))

        engine._event = event

        def uncertain_after_commit(_planner, batch):
            for job in batch:
                engine.store.jobs[(job["o_id"], job["io_id"])]["status"] = (
                    "RUNNING"
                )
            raise app.CommittedBatchUncertain(batch, "批量打印响应丢失")

        engine._process_print_batch = uncertain_after_commit
        engine._retire_uncertain_running = lambda *_args: self.fail(
            "committed batch must not arbitrarily retire its head"
        )

        def recover(_planner, job):
            recovered.append((job["o_id"], job["io_id"], job["status"]))
            engine.stop_event.set()

        engine._process_job = recover

        with mock.patch.object(app, "PlannerClient", return_value=object()):
            engine._worker()

        self.assertTrue(
            any(event_type == "BATCH_COMMIT_READBACK_RECOVERY" for event_type, _ in events)
        )
        self.assertEqual(
            recovered,
            [(jobs[0]["o_id"], jobs[0]["io_id"], "RUNNING")],
        )
        self.assertEqual(
            [
                engine.store.get_job(job["o_id"], job["io_id"])["status"]
                for job in jobs
            ],
            ["RUNNING", "RUNNING"],
        )

    def test_worker_running_dom_loss_continues_with_read_only_recovery(self):
        job = _print_job(0)
        engine = _base_engine([job])
        engine.workstation_id = "ws-running-dom-recovery"
        engine.stop_event = threading.Event()
        engine.shutdown_event = threading.Event()
        engine.run_event = threading.Event()
        engine.run_event.set()
        engine._wait_until_running = lambda: True
        statuses = []
        events = []
        waits = []
        disconnects = []
        engine.set_status = statuses.append
        engine._event = lambda _level, event_type, message, **_kwargs: events.append(
            (event_type, message)
        )
        engine._wait = lambda seconds: waits.append(seconds) or True
        engine._disconnect_browser = lambda: disconnects.append(True)
        calls = []

        def process(_planner, current):
            calls.append(str(current["status"]))
            if len(calls) == 1:
                engine.store.update_job(
                    current["o_id"],
                    current["io_id"],
                    step_index=current["step_index"],
                    status="RUNNING",
                )
                raise app.OrderRowNotReady("动作后 iframe 刷新")
            self.assertEqual(current["status"], "RUNNING")
            engine.stop_event.set()

        engine._process_job = process

        with mock.patch.object(app, "PlannerClient", return_value=object()):
            engine._worker()

        self.assertEqual(calls, ["PENDING", "RUNNING"])
        self.assertTrue(engine.run_event.is_set())
        self.assertIn(2.0, waits)
        # Keep the trusted CDP socket alive while RUNNING is reconciled from
        # backend readback. Closing it here would force another Chrome consent
        # dialog even though no browser action will be repeated.
        self.assertEqual(disconnects, [])
        self.assertTrue(
            any(
                kind
                in {
                    "RUNNING_ACTION_READBACK_RECOVERY",
                    "RUNNING_DOM_READBACK_RECOVERY",
                }
                for kind, _ in events
            )
        )

    def test_worker_post_commit_generic_error_gets_one_read_only_round(self):
        for failure in (
            RuntimeError("Target page, context or browser has been closed"),
            app.SafetyStop("打印按钮点击超时"),
        ):
            with self.subTest(failure=type(failure).__name__):
                job = _print_job(0)
                engine = _base_engine([job])
                engine.workstation_id = "ws-post-commit-readback"
                engine.stop_event = threading.Event()
                engine.shutdown_event = threading.Event()
                engine.run_event = threading.Event()
                engine.run_event.set()
                engine._wait_until_running = lambda: True
                engine.set_status = lambda _message: None
                engine._wait = lambda _seconds: True
                engine._disconnect_browser = lambda: None
                events = []
                engine._event = (
                    lambda _level, event_type, message, **_kwargs: events.append(
                        (event_type, message)
                    )
                )
                calls = []

                def process(_planner, current):
                    calls.append(str(current["status"]))
                    if len(calls) == 1:
                        engine.store.update_job(
                            current["o_id"],
                            current["io_id"],
                            step_index=current["step_index"],
                            status="RUNNING",
                        )
                        raise failure
                    self.assertEqual(current["status"], "RUNNING")
                    engine.stop_event.set()

                engine._process_job = process
                engine._retire_uncertain_running = lambda *_args: self.fail(
                    "a newly committed action must get read-only recovery first"
                )

                with mock.patch.object(app, "PlannerClient", return_value=object()):
                    engine._worker()

                self.assertEqual(calls, ["PENDING", "RUNNING"])
                self.assertTrue(engine.run_event.is_set())
                self.assertTrue(
                    any(
                        kind == "RUNNING_ACTION_READBACK_RECOVERY"
                        for kind, _ in events
                    )
                )

    def test_real_running_recovery_never_opens_browser_or_reclicks(self):
        print_job = _print_job(0)
        print_job["status"] = "RUNNING"
        print_engine = _base_engine([print_job])
        print_engine._browser_for = lambda *_args: self.fail(
            "RUNNING print recovery must not open the browser"
        )
        print_engine._inspect = lambda _planner, plan: {
            "found": True,
            "o_id": str(plan["o_id"]),
            "io_id": str(plan["io_id"]),
            "status": "WaitConfirm",
            "has_ship_action": False,
            "has_print_action": True,
        }
        print_completions = []
        print_engine._complete_claim = (
            lambda _planner, _plan, reason: print_completions.append(reason)
        )

        print_engine._process_job(object(), print_job)

        self.assertEqual(print_completions, ["PRINTED"])
        self.assertEqual(
            print_engine.store.get_job(print_job["o_id"], print_job["io_id"])[
                "status"
            ],
            "SKIPPED_ALREADY_PRINTED",
        )

        waybill_job = _waybill_job(0)
        waybill_job["status"] = "RUNNING"
        waybill_engine = _base_engine([waybill_job])
        waybill_engine._browser_for = lambda *_args: self.fail(
            "RUNNING waybill recovery must not open the browser"
        )
        waybill_engine._inspect = lambda _planner, plan: {
            "found": True,
            "o_id": str(plan["o_id"]),
            "io_id": str(plan["io_id"]),
            "status": "WaitConfirm",
            "has_ship_action": False,
            "has_print_action": False,
            "has_waybill": True,
            "carrier_id": app.SOURCE_CARRIER_ID,
            "carrier_name": app.SOURCE_CARRIER_NAME,
            "privacy_required": False,
        }

        waybill_engine._process_job(object(), waybill_job)

        recovered = waybill_engine.store.get_job(
            waybill_job["o_id"], waybill_job["io_id"]
        )
        self.assertEqual((recovered["status"], recovered["step_index"]), ("PENDING", 1))

    def test_changed_second_batch_candidate_rebuilds_that_exact_job(self):
        for batch_kind in ("waybill", "print"):
            with self.subTest(batch_kind=batch_kind):
                jobs = (
                    [_waybill_job(0), _waybill_job(1)]
                    if batch_kind == "waybill"
                    else [_print_job(0), _print_job(1)]
                )
                engine = _base_engine(jobs)
                processed = []
                engine._process_job = lambda _planner, job: processed.append(job)

                class Browser:
                    def reset_carrier_batch(self, *_args, **_kwargs):
                        raise app.BatchCandidateChanged(
                            {
                                "o_id": jobs[1]["o_id"],
                                "io_id": jobs[1]["io_id"],
                            },
                            "第二单点击前身份变化",
                        )

                    def print_express_batch(self, *_args, **_kwargs):
                        raise app.BatchCandidateChanged(
                            {
                                "o_id": jobs[1]["o_id"],
                                "io_id": jobs[1]["io_id"],
                            },
                            "第二单点击前身份变化",
                        )

                engine._browser_for = lambda _settings: Browser()

                with mock.patch.object(app, "print_service_online", return_value=True):
                    if batch_kind == "waybill":
                        engine._process_waybill_batch(object(), jobs)
                    else:
                        engine._process_print_batch(object(), jobs)

                self.assertEqual(processed, [jobs[1]])
                self.assertEqual(
                    [
                        engine.store.get_job(job["o_id"], job["io_id"])["status"]
                        for job in jobs
                    ],
                    ["PENDING", "PENDING"],
                )


if __name__ == "__main__":
    unittest.main()
