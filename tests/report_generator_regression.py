import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from services.member_performance_card import (
    MemberPerformance, build_member_performance, load_member_performance,
)
from services.quota_schedule import QuotaSchedule
from services.report_generator import ReportGenerator
from services import report_generator as reports


REPORT_DATE = date(2026, 11, 16)
MONTH_END = date(2026, 11, 30)
CLUB_ID = UUID(int=100)


def member_entry(member_id, name, fans, surplus, *, join_date=date(2026, 11, 1), baseline=0):
    return {
        "member": SimpleNamespace(
            member_id=UUID(int=member_id), club_id=CLUB_ID, trainer_name=name, join_date=join_date,
        ),
        "history": SimpleNamespace(cumulative_fans=fans, deficit_surplus=surplus),
        "yesterday_cumulative_fans": baseline,
    }


class MemberPerformanceTests(unittest.TestCase):
    def test_month_target_uses_join_date_and_quota_changes_for_all_periods(self):
        member = member_entry(1, "Late join", 104, -5, join_date=date(2026, 11, 10), baseline=98)
        future_change = date(2026, 11, 20)
        for period, days in QuotaSchedule.PERIOD_DAYS.items():
            with self.subTest(period=period):
                schedule = QuotaSchedule(REPORT_DATE, period, 10 * days, [
                    {"effective_date": future_change, "daily_quota": 20 * days},
                ])
                row, = build_member_performance([member], {}, schedule, REPORT_DATE, MONTH_END)
                self.assertEqual((row.target, row.day, row.surplus), (320, 6, -5))
                self.assertEqual((row.status, row.required_per_day), ("Behind", 16))

    def test_ties_rank_by_name_then_id_and_final_day_has_no_remaining_rate(self):
        roster = [
            member_entry(3, "Ace", 100, -2),
            member_entry(2, "Ace", 100, 0),
            member_entry(1, "Beta", 100, -1),
            member_entry(4, "Finisher", 310, 10),
        ]
        schedule = QuotaSchedule(MONTH_END, "daily", 10, [])
        rows = build_member_performance(roster, {}, schedule, MONTH_END, MONTH_END)
        self.assertEqual([row.name for row in rows], ["Finisher", "Ace", "Ace", "Beta"])
        self.assertEqual([row.surplus for row in rows], [10, 0, -2, -1])
        self.assertEqual([row.status for row in rows], ["Done", "Ahead", "Behind", "Behind"])
        self.assertEqual([row.required_per_day for row in rows], [0, None, None, None])

    def test_activity_uses_only_verified_gains_and_preserves_daily_baseline(self):
        report_date = date(2026, 11, 8)
        member = member_entry(1, "Member", 32, 0, join_date=date(2026, 10, 1), baseline=25)
        points = {UUID(int=1): [
            (date(2026, 10, 31), 1_000), (date(2026, 11, 1), 5),
            (date(2026, 11, 2), 9), (date(2026, 11, 4), 20),
            (date(2026, 11, 5), 19), (date(2026, 11, 6), 25), (report_date, 32),
        ]}
        schedule = QuotaSchedule(report_date, "daily", 10, [])
        row, = build_member_performance([member], points, schedule, report_date, MONTH_END)
        self.assertEqual(row.activity, {date(2026, 11, 2): 4, date(2026, 11, 6): 6})
        self.assertEqual(row.day, 7)


class ReportGeneratorCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_history_is_loaded_once_for_all_members(self):
        roster = [member_entry(1, "One", 45, 1), member_entry(2, "Two", 30, -1)]
        fetch = AsyncMock(side_effect=[[], []])
        with (
            patch("services.member_performance_card.Club.get_by_id",
                  return_value=SimpleNamespace(daily_quota=10)),
            patch("services.member_performance_card.db.fetch", new=fetch),
        ):
            rows = await load_member_performance(roster, REPORT_DATE, "daily", 10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(fetch.await_args.args[1:], (
            CLUB_ID, date(2026, 11, 9), REPORT_DATE, [UUID(int=1), UUID(int=2)],
        ))

    async def test_report_paginates_one_ranked_table_and_escapes_names(self):
        report = ReportGenerator()
        rows = [
            MemberPerformance("<Member>", 10, 10, 5, {}, 4, 0, "Done")
            for _ in range(31)
        ]
        status = {"on_track": [object()] * 10, "behind": [object()] * 21, "total_members": 31}
        with (
            patch("services.report_generator.load_member_performance", return_value=rows) as load,
            patch.object(report, "_generate_table_image", return_value=object()) as render,
        ):
            items = await report.create_daily_report(
                "<Club>", 7_000_000, status, REPORT_DATE,
                rank_data={"club_rank": 7, "monthly_rank": 648}, quota_period="weekly",
            )
        self.assertEqual(len(load.await_args.args[0]), 31)
        first_html, filename = render.await_args_list[0].args
        last_html, _ = render.await_args_list[1].args
        self.assertEqual(first_html.count('<tr class="done">'), 30)
        self.assertEqual(last_html.count('<tr class="done">'), 1)
        self.assertIn('class="position">31<', last_html)
        self.assertIn("&lt;Club&gt;", first_html)
        self.assertIn("&lt;Member&gt;", first_html)
        self.assertIn('class="tier">A<', first_html)
        self.assertIn('class="rank">#648<', first_html)
        self.assertIn("Goal 1.00M/member/day", first_html)
        self.assertFalse(items[0][1])
        self.assertEqual(items[1][0].image.url, f"attachment://{filename}")

    async def test_empty_roster_sends_summary_only(self):
        with patch("services.report_generator.load_member_performance") as load:
            items = await ReportGenerator().create_daily_report(
                "Club", 1, {"on_track": [], "behind": [], "total_members": 0}, REPORT_DATE,
            )
        load.assert_not_awaited()
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0][1])


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
        ):
            result = await ReportGenerator()._generate_table_image(
                "<html></html>", "test.png"
            )

        self.assertEqual(result.filename, "test.png")
        first_page.close.assert_awaited_once()
        second_page.close.assert_awaited_once()
        close_browser.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
