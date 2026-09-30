import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jst_auto_print_app as app


def _job(index: int) -> dict:
    o_id = str(7000 + index)
    io_id = str(9000 + index)
    return {
        "o_id": o_id,
        "io_id": io_id,
        "status": "PENDING",
        "step_index": 0,
        "plan": {
            "o_id": o_id,
            "io_id": io_id,
            "claim_token": f"token-{index}-" + "x" * 24,
        },
    }


def _readback(job: dict, *, complete: bool) -> dict:
    return {
        "found": True,
        "o_id": str(job["o_id"]),
        "io_id": str(job["io_id"]),
        "status": "WaitConfirm",
        "has_ship_action": False,
        "has_print_action": complete,
    }


class BatchReadbackPerformanceTests(unittest.TestCase):
    def test_client_sends_ten_credentials_in_one_batch_request(self):
        credentials = [
            (str(7000 + index), str(9000 + index), f"token-{index}-" + "x" * 24)
            for index in range(10)
        ]
        calls = []
        client = object.__new__(app.PlannerClient)
        client.settings = app.Settings()
        client.workstation_id = "ws-performance-test"

        def post(endpoint, payload, **kwargs):
            calls.append((endpoint, payload, kwargs))
            return {
                "results": [
                    {"o_id": o_id, "io_id": io_id}
                    for o_id, io_id, _token in credentials
                ]
            }

        client._post = post
        client._validate_live_payload = lambda *_args: None
        client._validate_inspect_schema = lambda *_args: None

        results = client.inspect_batch(credentials)

        self.assertEqual(len(results), 10)
        self.assertEqual(len(calls), 1)
        endpoint, payload, kwargs = calls[0]
        self.assertEqual(endpoint, "inspect-batch")
        self.assertEqual(len(payload["orders"]), 10)
        self.assertEqual(kwargs, {"timeout": 42, "attempts": 1})

    def test_ten_order_preflight_uses_one_batch_inspect(self):
        jobs = [_job(index) for index in range(10)]

        class Planner:
            calls = []

            def inspect_batch(self, credentials):
                self.calls.append(list(credentials))
                return [_readback(job, complete=False) for job in jobs]

        engine = object.__new__(app.AutomationEngine)
        engine._require_operator_permission = lambda: None
        planner = Planner()

        results = engine._batch_preflight_readbacks(planner, jobs)

        self.assertEqual(len(results), 10)
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(len(planner.calls[0]), 10)

    def test_batch_polling_returns_after_first_partial_settlement(self):
        jobs = [_job(0), _job(1)]

        class Planner:
            calls = []

            def inspect_batch(self, credentials):
                self.calls.append(list(credentials))
                if len(self.calls) == 1:
                    return [
                        _readback(jobs[0], complete=True),
                        _readback(jobs[1], complete=False),
                    ]
                return [_readback(jobs[1], complete=True)]

        engine = object.__new__(app.AutomationEngine)
        planner = Planner()
        with mock.patch.object(app.time, "sleep") as sleep:
            resolved, error = engine._poll_batch_readbacks(
                planner,
                [
                    (job, lambda data: bool(data.get("has_print_action")))
                    for job in jobs
                ],
                "批量打印动作",
                attempts=4,
            )

        self.assertIsNone(error)
        self.assertEqual(set(resolved), {("7000", "9000")})
        self.assertEqual([len(call) for call in planner.calls], [2])
        sleep.assert_not_called()

    def test_resolved_order_is_settled_before_a_slow_sibling_is_polled_again(self):
        jobs = [_job(0), _job(1)]
        settled = []

        class Planner:
            calls = 0

            def inspect_batch(self, credentials):
                self.calls += 1
                if self.calls == 1:
                    return [
                        _readback(jobs[0], complete=True),
                        _readback(jobs[1], complete=False),
                    ]
                self.assert_first_was_settled()
                return [_readback(jobs[1], complete=True)]

            @staticmethod
            def assert_first_was_settled():
                if settled != [("7000", "9000")]:
                    raise AssertionError("resolved sibling was not settled in its first round")

        engine = object.__new__(app.AutomationEngine)
        planner = Planner()
        with mock.patch.object(app.time, "sleep"):
            resolved, error = engine._poll_batch_readbacks(
                planner,
                [
                    (job, lambda data: bool(data.get("has_print_action")))
                    for job in jobs
                ],
                "批量打印动作",
                attempts=3,
                on_resolved=lambda job, _readback: settled.append(
                    (str(job["o_id"]), str(job["io_id"]))
                ),
            )

        self.assertIsNone(error)
        self.assertEqual(set(resolved), {("7000", "9000")})
        self.assertEqual(settled, [("7000", "9000")])
        self.assertEqual(planner.calls, 1)

    def test_callback_failure_does_not_block_same_round_sibling_settlement(self):
        jobs = [_job(0), _job(1)]
        settled = []

        class Planner:
            def inspect_batch(self, _credentials):
                return [_readback(job, complete=True) for job in jobs]

        def settle(job, _readback):
            pair = (str(job["o_id"]), str(job["io_id"]))
            settled.append(pair)
            if pair == ("7000", "9000"):
                raise app.SafetyStop("第一单本地结算失败")

        engine = object.__new__(app.AutomationEngine)
        resolved, error = engine._poll_batch_readbacks(
            Planner(),
            [
                (job, lambda data: bool(data.get("has_print_action")))
                for job in jobs
            ],
            "批量打印动作",
            on_resolved=settle,
        )

        self.assertEqual(set(resolved), {("7000", "9000"), ("7001", "9001")})
        self.assertEqual(settled, [("7000", "9000"), ("7001", "9001")])
        self.assertIsInstance(error, app.SafetyStop)
        self.assertEqual(getattr(error, "_jst_job_identity"), ("7000", "9000"))

    def test_legacy_thirty_second_preference_migrates_to_fast_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = root / "deployment.json"
            local = root / "config.json"
            deployment.write_text(
                json.dumps(
                    {
                        "api_url": "https://example.com/jst-print-api/v1",
                        "api_token": "x" * 40,
                        "debug_port": 9222,
                        "loop_seconds": 5,
                    }
                ),
                encoding="utf-8",
            )
            local.write_text(
                json.dumps(
                    {
                        "browser_name": "Chrome",
                        "debug_port": 9222,
                        "loop_seconds": 30,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(app, "APP_DIR", root), mock.patch.object(
                app, "PROFILE_ROOT", root / "profiles"
            ), mock.patch.object(app, "CONFIG_FILE", local), mock.patch.object(
                app, "deployment_config_path", return_value=deployment
            ):
                settings = app.load_settings()

        self.assertEqual(settings.loop_seconds, 5)

    def test_legacy_custom_polling_preference_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = root / "deployment.json"
            local = root / "config.json"
            deployment.write_text(
                json.dumps(
                    {
                        "api_url": "https://example.com/jst-print-api/v1",
                        "api_token": "x" * 40,
                        "loop_seconds": 5,
                    }
                ),
                encoding="utf-8",
            )
            local.write_text(json.dumps({"loop_seconds": 60}), encoding="utf-8")
            with mock.patch.object(app, "APP_DIR", root), mock.patch.object(
                app, "PROFILE_ROOT", root / "profiles"
            ), mock.patch.object(app, "CONFIG_FILE", local), mock.patch.object(
                app, "deployment_config_path", return_value=deployment
            ):
                settings = app.load_settings()

        self.assertEqual(settings.loop_seconds, 60)

    def test_polling_preference_rejects_bool_string_and_float(self):
        for invalid in (True, "5", 5.0):
            with self.subTest(value=invalid):
                settings = app.Settings(
                    api_url="https://example.com/jst-print-api/v1",
                    api_token="x" * 40,
                    loop_seconds=invalid,
                )
                with self.assertRaisesRegex(ValueError, "整数秒"):
                    settings.validate()


if __name__ == "__main__":
    unittest.main()
