import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

import jst_auto_print_app as app


class SkuExportDateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = app.EventStore(root / "events.sqlite3", root / "events.jsonl")
        self.root = root

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _readback(o_id: str, io_id: str, sku_name: str, qty: float):
        return {
            "o_id": o_id,
            "io_id": io_id,
            "items_complete": True,
            "source_item_count": 1,
            "item_validation_errors": [],
            "order_validation_errors": [],
            "privacy_required": False,
            "shop_name": "测试店铺",
            "io_date": "2026-08-20 12:00:00",
            "carrier_name": "中通速递-山东",
            "items": [
                {
                    "line_key": f"line-{io_id}",
                    "sku_id": f"sku-{io_id}",
                    "sku_name": sku_name,
                    "qty": qty,
                    "unit": "件",
                }
            ],
        }

    def _record(self, timestamp: str, o_id: str, io_id: str, sku_name: str):
        with mock.patch.object(app, "now_text", return_value=timestamp):
            return self.store.record_sku_outbound(
                self._readback(o_id, io_id, sku_name, 1)
            )

    def test_rows_use_print_completion_date_not_order_date(self):
        self._record("2026-08-27T23:59:59", "6795001", "13545001", "前一天商品")
        self._record("2026-08-28T00:00:00", "6795002", "13545002", "当天商品A")
        self._record("2026-08-28T23:59:59", "6795003", "13545003", "当天商品B")
        self._record("2026-08-29T00:00:00", "6795004", "13545004", "次日商品")

        rows = self.store.sku_outbound_rows("2026-08-28")

        self.assertEqual([row["o_id"] for row in rows], ["6795002", "6795003"])
        self.assertTrue(all(row["io_date"].startswith("2026-08-20") for row in rows))

    def test_export_contains_only_selected_print_date(self):
        self._record("2026-08-27T14:00:00", "6795101", "13545101", "不应导出商品")
        self._record("2026-08-28T14:00:00", "6795102", "13545102", "应导出商品")
        target = self.root / "selected.xlsx"

        counts = self.store.export_sku_outbound_xlsx(target, "2026-08-28")

        self.assertEqual(counts, (1, 1))
        with zipfile.ZipFile(target) as workbook:
            xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
        self.assertIn("2026-08-28", xml)
        self.assertIn("应导出商品", xml)
        self.assertNotIn("不应导出商品", xml)
        self.assertIn('<col min="1" max="1" width="27"', xml)
        self.assertIn('<col min="2" max="2" width="23"', xml)

    def test_same_name_with_different_sku_codes_exports_separately(self):
        target = self.root / "same-name-different-skus.xlsx"
        rows = [
            {
                "sku_id": "3343-Q",
                "sku_name": "同名不同商品",
                "qty": 2,
                "unit": "个",
                "o_id": "1001",
                "io_id": "2001",
            },
            {
                "sku_id": "3442MTS",
                "sku_name": "同名不同商品",
                "qty": 3,
                "unit": "个",
                "o_id": "1002",
                "io_id": "2002",
            },
        ]

        app.write_sku_outbound_xlsx(rows, target, selected_date="2026-08-28")

        with zipfile.ZipFile(target) as workbook:
            xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("3343-Q", xml)
        self.assertIn("3442MTS", xml)
        self.assertNotIn("3343-Q、3442MTS", xml)
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheet = ET.fromstring(xml)
        for reference, expected in {"C5": 3, "C6": 2, "C7": 5, "D7": 2}.items():
            cell = sheet.find(f".//s:c[@r='{reference}']/s:v", ns)
            self.assertEqual(float(cell.text), expected)

    def test_invalid_and_empty_dates_fail_before_export(self):
        for value in ("", "2026/08/28", "2026-8-28", "2026-02-30"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                    app.parse_export_date(value)

        with self.assertRaisesRegex(ValueError, "2026-08-28"):
            self.store.export_sku_outbound_xlsx(
                self.root / "empty.xlsx", "2026-08-28"
            )

    def test_print_commit_detail_is_durable_for_callback_recovery(self):
        detail = {
            "proof_version": 1,
            "waybill_suffix": "1234",
            "waybill_fingerprint": "a" * 64,
        }
        self.store.event(
            "INFO",
            "PRINT_COMMIT",
            "正在提交打印任务",
            o_id="6795201",
            io_id="13545201",
            detail=detail,
        )

        self.assertEqual(
            self.store.latest_event_detail(
                "PRINT_COMMIT", "6795201", "13545201"
            ),
            detail,
        )


if __name__ == "__main__":
    unittest.main()
