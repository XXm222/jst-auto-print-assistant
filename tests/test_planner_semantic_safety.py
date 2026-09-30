import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jst_print_shadow_plan as planner


def order_row(
    o_id: str = "41001",
    io_id: str = "51001",
    *,
    created: str = "2026-08-26 10:00:00",
) -> dict:
    return {
        "o_id": o_id,
        "io_id": io_id,
        "created": created,
        "io_date": created,
        "shop_name": "test-shop",
        "wms_co_id": planner.WAREHOUSE_ID,
        "status": "WaitConfirm",
        "logistics_company": planner.TARGET_CARRIER_NAME,
        "lc_id": planner.TARGET_CARRIER_ID,
        "weight": 1,
        "l_id": "",
        "is_print_express": False,
        "remark": "",
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


class PlannerSemanticSafetyTests(unittest.TestCase):
    def test_cancel_failure_and_unknown_related_actions_fail_closed(self):
        ambiguous_names = (
            "取消打印快递单",
            "打印快递单失败",
            "取消预发货",
            "预发货失败",
            "新版面单打印流程",
            "撤销指定快递",
        )
        for name in ambiguous_names:
            with self.subTest(name=name):
                actions = [{"name": name, "created": "2026-08-26 10:01:00"}]
                readback = planner._found_inspect_result(
                    order_row(),
                    actions,
                    True,
                    generated_at="2026-08-26T02:02:00Z",
                )
                self.assertFalse(readback["action_history_complete"])
                self.assertFalse(readback["has_print_request"])
                self.assertFalse(readback["has_print_action"])
                self.assertFalse(readback["has_ship_action"])
                self.assertFalse(readback["has_manual_carrier_action"])
                self.assertEqual(readback["actions"], [])

                plan = planner.make_plan(
                    order_row(), actions, action_history_complete=True
                )
                self.assertEqual(plan.state, "BLOCKED")
                self.assertIn("操作历史未完整覆盖，禁止自动处理", plan.blockers)

    def test_only_exact_known_action_names_are_success_evidence(self):
        cases = (
            ("请求打印快递单", "PRINT_REQUEST", "has_print_request"),
            ("打印快递单", "PRINT", "has_print_action"),
            ("预发货", "SHIP", "has_ship_action"),
            ("预发货成功", "SHIP", "has_ship_action"),
            ("检测到线上发货已经成功", "SHIP", "has_ship_action"),
            ("手工指定快递", "MANUAL_CARRIER", "has_manual_carrier_action"),
            ("获取电子面单", "WAYBILL", None),
        )
        for name, category, evidence_key in cases:
            with self.subTest(name=name):
                readback = planner._found_inspect_result(
                    order_row(),
                    [{"name": name, "created": "2026-08-26 10:01:00"}],
                    True,
                    generated_at="2026-08-26T02:02:00Z",
                )
                self.assertTrue(readback["action_history_complete"])
                self.assertEqual(readback["actions"], [{"name": category, "time": "2026-08-26 10:01:00"}])
                if evidence_key is not None:
                    self.assertTrue(readback[evidence_key])

    def test_print_request_blocks_reclick_but_is_not_completion_evidence(self):
        actions = [{"name": "请求打印快递单", "created": "2026-08-26 10:01:00"}]

        readback = planner._found_inspect_result(
            order_row(),
            actions,
            True,
            generated_at="2026-08-26T02:02:00Z",
        )
        plan = planner.make_plan(
            order_row(), actions, action_history_complete=True
        )

        self.assertTrue(readback["action_history_complete"])
        self.assertTrue(readback["has_print_request"])
        self.assertFalse(readback["has_print_action"])
        self.assertEqual(
            readback["actions"],
            [{"name": "PRINT_REQUEST", "time": "2026-08-26 10:01:00"}],
        )
        self.assertEqual(plan.state, "BLOCKED")
        self.assertIn("已有打印动作，禁止重复打印", plan.blockers)

    def test_exact_benign_warehouse_actions_do_not_make_history_ambiguous(self):
        for name in sorted(planner.BENIGN_RELATED_ACTIONS):
            with self.subTest(name=name):
                actions, ambiguous = planner._classify_actions([name])
                self.assertEqual(actions, set())
                self.assertFalse(ambiguous)

                plan = planner.make_plan(
                    order_row(), [{"name": name}], action_history_complete=True
                )
                self.assertEqual(plan.state, "READY_HYBRID")
                self.assertNotIn(
                    "操作历史未完整覆盖，禁止自动处理", plan.blockers
                )

    def test_exact_success_plus_unknown_related_action_remains_fail_closed(self):
        readback = planner._found_inspect_result(
            order_row(),
            [{"name": "打印快递单"}, {"name": "取消打印快递单"}],
            True,
            generated_at="2026-08-26T02:02:00Z",
        )
        self.assertTrue(readback["has_print_action"])
        self.assertFalse(readback["action_history_complete"])

    def test_exact_single_inspect_has_no_modified_window_and_sees_old_sibling(self):
        current = order_row(created="2026-08-26 10:00:00")
        old_sibling = order_row(
            io_id="51002", created="2026-07-01 10:00:00"
        )
        args = SimpleNamespace(
            bridge_root="/fake-bridge",
            action_history_days=7,
            inspect_o_id="41001",
            inspect_io_id="51001",
        )
        with mock.patch.object(
            planner, "_load_jst_client", return_value=object()
        ), mock.patch.object(
            planner, "_page_all", return_value=[current, old_sibling]
        ) as page_all, mock.patch.object(
            planner, "query_actions"
        ) as actions:
            result = planner.run_inspect_order(args)

        query_kwargs = page_all.call_args.kwargs
        self.assertNotIn("modified_begin", query_kwargs)
        self.assertNotIn("modified_end", query_kwargs)
        self.assertEqual(query_kwargs["o_ids"], [41001])
        self.assertFalse(result["action_history_complete"])
        actions.assert_not_called()

    def test_exact_batch_inspect_has_no_modified_window_and_sees_old_sibling(self):
        current = order_row(created="2026-08-26 10:00:00")
        old_sibling = order_row(
            io_id="51002", created="2026-07-01 10:00:00"
        )
        args = SimpleNamespace(
            bridge_root="/fake-bridge",
            action_history_days=7,
            inspect_pairs=[("41001", "51001")],
        )
        with mock.patch.object(
            planner, "_load_jst_client", return_value=object()
        ), mock.patch.object(
            planner, "_page_all", return_value=[current, old_sibling]
        ) as page_all, mock.patch.object(
            planner, "query_actions_bulk", return_value={}
        ) as actions:
            result = planner.run_inspect_batch(args)

        query_kwargs = page_all.call_args.kwargs
        self.assertNotIn("modified_begin", query_kwargs)
        self.assertNotIn("modified_end", query_kwargs)
        self.assertEqual(query_kwargs["o_ids"], [41001])
        self.assertFalse(result["results"][0]["action_history_complete"])
        self.assertEqual(actions.call_args.args[1], [])

    def test_candidate_exact_refresh_has_no_window_and_preserves_old_sibling(self):
        now = datetime(2026, 8, 26, 16, 0, 0)
        begin = now - timedelta(days=7)
        current = order_row(created="2026-08-26 10:00:00")
        old_sibling = order_row(
            io_id="51002", created="2026-07-01 10:00:00"
        )
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "candidate-orders-v1.json"
            planner._save_candidate_cache(
                cache_path, now - timedelta(minutes=1), ["41001"]
            )
            with mock.patch.object(
                planner,
                "_page_all",
                side_effect=[[], [current, old_sibling]],
            ) as page_all:
                rows = planner.discover_orders(
                    object(),
                    begin=begin,
                    now=now,
                    candidate_cache=str(cache_path),
                )

        exact_kwargs = page_all.call_args_list[1].kwargs
        self.assertNotIn("modified_begin", exact_kwargs)
        self.assertNotIn("modified_end", exact_kwargs)
        self.assertEqual(exact_kwargs["o_ids"], [41001])
        self.assertEqual(planner.split_order_ids(rows), {"41001"})

    def test_first_candidate_cache_bootstrap_also_exactly_refreshes_identity(self):
        now = datetime(2026, 8, 26, 16, 0, 0)
        current = order_row(created="2026-08-26 10:00:00")
        old_sibling = order_row(
            io_id="51002", created="2026-07-01 10:00:00"
        )
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "candidate-orders-v1.json"
            with mock.patch.object(
                planner,
                "_page_all",
                side_effect=[[current], [current, old_sibling]],
            ) as page_all:
                rows = planner.discover_orders(
                    object(),
                    begin=now - timedelta(days=7),
                    now=now,
                    candidate_cache=str(cache_path),
                )

        exact_kwargs = page_all.call_args_list[1].kwargs
        self.assertNotIn("modified_begin", exact_kwargs)
        self.assertNotIn("modified_end", exact_kwargs)
        self.assertEqual(planner.split_order_ids(rows), {"41001"})

    def test_utc_host_still_builds_shanghai_business_query_parameters(self):
        if not hasattr(time, "tzset"):
            self.skipTest("host does not support tzset")
        previous_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "UTC"
            time.tzset()
            utc_before = datetime.now(timezone.utc).replace(tzinfo=None)
            shanghai_now = planner.business_now()
            utc_after = datetime.now(timezone.utc).replace(tzinfo=None)
            self.assertGreaterEqual(shanghai_now, utc_before + timedelta(hours=8))
            self.assertLessEqual(shanghai_now, utc_after + timedelta(hours=8))

            class CaptureClient:
                def __init__(self):
                    self.kwargs = None

                def query_orders_out(self, **kwargs):
                    self.kwargs = kwargs
                    return {"datas": [], "has_next": False}

            client = CaptureClient()
            planner.discover_orders(
                client,
                begin=shanghai_now - timedelta(hours=1),
                now=shanghai_now,
                candidate_cache="",
            )
            self.assertEqual(
                client.kwargs["modified_end"],
                shanghai_now.strftime("%Y-%m-%d %H:%M:%S"),
            )
        finally:
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            time.tzset()

    def test_blocked_or_out_of_scope_rows_do_not_query_action_history(self):
        now = planner.business_now()
        created = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        ready = order_row("42001", "52001", created=created)
        mismatched_carrier = order_row("42002", "52002", created=created)
        mismatched_carrier["logistics_company"] = planner.SOURCE_CARRIER_NAME
        foreign_warehouse = order_row("42003", "52003", created=created)
        foreign_warehouse["wms_co_id"] = "other-warehouse"
        queried_o_ids: list[list[int]] = []

        class FakeClient:
            def query_order_action(self, **kwargs):
                queried_o_ids.append(list(kwargs["o_ids"]))
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
            planner,
            "discover_orders",
            return_value=[ready, mismatched_carrier, foreign_warehouse],
        ):
            result = planner.run_live_readonly(args)

        self.assertTrue(queried_o_ids)
        self.assertTrue(all(o_ids == [42001] for o_ids in queried_o_ids))
        self.assertEqual(result["counts"]["ready"], 1)
        self.assertEqual(result["counts"]["blocked"], 1)
        self.assertEqual(result["counts"]["orders_read"], 3)
        self.assertIn(
            "订单仓库、状态或快递字段不完整或不一致，禁止自动处理",
            result["blocked_preview"][0]["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
