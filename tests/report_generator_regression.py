import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.report_generator import ReportGenerator


class ReportGeneratorResourceTests(unittest.IsolatedAsyncioTestCase):
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
