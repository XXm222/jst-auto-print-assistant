"""Check exported values and real formula recalculation, not just formula text."""

import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import jst_auto_print_app as app


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")
SPLIT_CASES = {
    "same_name": (("3343-Q", "浴桶"), ("3442MTS", "浴桶")),
    "name_star": (("A", "浴桶70*65"), ("A", "浴桶70x65")),
    "name_question": (("A", "浴桶?"), ("A", "浴桶大")),
    "name_tilde": (("A", "浴桶~*"), ("A", "浴桶*")),
    "code_star": (("A*", "浴桶"), ("ABC", "浴桶")),
    "code_question": (("A?", "浴桶"), ("AB", "浴桶")),
    "code_tilde": (("A~*", "浴桶"), ("A*", "浴桶")),
    "code_case": (("abc", "浴桶"), ("ABC", "浴桶")),
    "name_case": (("A", "Tub"), ("A", "TUB")),
    "leading_zero": (("001", "浴桶"), ("1", "浴桶")),
    "blank_code": (("", "浴桶"), ("0", "浴桶")),
    "spaces": (("A", "浴桶 "), ("A", "浴桶")),
    "operator": ((">1", "浴桶"), ("2", "浴桶")),
    "long_name": (("A", "浴桶" * 150 + "甲"), ("A", "浴桶" * 150 + "乙")),
    "xml_text": (("A&B", "浴桶<大>"), ("A&B", "浴桶<小>")),
}


def read_sheet(path, sheet_number=1):
    with zipfile.ZipFile(path) as book:
        shared = []
        if "xl/sharedStrings.xml" in book.namelist():
            strings = ET.fromstring(book.read("xl/sharedStrings.xml"))
            shared = ["".join(si.itertext()) for si in strings]
        sheet = ET.fromstring(book.read(f"xl/worksheets/sheet{sheet_number}.xml"))
    cells = {}
    for cell in sheet.findall(".//s:c", NS):
        value = cell.findtext("s:v", namespaces=NS)
        kind = cell.get("t")
        if kind == "inlineStr":
            value = "".join(cell.find("s:is", NS).itertext())
        elif kind == "s":
            value = shared[int(value)]
        elif kind not in ("str", "e") and value is not None:
            value = float(value)
        cells[cell.get("r")] = value
    return cells


def split_rows(pair):
    return [dict(sku_id=code, sku_name=name, qty=qty, unit="件", o_id=str(i), io_id=str(i))
            for i, ((code, name), qty) in enumerate(zip(pair, (2, 3)), start=1)]


class SkuExportGroupingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def assert_split(self, path, pair):
        cells = read_sheet(path)
        for row, (code, name), qty in ((5, pair[1], 3), (6, pair[0], 2)):
            self.assertEqual(cells[f"A{row}"], name)
            self.assertEqual(cells.get(f"B{row}") or "", code)
            self.assertEqual([cells[f"{c}{row}"] for c in "CDEF"], [qty, 1, 1, "件"])
        self.assertEqual([cells[f"{c}7"] for c in "ACDE"], ["合计", 5, 2, 2])
        detail = read_sheet(path, 2)
        self.assertEqual([(detail.get(f"E{i}") or "", detail[f"F{i}"], detail[f"G{i}"])
                          for i in (5, 6)], [(pair[0][0], pair[0][1], 2), (pair[1][0], pair[1][1], 3)])

    def test_distinct_identifiers_and_names_keep_separate_totals(self):
        for label, pair in SPLIT_CASES.items():
            with self.subTest(case=label):
                path = self.root / f"{label}.xlsx"
                app.write_sku_outbound_xlsx(split_rows(pair), path)
                self.assert_split(path, pair)

    def test_duplicate_sku_lines_merge_but_order_count_is_unique(self):
        rows = split_rows(SPLIT_CASES["same_name"])
        rows.append(dict(rows[0], qty=4.5))
        path = self.root / "repeated.xlsx"
        app.write_sku_outbound_xlsx(rows, path)
        self.assert_merged(path)

    def assert_merged(self, path):
        cells = read_sheet(path)
        self.assertEqual(cells["B5"], "3343-Q")
        self.assertEqual([cells[f"{c}5"] for c in "CDE"], [6.5, 2, 1])
        self.assertEqual([cells[f"{c}6"] for c in "CDE"], [3, 1, 1])
        self.assertEqual([cells[f"{c}7"] for c in "CDE"], [9.5, 3, 2])

    @unittest.skipUnless(SOFFICE, "LibreOffice required for real formula recalculation")
    def test_native_recalculation_matches_exact_grouping(self):
        inputs = []
        samples = {label: split_rows(pair) for label, pair in SPLIT_CASES.items()}
        repeated = split_rows(SPLIT_CASES["same_name"])
        repeated.append(dict(repeated[0], qty=4.5))
        samples["repeated"] = repeated
        for label, rows in samples.items():
            original = self.root / f"{label}_original.xlsx"
            app.write_sku_outbound_xlsx(rows, original)
            path = self.root / f"{label}.xlsx"
            # Remove cached formula values so the engine must calculate them.
            # Merely opening/resaving an XLSX can preserve the old cached values.
            with zipfile.ZipFile(original) as source, zipfile.ZipFile(path, "w") as target:
                for entry in source.infolist():
                    data = source.read(entry.filename)
                    if entry.filename.startswith("xl/worksheets/") and entry.filename.endswith(".xml"):
                        tree = ET.fromstring(data)
                        for cell in tree.findall(".//s:c", NS):
                            value = cell.find("s:v", NS)
                            if cell.find("s:f", NS) is not None and value is not None:
                                cell.remove(value)
                        data = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
                    target.writestr(entry, data)
            inputs.append(str(path))
        output = self.root / "recalculated"
        result = subprocess.run(
            [SOFFICE, f"-env:UserInstallation={(self.root / 'profile').as_uri()}",
             "--headless", "--convert-to", "xlsx", "--outdir", str(output), *inputs],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for label, pair in SPLIT_CASES.items():
            with self.subTest(case=label):
                path = output / f"{label}.xlsx"
                self.assertTrue(path.exists(), result.stdout + result.stderr)
                self.assert_split(path, pair)
        self.assert_merged(output / "repeated.xlsx")


if __name__ == "__main__":
    unittest.main()
