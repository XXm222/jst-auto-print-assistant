"""Failure injection at the native transport and real submission boundaries."""

import json
import sys
import types
import unittest
from unittest import mock

import jst_auto_print_app as app
from test_native_cdp_fetch import _native_browser, _row


class NativeStabilityTests(unittest.TestCase):
    def connection(self, socket):
        module = types.ModuleType("websocket")
        module.create_connection = mock.Mock(return_value=socket)
        with mock.patch.dict(sys.modules, {"websocket": module}):
            return app._NativeCDPConnection("ws://127.0.0.1:9222/devtools/browser/test")

    def test_continuous_events_cannot_extend_request_deadline(self):
        clock = [0.0]
        socket = mock.Mock()

        def recv():
            clock[0] += 1
            if clock[0] > 5:
                raise AssertionError("unbounded event loop")
            return json.dumps({"method": "Runtime.consoleAPICalled", "params": {}})

        socket.recv.side_effect = recv
        connection = self.connection(socket)
        with mock.patch.object(app.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(TimeoutError):
                connection.call("Runtime.evaluate", timeout=2)
        self.assertEqual(socket.recv.call_count, 2)
        self.assertEqual(socket.send.call_count, 1)
        self.assertEqual(socket.settimeout.call_args.args, (1.0,))

    def test_late_response_is_ignored_without_replaying_timed_out_request(self):
        socket = mock.Mock()
        socket.recv.side_effect = [
            TimeoutError("timeout"),
            json.dumps({"id": 1, "result": {"late": True}}),
            json.dumps({"id": 2, "result": {"ok": True}}),
        ]
        connection = self.connection(socket)
        with self.assertRaises(TimeoutError):
            connection.call("Runtime.evaluate")
        self.assertEqual(connection.call("Browser.getVersion"), {"ok": True})
        sent = [json.loads(call.args[0])["method"] for call in socket.send.call_args_list]
        self.assertEqual(sent, ["Runtime.evaluate", "Browser.getVersion"])

    def test_failed_health_probe_closes_socket(self):
        socket = mock.Mock()
        socket.recv.side_effect = TimeoutError("lost connection")
        connection = self.connection(socket)
        with self.assertRaises(TimeoutError):
            connection.call("Browser.getVersion")
        connection.close()
        socket.close.assert_called_once()

    def test_read_transport_failure_enters_bounded_recovery(self):
        browser = _native_browser()
        browser._evaluate = mock.Mock(side_effect=TimeoutError("page stalled"))
        with self.assertRaises(app.OrderRowNotReady):
            browser._call_page("LoadDataToJSON", [])
        browser._evaluate.assert_called_once()

    def test_write_transport_failure_is_not_replayed_or_classified_as_preclick(self):
        browser = _native_browser()
        browser._evaluate = mock.Mock(side_effect=TimeoutError("response lost"))
        with self.assertRaises(TimeoutError):
            browser._call_page("SetElids", [])
        browser._evaluate.assert_called_once()

    def test_read_safety_rejection_is_not_downgraded_to_transport_retry(self):
        browser = _native_browser()
        browser._evaluate = mock.Mock(side_effect=app.SafetyStop("login required"))
        with self.assertRaises(app.SafetyStop) as raised:
            browser._call_page("LoadDataToJSON", [])
        self.assertNotIsInstance(raised.exception, app.OrderRowNotReady)

    def test_existing_tab_waits_for_iframe_without_creating_another_tab(self):
        browser = _native_browser()
        browser._targets = mock.Mock(return_value=[
            {"type": "page", "targetId": "old", "url": app.JST_HOME_URL}
        ])
        browser._attach = mock.Mock(side_effect=[app.OrderRowNotReady("loading"), None])
        browser._create_print_target = mock.Mock()
        with mock.patch.object(app.time, "sleep"):
            browser._bind_print_target(create_if_missing=True)
        self.assertEqual(browser._attach.call_count, 2)
        browser._create_print_target.assert_not_called()

    def test_replacement_discovery_error_closes_new_tab_and_restores_old(self):
        browser = _native_browser()
        browser._target_id = "old"
        old = {"type": "page", "targetId": "old", "url": app.JST_HOME_URL}
        browser._targets = mock.Mock(side_effect=[[old], RuntimeError("discovery lost"), [old]])
        browser._create_print_target = mock.Mock(return_value="new")
        browser._attach = mock.Mock()
        browser._connection = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "discovery lost"):
            browser.recover_print_page(replace_page=True)
        browser._connection.call.assert_any_call("Target.closeTarget", {"targetId": "new"})
        browser._attach.assert_called_once_with(old, resolve=False)

    def prepared_browser(self, count):
        browser = _native_browser()
        rows = [_row(str(6789001 + i), str(13549001 + i), l_id="1234567890") for i in range(count)]
        browser._exact_rows = mock.Mock(return_value=rows)
        browser._preflight_waybill_rows = mock.Mock()
        return browser, rows, [(row["o_id"], row["io_id"]) for row in rows]

    def test_print_preflight_failures_never_mark_running_or_submit(self):
        for count in (1, 2, 10):
            for stage in ("preflight", "carrier_template", "page_module"):
                with self.subTest(count=count, stage=stage):
                    browser, rows, identities = self.prepared_browser(count)
                    browser._print_preflight = mock.Mock()
                    browser._call_page = mock.Mock(return_value="ZTO.1")
                    browser._evaluate = mock.Mock(return_value="expressSetter")
                    if stage == "preflight":
                        browser._print_preflight.side_effect = app.SafetyStop("component not ready")
                    elif stage == "carrier_template":
                        browser._call_page.side_effect = app.OrderRowNotReady("template timeout")
                    else:
                        browser._evaluate.return_value = ""
                    mark = mock.Mock()
                    with self.assertRaises(app.SafetyStop):
                        if count == 1:
                            browser.print_express(*identities[0], True, mark_running=mark)
                        else:
                            browser.print_express_batch(identities, mark_running=mark)
                    mark.assert_not_called()
                    self.assertFalse(any('__JST_NATIVE_API_V2__.print(' in c.args[0] for c in browser._evaluate.call_args_list))

    def test_print_commit_occurs_after_preflight_and_final_pause_check(self):
        browser, rows, identities = self.prepared_browser(2)
        calls = []
        browser._print_preflight = lambda *_args: calls.append("preflight")
        browser._call_page = lambda *_args, **_kwargs: calls.append("template") or "ZTO.1"

        def evaluate(expression, **_kwargs):
            if "String(globalThis.__Moudle" in expression:
                calls.append("module")
                return "expressSetter"
            calls.append("submit")
            return {"state": "SUCCESS", "callbackMatchesExpected": True}

        browser._evaluate = evaluate
        browser.print_express_batch(
            identities, mark_running=lambda: calls.append("mark"),
            final_guard=lambda: calls.append("guard"),
        )
        self.assertEqual(calls, ["preflight", "template", "module", "guard", "mark", "submit"])

    def test_pause_requested_during_preflight_prevents_submit(self):
        browser, rows, identities = self.prepared_browser(1)
        browser._print_preflight = mock.Mock()
        browser._call_page = mock.Mock(return_value="ZTO.1")
        browser._evaluate = mock.Mock(return_value="expressSetter")
        mark = mock.Mock()
        with self.assertRaises(app.OperatorPaused):
            browser.print_express(
                *identities[0], True, mark_running=mark,
                final_guard=mock.Mock(side_effect=app.OperatorPaused("paused")),
            )
        mark.assert_not_called()
        self.assertEqual(browser._evaluate.call_count, 1)

    def test_waybill_preparation_failure_never_marks_running(self):
        for count in (1, 2, 10):
            with self.subTest(count=count):
                browser, rows, identities = self.prepared_browser(count)
                browser._evaluate = mock.Mock(side_effect=TimeoutError("dialog mode unavailable"))
                browser._call_page = mock.Mock()
                mark = mock.Mock()
                with self.assertRaises(app.OrderRowNotReady):
                    if count == 1:
                        browser.get_waybill(*identities[0], True, mark_running=mark)
                    else:
                        browser.get_waybill_batch(identities, mark_running=mark)
                mark.assert_not_called()
                browser._call_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
