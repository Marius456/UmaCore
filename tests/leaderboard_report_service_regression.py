from datetime import date, timedelta
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from scrapers.umamoe_api_scraper import UmaMoeAPIScraper
from services.leaderboard_report_service import LeaderboardReportService


class TodayRecordsTests(unittest.TestCase):
    def test_includes_a_personal_best_tied_on_the_latest_day(self):
        latest = date(2026, 7, 10)
        records = LeaderboardReportService._compute_today_records(
            {
                "Trainer": [
                    {"date": latest - timedelta(days=2), "delta": 1_000_000},
                    {"date": latest, "delta": 1_000_000},
                ]
            },
            latest,
        )

        self.assertEqual(records["count"], 1)
        self.assertTrue(records["members"][0]["is_tie"])
        self.assertEqual(records["members"][0]["prev_best_delta"], 1_000_000)


class RareAchievementTests(unittest.TestCase):
    def test_does_not_count_window_gains_below_an_earlier_personal_best(self):
        latest = date(2026, 7, 10)
        daily_rankings = {latest: [{"name": "Trainer"}]}
        deltas = {
            "Trainer": [
                {"date": latest - timedelta(days=8), "delta": 10_000_000},
                {"date": latest - timedelta(days=3), "delta": 1_000_000},
                {"date": latest - timedelta(days=2), "delta": 2_000_000},
                {"date": latest - timedelta(days=1), "delta": 3_000_000},
            ]
        }

        achievements = LeaderboardReportService._compute_rare_achievements(
            daily_rankings, deltas, club_record=None, latest_date=latest
        )

        self.assertFalse(any(a.title == "PB Spree" for a in achievements))


class ClubGoalTests(unittest.TestCase):
    latest = date(2026, 8, 20)
    rankings = {
        latest: [{"name": "Trainer", "fans": 9_000_000_000, "daily": 1_000_000}]
    }

    def test_uses_only_live_tier_distances(self):
        goal = LeaderboardReportService._compute_club_goal_tracker(
            self.rankings,
            self.latest,
            fans_to_next_tier=20_000_000,
            fans_to_lower_tier=80_000_000,
        )

        self.assertIsNotNone(goal)
        self.assertEqual(goal["progress_pct"], 80.0)
        self.assertEqual(goal["fans_to_next_tier"], 20_000_000)
        self.assertEqual(goal["bar"], "▰▰▰▰▰▰▰▰▱▱")

    def test_omits_goal_instead_of_using_large_total_as_estimate(self):
        goal = LeaderboardReportService._compute_club_goal_tracker(
            self.rankings, self.latest
        )

        self.assertIsNone(goal)

    def test_omits_missing_negative_and_zero_range_values(self):
        invalid_pairs = [
            (20_000_000, None),
            (None, 80_000_000),
            (-1, 80_000_000),
            (20_000_000, -1),
            (0, 0),
        ]

        for fans_to_next_tier, fans_to_lower_tier in invalid_pairs:
            with self.subTest(
                fans_to_next_tier=fans_to_next_tier,
                fans_to_lower_tier=fans_to_lower_tier,
            ):
                goal = LeaderboardReportService._compute_club_goal_tracker(
                    self.rankings,
                    self.latest,
                    fans_to_next_tier=fans_to_next_tier,
                    fans_to_lower_tier=fans_to_lower_tier,
                )
                self.assertIsNone(goal)


class ClubGoalRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_renders_live_remaining_fans_and_omits_missing_target(self):
        rows = [
            {
                "date": date(2026, 8, 1),
                "trainer_name": "Trainer",
                "cumulative_fans": 9_000_000_000,
                "deficit_surplus": 0,
            },
            {
                "date": date(2026, 8, 2),
                "trainer_name": "Trainer",
                "cumulative_fans": 9_001_000_000,
                "deficit_surplus": 0,
            },
        ]

        with patch(
            "services.leaderboard_report_service.QuotaHistory.get_current_month_for_club",
            new=AsyncMock(return_value=rows),
        ):
            live_embeds = await LeaderboardReportService.generate_leaderboard_report(
                UUID(int=1),
                "Paragon",
                2026,
                8,
                fans_to_next_tier=20_000_000,
                fans_to_lower_tier=80_000_000,
            )
            missing_embeds = await LeaderboardReportService.generate_leaderboard_report(
                UUID(int=1), "Paragon", 2026, 8
            )

        live_goals = [
            field.value
            for embed in live_embeds
            for field in embed.fields
            if field.name == "📈 CLUB GOAL"
        ]
        missing_goals = [
            field.value
            for embed in missing_embeds
            for field in embed.fields
            if field.name == "📈 CLUB GOAL"
        ]

        self.assertEqual(
            live_goals,
            [
                "**Next tier**: [▰▰▰▰▰▰▰▰▱▱] 80.0%"
                " · 20.0M remaining"
            ],
        )
        self.assertEqual(missing_goals, [])


class UmaMoeTierProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_and_validates_live_tier_progress(self):
        scraper = UmaMoeAPIScraper("123")
        response = {
            "circle": {"monthly_rank": 119},
            "fans_to_next_tier": 20_000_000,
            "fans_to_lower_tier": 80_000_000,
        }

        with (
            patch.object(
                scraper, "_fetch_via_direct_api", new=AsyncMock(return_value=response)
            ) as direct_fetch,
            patch.object(
                scraper, "_fetch_via_playwright", new=AsyncMock()
            ) as browser_fetch,
        ):
            result = await scraper.fetch_tier_progress(2026, 8)

        self.assertEqual(
            result,
            {
                "fans_to_next_tier": 20_000_000,
                "fans_to_lower_tier": 80_000_000,
            },
        )
        direct_fetch.assert_awaited_once_with(2026, 8)
        browser_fetch.assert_not_awaited()

    async def test_returns_none_when_both_fetch_paths_fail(self):
        scraper = UmaMoeAPIScraper("123")

        with (
            patch.object(
                scraper, "_fetch_via_direct_api", new=AsyncMock(return_value=None)
            ),
            patch.object(
                scraper,
                "_fetch_via_playwright",
                new=AsyncMock(side_effect=RuntimeError("unavailable")),
            ),
        ):
            result = await scraper.fetch_tier_progress(2026, 8)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
