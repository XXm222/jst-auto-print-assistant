import unittest

import jst_print_shadow_plan as planner


class PrivacyRemarkRoutingTests(unittest.TestCase):
    @staticmethod
    def _row(remark="", labels=""):
        return {
            "o_id": "6788091",
            "io_id": "13540001",
            "wms_co_id": planner.WAREHOUSE_ID,
            "status": "WaitConfirm",
            "logistics_company": planner.SOURCE_CARRIER_NAME,
            "lc_id": planner.SOURCE_CARRIER_ID,
            "weight": 1.0,
            "remark": remark,
            "labels": labels,
            "l_id": "",
            "items": [
                {
                    "ioi_id": "1",
                    "i_id": "PRODUCT-1",
                    "sku_id": "SKU-1",
                    "name": "测试商品",
                    "qty": 1,
                    "unit": "件",
                }
            ],
        }

    def _plan(self, remark="", labels=""):
        return planner.make_plan(
            self._row(remark, labels),
            [],
            action_history_complete=True,
        )

    def test_urgent_order_tag_uses_privacy_carrier_flow(self):
        plan = self._plan(labels="紧急单")

        self.assertTrue(plan.privacy_required)
        self.assertEqual(plan.state, "READY_HYBRID")
        self.assertEqual(
            plan.steps,
            [
                "RESET_CARRIER_AND_GET_WAYBILL:ZTO:中通-天猫-隐私面单",
                "PRINT_EXPRESS",
                "STOP_BEFORE_PRESHIP",
            ],
        )

    def test_urgent_order_token_can_appear_inside_multiple_tags(self):
        self.assertTrue(self._plan(labels="平台活动,紧急单,重点关注").privacy_required)

    def test_urgent_text_in_seller_remark_does_not_broaden_tag_rule(self):
        self.assertFalse(self._plan(remark="紧急单，请优先").privacy_required)

    def test_generic_urgent_tag_does_not_match_exact_urgent_order_token(self):
        self.assertFalse(self._plan(labels="紧急").privacy_required)

    def test_existing_privacy_token_still_uses_privacy_flow(self):
        self.assertTrue(self._plan("隐私发货").privacy_required)

    def test_planner_preserves_jst_main_product_id_for_grouping(self):
        plan = self._plan()

        self.assertEqual(plan.items[0]["product_id"], "PRODUCT-1")

    def test_existing_normal_zto_is_in_scope_and_gets_number_without_reset(self):
        row = self._row()
        row.update(
            lc_id=planner.TARGET_CARRIER_ID,
            logistics_company=planner.TARGET_CARRIER_NAME,
            weight=2.8,
        )

        plan = planner.make_plan(
            row,
            [],
            action_history_complete=True,
        )

        self.assertEqual(planner.row_scope_state(row), "CANDIDATE")
        self.assertEqual(
            plan.steps,
            ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
        )
        self.assertEqual(plan.current_carrier_id, planner.TARGET_CARRIER_ID)

    def test_over_3kg_existing_zto_resets_to_sto(self):
        row = self._row()
        row.update(
            lc_id=planner.TARGET_CARRIER_ID,
            logistics_company=planner.TARGET_CARRIER_NAME,
            weight=3.001,
        )

        plan = planner.make_plan(
            row,
            [],
            action_history_complete=True,
        )

        self.assertEqual(
            plan.steps,
            [
                "RESET_CARRIER_AND_GET_WAYBILL:STO:申通E物流-山东",
                "PRINT_EXPRESS",
                "STOP_BEFORE_PRESHIP",
            ],
        )

    def test_exactly_3kg_still_routes_to_normal_zto(self):
        row = self._row()
        row["weight"] = 3.0

        plan = planner.make_plan(
            row,
            [],
            action_history_complete=True,
        )

        self.assertEqual(
            plan.steps[0],
            "RESET_CARRIER_AND_GET_WAYBILL:ZTO.1:中通速递-山东",
        )

    def test_over_3kg_existing_sto_keeps_sto_without_reset(self):
        row = self._row()
        row["weight"] = 8.0

        plan = planner.make_plan(
            row,
            [],
            action_history_complete=True,
        )

        self.assertEqual(
            plan.steps,
            ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
        )

    def test_xiaohongshu_exception_resets_existing_zto_to_sto_under_3kg(self):
        row = self._row()
        row.update(
            lc_id=planner.TARGET_CARRIER_ID,
            logistics_company=planner.TARGET_CARRIER_NAME,
            weight=1.2,
            shop_name="小红书旗舰店",
        )

        plan = planner.make_plan(
            row,
            [],
            action_history_complete=True,
        )

        self.assertEqual(
            plan.steps[0],
            "RESET_CARRIER_AND_GET_WAYBILL:STO:申通E物流-山东",
        )

    def test_over_3kg_privacy_order_still_routes_to_privacy(self):
        row = self._row(labels="紧急单")
        row.update(
            lc_id=planner.TARGET_CARRIER_ID,
            logistics_company=planner.TARGET_CARRIER_NAME,
            weight=11.0,
        )

        plan = planner.make_plan(
            row,
            [],
            action_history_complete=True,
        )

        self.assertEqual(
            plan.steps[0],
            "RESET_CARRIER_AND_GET_WAYBILL:ZTO:中通-天猫-隐私面单",
        )

    def test_over_3kg_manual_zto_is_blocked_before_sto_overwrite(self):
        row = self._row()
        row.update(
            lc_id=planner.TARGET_CARRIER_ID,
            logistics_company=planner.TARGET_CARRIER_NAME,
            weight=4.62,
        )

        plan = planner.make_plan(
            row,
            [{"name": "手工指定快递"}],
            action_history_complete=True,
        )

        self.assertEqual(plan.state, "BLOCKED")
        self.assertIn("审单后已有手工指定快递，禁止自动覆盖人工决定", plan.blockers)

    def test_over_3kg_zto_with_waybill_is_blocked_before_sto_reset(self):
        row = self._row()
        row.update(
            lc_id=planner.TARGET_CARRIER_ID,
            logistics_company=planner.TARGET_CARRIER_NAME,
            weight=6.6,
            l_id="masked-waybill",
        )

        plan = planner.make_plan(
            row,
            [],
            action_history_complete=True,
        )

        self.assertEqual(plan.state, "BLOCKED")
        self.assertIn("已有运单号，重设快递前需人工确认旧面单处置", plan.blockers)

    def test_manual_existing_allowed_carrier_is_preserved_not_blocked(self):
        row = self._row()
        row.update(
            lc_id=planner.TARGET_CARRIER_ID,
            logistics_company=planner.TARGET_CARRIER_NAME,
        )
        plan = planner.make_plan(
            row,
            [{"name": "手工指定快递"}],
            action_history_complete=True,
        )

        self.assertEqual(plan.state, "READY_HYBRID")
        self.assertEqual(plan.blockers, [])

    def test_manual_carrier_is_still_blocked_when_plan_would_overwrite_it(self):
        plan = planner.make_plan(
            self._row(),
            [{"name": "手工指定快递"}],
            action_history_complete=True,
        )

        self.assertEqual(plan.state, "BLOCKED")
        self.assertIn("审单后已有手工指定快递，禁止自动覆盖人工决定", plan.blockers)

    def test_batch_audit_carrier_assignment_is_not_a_manual_override(self):
        actions = [
            {"name": "手工指定快递", "created": "2026-08-28 09:16:53"},
            {"name": "强制审核", "created": "2026-08-28 09:16:55"},
            {"name": "提交仓库发货", "created": "2026-08-28 09:34:27"},
        ]

        plan = planner.make_plan(
            self._row(),
            actions,
            action_history_complete=True,
        )

        self.assertEqual(plan.state, "READY_HYBRID")
        self.assertEqual(plan.blockers, [])
        self.assertEqual(
            plan.steps[0],
            "RESET_CARRIER_AND_GET_WAYBILL:ZTO.1:中通速递-山东",
        )
        self.assertFalse(planner.has_protected_manual_carrier_action(actions))

    def test_manual_carrier_change_after_audit_remains_protected(self):
        actions = [
            {"name": "强制审核", "created": "2026-08-28 09:16:55"},
            {"name": "手工指定快递", "created": "2026-08-28 09:20:00"},
        ]

        plan = planner.make_plan(
            self._row(),
            actions,
            action_history_complete=True,
        )

        self.assertEqual(plan.state, "BLOCKED")
        self.assertTrue(planner.has_protected_manual_carrier_action(actions))

    def test_distant_audit_does_not_explain_manual_carrier_change(self):
        actions = [
            {"name": "手工指定快递", "created": "2026-08-28 09:00:00"},
            {"name": "强制审核", "created": "2026-08-28 09:06:00"},
        ]

        self.assertTrue(planner.has_protected_manual_carrier_action(actions))

    def test_different_actor_cannot_explain_manual_carrier_change(self):
        actions = [
            {
                "name": "手工指定快递",
                "created": "2026-08-28 09:00:00",
                "creator_id": "human-operator",
            },
            {
                "name": "强制审核",
                "created": "2026-08-28 09:00:02",
                "creator_id": "batch-audit-service",
            },
        ]

        self.assertTrue(planner.has_protected_manual_carrier_action(actions))

    def test_same_actor_keeps_tightly_bounded_audit_pair(self):
        actions = [
            {
                "name": "手工指定快递",
                "created": "2026-08-28 09:00:00",
                "creator_id": "audit-service",
            },
            {
                "name": "强制审核",
                "created": "2026-08-28 09:00:02",
                "creator_id": "audit-service",
            },
        ]

        self.assertFalse(planner.has_protected_manual_carrier_action(actions))

    def test_two_manual_changes_cannot_reuse_one_audit_confirmation(self):
        actions = [
            {"name": "手工指定快递", "created": "2026-08-28 09:20:00"},
            {"name": "手工指定快递", "created": "2026-08-28 09:22:00"},
            {"name": "强制审核", "created": "2026-08-28 09:23:00"},
        ]

        self.assertTrue(planner.has_protected_manual_carrier_action(actions))

    def test_second_actorless_audit_pair_is_protected_as_ambiguous(self):
        actions = [
            {"name": "手工指定快递", "created": "2026-08-28 09:20:00"},
            {"name": "强制审核", "created": "2026-08-28 09:20:02"},
            {"name": "手工指定快递", "created": "2026-08-28 09:22:00"},
            {"name": "强制审核", "created": "2026-08-28 09:22:03"},
        ]

        self.assertTrue(planner.has_protected_manual_carrier_action(actions))

    def test_actorless_pair_after_prior_audit_is_protected(self):
        actions = [
            {"name": "强制审核", "created": "2026-08-28 09:00:00"},
            {"name": "手工指定快递", "created": "2026-08-28 09:20:00"},
            {"name": "强制审核", "created": "2026-08-28 09:20:02"},
        ]

        self.assertTrue(planner.has_protected_manual_carrier_action(actions))

    def test_multiple_audit_pairs_with_same_actor_remain_attributable(self):
        actions = [
            {
                "name": "手工指定快递",
                "created": "2026-08-28 09:20:00",
                "creator_id": "audit-service",
            },
            {
                "name": "强制审核",
                "created": "2026-08-28 09:20:02",
                "creator_id": "audit-service",
            },
            {
                "name": "手工指定快递",
                "created": "2026-08-28 09:22:00",
                "creator_id": "audit-service",
            },
            {
                "name": "强制审核",
                "created": "2026-08-28 09:22:03",
                "creator_id": "audit-service",
            },
        ]

        self.assertFalse(planner.has_protected_manual_carrier_action(actions))

    def test_same_timestamp_order_is_stable_and_fail_closed_when_reversed(self):
        valid = [
            {"name": "手工指定快递", "created": "2026-08-28 09:20:00"},
            {"name": "强制审核", "created": "2026-08-28 09:20:00"},
        ]
        reversed_order = list(reversed(valid))

        self.assertFalse(planner.has_protected_manual_carrier_action(valid))
        self.assertTrue(planner.has_protected_manual_carrier_action(reversed_order))

    def test_existing_zto_does_not_bypass_duplicate_print_guard(self):
        row = self._row()
        row.update(
            lc_id=planner.TARGET_CARRIER_ID,
            logistics_company=planner.TARGET_CARRIER_NAME,
        )
        printed = planner.make_plan(
            row,
            [{"name": "打印快递单"}],
            action_history_complete=True,
        )

        self.assertEqual(printed.state, "BLOCKED")
        self.assertIn("已有打印动作，禁止重复打印", printed.blockers)


if __name__ == "__main__":
    unittest.main()
