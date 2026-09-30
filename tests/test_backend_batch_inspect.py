import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jst_print_shadow_plan as planner


SERVER_DIR = Path(__file__).resolve().parents[1] / "server_batch_v056"
sys.path.insert(0, str(SERVER_DIR))
try:
    import jst_print_api_server as api
    from jst_lease_store import LeaseConflict, LeaseStore
finally:
    sys.path.remove(str(SERVER_DIR))


def order_row(
    o_id: str,
    io_id: str,
    *,
    created: str = "2026-08-26 10:00:00",
    printed: bool = False,
    remark: str = "",
    carrier_id: str = planner.TARGET_CARRIER_ID,
    carrier_name: str = planner.TARGET_CARRIER_NAME,
    waybill: str = "",
) -> dict:
    return {
        "o_id": o_id,
        "io_id": io_id,
        "created": created,
        "io_date": created,
        "shop_name": "test-shop",
        "wms_co_id": planner.WAREHOUSE_ID,
        "status": "WaitConfirm",
        "logistics_company": carrier_name,
        "lc_id": carrier_id,
        "weight": 1,
        "l_id": waybill,
        "is_print_express": printed,
        "remark": remark,
        "labels": "",
        "items": [
            {
                "ioi_id": f"line-{io_id}",
                "i_id": f"product-{io_id}",
                "sku_id": f"sku-{io_id}",
                "name": "test-item",
                "qty": 1,
                "unit": "piece",
            }
        ],
    }


class MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class BackendBatchInspectTests(unittest.TestCase):
    def tearDown(self):
        with api._plan_cache_condition:
            api._plan_cache_result = None
            api._plan_cache_time = 0.0
            api._plan_cache_refreshing = False
            api._plan_cache_flight = None
            api._plan_cache_condition.notify_all()

    def test_duplicate_exact_pair_requires_identical_safety_fingerprint(self):
        first = order_row("10001", "20001")
        identical = order_row("10001", "20001")

        result = planner._deduplicate_orders([first, identical])

        self.assertEqual(result, [first])

    def test_duplicate_exact_pair_with_conflicting_route_fails_closed(self):
        first = order_row(
            "10001",
            "20001",
            carrier_id=planner.SOURCE_CARRIER_ID,
            carrier_name=planner.SOURCE_CARRIER_NAME,
        )
        conflicting = order_row(
            "10001",
            "20001",
            carrier_id=planner.TARGET_CARRIER_ID,
            carrier_name=planner.TARGET_CARRIER_NAME,
        )
        conflicting["weight"] = 4

        with self.assertRaisesRegex(RuntimeError, "安全字段互相冲突"):
            planner._deduplicate_orders([first, conflicting])

    def test_batch_planner_uses_one_order_read_and_one_bulk_action_pass(self):
        requested = [("10002", "20002"), ("10001", "20001"), ("10003", "20003")]
        rows = [order_row("10001", "20001"), order_row("10002", "20002")]
        args = SimpleNamespace(
            bridge_root="/fake-bridge",
            action_history_days=7,
            inspect_pairs=requested,
        )
        action_proofs = {
            "10001": ([{"name": "view", "created": "2026-08-26 10:01:00"}], True),
            "10002": ([], True),
        }

        with mock.patch.object(planner, "_load_jst_client", return_value=object()), mock.patch.object(
            planner, "_page_all", return_value=rows
        ) as page_all, mock.patch.object(
            planner, "query_actions_bulk", return_value=action_proofs
        ) as actions_bulk:
            result = planner.run_inspect_batch(args)

        self.assertEqual(page_all.call_count, 1)
        self.assertEqual(actions_bulk.call_count, 1)
        queried_rows = actions_bulk.call_args.args[1]
        self.assertEqual(
            {(row["o_id"], row["io_id"]) for row in queried_rows},
            {("10001", "20001"), ("10002", "20002")},
        )
        self.assertEqual(result["mode"], "ORDER_BATCH_READBACK_V5")
        self.assertEqual(result["schema_version"], 5)
        self.assertEqual(
            [(item["o_id"], item["io_id"]) for item in result["results"]],
            requested,
        )
        self.assertEqual([item["found"] for item in result["results"]], [True, True, False])
        self.assertTrue(
            all(
                item["warehouse_id"] == planner.WAREHOUSE_ID
                for item in result["results"]
                if item["found"]
            )
        )
        api.validate_inspect_batch_result(result, requested)
        wrong_warehouse = dict(result)
        wrong_warehouse["results"] = [dict(item) for item in result["results"]]
        wrong_warehouse["results"][0]["warehouse_id"] = "other-warehouse"
        with self.assertRaisesRegex(RuntimeError, "warehouse"):
            api.validate_inspect_batch_result(wrong_warehouse, requested)

    def test_static_blockers_never_enter_action_history_query(self):
        # Keep the candidate inside the rolling action-history window; a fixed
        # calendar date eventually makes even the ready order a static blocker.
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ready = order_row("11001", "21001", created=created)
        printed = order_row("11002", "21002", created=created, printed=True)
        held = order_row("11003", "21003", created=created, remark="暂停发货")
        old_waybill = order_row(
            "11004",
            "21004",
            created=created,
            carrier_id=planner.SOURCE_CARRIER_ID,
            carrier_name=planner.SOURCE_CARRIER_NAME,
            waybill="1234567890",
        )
        rows = [ready, printed, held, old_waybill]
        action_o_ids: list[list[int]] = []

        class FakeClient:
            def query_order_action(self, **kwargs):
                action_o_ids.append(list(kwargs["o_ids"]))
                return {"datas": [], "has_next": False}

        fake_client = FakeClient()
        app_module = types.ModuleType("app")
        clients_module = types.ModuleType("app.clients")
        jst_client_module = types.ModuleType("app.clients.jst_client")
        jst_client_module.JSTClient = lambda: fake_client
        args = SimpleNamespace(
            bridge_root="/fake-bridge",
            lookback_hours=168,
            candidate_cache="",
            action_history_days=7,
            max_candidates=10,
        )

        with mock.patch.dict(
            sys.modules,
            {
                "app": app_module,
                "app.clients": clients_module,
                "app.clients.jst_client": jst_client_module,
            },
        ), mock.patch.object(planner.Path, "exists", return_value=True), mock.patch.object(
            planner, "discover_orders", return_value=rows
        ):
            result = planner.run_live_readonly(args)

        self.assertTrue(action_o_ids)
        self.assertTrue(all(o_ids == [11001] for o_ids in action_o_ids))
        self.assertEqual(result["counts"]["ready"], 1)
        self.assertEqual(result["counts"]["blocked"], 3)
        blocked = {item["o_id"]: item["blockers"] for item in result["blocked_preview"]}
        self.assertIn("聚水潭已标记打印，需人工核查，禁止重复打印", blocked["11002"])
        self.assertTrue(any("停发标记" in reason for reason in blocked["11003"]))
        self.assertIn("已有运单号，重设快递前需人工确认旧面单处置", blocked["11004"])

    def test_future_created_order_never_claims_complete_action_history(self):
        now = datetime(2026, 8, 26, 16, 0, 0)
        too_far_future = now + timedelta(
            minutes=planner.MAX_CREATED_CLOCK_SKEW_MINUTES + 1
        )

        class NoActionQueryClient:
            def query_order_action(self, **_kwargs):
                raise AssertionError("future-created order must not query actions")

        actions, complete = planner.query_actions(
            NoActionQueryClient(), "11501", too_far_future, now, 7
        )
        self.assertEqual(actions, [])
        self.assertFalse(complete)

        bulk = planner.query_actions_bulk(
            NoActionQueryClient(),
            [
                order_row(
                    "11502",
                    "21502",
                    created=too_far_future.strftime("%Y-%m-%d %H:%M:%S"),
                )
            ],
            now,
            7,
        )
        self.assertEqual(bulk, {"11502": ([], False)})

        class EmptyActionClient:
            def __init__(self):
                self.calls = 0

            def query_order_action(self, **_kwargs):
                self.calls += 1
                return {"datas": [], "has_next": False}

        allowed_client = EmptyActionClient()
        allowed_actions, allowed_complete = planner.query_actions(
            allowed_client,
            "11503",
            now + timedelta(minutes=planner.MAX_CREATED_CLOCK_SKEW_MINUTES),
            now,
            7,
        )
        self.assertEqual(allowed_actions, [])
        self.assertTrue(allowed_complete)
        self.assertEqual(allowed_client.calls, 1)

    def test_runtime_timing_uses_larger_plan_or_inspect_timeout(self):
        api.validate_runtime_timing(30, 60, 91)
        api.validate_runtime_timing(105, 35, 136)
        with self.assertRaisesRegex(RuntimeError, "maximum planner/inspect timeout"):
            api.validate_runtime_timing(30, 60, 90)
        with self.assertRaisesRegex(RuntimeError, "maximum planner/inspect timeout"):
            api.validate_runtime_timing(105, 35, 135)

    def test_renew_many_is_all_or_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300, clock=clock)
            first = store.claim("12001", "22001", "ws-batch-0001")
            second = store.claim("12002", "22002", "ws-batch-0001")
            assert first is not None and second is not None
            clock.value += 10

            with self.assertRaises(LeaseConflict):
                store.renew_many(
                    [
                        (first.o_id, first.io_id, first.workstation_id, first.claim_token),
                        (second.o_id, second.io_id, second.workstation_id, "wrong-token"),
                    ]
                )
            unchanged = store.require_active(
                first.o_id, first.io_id, first.workstation_id, first.claim_token
            )
            self.assertEqual(unchanged.expires_epoch, first.expires_epoch)

            renewed = store.renew_many(
                [
                    (first.o_id, first.io_id, first.workstation_id, first.claim_token),
                    (second.o_id, second.io_id, second.workstation_id, second.claim_token),
                ]
            )
            self.assertEqual(len(renewed), 2)
            self.assertTrue(all(lease.expires_epoch == clock.value + 300 for lease in renewed))

    def test_renew_many_cannot_revive_expired_batch_above_workstation_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=30, clock=clock)
            expired_batch = [
                store.claim(str(14000 + index), str(24000 + index), "ws-batch-0004")
                for index in range(10)
            ]
            self.assertTrue(all(lease is not None for lease in expired_batch))
            clock.value += 31
            current_batch = [
                store.claim(str(15000 + index), str(25000 + index), "ws-batch-0004")
                for index in range(10)
            ]
            self.assertTrue(all(lease is not None for lease in current_batch))

            first_expired = expired_batch[0]
            assert first_expired is not None
            with self.assertRaisesRegex(LeaseConflict, "workstation_batch_exceeds_limit"):
                store.renew(
                    first_expired.o_id,
                    first_expired.io_id,
                    first_expired.workstation_id,
                    first_expired.claim_token,
                )

            with self.assertRaisesRegex(LeaseConflict, "workstation_batch_exceeds_limit"):
                store.renew_many(
                    [
                        (lease.o_id, lease.io_id, lease.workstation_id, lease.claim_token)
                        for lease in expired_batch
                        if lease is not None
                    ]
                )

            active = store.active_for_workstation("ws-batch-0004")
            self.assertEqual(len(active), 10)
            self.assertEqual(
                {(lease.o_id, lease.io_id) for lease in active},
                {
                    (lease.o_id, lease.io_id)
                    for lease in current_batch
                    if lease is not None
                },
            )

    def test_server_batch_endpoint_starts_planner_once_and_returns_claimed_children(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
            first = store.claim("13001", "23001", api._server_workstation_id())
            second = store.claim("13002", "23002", api._server_workstation_id())
            assert first is not None and second is not None
            generated_at = planner.utc_now_text()
            raw_results = [
                planner._found_inspect_result(
                    order_row("13001", "23001"), [], True, generated_at=generated_at
                ),
                planner._found_inspect_result(
                    order_row("13002", "23002"), [], True, generated_at=generated_at
                ),
            ]
            raw = {
                "mode": "ORDER_BATCH_READBACK_V5",
                "schema_version": 5,
                "generated_at": generated_at,
                "results": raw_results,
            }
            body = {
                "workstation_id": "ws-batch-0002",
                "orders": [
                    {"o_id": first.o_id, "io_id": first.io_id, "claim_token": first.claim_token},
                    {"o_id": second.o_id, "io_id": second.io_id, "claim_token": second.claim_token},
                ],
            }

            with mock.patch.object(api, "run_planner", return_value=raw) as run_planner:
                result = api.process_request(
                    "/jst-print-api/v1/inspect-batch", body, store=store
                )

            self.assertEqual(run_planner.call_count, 1)
            self.assertEqual(run_planner.call_args.kwargs["timeout"], api.INSPECT_TIMEOUT_SECONDS)
            self.assertEqual(result["mode"], "ORDER_BATCH_READBACK_CLAIMED_V1")
            self.assertEqual(result["api_schema_version"], 5)
            self.assertEqual(result["planner_schema_version"], 5)
            self.assertEqual(len(result["results"]), 2)
            self.assertTrue(all(item["mode"] == api.INSPECT_MODE for item in result["results"]))
            self.assertTrue(all(item["lease_required"] is True for item in result["results"]))

    def test_inspect_schema_rejects_unknown_fields_and_full_waybill(self):
        generated_at = planner.utc_now_text()
        found = planner._found_inspect_result(
            order_row("16001", "26001", waybill="1234567890"),
            [],
            True,
            generated_at=generated_at,
        )
        api.validate_inspect_result(found, "16001", "26001")

        leaked_found = dict(found)
        leaked_found["buyer_name"] = "secret"
        with self.assertRaisesRegex(RuntimeError, "found inspect schema"):
            api.validate_inspect_result(leaked_found, "16001", "26001")

        full_waybill = dict(found)
        full_waybill["waybill_suffix"] = "1234567890"
        with self.assertRaisesRegex(RuntimeError, "waybill suffix"):
            api.validate_inspect_result(full_waybill, "16001", "26001")

        invalid_fingerprint = dict(found, waybill_fingerprint="0" * 63)
        with self.assertRaisesRegex(RuntimeError, "waybill fingerprint"):
            api.validate_inspect_result(invalid_fingerprint, "16001", "26001")

        not_found = planner._not_found_inspect_result(
            "16002", "26002", generated_at=generated_at
        )
        leaked_not_found = dict(not_found)
        leaked_not_found["receiver_address"] = "secret"
        with self.assertRaisesRegex(RuntimeError, "not-found inspect schema"):
            api.validate_inspect_result(leaked_not_found, "16002", "26002")

        leaked_batch_child = dict(found)
        leaked_batch_child["receiver_phone"] = "secret"
        with self.assertRaisesRegex(RuntimeError, "found inspect schema"):
            api.validate_inspect_batch_result(
                {
                    "mode": api.RAW_INSPECT_BATCH_MODE,
                    "schema_version": api.PLANNER_SCHEMA_VERSION,
                    "generated_at": generated_at,
                    "results": [leaked_batch_child],
                },
                [("16001", "26001")],
            )

    def test_batch_endpoint_rejects_non_exact_or_duplicate_requests_before_planner(self):
        token = "a" * 32
        invalid_bodies = [
            {
                "workstation_id": "ws-batch-0003",
                "orders": [{"o_id": "1", "io_id": "2", "claim_token": token}],
                "extra": True,
            },
            {
                "workstation_id": "ws-batch-0003",
                "orders": [
                    {"o_id": "1", "io_id": "2", "claim_token": token},
                    {"o_id": "1", "io_id": "2", "claim_token": token},
                ],
            },
        ]
        with mock.patch.object(api, "run_planner") as run_planner:
            for body in invalid_bodies:
                with self.subTest(body=body), self.assertRaises(api.APIError) as raised:
                    api.process_request(
                        "/jst-print-api/v1/inspect-batch", body, store=object()
                    )
                self.assertEqual(raised.exception.status, 400)
        run_planner.assert_not_called()

    def test_stale_plan_returns_immediately_and_starts_only_one_background_refresh(self):
        old = {"marker": "old"}
        new = {"marker": "new"}
        targets = []

        class DeferredThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self):
                targets.append(self.target)

        with api._plan_cache_condition:
            api._plan_cache_result = old
            api._plan_cache_time = 80.0
            api._plan_cache_refreshing = False
        with mock.patch.object(api.time, "monotonic", return_value=100.0), mock.patch.object(
            api.threading, "Thread", DeferredThread
        ), mock.patch.object(api, "_read_plan_pool", return_value=new) as refresh:
            self.assertEqual(api.get_plan_pool(), old)
            self.assertEqual(api.get_plan_pool(), old)
            self.assertEqual(len(targets), 1)
            self.assertEqual(refresh.call_count, 0)
            targets[0]()
            self.assertEqual(refresh.call_count, 1)
            self.assertEqual(api.get_plan_pool(), new)

    def test_plan_older_than_hard_limit_refreshes_synchronously(self):
        old = {"marker": "old"}
        new = {"marker": "new"}
        with api._plan_cache_condition:
            api._plan_cache_result = old
            api._plan_cache_time = 1.0
            api._plan_cache_refreshing = False
        with mock.patch.object(api.time, "monotonic", return_value=100.0), mock.patch.object(
            api, "_read_plan_pool", return_value=new
        ) as refresh:
            self.assertEqual(api.get_plan_pool(), new)
        self.assertEqual(refresh.call_count, 1)

    def test_failed_background_plan_refresh_keeps_old_snapshot_and_retries(self):
        old = {"marker": "old"}
        new = {"marker": "new"}
        targets = []

        class DeferredThread:
            def __init__(self, *, target, name, daemon):
                self.target = target

            def start(self):
                targets.append(self.target)

        with api._plan_cache_condition:
            api._plan_cache_result = old
            api._plan_cache_time = 80.0
            api._plan_cache_refreshing = False
        with mock.patch.object(api.time, "monotonic", return_value=100.0), mock.patch.object(
            api.threading, "Thread", DeferredThread
        ), mock.patch.object(
            api, "_read_plan_pool", side_effect=[RuntimeError("temporary"), new]
        ):
            self.assertEqual(api.get_plan_pool(), old)
            targets.pop(0)()
            self.assertEqual(api.get_plan_pool(), old)
            targets.pop(0)()
            self.assertEqual(api.get_plan_pool(), new)

    def test_concurrent_cold_plan_requests_share_one_synchronous_refresh(self):
        started = threading.Event()
        release = threading.Event()
        results = []
        errors = []
        new = {"marker": "new"}

        def slow_refresh():
            started.set()
            if not release.wait(2):
                raise RuntimeError("test refresh release timed out")
            return new

        def request_pool():
            try:
                results.append(api.get_plan_pool())
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(api, "_read_plan_pool", side_effect=slow_refresh) as refresh:
            first = threading.Thread(target=request_pool)
            second = threading.Thread(target=request_pool)
            first.start()
            self.assertTrue(started.wait(1))
            second.start()
            release.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [new, new])
        self.assertEqual(refresh.call_count, 1)

    def test_concurrent_cold_plan_failure_is_shared_without_repeat_refresh(self):
        started = threading.Event()
        release = threading.Event()
        both_waiting = threading.Event()
        wait_count = 0
        wait_count_lock = threading.Lock()
        errors = []

        def slow_failure():
            started.set()
            if not release.wait(2):
                raise RuntimeError("test refresh release timed out")
            raise RuntimeError("planner unavailable")

        def request_pool():
            try:
                api.get_plan_pool()
            except Exception as exc:
                errors.append(exc)

        original_wait = api._plan_cache_condition.wait

        def observed_wait(timeout=None):
            nonlocal wait_count
            with wait_count_lock:
                wait_count += 1
                if wait_count == 2:
                    both_waiting.set()
            return original_wait(timeout)

        with mock.patch.object(api, "_read_plan_pool", side_effect=slow_failure) as refresh, mock.patch.object(
            api._plan_cache_condition, "wait", side_effect=observed_wait
        ):
            threads = [threading.Thread(target=request_pool) for _ in range(3)]
            threads[0].start()
            self.assertTrue(started.wait(1))
            threads[1].start()
            threads[2].start()
            self.assertTrue(both_waiting.wait(1))
            release.set()
            for thread in threads:
                thread.join(2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(errors), 3)
        self.assertEqual(refresh.call_count, 1)

        new = {"marker": "retry-success"}
        with mock.patch.object(api, "_read_plan_pool", return_value=new) as retry:
            self.assertEqual(api.get_plan_pool(), new)
        self.assertEqual(retry.call_count, 1)

    def test_missing_candidate_cache_bootstraps_but_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 8, 26, 16, 0, 0)
            begin = datetime(2026, 8, 19, 16, 0, 0)
            cache_path = Path(directory) / "candidate-orders-v1.json"
            row = order_row("17001", "27001")

            with mock.patch.object(planner, "_page_all", return_value=[row]) as page_all:
                discovered = planner.discover_orders(
                    object(),
                    begin=begin,
                    now=now,
                    candidate_cache=str(cache_path),
                )
            self.assertEqual(discovered, [row])
            self.assertEqual(page_all.call_count, 2)
            exact_kwargs = page_all.call_args_list[1].kwargs
            self.assertEqual(exact_kwargs["o_ids"], [17001])
            self.assertNotIn("modified_begin", exact_kwargs)
            self.assertNotIn("modified_end", exact_kwargs)
            self.assertTrue(cache_path.is_file())
            _scanned_at, cached_ids = planner._load_candidate_cache(
                cache_path, begin, now
            )
            self.assertEqual(cached_ids, {"17001"})

            cache_path.write_text("{broken", encoding="utf-8")
            with mock.patch.object(planner, "_page_all") as unexpected_scan:
                with self.assertRaisesRegex(RuntimeError, "缺失或损坏"):
                    planner.discover_orders(
                        object(),
                        begin=begin,
                        now=now,
                        candidate_cache=str(cache_path),
                    )
            unexpected_scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
