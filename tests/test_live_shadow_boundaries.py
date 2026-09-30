import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

import jst_print_shadow_plan as planner
import live_shadow_test as shadow

SERVER_DIR = Path(__file__).resolve().parents[1] / "server_batch_v056"
sys.path.insert(0, str(SERVER_DIR))
try:
    import jst_print_api_server as api
finally:
    sys.path.remove(str(SERVER_DIR))


class LiveShadowBoundaryTests(unittest.TestCase):
    def test_shadow_run_requires_explicit_no_active_lease_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "活动租约"):
            shadow.run(1, planner.TARGET_CARRIER_ID)

    def test_shadow_waitconfirm_ship_action_pauses_instead_of_retires(self):
        result = shadow.evaluate(
            {
                "o_id": "10001",
                "io_id": "20001",
                "steps": ["PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
            },
            {
                "found": True,
                "o_id": "10001",
                "io_id": "20001",
                "status": "WaitConfirm",
                "has_ship_action": True,
                "has_print_action": False,
                "privacy_required": False,
                "delivery_hold_marked": False,
                "items_complete": True,
                "items": [],
                "has_waybill": True,
            },
        )
        self.assertEqual(result["decision"], "SAFE_PAUSE_SHIP_ACTION_UNPROVEN")

    def test_server_and_planner_use_inventory_free_current_contract(self):
        self.assertEqual(planner.PLANNER_SCHEMA_VERSION, 5)
        self.assertEqual(api.PLANNER_SCHEMA_VERSION, 5)
        self.assertEqual(api.RAW_PLAN_MODE, "LIVE_READ_ONLY_SHADOW_V5")
        self.assertEqual(api.RAW_INSPECT_MODE, "ORDER_READBACK_V5")

    def test_action_history_limit_matches_real_jst_seven_day_api_limit(self):
        self.assertEqual(planner.MAX_ACTION_HISTORY_DAYS, 7)
        self.assertEqual(api.MAX_ACTION_HISTORY_DAYS, 7)
        self.assertEqual(api.ACTION_HISTORY_DAYS, 7)

    def test_planner_accepts_seven_days(self):
        argv = [
            "jst_print_shadow_plan.py",
            "--live-readonly",
            "--action-history-days",
            "7",
        ]
        with mock.patch.object(sys, "argv", argv):
            args = planner.parse_args()
        self.assertEqual(args.action_history_days, 7)

    def test_planner_rejects_zero_and_eight_days_before_network_access(self):
        for value in ("0", "8"):
            with self.subTest(value=value):
                argv = [
                    "jst_print_shadow_plan.py",
                    "--live-readonly",
                    "--action-history-days",
                    value,
                ]
                with mock.patch.object(sys, "argv", argv):
                    with self.assertRaises(SystemExit) as raised:
                        planner.parse_args()
                self.assertEqual(raised.exception.code, 2)

    def test_live_plan_never_queries_stock_and_omits_stock_fields(self):
        def order(o_id, io_id, created, *, printed):
            return {
                "o_id": o_id,
                "io_id": io_id,
                "created": created,
                "wms_co_id": planner.WAREHOUSE_ID,
                "status": "WaitConfirm",
                "logistics_company": planner.TARGET_CARRIER_NAME,
                "lc_id": planner.TARGET_CARRIER_ID,
                "weight": 1,
                "l_id": "",
                "is_print_express": printed,
                "remark": "",
                "labels": "",
                "shop_name": "普通店铺",
                "items": [
                    {
                        "ioi_id": f"{io_id}1",
                        "i_id": "product-1",
                        "sku_id": "SKU-1",
                        "name": "测试商品",
                        "qty": 1,
                    }
                ],
            }

        rows = [
            order("10001", "20001", "2026-08-25 10:00:00", printed=True),
            order("10003", "20003", "2026-08-25 10:00:30", printed=False),
            order("10002", "20002", "2026-08-25 10:01:00", printed=False),
        ]

        class FakeClient:
            def query_inventory(self, **_kwargs):
                raise AssertionError("planner must not query inventory")

        client = FakeClient()
        app_module = types.ModuleType("app")
        clients_module = types.ModuleType("app.clients")
        jst_client_module = types.ModuleType("app.clients.jst_client")
        jst_client_module.JSTClient = lambda: client
        args = SimpleNamespace(
            bridge_root="/fake-bridge",
            lookback_hours=168,
            candidate_cache="",
            action_history_days=7,
            max_candidates=10,
        )
        action_proofs = {
            "10001": ([], True),
            "10003": ([{"name": "打印快递单"}], True),
            "10002": ([], True),
        }

        with mock.patch.dict(
            sys.modules,
            {
                "app": app_module,
                "app.clients": clients_module,
                "app.clients.jst_client": jst_client_module,
            },
        ), mock.patch.object(planner.Path, "exists", return_value=True), mock.patch.object(
            planner, "discover_orders", return_value=rows
        ), mock.patch.object(
            planner, "query_actions_bulk", return_value=action_proofs
        ):
            result = planner.run_live_readonly(args)

        self.assertEqual(result["schema_version"], 5)
        self.assertEqual(result["mode"], "LIVE_READ_ONLY_SHADOW_V5")
        self.assertEqual(result["counts"]["ready"], 1)
        self.assertEqual(result["counts"]["blocked"], 2)
        self.assertEqual(
            [(item["o_id"], item["io_id"]) for item in result["selected"]],
            [("10002", "20002")],
        )
        self.assertNotIn("inventory_complete", result["selected"][0])
        self.assertNotIn("inventory_sufficient", result["selected"][0])
        self.assertEqual(
            result["blocked_preview"][0]["blockers"],
            ["聚水潭已标记打印，需人工核查，禁止重复打印"],
        )
        self.assertEqual(
            result["blocked_preview"][1]["blockers"],
            ["已有打印动作，禁止重复打印"],
        )

    def test_exact_readback_never_queries_stock_and_omits_stock_fields(self):
        row = {
            "o_id": "10002",
            "io_id": "20002",
            "created": "2026-08-25 10:01:00",
            "wms_co_id": planner.WAREHOUSE_ID,
            "status": "WaitConfirm",
            "logistics_company": planner.TARGET_CARRIER_NAME,
            "lc_id": planner.TARGET_CARRIER_ID,
            "weight": 1,
            "l_id": "",
            "is_print_express": False,
            "remark": "",
            "labels": "",
            "shop_name": "普通店铺",
            "items": [
                {
                    "ioi_id": "200021",
                    "i_id": "product-1",
                    "sku_id": "SKU-1",
                    "name": "测试商品",
                    "qty": 1,
                }
            ],
        }

        class FakeClient:
            def query_inventory(self, **_kwargs):
                raise AssertionError("planner must not query inventory")

        client = FakeClient()
        app_module = types.ModuleType("app")
        clients_module = types.ModuleType("app.clients")
        jst_client_module = types.ModuleType("app.clients.jst_client")
        jst_client_module.JSTClient = lambda: client
        args = SimpleNamespace(
            bridge_root="/fake-bridge",
            action_history_days=7,
            inspect_o_id="10002",
            inspect_io_id="20002",
        )

        with mock.patch.dict(
            sys.modules,
            {
                "app": app_module,
                "app.clients": clients_module,
                "app.clients.jst_client": jst_client_module,
            },
        ), mock.patch.object(planner.Path, "exists", return_value=True), mock.patch.object(
            planner, "_page_all", return_value=[row]
        ), mock.patch.object(planner, "query_actions", return_value=([], True)):
            result = planner.run_inspect_order(args)

        self.assertEqual(result["schema_version"], 5)
        self.assertEqual(result["mode"], "ORDER_READBACK_V5")
        self.assertNotIn("inventory_complete", result)
        self.assertNotIn("inventory_sufficient", result)
        self.assertNotIn("inventory_validation_errors", result)


if __name__ == "__main__":
    unittest.main()
