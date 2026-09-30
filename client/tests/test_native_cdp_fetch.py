import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import jst_auto_print_app as app


def _native_browser(**attributes):
    browser = object.__new__(app.JSTNativeBrowser)
    browser._lock = threading.RLock()
    for name, value in attributes.items():
        setattr(browser, name, value)
    return browser


def _row(o_id="6789001", io_id="13549001", **changes):
    row = {
        "o_id": o_id,
        "io_id": io_id,
        "wms_co_id": app.TARGET_WAREHOUSE_ID,
        "shop_id": "10",
        "lc_id": "20",
        "l_id": "",
        "__KeyData": json.dumps({"io_id": io_id}),
    }
    row.update(changes)
    return row


class NativeCdpFetchTests(unittest.TestCase):
    def test_production_alias_is_native_and_has_no_dom_action_source(self):
        self.assertIs(app.JSTBrowser, app.JSTNativeBrowser)
        self.assertNotIn("playwright", app.JSTNativeBrowser.__module__.lower())
        for method in (
            app.JSTNativeBrowser.get_waybill,
            app.JSTNativeBrowser.reset_carrier,
            app.JSTNativeBrowser.print_express,
        ):
            source = __import__("inspect").getsource(method)
            self.assertNotIn(".locator(", source)
            self.assertNotIn(".click(", source)
            self.assertNotIn("query_selector", source)

    def test_existing_login_page_opener_closes_its_probe_connection(self):
        settings = app.Settings(
            api_url="https://example.com/jst-print-api/v1",
            api_token="a" * 32,
        )
        browser = mock.Mock()
        browser.is_healthy.return_value = True
        with mock.patch.object(app, "JSTNativeBrowser", return_value=browser):
            app.open_jst_print_page(settings)
        browser.is_healthy.assert_called_once_with()
        browser.close.assert_called_once_with()

        browser.reset_mock()
        browser.is_healthy.return_value = False
        with mock.patch.object(app, "JSTNativeBrowser", return_value=browser):
            with self.assertRaisesRegex(RuntimeError, "健康检查失败"):
                app.open_jst_print_page(settings)
        browser.close.assert_called_once_with()

    def test_desktop_startup_schedules_existing_browser_navigation(self):
        source = __import__("inspect").getsource(app.DesktopApp.__init__)
        self.assertIn("self.root.after(800, self._open_browser)", source)
        opener_source = __import__("inspect").getsource(app.DesktopApp._open_browser)
        self.assertIn("launch_browser(settings)", opener_source)
        self.assertIn("open_jst_print_page(settings)", opener_source)
        self.assertNotIn("JST_HOME_URL, shell=True", opener_source)

    def test_browser_launch_uses_dedicated_profile_without_debugging_ui(self):
        settings = app.Settings(
            api_url="https://example.com/jst-print-api/v1",
            api_token="a" * 32,
            browser_name="Chrome",
        )
        process = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            profile_root = Path(directory) / "profiles"
            with (
                mock.patch.object(app, "PROFILE_ROOT", profile_root),
                mock.patch.object(
                    app,
                    "cdp_endpoint",
                    side_effect=[RuntimeError("not ready"), "ws://ready"],
                ),
                mock.patch.object(
                    app, "browser_executable", return_value=r"C:\Chrome\chrome.exe"
                ),
                mock.patch.object(app.platform, "system", return_value=app.platform.system()),
                mock.patch.object(app.subprocess, "Popen", return_value=process) as popen,
                mock.patch.object(app, "_tighten_private_permissions"),
                mock.patch.object(app.time, "sleep"),
            ):
                result = app.launch_browser(settings)

        self.assertIs(result, process)
        popen.assert_called_once()
        jst_args = popen.call_args.args[0]
        self.assertEqual(jst_args[0], r"C:\Chrome\chrome.exe")
        self.assertIn("--remote-debugging-port=9222", jst_args)
        self.assertIn("--remote-debugging-address=127.0.0.1", jst_args)
        self.assertIn(f"--user-data-dir={(profile_root / 'chrome').resolve()}", jst_args)
        self.assertIn("--no-first-run", jst_args)
        self.assertIn("--no-default-browser-check", jst_args)
        self.assertEqual(jst_args[-1], app.JST_HOME_URL)
        self.assertNotIn("chrome://inspect/#remote-debugging", jst_args)

    def test_page_helper_is_fetch_only_and_origin_path_restricted(self):
        source = app._JST_NATIVE_HELPER_JS
        self.assertIn("await fetch(url.href", source)
        self.assertIn("credentials: 'same-origin'", source)
        self.assertIn("url.origin !== location.origin", source)
        self.assertIn("'/app/wms/express/expresssetter.aspx'", source)
        self.assertIn("'/app/wms/express/setelids.aspx'", source)
        self.assertNotIn("querySelector", source)
        self.assertNotIn(".click()", source)

    def test_action_whitelist_excludes_shipping_and_rejects_unknown_method(self):
        lowered = {name.lower() for name in app._NATIVE_ACTIONS}
        self.assertTrue(lowered.isdisjoint({"sendby", "preship", "shipping"}))
        browser = _native_browser()
        browser._evaluate = mock.Mock()
        with self.assertRaises(app.SafetyStop):
            browser._call_page("SendBy", ["13549001"])
        browser._evaluate.assert_not_called()

    def test_call_page_accepts_only_clean_success_envelope(self):
        browser = _native_browser()
        browser._evaluate = mock.Mock(
            return_value={
                "status": 200,
                "envelope": {
                    "IsSuccess": True,
                    "GotoLogin": False,
                    "Message": "",
                    "ExceptionMessage": "",
                    "ClientScript": "",
                    "IsReloadPage": False,
                    "LocationUrl": "",
                    "OpenUrl": "",
                    "ReturnValue": "ok",
                },
            }
        )
        self.assertEqual(browser._call_page("CheckAndLoadLCId", ["1"]), "ok")
        options_source = browser._evaluate.call_args.args[0]
        self.assertIn('"method":"CheckAndLoadLCId"', options_source)

        browser._evaluate.return_value["envelope"]["GotoLogin"] = True
        with self.assertRaisesRegex(app.SafetyStop, "登录态已失效"):
            browser._call_page("CheckAndLoadLCId", ["1"])

    def test_stale_page_auto_refreshes_one_read_but_never_replays_write(self):
        failed = {
            "status": 200,
            "envelope": {
                "IsSuccess": False,
                "GotoLogin": False,
                "ExceptionMessage": "ErrorCode:120009 系统功能已升级",
            },
        }
        succeeded = {
            "status": 200,
            "envelope": {
                "IsSuccess": True,
                "GotoLogin": False,
                "Message": "",
                "ClientScript": "",
                "IsReloadPage": False,
                "LocationUrl": "",
                "OpenUrl": "",
                "ReturnValue": "fresh",
            },
        }
        browser = _native_browser()
        browser._evaluate = mock.Mock(side_effect=[failed, succeeded])
        browser.recover_print_page = mock.Mock()
        self.assertEqual(browser._call_page("LoadDataToJSON", ["1", "[]", "{}"]), "fresh")
        browser.recover_print_page.assert_called_once_with(replace_page=False)
        self.assertEqual(browser._evaluate.call_count, 2)

        browser._evaluate.reset_mock(side_effect=True)
        browser._evaluate.return_value = failed
        browser.recover_print_page.reset_mock()
        with self.assertRaisesRegex(app.SafetyStop, "120009"):
            browser._call_page("SetElids", ["key"])
        browser.recover_print_page.assert_not_called()
        browser._evaluate.assert_called_once()

    def test_two_stale_read_failures_escalate_to_same_context_new_page(self):
        failed = {
            "status": 200,
            "envelope": {
                "IsSuccess": False,
                "GotoLogin": False,
                "ExceptionMessage": "ErrorCode:120009 系统功能已升级",
            },
        }
        succeeded = {
            "status": 200,
            "envelope": {
                "IsSuccess": True,
                "GotoLogin": False,
                "Message": "",
                "ClientScript": "",
                "IsReloadPage": False,
                "LocationUrl": "",
                "OpenUrl": "",
                "ReturnValue": "fresh-new-page",
            },
        }
        browser = _native_browser()
        browser._evaluate = mock.Mock(side_effect=[failed, failed, succeeded])
        browser.recover_print_page = mock.Mock()
        self.assertEqual(
            browser._call_page("LoadDataToJSON", ["1", "[]", "{}"]),
            "fresh-new-page",
        )
        self.assertEqual(
            browser.recover_print_page.call_args_list,
            [mock.call(replace_page=False), mock.call(replace_page=True)],
        )

    def test_new_target_uses_default_context_without_switching_profile(self):
        browser = _native_browser()
        browser._connection = mock.Mock()
        browser._connection.call.return_value = {"targetId": "new-target"}
        targets = [
            {
                "type": "page",
                "targetId": "existing",
                "url": "https://www.erp321.com/epaas?n=订单",
            }
        ]
        self.assertEqual(browser._create_print_target(targets), "new-target")
        browser._connection.call.assert_called_once_with(
            "Target.createTarget", {"url": app.JST_HOME_URL}
        )

    def test_exact_rows_requires_complete_exact_pair_and_warehouse(self):
        expected = [("6789001", "13549001")]
        browser = _native_browser()
        browser._call_page = mock.Mock(
            return_value=json.dumps(
                {"dp": {"DataCount": 1}, "datas": [_row()]},
                ensure_ascii=False,
            )
        )
        self.assertEqual(browser._exact_rows(expected)[0]["io_id"], "13549001")
        args = browser._call_page.call_args.args
        self.assertEqual(args[0], "LoadDataToJSON")
        filters = json.loads(args[1][1])
        self.assertEqual(filters, [{"k": "o_id", "v": "6789001", "c": "@="}])
        self.assertIsNone(browser._call_page.call_args.kwargs["call_control"])

        browser._call_page.return_value = json.dumps(
            {
                "dp": {"DataCount": 2},
                "datas": [_row(), _row("6789999", "13549999")],
            }
        )
        with self.assertRaisesRegex(app.SafetyStop, "额外出库身份"):
            browser._exact_rows(expected)

        browser._call_page.return_value = json.dumps(
            {"dp": {"DataCount": 1}, "datas": [_row(wms_co_id="foreign")]}
        )
        with self.assertRaisesRegex(app.SafetyStop, "不属于目标仓库"):
            browser._exact_rows(expected)

    def test_exact_rows_refuses_incomplete_page_and_missing_key_data(self):
        browser = _native_browser()
        browser._call_page = mock.Mock(
            return_value=json.dumps({"dp": {"DataCount": 2}, "datas": [_row()]})
        )
        with self.assertRaises(app.BatchSearchUnsupported):
            browser._exact_rows([("6789001", "13549001")])

        browser._call_page.return_value = json.dumps(
            {"dp": {"DataCount": 1}, "datas": [_row(__KeyData="")]}
        )
        with self.assertRaisesRegex(app.SafetyStop, "__KeyData"):
            browser._exact_rows([("6789001", "13549001")])

    def test_waybill_guard_order_and_no_mark_before_final_guard(self):
        row = _row()
        browser = _native_browser()
        browser._exact_rows = mock.Mock(return_value=[row])
        browser._preflight_waybill_rows = mock.Mock()
        events = []
        def submit(_rows, before_submit=None):
            if before_submit is not None:
                before_submit()
            events.append("submit")

        browser._submit_waybill = submit
        browser.get_waybill(
            "6789001",
            "13549001",
            True,
            before_click=lambda: events.append("before"),
            final_guard=lambda: events.append("guard"),
            mark_running=lambda: events.append("mark"),
        )
        self.assertEqual(events, ["before", "guard", "mark", "submit"])
        self.assertEqual(browser._exact_rows.call_count, 3)

        events.clear()
        browser._exact_rows.reset_mock()
        with self.assertRaisesRegex(app.SafetyStop, "changed"):
            browser.get_waybill(
                "6789001",
                "13549001",
                True,
                final_guard=lambda: (_ for _ in ()).throw(app.SafetyStop("changed")),
                mark_running=lambda: events.append("mark"),
            )
        self.assertEqual(events, [])

    def test_waybill_new_dialog_uses_acall1_and_validates_every_key(self):
        rows = [_row(), _row("6789002", "13549002")]
        browser = _native_browser()
        browser._evaluate = mock.Mock(return_value=True)
        keys = [row["__KeyData"] for row in rows]
        browser._call_page = mock.Mock(
            return_value="L:" + json.dumps({keys[0]: "L001", keys[1]: "L002"})
        )
        browser._submit_waybill(rows)
        _, args = browser._call_page.call_args.args
        kwargs = browser._call_page.call_args.kwargs
        self.assertEqual(browser._call_page.call_args.args[0], "SetElidsForExpressSetter")
        self.assertEqual(args, [",".join(keys)])
        self.assertEqual(kwargs["callback_id"], "ACall1")
        self.assertEqual(kwargs["url"], "SetELids.aspx")
        self.assertTrue(kwargs["inherit_search"])

        browser._call_page.return_value = "L:" + json.dumps({keys[0]: "L001"})
        with self.assertRaisesRegex(app.SafetyStop, "未完整返回"):
            browser._submit_waybill(rows)

    def test_reset_payload_and_ack_are_exact(self):
        rows = [_row(), _row("6789002", "13549002")]
        browser = _native_browser()
        browser._call_page = mock.Mock(
            return_value={
                "Ios": [{"io_id": "13549001"}, {"io_id": "13549002"}],
                "Message": "",
            }
        )
        browser._submit_reset(
            rows, app.TARGET_CARRIER_ID, app.TARGET_CARRIER_NAME
        )
        self.assertEqual(
            browser._call_page.call_args.args[0], "SetLogisticsCompaniesV2New"
        )
        payload = json.loads(browser._call_page.call_args.args[1][0])
        self.assertEqual(
            payload["lcInfo"],
            f"{app.TARGET_CARRIER_ID},{app.TARGET_CARRIER_NAME}",
        )
        self.assertEqual(payload["oids"], "6789001,6789002")
        self.assertEqual(
            set(payload), {"lcInfo", "keyDatas", "oids", "LcType"}
        )
        self.assertEqual(payload["LcType"], 1)

        browser._call_page.return_value = {
            "Ios": [{"io_id": "13549001"}],
            "Message": "",
        }
        with self.assertRaisesRegex(app.SafetyStop, "未完整回执"):
            browser._submit_reset(
                rows, app.TARGET_CARRIER_ID, app.TARGET_CARRIER_NAME
            )

        with self.assertRaisesRegex(app.SafetyStop, "安全白名单"):
            browser._submit_reset(rows, "33", "carrier")

    def test_print_uses_live_page_module_and_rejects_callback_mismatch(self):
        rows = [_row(l_id="L001")]
        browser = _native_browser()
        browser._print_preflight = mock.Mock()
        browser._call_page = mock.Mock(return_value="77")
        browser._evaluate = mock.Mock(
            side_effect=["expressSetter-live", {"state": "SUCCESS", "callbackMatchesExpected": True}]
        )
        browser._submit_print(rows)
        expression = browser._evaluate.call_args_list[1].args[0]
        self.assertIn('"moudle":"expressSetter-live"', expression)

        browser._evaluate.side_effect = [
            "expressSetter-live",
            {"state": "SUCCESS", "callbackMatchesExpected": False},
        ]
        with self.assertRaisesRegex(app.SafetyStop, "集合与安全批次不一致"):
            browser._submit_print(rows)

    def test_raw_cdp_connection_is_created_once_and_reused(self):
        class Socket:
            def __init__(self):
                self.last = None
                self.closed = False

            def settimeout(self, _value):
                pass

            def send(self, value):
                self.last = json.loads(value)

            def recv(self):
                return json.dumps(
                    {"id": self.last["id"], "result": {"method": self.last["method"]}}
                )

            def close(self):
                self.closed = True

        socket = Socket()
        module = types.ModuleType("websocket")
        module.create_connection = mock.Mock(return_value=socket)
        with mock.patch.dict(sys.modules, {"websocket": module}):
            connection = app._NativeCDPConnection("ws://127.0.0.1:9222/devtools/browser/x")
            self.assertEqual(connection.call("Browser.getVersion")["method"], "Browser.getVersion")
            self.assertEqual(connection.call("Target.getTargets")["method"], "Target.getTargets")
            connection.close()
        module.create_connection.assert_called_once_with(
            "ws://127.0.0.1:9222/devtools/browser/x",
            timeout=45,
            suppress_origin=True,
        )
        self.assertTrue(socket.closed)

    def test_native_browser_close_releases_the_single_connection(self):
        browser = _native_browser(_connection=mock.Mock())
        browser.close()
        browser._connection.close.assert_called_once_with()

    def test_context_resolution_discards_stale_runtime_snapshots(self):
        frame_id = "frame-express"

        class Connection:
            def __init__(self):
                self.events = [
                    {
                        "params": {
                            "context": {
                                "id": 7,
                                "auxData": {"frameId": frame_id, "isDefault": True},
                            }
                        }
                    }
                ]
                self.clears = 0

            def take_events(self, method, session_id):
                self.clears += 1
                events, self.events = self.events, []
                return events

            def call(self, method, params=None, *, session_id=None, timeout=45):
                if method == "Runtime.enable":
                    self.events.append(
                        {
                            "params": {
                                "context": {
                                    "id": 8,
                                    "auxData": {
                                        "frameId": frame_id,
                                        "isDefault": True,
                                    },
                                }
                            }
                        }
                    )
                if method == "Page.getFrameTree":
                    return {
                        "frameTree": {
                            "frame": {"id": "root", "url": app.JST_HOME_URL},
                            "childFrames": [
                                {
                                    "frame": {
                                        "id": frame_id,
                                        "url": "https://www.erp321.com/app/wms/express/expresssetter.aspx?_c=jst-epaas",
                                    }
                                }
                            ],
                        }
                    }
                return {}

        connection = Connection()
        browser = _native_browser(
            _connection=connection,
            _session_id="session-1",
            _context_id=None,
            _frame_id=None,
        )
        self.assertEqual(browser._resolve_express_context(), 8)
        self.assertGreaterEqual(connection.clears, 3)


if __name__ == "__main__":
    unittest.main()
