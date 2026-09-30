import inspect
import unittest
from datetime import date

import jst_auto_print_app as app


class DatePickerTests(unittest.TestCase):
    def test_month_grid_is_monday_first_and_contains_each_month_day_once(self):
        weeks = app.calendar_month_dates(2026, 8)
        self.assertIn(len(weeks), (5, 6))
        self.assertEqual(weeks[0][0].weekday(), 0)
        current = [day for week in weeks for day in week if day.month == 8]
        self.assertEqual(current, [date(2026, 8, day) for day in range(1, 32)])

    def test_month_navigation_crosses_year_boundaries(self):
        self.assertEqual(app.shift_calendar_month(2026, 1, -1), (2025, 12))
        self.assertEqual(app.shift_calendar_month(2026, 12, 1), (2027, 1))
        self.assertEqual(app.shift_calendar_month(2026, 8, 0), (2026, 8))

    def test_invalid_calendar_values_fail_closed(self):
        for args in ((2026, 0), (2026, 13), (0, 1), (True, 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                app.calendar_month_dates(*args)
        with self.assertRaises(ValueError):
            app.shift_calendar_month(1, 1, -1)

    def test_sku_export_date_field_is_readonly_and_uses_picker(self):
        source = inspect.getsource(app.DesktopApp._build)
        self.assertIn('state="readonly"', source)
        self.assertIn('text="选择日期"', source)
        self.assertIn("command=self._choose_sku_export_date", source)


if __name__ == "__main__":
    unittest.main()
