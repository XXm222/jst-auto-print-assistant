import gc
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


SERVER_DIR = Path(__file__).resolve().parents[1] / "server_batch_v056"
sys.path.insert(0, str(SERVER_DIR))
try:
    import jst_print_api_server as api
    import jst_print_shadow_plan as planner
    from jst_lease_store import Lease, LeaseConflict, LeaseStore
finally:
    sys.path.remove(str(SERVER_DIR))


def _candidate(index: int) -> dict:
    return {
        "o_id": str(17000 + index),
        "io_id": str(27000 + index),
        "current_carrier_id": api.TARGET_CARRIER_ID,
        "current_carrier": api.PRINT_PROFILE_CARRIERS[api.TARGET_CARRIER_ID],
        "steps": ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
        "items": [{"product_id": f"product-{index}"}],
    }


def _raw_pool(count: int = 11) -> dict:
    return {
        "generated_at": planner.utc_now_text(),
        "scope": {},
        "counts": {"selected": count},
        "selected": [_candidate(index) for index in range(count)],
        "blocked_preview": [],
        "blocked_reason_counts": {},
        "guardrails": [],
    }


def _order_row(o_id: str, io_id: str, *, status: str = "WaitConfirm") -> dict:
    return {
        "o_id": o_id,
        "io_id": io_id,
        "wms_co_id": api.TARGET_WAREHOUSE_ID,
        "status": status,
        "weight": 1.0,
        "created": "2026-08-27 09:00:00",
        "io_date": "2026-08-27 09:05:00",
        "shop_name": "安全测试店铺",
        "lc_id": api.TARGET_CARRIER_ID,
        "logistics_company": api.PRINT_PROFILE_CARRIERS[api.TARGET_CARRIER_ID],
        "l_id": "",
        "is_print_express": False,
        "remark": "",
        "labels": "",
        "items": [
            {
                "ioi_id": "1",
                "i_id": "product-1",
                "sku_id": "sku-1",
                "name": "普通商品",
                "qty": 1,
                "unit": "件",
            }
        ],
    }


def _inspect_result(
    o_id: str,
    io_id: str,
    *,
    status: str = "WaitConfirm",
    action_name: str = "",
    history_complete: bool = True,
) -> dict:
    actions = []
    if action_name:
        actions.append(
            {
                "o_id": o_id,
                "name": action_name,
                "created": "2026-08-27 09:10:00",
            }
        )
    return planner._found_inspect_result(
        _order_row(o_id, io_id, status=status),
        actions,
        history_complete,
        generated_at=planner.utc_now_text(),
    )


