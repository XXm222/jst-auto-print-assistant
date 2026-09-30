from contextlib import closing
import csv
import inspect
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import jst_auto_print_app as app


def _settings() -> app.Settings:
    return app.Settings(
        browser_name="Chrome",
        debug_port=9222,
        api_url="https://api.example.test/jst-print-api/v1",
        api_token="a" * 32,
        loop_seconds=5,
        allow_write=True,
        allow_print=True,
        print_profile=app.TARGET_CARRIER_ID,
    )


def _candidate(o_id: str, io_id: str, token: str) -> dict:
    return {
        "o_id": o_id,
        "io_id": io_id,
        "outbound_identity_unique": True,
        "weight_kg": 2.0,
        "current_carrier": app.TARGET_CARRIER_NAME,
        "current_carrier_id": app.TARGET_CARRIER_ID,
        "has_waybill": False,
        "privacy_required": False,
        "delivery_hold_marked": False,
        "items_complete": True,
        "source_item_count": 1,
        "items": [
            {
                "line_key": f"line-{o_id}-{io_id}",
                "product_id": "P1",
                "sku_id": "S1",
                "sku_name": "测试商品",
                "qty": 1.0,
                "unit": "件",
            }
        ],
        "state": "READY_HYBRID",
        "steps": ["GET_WAYBILL", "PRINT_EXPRESS", "STOP_BEFORE_PRESHIP"],
        "blockers": [],
        "claim_token": token,
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(),
        "lease_ttl_seconds": 300,
    }


def _plan_response(selected: list[dict]) -> dict:
    count = len(selected)
    return {
        "mode": "LIVE_READ_ONLY_CLAIMED_V1",
        "api_schema_version": app.API_SCHEMA_VERSION,
        "planner_schema_version": app.PLANNER_SCHEMA_VERSION,
        "lease_required": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "print_profile": app.TARGET_CARRIER_ID,
        "selected": selected,
        "blocked_preview": [],
        "counts": {
            "orders_read": count,
            "in_scope": count,
            "ready": count,
            "blocked": 0,
            "profile_ready": count,
            "selected": count,
        },
    }


class _Response:
    def __init__(self, url: str, body: bytes):
        self.url = url
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, *_args):
        return self.body


class _Opener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Combo:
    def __init__(self):
        self.states = []

    def configure(self, **kwargs):
        self.states.append(kwargs.get("state"))


class ClientSecurityBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This historical suite exercises the retired DOM adapter. Native CDP
        # and fetch boundaries have their own production-path test module.
        cls._legacy_browser_patch = mock.patch.object(
            app, "JSTBrowser", app._LegacyPlaywrightJSTBrowser
        )
        cls._legacy_browser_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._legacy_browser_patch.stop()

    def test_protocol_version_is_schema_five_client_0525(self):
        self.assertEqual(app.APP_VERSION, "0.5.25")
        self.assertEqual(app.API_SCHEMA_VERSION, 5)

    def test_ping_pins_single_workstation_binding_and_completion_reasons(self):
        client = object.__new__(app.PlannerClient)
        client.settings = app.Settings()
        baseline = {
            "ok": True,
            "api_schema_version": app.API_SCHEMA_VERSION,
            "minimum_client_version": app.APP_VERSION,
            "lease_required": True,
            "workstation_binding": app.WORKSTATION_BINDING_MODE,
            "completion_reasons": sorted(app.COMPLETION_REASONS),
            "plan_mode": "LIVE_READ_ONLY_CLAIMED_V1",
            "inspect_mode": "ORDER_READBACK_CLAIMED_V1",
            "batch_inspect_mode": "ORDER_BATCH_READBACK_CLAIMED_V1",
        }
        client._post = lambda *_args, **_kwargs: dict(baseline)
        self.assertTrue(client.ping())

        for changed in (
            {"workstation_binding": "CLIENT_SUPPLIED"},
            {"completion_reasons": ["PRINTED", "TERMINAL"]},
            {"completion_reasons": sorted(app.COMPLETION_REASONS) + ["UNKNOWN"]},
        ):
            with self.subTest(changed=changed):
                client._post = lambda *_args, changed=changed, **_kwargs: dict(
                    baseline, **changed
                )
                with self.assertRaises(app.BackendSchemaError):
                    client.ping()

    def test_jst_urls_require_https_real_domain_and_exact_paths(self):
        for url in (
            "https://erp321.com/epaas",
            "https://www.erp321.com/epaas?n=x",
            "https://sub.www.erp321.com/epaas#menu",
        ):
            with self.subTest(url=url):
                self.assertTrue(app._is_jst_https_url(url))
        for url in (
            "http://www.erp321.com/epaas",
            "https://erp321.com.evil.test/epaas",
            "https://evil-erp321.com/epaas",
            "https://erp321.com@evil.test/epaas",
            "javascript:https://www.erp321.com/epaas",
        ):
            with self.subTest(url=url):
                self.assertFalse(app._is_jst_https_url(url))

        self.assertTrue(
            app._is_jst_frame_path("https://www.erp321.com/epaas?n=x", "page")
        )
        self.assertFalse(
            app._is_jst_frame_path("https://www.erp321.com/not-epaas", "page")
        )
        self.assertTrue(
            app._is_jst_frame_path(
                "https://www.erp321.com/app/ExpressSetter.aspx?x=1", "express"
            )
        )
        self.assertFalse(
            app._is_jst_frame_path(
                "https://www.erp321.com/app/evil-expresssetter.aspx", "express"
            )
        )

    def test_api_rejects_redirect_and_never_surfaces_raw_http_body(self):
        client = app.PlannerClient(
            _settings(), "ws-00000000-0000-0000-0000-000000000000"
        )
        url = "https://api.example.test/jst-print-api/v1/ping"
        redirect = urllib.error.HTTPError(
            url, 302, "redirect", {"Location": "https://evil.test"}, io.BytesIO(b"SECRET")
        )
        client._opener = _Opener(redirect)
        with self.assertRaisesRegex(RuntimeError, "重定向") as raised:
            client._post("ping", attempts=1)
        self.assertNotIn("SECRET", str(raised.exception))

        rejected = urllib.error.HTTPError(
            url, 400, "bad", {}, io.BytesIO(b"RAW_ORDER_AND_TOKEN_SECRET")
        )
        client._opener = _Opener(rejected)
        with self.assertRaises(RuntimeError) as raised:
            client._post("ping", attempts=1)
        self.assertNotIn("RAW_ORDER_AND_TOKEN_SECRET", str(raised.exception))

    def test_http_409_distinguishes_lease_loss_from_failed_completion_proof(self):
        client = app.PlannerClient(
            _settings(), "ws-00000000-0000-0000-0000-000000000000"
        )
        url = "https://api.example.test/jst-print-api/v1/lease/complete"
        cases = (
            (b'{"error":"lease_conflict","detail":"SECRET"}', app.LeaseLostError),
            (
                b'{"error":"completion_proof_failed","detail":"SECRET"}',
                app.CompletionProofError,
            ),
            (
                b'{"error":"unknown_conflict","detail":"SECRET"}',
                app.CompletionProofError,
            ),
            (b'not-json SECRET', app.CompletionProofError),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                rejected = urllib.error.HTTPError(
                    url, 409, "conflict", {}, io.BytesIO(body)
                )
                client._opener = _Opener(rejected)
                with self.assertRaises(expected) as raised:
                    client._post("lease/complete", attempts=1)
                self.assertNotIn("SECRET", str(raised.exception))

    def test_plan_does_not_inline_retry_an_expensive_full_refresh(self):
        client = object.__new__(app.PlannerClient)
        client.settings = app.Settings()
        client.workstation_id = "ws-00000000-0000-0000-0000-000000000000"
        captured = {}

        def post(_endpoint, _payload, **kwargs):
            captured.update(kwargs)
            return _plan_response([])

        client._post = post
        client.plan([], app.TARGET_CARRIER_ID)
        self.assertEqual(captured.get("attempts"), 1)

    def test_ship_action_without_terminal_status_is_paused_not_completed(self):
        engine = object.__new__(app.AutomationEngine)
        planner = mock.Mock()
        job = {
            "step_index": 0,
            "plan": {
                "o_id": "101",
                "io_id": "201",
                "claim_token": "a" * 32,
            },
        }
        readback = {
            "found": True,
            "status": "WaitConfirm",
            "has_ship_action": True,
            "action_history_complete": True,
        }
        with self.assertRaises(app.CompletionProofError):
            engine._skip_shipped_job(planner, job, readback)
        planner.complete.assert_not_called()

    def test_api_rejects_changed_final_url_even_on_success(self):
        client = app.PlannerClient(
            _settings(), "ws-00000000-0000-0000-0000-000000000000"
        )
        client._opener = _Opener(_Response("https://evil.test/result", b"{}"))
        with self.assertRaisesRegex(app.BackendSchemaError, "重定向或跨源"):
            client._post("ping", attempts=1)

    def test_cdp_accepts_only_loopback_websocket_and_normalizes_host(self):
        body = b'{"webSocketDebuggerUrl":"ws://localhost:9222/devtools/browser/abc_DEF-1"}'
        opener = _Opener(_Response("http://127.0.0.1:9222/json/version", body))
        with mock.patch.object(app.urllib.request, "build_opener", return_value=opener), mock.patch.object(
            app, "_devtools_active_port_paths", return_value=[Path("/definitely/missing")]
        ):
            self.assertEqual(
                app.cdp_endpoint(9222),
                "ws://127.0.0.1:9222/devtools/browser/abc_DEF-1",
            )

        bad_body = b'{"webSocketDebuggerUrl":"ws://evil.test:9222/devtools/browser/abc"}'
        bad_opener = _Opener(
            _Response("http://127.0.0.1:9222/json/version", bad_body)
        )
        with mock.patch.object(app.urllib.request, "build_opener", return_value=bad_opener), mock.patch.object(
            app, "_devtools_active_port_paths", return_value=[Path("/definitely/missing")]
        ):
            with self.assertRaisesRegex(RuntimeError, "不是当前本机"):
                app.cdp_endpoint(9222)

    def test_active_port_file_is_rejected_when_listener_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_port = Path(tmp) / "DevToolsActivePort"
            active_port.write_text(
                "9222\n/devtools/browser/abc_DEF-1\n", encoding="utf-8"
            )
            with mock.patch.object(
                app, "_devtools_active_port_paths", return_value=[active_port]
            ), mock.patch.object(
                app.socket,
                "create_connection",
                side_effect=ConnectionRefusedError("stale"),
            ):
                self.assertIsNone(app._active_port_websocket(9222, "Chrome"))

    def test_active_port_file_is_accepted_when_listener_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_port = Path(tmp) / "DevToolsActivePort"
            active_port.write_text(
                "9222\n/devtools/browser/abc_DEF-1\n", encoding="utf-8"
            )
            connection = mock.MagicMock()
            connection.__enter__.return_value = connection
            with mock.patch.object(
                app, "_devtools_active_port_paths", return_value=[active_port]
            ), mock.patch.object(
                app.socket, "create_connection", return_value=connection
            ):
                self.assertEqual(
                    app._active_port_websocket(9222, "Chrome"),
                    "ws://127.0.0.1:9222/devtools/browser/abc_DEF-1",
                )

    def test_active_port_file_accepts_chrome_randomized_permission_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_port = Path(tmp) / "DevToolsActivePort"
            active_port.write_text(
                "49321\n/devtools/browser/randomized_ABC-1\n", encoding="utf-8"
            )
            connection = mock.MagicMock()
            connection.__enter__.return_value = connection
            with mock.patch.object(
                app, "_devtools_active_port_paths", return_value=[active_port]
            ), mock.patch.object(
                app.socket, "create_connection", return_value=connection
            ) as connect:
                self.assertEqual(
                    app._active_port_websocket(9222, "Chrome"),
                    "ws://127.0.0.1:49321/devtools/browser/randomized_ABC-1",
                )
            connect.assert_called_once_with(("127.0.0.1", 49321), timeout=0.5)

    def test_plan_rejects_duplicate_order_or_token_before_return(self):
        client = object.__new__(app.PlannerClient)
        client.settings = app.Settings()
        client.workstation_id = "ws-00000000-0000-0000-0000-000000000000"
        cases = (
            [
                _candidate("101", "201", "a" * 32),
                _candidate("101", "202", "b" * 32),
            ],
            [
                _candidate("101", "201", "a" * 32),
                _candidate("102", "201", "b" * 32),
            ],
            [
                _candidate("101", "201", "a" * 32),
                _candidate("102", "202", "a" * 32),
            ],
        )
        for selected in cases:
            with self.subTest(selected=selected):
                client._post = lambda *_args, selected=selected, **_kwargs: _plan_response(
                    selected
                )
                with self.assertRaisesRegex(app.BackendSchemaError, "重复"):
                    client.plan([], app.TARGET_CARRIER_ID)

    def test_blocked_preview_is_exact_bounded_and_non_injectable(self):
        valid = {
            "o_id": "101",
            "io_id": "201",
            "identity_complete": True,
            "weight_kg": 0.0,
            "items_complete": False,
            "blockers": ["已无可执行步骤"],
        }
        app.PlannerClient._validate_blocked_preview([valid])
        attacks = (
            dict(valid, buyer_phone="13800000000"),
            dict(valid, o_id="=WEBSERVICE(1)"),
            dict(valid, weight_kg=float("inf")),
            dict(valid, blockers=["bad\nforged-row"]),
            dict(valid, identity_complete=False),
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                with self.assertRaises(app.BackendSchemaError):
                    app.PlannerClient._validate_blocked_preview([attack])

    def test_order_ids_are_ascii_and_limited_to_twenty_digits(self):
        for valid in ("1", "0" * 20):
            self.assertTrue(app._is_ascii_order_id(valid))
        for invalid in ("", "1" * 21, "１２３", 123, True, "1\n"):
            self.assertFalse(app._is_ascii_order_id(invalid))

    def test_deployment_bearer_uses_backend_length_contract(self):
        for length in (32, 128):
            settings = _settings()
            settings.api_token = "a" * length
            settings.validate()
        for invalid in ("a" * 31, "a" * 129, "a" * 31 + "!"):
            settings = _settings()
            settings.api_token = invalid
            with self.assertRaisesRegex(ValueError, "API 凭证"):
                settings.validate()

    def test_legacy_active_job_with_short_token_is_retired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "events.sqlite3"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """CREATE TABLE jobs (
                    o_id TEXT NOT NULL, io_id TEXT NOT NULL,
                    plan_json TEXT NOT NULL, step_index INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL, pause_kind TEXT NOT NULL DEFAULT '',
                    pause_reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    PRIMARY KEY(o_id, io_id))"""
                )
                plan = {
                    "o_id": "101",
                    "io_id": "201",
                    "claim_token": "x" * 31,
                    "steps": [
                        "GET_WAYBILL",
                        "PRINT_EXPRESS",
                        "STOP_BEFORE_PRESHIP",
                    ],
                    "outbound_identity_unique": True,
                }
                connection.execute(
                    "INSERT INTO jobs VALUES (?, ?, ?, 0, 'PENDING', '', '', ?)",
                    ("101", "201", json.dumps(plan), datetime.now().isoformat()),
                )

            store = app.EventStore(database, root / "events.jsonl")
            self.assertEqual(
                store.get_job("101", "201")["status"],
                "SKIPPED_LEGACY_UNCLAIMED",
            )

    def test_inspect_freshness_is_30_seconds_but_plan_is_10_minutes(self):
        base = {
            "api_schema_version": app.API_SCHEMA_VERSION,
            "planner_schema_version": app.PLANNER_SCHEMA_VERSION,
            "lease_required": True,
        }
        plan = dict(
            base,
            mode="LIVE_READ_ONLY_CLAIMED_V1",
            generated_at=(datetime.now(timezone.utc) - timedelta(minutes=9)).isoformat(),
        )
        app.PlannerClient._validate_live_payload(plan, plan["mode"])

        inspect_payload = dict(
            base,
            mode="ORDER_READBACK_CLAIMED_V1",
            generated_at=(datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat(),
        )
        with self.assertRaisesRegex(app.BackendSchemaError, "30 秒"):
            app.PlannerClient._validate_live_payload(
                inspect_payload, inspect_payload["mode"]
            )

    def test_completion_reason_is_required_and_sent_on_wire(self):
        client = object.__new__(app.PlannerClient)
        client.settings = app.Settings()
        client.workstation_id = "ws-00000000-0000-0000-0000-000000000000"
        captured = []

        def post(endpoint, payload, **_kwargs):
            captured.append((endpoint, payload))
            return {
                "api_schema_version": app.API_SCHEMA_VERSION,
                "lease_required": True,
                "ok": True,
                "state": "COMPLETED",
                "o_id": "101",
                "io_id": "201",
                "claim_token": "a" * 32,
            }

        client._post = post
        client.complete("101", "201", "a" * 32, "PRINTED")
        self.assertEqual(captured[0][1]["completion_reason"], "PRINTED")
        with self.assertRaises(ValueError):
            client.complete("101", "201", "a" * 32, "UNKNOWN")

    def test_easyui_model_never_uses_executable_html_sink(self):
        source = inspect.getsource(app.JSTBrowser._easyui_result_indices)
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("DOMParser", source)

    def test_final_readback_and_second_dom_proof_share_action_lock(self):
        browser = object.__new__(app.JSTBrowser)
        lock = threading.RLock()
        calls = []
        selected = app.SelectedOrder(object(), None, "1", object(), o_id="101", io_id="201")

        class Button:
            def click(self):
                calls.append(("click", lock._is_owned()))

        browser.select_order = lambda *_args, **_kwargs: selected
        browser._verify_selected_order = lambda _selected: calls.append(
            ("verify", lock._is_owned())
        )
        browser._unique_button = lambda *_args: Button()
        browser._prove_button_actionable = lambda *_args: None
        browser._clear_selected_order = lambda *_args: None

        browser.get_waybill(
            "101",
            "201",
            before_click=lambda: calls.append(("readback", lock._is_owned())),
            mark_running=lambda: calls.append(("running", lock._is_owned())),
            action_lock=lock,
        )

        self.assertEqual([name for name, _owned in calls], [
            "verify", "readback", "verify", "running", "click"
        ])
        self.assertTrue(all(owned for _name, owned in calls))

    def test_start_immediately_locks_and_captures_both_selectors(self):
        desktop = object.__new__(app.DesktopApp)
        desktop.settings = _settings()
        desktop.no_print = False
        desktop.browser_var = _Var("Chrome")
        desktop.skip_external_orders_var = _Var(False)
        desktop.print_profile_var = _Var(app.PRINT_PROFILE_LABELS[app.TARGET_CARRIER_ID])
        desktop.status_var = _Var()
        desktop.browser_combo = _Combo()
        desktop.print_profile_combo = _Combo()
        desktop.skip_external_orders_check = _Combo()
        desktop._start_check_running = False
        desktop._pending_start_settings = None
        desktop.workstation_id = "ws-00000000-0000-0000-0000-000000000000"
        tasks = []
        desktop._run_async = tasks.append

        with mock.patch.object(app, "save_settings"), mock.patch.object(
            app.messagebox, "askyesno", return_value=True
        ):
            app.DesktopApp._start(desktop)

        self.assertEqual(desktop.skip_external_orders_check.states[-1], "disabled")
        self.assertFalse(desktop._pending_start_settings.skip_external_orders)
        self.assertEqual(desktop.browser_combo.states[-1], "disabled")
        self.assertEqual(desktop.print_profile_combo.states[-1], "disabled")
        self.assertEqual(desktop._pending_start_settings.browser_name, "Chrome")
        self.assertEqual(
            desktop._pending_start_settings.print_profile, app.TARGET_CARRIER_ID
        )
        self.assertEqual(len(tasks), 1)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not Windows ACLs")
    def test_private_file_permissions_are_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "secret.txt"
            target.write_text("secret", encoding="utf-8")
            os.chmod(target, 0o666)
            app._tighten_private_permissions(target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_csv_export_neutralizes_spreadsheet_formulas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = app.EventStore(root / "events.sqlite3", root / "events.jsonl")
            store.event("ERROR", "TEST", "=WEBSERVICE(\"https://evil.test\")")
            target = root / "events.csv"
            store.export_anomalies(target)
            with target.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertTrue(rows[1][-1].startswith("'="))


if __name__ == "__main__":
    unittest.main()
