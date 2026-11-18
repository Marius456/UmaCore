import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services.report_generator import ReportGenerator
from services import report_generator as reports


class ReportGeneratorDailyProgressTests(unittest.TestCase):
    def test_daily_progress_in_both_tables(self):
        cases = [
            (date(2026, 10, 1), 28_000_000, 0, "+28.0M", "28.0M", "28.0M"),
            (date(2027, 1, 1), 5_000_000, 0, "+5.0M", "5.0M", "5.0M"),
            (date(2026, 10, 2), 30_000_000, 28_000_000, "+2.0M", "15.0M", "30.0M"),
            (date(2026, 10, 4), 36_000_000, 30_000_000, "+6.0M", "9.0M", "36.0M"),
            (date(2026, 10, 3), 30_000_000, 30_000_000, "+0", "10.0M", "30.0M"),
        ]
        report = ReportGenerator()
        for report_date, total, baseline, daily, average, total_text in cases:
            for behind in (False, True):
                with self.subTest(report_date=report_date, behind=behind):
                    item = {
                        "member": SimpleNamespace(trainer_name="RanfeaR"),
                        "history": SimpleNamespace(
                            date=report_date,
                            cumulative_fans=total,
                            deficit_surplus=-1_000_000 if behind else 25_000_000,
                        ),
                        "yesterday_cumulative_fans": baseline,
                    }
                    prepare = (
                        report._prepare_behind_table_data if behind else report._prepare_table_data
                    )
                    self.assertEqual(
                        prepare([item])[0],
                        [1, "RanfeaR", daily, "-1.0M" if behind else "+25.0M", average, total_text],
                    )


class ReportGeneratorResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_connected_browser_is_reused_without_a_temporary_context(self):
        browser = MagicMock()
        browser.is_connected.return_value = True
        with patch.object(reports, '_playwright_browser', browser):
            self.assertIs(await reports._ensure_playwright_browser_async(), browser)
        browser.new_page.assert_not_called()

    async def test_launch_uses_regular_chromium_processes(self):
        browser = MagicMock(new_context=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        with patch.object(reports, '_playwright_browser', None), \
             patch.object(reports, '_playwright_context', None), \
             patch.object(reports, '_playwright', playwright):
            self.assertIs(await reports._ensure_playwright_browser_async(), browser)
        flags = playwright.chromium.launch.await_args.kwargs['args']
        self.assertNotIn('--single-process', flags)
        self.assertNotIn('--no-zygote', flags)

    async def test_failed_render_closes_original_page_before_retry(self):
        first_page = MagicMock()
        first_page.set_content = AsyncMock(side_effect=RuntimeError("browser died"))
        first_page.close = AsyncMock()

        second_page = MagicMock()
        second_page.set_content = AsyncMock()
        second_page.wait_for_timeout = AsyncMock()
        second_locator = MagicMock()
        second_locator.screenshot = AsyncMock(return_value=b"png")
        second_page.locator.return_value = second_locator
        second_page.close = AsyncMock()

        contexts = [
            MagicMock(new_page=AsyncMock(return_value=first_page)),
            MagicMock(new_page=AsyncMock(return_value=second_page)),
        ]

        with (
            patch(
                "services.report_generator._get_browser_context_async",
                new=AsyncMock(side_effect=contexts),
            ),
            patch(
                "services.report_generator._close_playwright_browser_unlocked_async",
                new=AsyncMock(),
            ) as close_browser,
            patch("services.report_generator.pio.to_html", return_value="<html></html>"),
        ):
            result = await ReportGenerator()._generate_table_image(
                ["Name"], [["Test"]], 0x123456, "test.png"
            )

        self.assertEqual(result.filename, "test.png")
        first_page.close.assert_awaited_once()
        second_page.close.assert_awaited_once()
        close_browser.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
