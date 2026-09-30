"""Read-only production audit for manual-carrier action classification."""

import argparse
import json
from pathlib import Path

import jst_print_shadow_plan as planner


def action_time(action: dict) -> str:
    parsed = planner._parse_dt(  # noqa: SLF001 - diagnostic uses planner contract
        action.get("created") or action.get("modified") or action.get("time")
    )
    return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-root", required=True)
    parser.add_argument("--candidate-cache", required=True)
    parser.add_argument("--lookback-hours", type=int, default=168)
    parser.add_argument("--history-days", type=int, default=7)
    args = parser.parse_args()

    now = planner.business_now()
    client = planner._load_jst_client(args.bridge_root)  # noqa: SLF001
    rows = planner.discover_orders(
        client,
        begin=now - planner.timedelta(hours=args.lookback_hours),
        now=now,
        candidate_cache=args.candidate_cache,
    )
    split_ids = planner.split_order_ids(rows)
    queried_rows = [
        row
        for row in rows
        if planner.row_scope_state(row) != "OUT_OF_SCOPE"
        and planner.is_preliminary_candidate(row)
        and planner._text(row.get("o_id")) not in split_ids  # noqa: SLF001
        and not planner.action_history_independent_blocked(row)
    ]
    proofs = planner.query_actions_bulk(
        client, queried_rows, now, args.history_days
    )

    findings = []
    for row in queried_rows:
        o_id = planner._text(row.get("o_id"))  # noqa: SLF001
        io_id = planner._text(row.get("io_id"))  # noqa: SLF001
        actions, complete = proofs.get(o_id, ([], False))
        manual = [
            action
            for action in actions
            if planner._text(action.get("name"))  # noqa: SLF001
            in planner.MANUAL_CARRIER_ACTION_MARKERS
        ]
        if not manual:
            continue
        audit = [
            action
            for action in actions
            if planner._text(action.get("name"))  # noqa: SLF001
            == planner.AUDIT_STAGE_MANUAL_CARRIER_CONFIRM_ACTION
        ]
        protected = planner.has_protected_manual_carrier_action(actions)
        plan = planner.make_plan(
            row,
            actions,
            action_history_complete=complete,
            split_outbound_order=False,
        )
        desired_id, desired_name = planner.desired_carrier(row)
        findings.append(
            {
                "o_id": o_id,
                "io_id": io_id,
                "status": planner._text(row.get("status")),  # noqa: SLF001
                "current_carrier_id": planner._text(row.get("lc_id")),  # noqa: SLF001
                "current_carrier_name": planner._text(  # noqa: SLF001
                    row.get("logistics_company")
                ),
                "desired_carrier_id": desired_id,
                "desired_carrier_name": desired_name,
                "weight_kg": planner._weight(row),  # noqa: SLF001
                "has_waybill": bool(planner._text(row.get("l_id"))),  # noqa: SLF001
                "history_complete": complete,
                "classification": (
                    "PROTECTED_MANUAL_OVERRIDE"
                    if protected
                    else "AUDIT_STAGE_ASSIGNMENT"
                ),
                "manual_action_times": [action_time(action) for action in manual],
                "forced_audit_times": [action_time(action) for action in audit],
                "plan_state": plan.state,
                "first_step": plan.steps[0] if plan.steps else "",
                "blockers": plan.blockers,
            }
        )

    result = {
        "mode": "LIVE_MANUAL_CARRIER_READ_ONLY_AUDIT_V1",
        "generated_at": planner.utc_now_text(),
        "orders_read": len(rows),
        "action_histories_queried": len(queried_rows),
        "manual_action_findings": len(findings),
        "audit_stage_assignments": sum(
            item["classification"] == "AUDIT_STAGE_ASSIGNMENT"
            for item in findings
        ),
        "protected_manual_overrides": sum(
            item["classification"] == "PROTECTED_MANUAL_OVERRIDE"
            for item in findings
        ),
        "findings": findings,
        "redaction": "buyer, address, phone, SKU and full waybill omitted",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
