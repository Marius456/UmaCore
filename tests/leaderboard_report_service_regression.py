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

    def test_uses_live_tier_distances_and_valid_rank_metadata(self):
        goal = LeaderboardReportService._compute_club_goal_tracker(
            self.rankings,
            self.latest,
            fans_to_next_tier=20_000_000,
            fans_to_lower_tier=80_000_000,
            club_rank=6,
            monthly_rank=1_137,
        )

        self.assertIsNotNone(goal)
        self.assertEqual(goal["fans_to_next_tier"], 20_000_000)
        self.assertEqual(goal["fans_to_lower_tier"], 80_000_000)
        self.assertEqual(goal["club_rank"], 6)
        self.assertEqual(goal["monthly_rank"], 1_137)

    def test_invalid_rank_metadata_does_not_hide_valid_distances(self):
        goal = LeaderboardReportService._compute_club_goal_tracker(
            self.rankings,
            self.latest,
            fans_to_next_tier=20_000_000,
            fans_to_lower_tier=80_000_000,
            club_rank=12,
            monthly_rank=0,
        )

        self.assertIsNotNone(goal)
        self.assertIsNone(goal["club_rank"])
        self.assertIsNone(goal["monthly_rank"])

    def test_maps_every_uma_moe_club_rank_code(self):
        expected = (
            "D",
            "D+",
            "C",
            "C+",
            "B",
            "B+",
            "A",
            "A+",
            "S",
            "S+",
            "SS",
        )

        for club_rank, label in enumerate(expected, start=1):
            with self.subTest(club_rank=club_rank):
                line = LeaderboardReportService._format_club_tier_line(
                    club_rank, 1_137
                )
                self.assertIn(f"**{label}** (Rank #1,137)", line)

    def test_tier_line_handles_lowest_and_highest_boundaries(self):
        self.assertEqual(
            LeaderboardReportService._format_club_tier_line(1, 10_001),
            "**D** (Rank #10,001) ▶ **D+**",
        )
        self.assertEqual(
            LeaderboardReportService._format_club_tier_line(11, 1),
            "**S+** ◀ **SS** (Rank #1)",
        )

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
    async def test_renders_rank_buffer_and_needed_and_omits_missing_target(self):
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
                club_rank=6,
                monthly_rank=1_137,
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
                "**B** ◀ **B+** (Rank #1,137) ▶ **A**\n"
                "**BUFFER:** 80,000,000\n"
                "**NEEDED:** 20,000,000"
            ],
        )
        self.assertEqual(missing_goals, [])

    async def test_renders_zero_needed_at_ss_without_a_next_tier(self):
        rows = [
            {
                "date": date(2026, 8, 1),
                "trainer_name": "Trainer",
                "cumulative_fans": 9_000_000_000,
                "deficit_surplus": 0,
            },
        ]

        with patch(
            "services.leaderboard_report_service.QuotaHistory.get_current_month_for_club",
            new=AsyncMock(return_value=rows),
        ):
            embeds = await LeaderboardReportService.generate_leaderboard_report(
                UUID(int=1),
                "Paragon",
                2026,
                8,
                fans_to_next_tier=0,
                fans_to_lower_tier=338_997_978,
                club_rank=11,
                monthly_rank=1,
            )

        goal = next(
            field.value
            for embed in embeds
            for field in embed.fields
            if field.name == "📈 CLUB GOAL"
        )
        self.assertEqual(
            goal,
            "**S+** ◀ **SS** (Rank #1)\n"
            "**BUFFER:** 338,997,978\n"
            "**NEEDED:** 0",
        )

    async def test_omits_top_movers_section(self):
        rows = [
            {
                "date": date(2026, 8, 1),
                "trainer_name": "Alpha",
                "cumulative_fans": 10_000_000,
                "deficit_surplus": 0,
            },
            {
                "date": date(2026, 8, 1),
                "trainer_name": "Bravo",
                "cumulative_fans": 9_000_000,
                "deficit_surplus": 0,
            },
            {
                "date": date(2026, 8, 2),
                "trainer_name": "Bravo",
                "cumulative_fans": 12_000_000,
                "deficit_surplus": 0,
            },
            {
                "date": date(2026, 8, 2),
                "trainer_name": "Alpha",
                "cumulative_fans": 11_000_000,
                "deficit_surplus": 0,
            },
        ]

        with patch(
            "services.leaderboard_report_service.QuotaHistory.get_current_month_for_club",
            new=AsyncMock(return_value=rows),
        ):
            embeds = await LeaderboardReportService.generate_leaderboard_report(
                UUID(int=1), "Paragon", 2026, 8
            )

        field_names = [field.name for embed in embeds for field in embed.fields]
        self.assertNotIn("TOP MOVERS", field_names)


class UmaMoeTierProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_and_validates_live_tier_progress(self):
        scraper = UmaMoeAPIScraper("123")
        response = {
            "circle": {"monthly_rank": 119},
            "club_rank": 8,
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
                "club_rank": 8,
                "monthly_rank": 119,
            },
        )
        direct_fetch.assert_awaited_once_with(2026, 8)
        browser_fetch.assert_not_awaited()

    def test_ignores_invalid_rank_metadata(self):
        metadata = UmaMoeAPIScraper._extract_club_rank_metadata(
            {"club_rank": 0, "circle": {"monthly_rank": True}}
        )

        self.assertEqual(metadata, {})

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
