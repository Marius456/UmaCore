import unittest
from base64 import b64encode
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from services.club_race_card import (
    QuotaReportHistory, build_club_race, load_quota_report_history, race_card_html,
)
from services.member_performance_card import (
    build_member_performance, card_html,
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
    def test_all_tiers_embed_the_matching_local_webp(self):
        assets = Path(__file__).resolve().parents[1] / "assets" / "circle_ranks"
        tiers = ("D", "D+", "C", "C+", "B", "B+", "A", "A+", "S", "S+", "SS")
        for code, tier in enumerate(tiers, start=1):
            with self.subTest(tier=tier):
                icon = assets / f"utx_ico_circle_rank_{code:02d}.webp"
                encoded = b64encode(icon.read_bytes()).decode("ascii")
                html = card_html("Club", REPORT_DATE, 0, 10, {"club_rank": code}, [], 1)
                self.assertIn(f'alt="{tier}" src="data:image/webp;base64,{encoded}"', html)

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
        points = {UUID(int=1): {
            date(2026, 10, 31): 1_000, date(2026, 11, 1): 5,
            date(2026, 11, 2): 9, date(2026, 11, 4): 20,
            date(2026, 11, 5): 19, date(2026, 11, 6): 25, report_date: 32,
        }}
        schedule = QuotaSchedule(report_date, "daily", 10, [])
        row, = build_member_performance([member], points, schedule, report_date, MONTH_END)
        self.assertEqual(row.activity, {date(2026, 11, 2): 4, date(2026, 11, 6): 6})
        self.assertEqual(row.day, 7)


class ClubRaceTests(unittest.TestCase):
    def test_club_race_counts_joins_and_quota_changes_with_gaps_and_losses(self):
        day = date(2026, 11, 6)
        early = member_entry(1, "Early", 22, -10)
        late = member_entry(2, "Late", 38, 1, join_date=date(2026, 11, 3))
        points = {
            UUID(int=1): {day.replace(day=d): v for d, v in [(1, 9), (2, 9), (4, 26), (5, 24), (6, 22)]},
            UUID(int=2): {day.replace(day=d): v for d, v in [(3, 12), (4, 36), (5, 36), (6, 38)]},
        }
        for period, period_days in QuotaSchedule.PERIOD_DAYS.items():
            with self.subTest(period=period):
                schedule = QuotaSchedule(day, period, 10 * period_days, [
                    {"effective_date": day.replace(day=4), "daily_quota": 20 * period_days},
                ])
                history = QuotaReportHistory(MONTH_END, schedule, points, {2: 70, 4: 60})
                rank_data = {"club_rank": 7, "monthly_rank": 52, "yesterday_rank": 50, "last_month_rank": 90}
                race = build_club_race([early, late], history, day, rank_data)
                self.assertEqual(race.totals, {1: 9, 2: 9, 4: 62, 5: 60, 6: 60})
                self.assertEqual(race.gains, {1: 9, 2: 0, 5: -2, 6: 0})
                self.assertEqual(race.targets, {1: 10, 2: 20, 3: 40, 4: 80, 5: 120, 6: 160})
                self.assertEqual(race.rank_movement, -2)
                self.assertEqual(race.daily_leader, ("Late", 2))
                self.assertEqual(race.monthly_leader, ("Late", 38))
                self.assertEqual(race.ranks, {2: 70, 4: 60, 5: 50, 6: 52})
                status = {"active_members": 3, "total_members": 2, "on_track": [late]}
                html = race_card_html("<Club>", day, 20, status, rank_data, history, race)
                for text in (
                    'class="cell zero">2', 'class="cell missing">3', 'class="cell loss">5',
                    'class="cell upcoming">7', "1/3 on target", "1 missing quota data",
                    "Last month #90", "4/7 days observed", 'class="negative">-2',
                    "&lt;Club&gt;",
                ):
                    self.assertIn(text, html)

    def test_month_rollover_does_not_compare_prior_month_rank_or_fans(self):
        day = date(2026, 12, 1)
        points = {UUID(int=1): {REPORT_DATE: 900, day: 12}}
        history = QuotaReportHistory(day.replace(day=31), QuotaSchedule(day, "daily", 10, []), points, {})
        race = build_club_race([member_entry(1, "Early", 12, 2)], history, day,
                               {"monthly_rank": 20, "yesterday_rank": 9})
        self.assertEqual(race.gains, {1: 12})
        self.assertEqual(race.ranks, {1: 20})
        self.assertIsNone(race.rank_movement)
        self.assertEqual(race.daily_leader, ("Early", 12))

    def test_rank_movement_uses_previous_calendar_day_from_history(self):
        history = QuotaReportHistory(
            MONTH_END, QuotaSchedule(REPORT_DATE, "daily", 10, []), {}, {14: 100, 15: 45},
        )
        race = build_club_race([], history, REPORT_DATE, {"monthly_rank": 41})
        self.assertEqual(race.rank_movement, 4)
        race = build_club_race([], history, REPORT_DATE.replace(day=17), {"monthly_rank": 43})
        self.assertIsNone(race.rank_movement)


class ReportGeneratorCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_month_history_is_loaded_once_for_all_members(self):
        roster = [member_entry(1, "One", 45, 1), member_entry(2, "Two", 30, -1)]
        fetch = AsyncMock(side_effect=[[], [], [{"date": REPORT_DATE, "monthly_rank": 648}]])
        with (
            patch("services.club_race_card.Club.get_by_id",
                  return_value=SimpleNamespace(daily_quota=10)),
            patch("services.club_race_card.db.fetch", new=fetch),
        ):
            history = await load_quota_report_history(CLUB_ID, roster, REPORT_DATE, "daily", 10)
        self.assertEqual(history.points[UUID(int=1)][REPORT_DATE], 45)
        self.assertEqual(history.ranks, {16: 648})
        self.assertEqual(fetch.await_count, 3)
        self.assertEqual(fetch.await_args_list[1].args[1:], (
            CLUB_ID, date(2026, 11, 1), REPORT_DATE, [UUID(int=1), UUID(int=2)],
        ))

    async def test_report_paginates_one_ranked_table_and_escapes_names(self):
        report = ReportGenerator()
        roster = [member_entry(i, "<Member>", 31_000_000, 4) for i in range(31)]
        history = QuotaReportHistory(
            MONTH_END, QuotaSchedule(REPORT_DATE, "weekly", 7_000_000, []), {}, {},
        )
        status = {"on_track": roster, "behind": [], "total_members": 31, "active_members": 31}
        with (
            patch("services.report_generator.load_quota_report_history", return_value=history) as load,
            patch.object(report, "_generate_table_image", return_value=object()) as render,
        ):
            items = await report.create_daily_report(
                "<Club>", 7_000_000, status, REPORT_DATE,
                club_id=CLUB_ID, rank_data={"club_rank": 7, "monthly_rank": 648}, quota_period="weekly",
            )
        self.assertEqual(load.await_args.args[:2], (CLUB_ID, roster))
        race_html, race_file = render.await_args_list[0].args
        first_html, filename = render.await_args_list[1].args
        last_html, _ = render.await_args_list[2].args
        self.assertEqual(first_html.count('<tr class="done">'), 30)
        self.assertEqual(last_html.count('<tr class="done">'), 1)
        self.assertIn('class="position">31<', last_html)
        self.assertIn("&lt;Club&gt;", first_html)
        self.assertIn("&lt;Member&gt;", first_html)
        self.assertIn('<img class="tier" alt="A" src="data:image/webp;base64,', first_html)
        self.assertIn('class="rank">#648<', first_html)
        self.assertIn("Goal 1.00M/member/day", first_html)
        self.assertEqual(race_file, "club_race_card.png")
        self.assertIn("31/31 on target", race_html)
        self.assertEqual(filename, "member_performance_1.png")
        self.assertEqual(items, [render.return_value] * 3)

    async def test_empty_roster_uses_saved_ranks_and_sends_only_the_race_card(self):
        report = ReportGenerator()
        status = {"on_track": [], "behind": [], "total_members": 0, "active_members": 0}
        with (
            patch("services.club_race_card.db.fetch", return_value=[
                {"date": REPORT_DATE, "monthly_rank": 18},
            ]) as fetch,
            patch.object(report, "_generate_table_image", return_value=object()) as render,
        ):
            items = await report.create_daily_report(
                "Club", 1, status, REPORT_DATE, club_id=CLUB_ID,
            )
        fetch.assert_awaited_once()
        self.assertEqual(fetch.await_args.args[1:], (CLUB_ID, REPORT_DATE.replace(day=1), REPORT_DATE))
        self.assertEqual(items, [render.return_value])
        html, filename = render.await_args.args
        self.assertEqual(filename, "club_race_card.png")
        self.assertIn('class="rank">#18<', html)
        self.assertIn('class="chart-empty">No history available', html)


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
