#!/usr/bin/env python3
"""Production read-only shadow run with temporary claimed leases.

This exercises the real HTTPS planner and exact-order inspect endpoints, but it
never starts a browser and never calls any JST write/print operation. Every
lease obtained by the run is released in ``finally``.
"""

from __future__ import annotations

import argparse
import json
import secrets
from collections import Counter
from typing import Any

from jst_auto_print_app import (
    AutomationEngine,
    PRINT_PROFILE_CARRIERS,
    PlannerClient,
    load_settings,
)


def evaluate(plan: dict[str, Any], readback: dict[str, Any]) -> dict[str, Any]:
    o_id = str(plan["o_id"])
    io_id = str(plan["io_id"])
    steps = list(plan.get("steps") or [])
    step = str(steps[0]) if steps else ""
    AutomationEngine._require_job_identity(readback, o_id, io_id)
    terminal_kind = AutomationEngine._terminal_status_kind(readback)
    if terminal_kind is not None:
        decision = f"SAFE_RETIRE_{terminal_kind}"
    elif readback.get("has_ship_action") is True:
        # An o_id-only action row is not an authoritative outbound terminal
        # state.  Match the production client: WaitConfirm plus a ship-looking
        # action must pause for review, never retire the order.
        decision = "SAFE_PAUSE_SHIP_ACTION_UNPROVEN"
    elif readback.get("has_print_action") is True:
        decision = "SAFE_RETIRE_ALREADY_PRINTED"
    else:
        AutomationEngine._require_waitconfirm_status(readback)
        AutomationEngine._validate_items_unchanged(plan, readback)
        AutomationEngine._preflight(readback, step, plan)
        if step == "PRINT_EXPRESS":
            AutomationEngine._validate_final_print_readback(
                plan, readback, o_id, io_id
            )
        decision = "READY_SHADOW_ONLY"
    return {
        "o_id": o_id,
        "io_id": io_id,
        "status": str(readback.get("status", "")),
        "decision": decision,
        "steps": steps,
        "privacy_required": bool(readback.get("privacy_required")),
        "delivery_hold_marked": bool(readback.get("delivery_hold_marked")),
        "items_complete": readback.get("items_complete") is True,
        "sku_lines": len(readback.get("items") or []),
        "has_waybill": readback.get("has_waybill") is True,
        "has_print_action": readback.get("has_print_action") is True,
        "has_ship_action": readback.get("has_ship_action") is True,
    }


def run(
    rounds: int,
    print_profile: str,
    *,
    confirm_no_active_production_leases: bool = False,
) -> dict[str, Any]:
    if not confirm_no_active_production_leases:
        raise RuntimeError(
            "影子运行与正式客户端使用同一 Bearer 工作站主体；"
            "必须先停止正式客户端并确认没有待恢复/暂停任务或活动租约"
        )
    settings = load_settings()
    workstation_id = f"ws-shadow-v0511-{secrets.token_hex(6)}"
    client = PlannerClient(settings, workstation_id)
    client.ping()
    excluded: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    released: list[dict[str, str]] = []
    release_failures: list[dict[str, str]] = []
    last_counts: dict[str, Any] = {}
    blocked_reason_counts: dict[str, Any] = {}

    for round_index in range(rounds):
        claimed: list[dict[str, Any]] = []
        try:
            payload = client.plan(excluded, print_profile)
            last_counts = dict(payload.get("counts") or {})
            blocked_reason_counts = dict(payload.get("blocked_reason_counts") or {})
            claimed = list(payload.get("selected") or [])
            if not claimed:
                break
            for plan in claimed:
                o_id = str(plan.get("o_id", ""))
                io_id = str(plan.get("io_id", ""))
                try:
                    readback = client.inspect(
                        o_id, io_id, str(plan.get("claim_token", ""))
                    )
                    result = evaluate(plan, readback)
                    result["round"] = round_index + 1
                    rows.append(result)
                except Exception as exc:
                    failures.append(
                        {
                            "round": str(round_index + 1),
                            "o_id": o_id,
                            "io_id": io_id,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                excluded.append({"o_id": o_id, "io_id": io_id})
        finally:
            for plan in claimed:
                o_id = str(plan.get("o_id", ""))
                io_id = str(plan.get("io_id", ""))
                try:
                    client.release(o_id, io_id, str(plan.get("claim_token", "")))
                    released.append({"o_id": o_id, "io_id": io_id})
                except Exception as exc:
                    release_failures.append(
                        {
                            "o_id": o_id,
                            "io_id": io_id,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )

    decisions = Counter(row["decision"] for row in rows)
    status_counts = Counter(row["status"] for row in rows)
    return {
        "mode": "PRODUCTION_HTTPS_READ_ONLY_SHADOW",
        "print_profile": print_profile,
        "workstation_id": workstation_id,
        "rounds_requested": rounds,
        "orders_inspected": len(rows),
        "decisions": dict(sorted(decisions.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "privacy_orders": sum(bool(row["privacy_required"]) for row in rows),
        "orders": rows,
        "failures": failures,
        "leases_claimed": len(rows) + len(failures),
        "leases_released": len(released),
        "release_failures": release_failures,
        "last_plan_counts": last_counts,
        "blocked_reason_counts": blocked_reason_counts,
        "pass": not failures
        and not release_failures
        and len(released) == len(rows) + len(failures),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1, choices=range(1, 21))
    parser.add_argument(
        "--print-profile", required=True, choices=tuple(PRINT_PROFILE_CARRIERS)
    )
    parser.add_argument(
        "--confirm-no-active-production-leases",
        action="store_true",
        help="确认正式客户端已停止，且没有待恢复、暂停任务或活动租约",
    )
    args = parser.parse_args()
    report = run(
        args.rounds,
        args.print_profile,
        confirm_no_active_production_leases=(
            args.confirm_no_active_production_leases
        ),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
