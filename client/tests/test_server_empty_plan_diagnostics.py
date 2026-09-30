import sys
import unittest
from pathlib import Path
from unittest import mock


SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER_DIR))
try:
    import jst_print_api_server as api
finally:
    sys.path.remove(str(SERVER_DIR))


def _candidate() -> dict:
    return {
        "o_id": "101",
        "io_id": "201",
        "current_carrier_id": api.TARGET_CARRIER_ID,
        "current_carrier": api.PRINT_PROFILE_CARRIERS[api.TARGET_CARRIER_ID],
        "steps": ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
        "items": [{"product_id": "PRODUCT-1"}],
    }


def _raw() -> dict:
    return {
        "generated_at": "2026-08-26T00:00:00Z",
        "scope": {},
        "counts": {
            "orders_read": 1,
            "in_scope": 1,
            "ready": 1,
            "blocked": 0,
            "selected": 1,
        },
        "selected": [_candidate()],
        "blocked_preview": [],
        "blocked_reason_counts": {},
        "guardrails": [],
    }


class _Store:
    def __init__(self, claim_result=None):
        self.claim_result = claim_result
        self.claimed = []

    def active_for_workstation(self, _workstation_id):
        return []

    def claim(self, o_id, io_id, workstation_id):
        self.claimed.append((o_id, io_id, workstation_id))
        return self.claim_result


class EmptyPlanDiagnosticTests(unittest.TestCase):
    def test_backend_rejects_old_or_unidentified_client_user_agents(self):
        self.assertFalse(api.supported_client_user_agent("JSTAutoPrint/0.5.18"))
        self.assertFalse(api.supported_client_user_agent("JSTAutoPrint/0.5.19"))
        self.assertFalse(api.supported_client_user_agent("JSTAutoPrint/0.5.20"))
        self.assertFalse(api.supported_client_user_agent("Python-urllib/3.10"))
        self.assertTrue(api.supported_client_user_agent("JSTAutoPrint/0.5.21"))
        self.assertTrue(api.supported_client_user_agent("JSTAutoPrint/0.6.0"))

    def test_ping_advertises_minimum_client_version(self):
        result = api.process_request("/jst-print-api/v1/ping", {})

        self.assertEqual(result["minimum_client_version"], api.MIN_CLIENT_VERSION)

    def test_reports_eligible_pair_hidden_by_local_exclude(self):
        store = _Store()
        with mock.patch.object(api, "get_plan_pool", return_value=_raw()):
            result = api._plan(
                {
                    "workstation_id": "workstation-123",
                    "exclude": [{"o_id": "101", "io_id": "201"}],
                    "max_candidates": 10,
                    "print_profile": api.TARGET_CARRIER_ID,
                },
                store,
            )

        self.assertEqual(result["selected"], [])
        self.assertEqual(result["counts"]["profile_ready"], 1)
        self.assertEqual(result["counts"]["profile_excluded"], 1)
        self.assertEqual(result["counts"]["claim_attempted"], 0)
        self.assertEqual(result["counts"]["claim_unavailable"], 0)
        self.assertEqual(store.claimed, [])

    def test_reports_eligible_pair_unavailable_at_claim_time(self):
        store = _Store(claim_result=None)
        with mock.patch.object(api, "get_plan_pool", return_value=_raw()):
            result = api._plan(
                {
                    "workstation_id": "workstation-123",
                    "exclude": [],
                    "max_candidates": 10,
                    "print_profile": api.TARGET_CARRIER_ID,
                },
                store,
            )

        self.assertEqual(result["selected"], [])
        self.assertEqual(result["counts"]["profile_ready"], 1)
        self.assertEqual(result["counts"]["profile_excluded"], 0)
        self.assertEqual(result["counts"]["claim_attempted"], 1)
        self.assertEqual(result["counts"]["claim_unavailable"], 1)


if __name__ == "__main__":
    unittest.main()
