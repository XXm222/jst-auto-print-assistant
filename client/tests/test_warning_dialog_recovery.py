import unittest
from unittest import mock

import jst_auto_print_app as app


class _Locator:
    def __init__(self, items):
        self._items = list(items)

    def count(self):
        return len(self._items)

    def nth(self, index):
        return self._items[index]


class _Button:
    def __init__(self, dialog):
        self._dialog = dialog
        self.clicks = 0
        self.trial_clicks = 0

    def is_visible(self):
        return self._dialog.visible

    def is_enabled(self):
        return True

    def evaluate(self, _script):
        return False

    def click(self, *, trial=False, timeout=None):
        del timeout
        if trial:
            self.trial_clicks += 1
            return
        self.clicks += 1
        self._dialog.visible = False


class _Prompt:
    def __init__(self, dialog, message):
        self._dialog = dialog
        self._message = message

    def is_visible(self):
        return self._dialog.visible

    def inner_text(self):
        return self._message


class _WarningDialog:
    url = "https://www.erp321.com/epaas-dialog-frame.html"

    def __init__(self, message):
        self.visible = True
        self.confirm = _Button(self)
        self.cancel = _Button(self)
        self.prompt = _Prompt(self, message)

    def is_detached(self):
        return False

    def frame_element(self):
        return self

    def is_visible(self):
        return self.visible

    def locator(self, selector):
        return {
            "#confirm_confirm": _Locator([self.confirm]),
            "#confirm_close": _Locator([self.cancel]),
            "#confirm_top": _Locator([self.prompt]),
        }.get(selector, _Locator([]))


class WarningDialogRecoveryTests(unittest.TestCase):
    """Regression coverage for dialogs used only by the retired DOM adapter."""

    @classmethod
    def setUpClass(cls):
        cls._legacy_browser_patch = mock.patch.object(
            app, "JSTBrowser", app._LegacyPlaywrightJSTBrowser
        )
        cls._legacy_browser_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._legacy_browser_patch.stop()

    @staticmethod
    def _selected_order():
        return type("SelectedOrderProof", (), {"o_id": "6789727"})()

    def test_known_warnings_cancel_once_never_confirm_and_stop_safely(self):
        messages = (
            "订单：6789727已经预发货成功，如需继续设定面单号请点击确定按钮",
            "快递单(运单)号：773438059044157已经打印过面单 "
            "请确认是否重设快递公司？",
        )
        browser = object.__new__(app.JSTBrowser)

        for message in messages:
            with self.subTest(message=message):
                dialog = _WarningDialog(message)

                with self.assertRaises(app.SafetyStop):
                    browser._confirm_reset_warning(
                        self._selected_order(), dialog, set()
                    )

                self.assertEqual(dialog.cancel.clicks, 1)
                self.assertEqual(dialog.confirm.clicks, 0)

    def test_unknown_warning_never_clicks_confirm_or_cancel(self):
        browser = object.__new__(app.JSTBrowser)
        dialog = _WarningDialog("确定删除当前订单？")

        with self.assertRaises(app.SafetyStop):
            browser._confirm_reset_warning(
                self._selected_order(), dialog, set()
            )

        self.assertEqual(dialog.confirm.clicks, 0)
        self.assertEqual(dialog.cancel.clicks, 0)

    def test_stale_known_warning_is_cancelled_once_then_no_longer_blocks_search(self):
        browser = object.__new__(app.JSTBrowser)
        main_frame = object()
        dialog = _WarningDialog(
            "订单：6789727已经预发货成功，如需继续设定面单号请点击确定按钮"
        )
        browser.page = type(
            "Page", (), {"main_frame": main_frame, "frames": [main_frame, dialog]}
        )()

        self.assertTrue(browser._dismiss_stale_carrier_dialog())
        self.assertEqual(dialog.cancel.clicks, 1)
        self.assertEqual(dialog.confirm.clicks, 0)
        self.assertFalse(browser._dismiss_stale_carrier_dialog())
        self.assertEqual(dialog.cancel.clicks, 1)


if __name__ == "__main__":
    unittest.main()
