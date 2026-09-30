"""Generate and verify a dated SKU export with boundary timestamps."""

import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jst_auto_print_app as app


def readback(o_id: str, io_id: str, sku_name: str) -> dict:
    return {
        "o_id": o_id,
        "io_id": io_id,
        "items_complete": True,
        "source_item_count": 1,
        "item_validation_errors": [],
        "order_validation_errors": [],
        "privacy_required": False,
        "shop_name": "日期导出针对性测试",
        # Keep every source order date the same to prove that filtering uses
        # confirmed print time rather than the JST order/outbound date.
        "io_date": "2026-08-20 12:00:00",
        "carrier_name": "中通速递-山东",
        "items": [
            {
                "line_key": f"line-{io_id}",
                "sku_id": f"sku-{io_id}",
                "sku_name": sku_name,
                "qty": 1,
                "unit": "件",
            }
        ],
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: windows_targeted_export_probe.py OUTPUT.xlsx")
    target = Path(sys.argv[1])
    if target.exists():
        raise RuntimeError(f"refusing to overwrite existing probe output: {target}")

    samples = [
        ("2026-08-27T23:59:59", "9001001", "19001001", "前一天-应排除"),
        ("2026-08-28T00:00:00", "9001002", "19001002", "当天边界A-应导出"),
        ("2026-08-28T23:59:59", "9001003", "19001003", "当天边界B-应导出"),
        ("2026-08-29T00:00:00", "9001004", "19001004", "次日-应排除"),
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        store = app.EventStore(root / "events.sqlite3", root / "events.jsonl")
        original_now_text = app.now_text
        try:
            for timestamp, o_id, io_id, sku_name in samples:
                app.now_text = lambda value=timestamp: value
                inserted = store.record_sku_outbound(
                    readback(o_id, io_id, sku_name)
                )
                if inserted != 1:
                    raise AssertionError(f"unexpected inserted row count: {inserted}")
        finally:
            app.now_text = original_now_text

        counts = store.export_sku_outbound_xlsx(target, "2026-08-28")

    if counts != (2, 2):
        raise AssertionError(f"expected exactly two lines/orders, got {counts}")
    with zipfile.ZipFile(target) as workbook:
        detail_xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
    for expected in ("当天边界A-应导出", "当天边界B-应导出"):
        if expected not in detail_xml:
            raise AssertionError(f"selected-date record is missing: {expected}")
    for excluded in ("前一天-应排除", "次日-应排除"):
        if excluded in detail_xml:
            raise AssertionError(f"out-of-date record leaked into export: {excluded}")
    print(
        json.dumps(
            {
                "result": "PASS",
                "platform": sys.platform,
                "app_version": app.APP_VERSION,
                "selected_date": "2026-08-28",
                "exported_lines": counts[0],
                "exported_orders": counts[1],
                "output": str(target),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