class BackendSecurityV4Tests(unittest.TestCase):
    def setUp(self):
        api._clear_inspect_proof_cache()

    def tearDown(self):
        api._clear_inspect_proof_cache()

    def test_schema_and_minimum_client_require_completion_contract(self):
        self.assertEqual(api.API_SCHEMA_VERSION, 5)
        self.assertEqual(api.MIN_CLIENT_VERSION, "0.5.21")

    def test_random_client_workstation_ids_share_one_bearer_principal_and_capacity(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            api, "TOKEN", "single-machine-token-" + "x" * 40
        ), mock.patch.object(api, "get_plan_pool", return_value=_raw_pool()):
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
            first = api._plan(
                {
                    "workstation_id": "ws-client-original",
                    "print_profile": api.TARGET_CARRIER_ID,
                    "max_candidates": 10,
                },
                store,
            )
            retry_with_invented_id = api._plan(
                {
                    "workstation_id": "ws-client-invented",
                    "print_profile": api.TARGET_CARRIER_ID,
                    "max_candidates": 10,
                },
                store,
            )

            self.assertEqual(len(first["selected"]), 10)
            self.assertEqual(len(retry_with_invented_id["selected"]), 10)
            self.assertEqual(
                [item["claim_token"] for item in first["selected"]],
                [item["claim_token"] for item in retry_with_invented_id["selected"]],
            )
            active = store.active_for_workstation(api._server_workstation_id())
            self.assertEqual(len(active), 10)
            self.assertNotIn("17010", {lease.o_id for lease in active})

    def test_complete_requires_a_strict_reason(self):
        base = {
            "workstation_id": "ws-completion-client",
            "o_id": "18001",
            "io_id": "28001",
            "claim_token": "x" * 32,
        }
        with self.assertRaises(api.APIError) as missing:
            api._completion_credentials(base)
        self.assertEqual((missing.exception.status, missing.exception.code), (400, "invalid_completion_request"))

        with self.assertRaises(api.APIError) as unknown:
            api._completion_credentials(dict(base, completion_reason="OTHER"))
        self.assertEqual((unknown.exception.status, unknown.exception.code), (400, "invalid_completion_reason"))

    def test_manual_completion_persists_reason_and_only_same_reason_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            api, "TOKEN", "single-machine-token-" + "y" * 40
        ):
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
            lease = store.claim("18002", "28002", api._server_workstation_id())
            assert lease is not None
            body = {
                "workstation_id": "ws-completion-client",
                "o_id": lease.o_id,
                "io_id": lease.io_id,
                "claim_token": lease.claim_token,
                "completion_reason": "OPERATOR_SKIPPED",
            }

            first = api.process_request(
                "/jst-print-api/v1/lease/complete", body, store=store
            )
            retry = api.process_request(
                "/jst-print-api/v1/lease/complete", body, store=store
            )
            self.assertEqual(first["completion_reason"], "OPERATOR_SKIPPED")
            self.assertEqual(retry["completion_reason"], "OPERATOR_SKIPPED")

            with self.assertRaises(api.APIError) as changed:
                api.process_request(
                    "/jst-print-api/v1/lease/complete",
                    dict(body, completion_reason="UNCERTAIN_ACTION"),
                    store=store,
                )
            self.assertEqual((changed.exception.status, changed.exception.code), (409, "lease_conflict"))
            # Release the captured exception chain before Windows removes the database.
            del changed
            gc.collect()

    def test_printed_completion_requires_live_complete_history_and_print_action(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            api, "TOKEN", "single-machine-token-" + "z" * 40
        ):
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
            lease = store.claim("18003", "28003", api._server_workstation_id())
            assert lease is not None
            body = {
                "workstation_id": "ws-completion-client",
                "o_id": lease.o_id,
                "io_id": lease.io_id,
                "claim_token": lease.claim_token,
                "completion_reason": "PRINTED",
            }
            proof = _inspect_result(
                lease.o_id, lease.io_id, action_name="打印快递单"
            )
            with mock.patch.object(api, "run_planner", return_value=proof):
                result = api.process_request(
                    "/jst-print-api/v1/lease/complete", body, store=store
                )
            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(result["completion_reason"], "PRINTED")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            api, "TOKEN", "single-machine-token-" + "z" * 40
        ):
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
            lease = store.claim("18004", "28004", api._server_workstation_id())
            assert lease is not None
            body.update(
                o_id=lease.o_id,
                io_id=lease.io_id,
                claim_token=lease.claim_token,
            )
            no_proof = _inspect_result(lease.o_id, lease.io_id)
            with mock.patch.object(api, "run_planner", return_value=no_proof):
                with self.assertRaises(api.APIError) as rejected:
                    api.process_request(
                        "/jst-print-api/v1/lease/complete", body, store=store
                    )
            self.assertEqual((rejected.exception.status, rejected.exception.code), (409, "completion_proof_failed"))
            self.assertEqual(
                store.require_active(
                    lease.o_id,
                    lease.io_id,
                    lease.workstation_id,
                    lease.claim_token,
                ).state,
                "ACTIVE",
            )

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            api, "TOKEN", "single-machine-token-" + "z" * 40
        ):
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
            lease = store.claim("18006", "28006", api._server_workstation_id())
            assert lease is not None
            body.update(
                o_id=lease.o_id,
                io_id=lease.io_id,
                claim_token=lease.claim_token,
            )
            incomplete = _inspect_result(
                lease.o_id,
                lease.io_id,
                action_name="打印快递单",
                history_complete=False,
            )
            with mock.patch.object(api, "run_planner", return_value=incomplete):
                with self.assertRaises(api.APIError) as rejected:
                    api.process_request(
                        "/jst-print-api/v1/lease/complete", body, store=store
                    )
            self.assertEqual(rejected.exception.code, "completion_proof_failed")

    def test_terminal_completion_requires_current_terminal_status(self):
        for status, action_name in (("Sent", ""), ("Delete", "")):
            with self.subTest(status=status, action_name=action_name), tempfile.TemporaryDirectory() as directory, mock.patch.object(
                api, "TOKEN", "single-machine-token-" + "t" * 40
            ):
                store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
                o_id = str(18100 + len(status) + len(action_name))
                io_id = str(28100 + len(status) + len(action_name))
                lease = store.claim(o_id, io_id, api._server_workstation_id())
                assert lease is not None
                proof = _inspect_result(
                    lease.o_id,
                    lease.io_id,
                    status=status,
                    action_name=action_name,
                )
                with mock.patch.object(api, "run_planner", return_value=proof):
                    result = api.process_request(
                        "/jst-print-api/v1/lease/complete",
                        {
                            "workstation_id": "ws-completion-client",
                            "o_id": lease.o_id,
                            "io_id": lease.io_id,
                            "claim_token": lease.claim_token,
                            "completion_reason": "TERMINAL",
                        },
                        store=store,
                    )
                self.assertEqual(result["completion_reason"], "TERMINAL")

        for o_id, action_name in (("18198", ""), ("18199", "发货成功")):
            with self.subTest(action_name=action_name), tempfile.TemporaryDirectory() as directory, mock.patch.object(
                api, "TOKEN", "single-machine-token-" + "t" * 40
            ):
                store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
                lease = store.claim(o_id, str(int(o_id) + 10000), api._server_workstation_id())
                assert lease is not None
                proof = _inspect_result(
                    lease.o_id,
                    lease.io_id,
                    status="WaitConfirm",
                    action_name=action_name,
                )
                with mock.patch.object(api, "run_planner", return_value=proof):
                    with self.assertRaises(api.APIError) as rejected:
                        api.process_request(
                            "/jst-print-api/v1/lease/complete",
                            {
                                "workstation_id": "ws-completion-client",
                                "o_id": lease.o_id,
                                "io_id": lease.io_id,
                                "claim_token": lease.claim_token,
                                "completion_reason": "TERMINAL",
                            },
                            store=store,
                        )
                self.assertEqual(rejected.exception.code, "completion_proof_failed")
                self.assertEqual(
                    store.require_active(
                        lease.o_id,
                        lease.io_id,
                        lease.workstation_id,
                        lease.claim_token,
                    ).state,
                    "ACTIVE",
                )

    def test_recent_single_inspect_proof_avoids_a_second_planner(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            api, "TOKEN", "single-machine-token-" + "c" * 40
        ):
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
            lease = store.claim("18201", "28201", api._server_workstation_id())
            assert lease is not None
            inspect_body = {
                "workstation_id": "ws-completion-client",
                "o_id": lease.o_id,
                "io_id": lease.io_id,
                "claim_token": lease.claim_token,
            }
            proof = _inspect_result(
                lease.o_id, lease.io_id, action_name="打印快递单"
            )
            with mock.patch.object(
                api, "run_planner", return_value=proof
            ) as planner_run:
                api.process_request(
                    "/jst-print-api/v1/inspect", inspect_body, store=store
                )
                result = api.process_request(
                    "/jst-print-api/v1/lease/complete",
                    dict(inspect_body, completion_reason="PRINTED"),
                    store=store,
                )

            self.assertEqual(result["completion_reason"], "PRINTED")
            self.assertEqual(planner_run.call_count, 1)

    def test_expired_inspect_proof_forces_live_readback(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            api, "TOKEN", "single-machine-token-" + "e" * 40
        ):
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
            lease = store.claim("18202", "28202", api._server_workstation_id())
            assert lease is not None
            inspect_body = {
                "workstation_id": "ws-completion-client",
                "o_id": lease.o_id,
                "io_id": lease.io_id,
                "claim_token": lease.claim_token,
            }
            proof = _inspect_result(
                lease.o_id, lease.io_id, action_name="打印快递单"
            )
            with mock.patch.object(
                api, "run_planner", return_value=proof
            ) as planner_run:
                api.process_request(
                    "/jst-print-api/v1/inspect", inspect_body, store=store
                )
                key = (lease.o_id, lease.io_id, lease.claim_token)
                with api._inspect_proof_cache_lock:
                    recorded_at, cached = api._inspect_proof_cache[key]
                    api._inspect_proof_cache[key] = (
                        recorded_at - api.INSPECT_PROOF_CACHE_SECONDS - 1,
                        cached,
                    )
                result = api.process_request(
                    "/jst-print-api/v1/lease/complete",
                    dict(inspect_body, completion_reason="PRINTED"),
                    store=store,
                )

            self.assertEqual(result["completion_reason"], "PRINTED")
            self.assertEqual(planner_run.call_count, 2)

    def test_cached_proof_for_a_different_reason_forces_live_readback(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            api, "TOKEN", "single-machine-token-" + "m" * 40
        ):
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
            lease = store.claim("18203", "28203", api._server_workstation_id())
            assert lease is not None
            inspect_body = {
                "workstation_id": "ws-completion-client",
                "o_id": lease.o_id,
                "io_id": lease.io_id,
                "claim_token": lease.claim_token,
            }
            print_only = _inspect_result(
                lease.o_id, lease.io_id, action_name="打印快递单"
            )
            terminal = _inspect_result(lease.o_id, lease.io_id, status="Sent")
            with mock.patch.object(
                api,
                "run_planner",
                side_effect=[print_only, terminal],
            ) as planner_run:
                api.process_request(
                    "/jst-print-api/v1/inspect", inspect_body, store=store
                )
                result = api.process_request(
                    "/jst-print-api/v1/lease/complete",
                    dict(inspect_body, completion_reason="TERMINAL"),
                    store=store,
                )

            self.assertEqual(result["completion_reason"], "TERMINAL")
            self.assertEqual(planner_run.call_count, 2)

    def test_recent_batch_inspect_proof_is_cached_and_cache_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            api, "TOKEN", "single-machine-token-" + "b" * 40
        ):
            store = LeaseStore(Path(directory) / "leases.sqlite3", ttl_seconds=300)
            leases = [
                store.claim("18301", "28301", api._server_workstation_id()),
                store.claim("18302", "28302", api._server_workstation_id()),
            ]
            assert all(lease is not None for lease in leases)
            claimed = [lease for lease in leases if lease is not None]
            generated_at = planner.utc_now_text()
            results = [
                _inspect_result(
                    lease.o_id,
                    lease.io_id,
                    action_name="打印快递单",
                )
                for lease in claimed
            ]
            for result in results:
                result["generated_at"] = generated_at
            raw_batch = {
                "mode": api.RAW_INSPECT_BATCH_MODE,
                "schema_version": api.PLANNER_SCHEMA_VERSION,
                "generated_at": generated_at,
                "results": results,
            }
            batch_body = {
                "workstation_id": "ws-completion-client",
                "orders": [
                    {
                        "o_id": lease.o_id,
                        "io_id": lease.io_id,
                        "claim_token": lease.claim_token,
                    }
                    for lease in claimed
                ],
            }
            with mock.patch.object(
                api, "run_planner", return_value=raw_batch
            ) as planner_run:
                api.process_request(
                    "/jst-print-api/v1/inspect-batch", batch_body, store=store
                )
                first = claimed[0]
                result = api.process_request(
                    "/jst-print-api/v1/lease/complete",
                    {
                        "workstation_id": "ws-completion-client",
                        "o_id": first.o_id,
                        "io_id": first.io_id,
                        "claim_token": first.claim_token,
                        "completion_reason": "PRINTED",
                    },
                    store=store,
                )
            self.assertEqual(result["completion_reason"], "PRINTED")
            self.assertEqual(planner_run.call_count, 1)

        api._clear_inspect_proof_cache()
        matching_proof = {
            "found": True,
            "status": "WaitConfirm",
            "action_history_complete": True,
            "has_print_action": True,
            "has_ship_action": False,
        }
        for index in range(api.INSPECT_PROOF_CACHE_MAX_ENTRIES + 40):
            api._remember_inspect_proof(
                str(19000 + index),
                str(29000 + index),
                f"token-{index}",
                matching_proof,
            )
        with api._inspect_proof_cache_lock:
            self.assertEqual(
                len(api._inspect_proof_cache),
                api.INSPECT_PROOF_CACHE_MAX_ENTRIES,
            )

    def test_claimed_inspect_omits_upstream_action_text(self):
        raw = _inspect_result("18005", "28005", action_name="打印快递单")
        raw["actions"] = [
            {"name": "PRINT", "time": "2026-08-27 09:10:00"}
        ]
        lease = Lease(
            o_id="18005",
            io_id="28005",
            workstation_id="ws-safe-principal",
            claim_token="x" * 32,
            state="ACTIVE",
            expires_epoch=1_900_000_000.0,
            lease_ttl_seconds=300,
        )
        public = api._claimed_inspect_result(raw, lease)
        self.assertEqual(public["actions"], [])
        self.assertFalse(public["has_print_request"])
        self.assertTrue(public["has_print_action"])

    def test_claimed_inspect_preserves_print_request_as_non_completion_evidence(self):
        raw = _inspect_result(
            "18006", "28006", action_name="请求打印快递单"
        )
        lease = Lease(
            o_id="18006",
            io_id="28006",
            workstation_id="ws-safe-principal",
            claim_token="y" * 32,
            state="ACTIVE",
            expires_epoch=1_900_000_000.0,
            lease_ttl_seconds=300,
        )

        public = api._claimed_inspect_result(raw, lease)

        self.assertTrue(public["has_print_request"])
        self.assertFalse(public["has_print_action"])
        self.assertEqual(public["actions"], [])

    def test_planner_capacity_exhaustion_returns_503_without_spawning(self):
        one_slot = threading.BoundedSemaphore(1)
        one_slot.acquire()
        try:
            with mock.patch.object(api, "_planner_slots", one_slot), mock.patch.object(
                api.subprocess, "run"
            ) as subprocess_run:
                with self.assertRaises(api.APIError) as busy:
                    api.run_planner(["--live-readonly"])
            self.assertEqual((busy.exception.status, busy.exception.code), (503, "planner_busy"))
            subprocess_run.assert_not_called()
        finally:
            one_slot.release()

    def test_planner_slot_is_released_when_subprocess_fails(self):
        one_slot = threading.BoundedSemaphore(1)
        with mock.patch.object(api, "_planner_slots", one_slot), mock.patch.object(
            api.subprocess, "run", side_effect=RuntimeError("spawn failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "spawn failed"):
                api.run_planner(["--live-readonly"])
        self.assertTrue(one_slot.acquire(blocking=False))
        one_slot.release()

    def test_planner_subprocess_receives_configured_bridge_root(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok": true}', stderr=""
        )
        with mock.patch.object(
            api, "BRIDGE_ROOT", "/opt/jst-erp-bridge"
        ), mock.patch.object(
            api, "PLANNER", "/opt/jst-print-api/jst_print_shadow_plan.py"
        ), mock.patch.object(
            api.subprocess, "run", return_value=completed
        ) as subprocess_run:
            self.assertEqual(api.run_planner(["--live-readonly"]), {"ok": True})

        command = subprocess_run.call_args.args[0]
        self.assertEqual(
            command,
            [
                "/usr/bin/python3",
                "/opt/jst-print-api/jst_print_shadow_plan.py",
                "--live-readonly",
                "--bridge-root",
                "/opt/jst-erp-bridge",
            ],
        )
        self.assertEqual(
            subprocess_run.call_args.kwargs["cwd"], "/opt/jst-erp-bridge"
        )

    def test_http_identity_requires_bounded_ascii_strings(self):
        valid = api._identity({"o_id": "1", "io_id": "9" * 20})
        self.assertEqual(valid, ("1", "9" * 20))
        for bad in (
            {"o_id": "１２３", "io_id": "2"},
            {"o_id": "1" * 21, "io_id": "2"},
            {"o_id": 1, "io_id": "2"},
        ):
            with self.subTest(bad=bad), self.assertRaises(api.APIError):
                api._identity(bad)

    def test_server_planner_cli_rejects_unicode_or_oversized_ids(self):
        script = SERVER_DIR / "jst_print_shadow_plan.py"
        for o_id in ("１２３", "1" * 21):
            with self.subTest(o_id=o_id):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        "--inspect-o-id",
                        o_id,
                        "--inspect-io-id",
                        "2",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertIn("digits", completed.stderr)

    def test_systemd_unit_uses_low_privilege_and_resource_limits(self):
        unit = (SERVER_DIR / "jst-print-api.service").read_text(encoding="utf-8")
        self.assertIn("User=jst-print-api", unit)
        self.assertNotIn("User=root", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("TasksMax=64", unit)
        self.assertIn("MemoryMax=768M", unit)
        self.assertIn("Environment=TZ=Asia/Shanghai", unit)
        self.assertIn("JST_PRINT_INSPECT_PROOF_CACHE_SECONDS=30", unit)

    def test_existing_database_is_migrated_online(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "leases.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE leases (
                    o_id TEXT NOT NULL,
                    io_id TEXT NOT NULL,
                    workstation_id TEXT NOT NULL,
                    claim_token TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    claimed_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    completed_at REAL,
                    PRIMARY KEY (o_id, io_id)
                );
                INSERT INTO leases VALUES
                    ('19001','29001','ws-legacy','legacy-token','COMPLETED',1,1,1,1);
                """
            )
            connection.commit()
            connection.close()

            LeaseStore(path, ttl_seconds=300)
            connection = sqlite3.connect(path)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(leases)")
            }
            reason = connection.execute(
                "SELECT completion_reason FROM leases WHERE o_id='19001'"
            ).fetchone()[0]
            connection.close()
            self.assertIn("completion_reason", columns)
            self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
