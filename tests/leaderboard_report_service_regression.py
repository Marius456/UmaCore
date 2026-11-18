from datetime import date, datetime, timedelta
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


class MonthlyRecordsTests(unittest.TestCase):
    def test_biggest_day_includes_the_first_day_of_the_month(self):
        first = date(2026, 8, 1)
        rankings = {
            first: [{"name": "Alpha", "daily": 8_000_000, "rank": 1, "prev_rank": None}],
            first + timedelta(days=1): [
                {"name": "Alpha", "daily": 2_000_000, "rank": 1, "prev_rank": 1}
            ],
        }

        records = LeaderboardReportService._compute_history_records(rankings)

        self.assertIn("📊 **Biggest Day**: **Alpha** — +8,000,000 fans", records)

    def test_biggest_climber_scans_the_entire_month(self):
        first = date(2026, 8, 1)
        rankings = {
            first: [
                {"name": "Alpha", "daily": 1, "rank": 1, "prev_rank": None},
                {"name": "Bravo", "daily": 1, "rank": 4, "prev_rank": None},
            ],
            first + timedelta(days=1): [
                {"name": "Bravo", "daily": 1, "rank": 1, "prev_rank": 4},
                {"name": "Alpha", "daily": 1, "rank": 2, "prev_rank": 1},
            ],
            first + timedelta(days=2): [
                {"name": "Bravo", "daily": 1, "rank": 1, "prev_rank": 1},
                {"name": "Alpha", "daily": 1, "rank": 2, "prev_rank": 2},
            ],
        }

        records = LeaderboardReportService._compute_history_records(rankings)

        self.assertIn("⬆️ **Biggest Climber**: **Bravo** — up 3 spots", records)


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


class MonthEndRecapTests(unittest.IsolatedAsyncioTestCase):
    async def _generate(self, closing_date, standings, final_rank=None):
        rows = [
            {"date": day, "trainer_name": name, "cumulative_fans": fans, "deficit_surplus": 0}
            for day, entries in standings.items()
            for name, fans in entries.items()
        ]
        with (
            patch(
                "services.leaderboard_report_service.QuotaHistory.get_current_month_for_club",
                new=AsyncMock(return_value=rows),
            ) as history,
            patch("services.leaderboard_report_service.load_prediction_snapshot") as load,
        ):
            embeds = await LeaderboardReportService.generate_month_end_recap(
                UUID(int=1), "Paragon", closing_date.year, closing_date.month,
                final_rank=final_rank,
            )
        history.assert_awaited_once_with(UUID(int=1), closing_date.year, closing_date.month)
        load.assert_not_called()
        return embeds

    async def test_closes_month_with_podium_ties_records_and_rivalry_results(self):
        closing = date(2026, 10, 31)
        embeds = await self._generate(closing, {
            date(2026, 10, 1): {"Alpha": 1_000, "Early": 500},
            date(2026, 10, 28): {"Alpha": 8_000_000, "Bravo": 7_000_000, "Charlie": 6_000_000},
            date(2026, 10, 29): {"Alpha": 9_000_000, "Bravo": 10_000_000, "Charlie": 8_000_000},
            date(2026, 10, 30): {"Alpha": 12_000_000, "Bravo": 11_000_000, "Charlie": 9_000_000},
            closing: {"Alpha": 13_000_000, "Bravo": 13_000_000, "Charlie": 10_000_000},
        }, final_rank=83)

        self.assertIn("Month-End Leaderboard Recap", embeds[0].title)
        self.assertIn("October 2026** · 3 finishing members", embeds[0].description)
        self.assertIn("Oct 31", embeds[0].description)
        fields = {field.name: field.value for field in embeds[0].fields}
        self.assertEqual(fields["🏆 FINAL PODIUM"].splitlines(), [
            "**#1 Alpha** — 13,000,000 fans",
            "**#1 Bravo** — 13,000,000 fans",
            "**#3 Charlie** — 10,000,000 fans",
        ])
        self.assertEqual(fields["🏁 CLUB FINISH"], "**Final rank #83**")
        self.assertIn("Biggest Day**: **Bravo** — +3,000,000 fans", fields["📊 MONTHLY RECORDS"])
        self.assertIn("Biggest Climber", fields["📊 MONTHLY RECORDS"])
        self.assertIn("finished tied (2 swaps)", fields["⚔️ RIVALRY RESULTS"])
        self.assertEqual(len(fields), 4)

    async def test_rivalry_result_reports_the_final_fan_gap(self):
        closing = date(2026, 10, 31)
        embeds = await self._generate(closing, {
            closing - timedelta(days=1): {"Alpha": 5_000_000, "Bravo": 4_000_000},
            closing: {"Alpha": 6_000_000, "Bravo": 6_123_456},
        })
        rivalry = next(field.value for field in embeds[0].fields if field.name == "⚔️ RIVALRY RESULTS")
        self.assertIn("**Bravo** finished ahead by 123,456 fans (1 swap)", rivalry)

    async def test_sparse_leap_day_recaps_omit_unverified_highlights(self):
        closing = date(2028, 2, 29)
        for rank in (None, 0, -3, True, "83"):
            with self.subTest(rank=rank):
                embeds = await self._generate(closing, {closing: {"Solo": 5_000_000}}, rank)
                fields = {field.name: field.value for field in embeds[0].fields}
                self.assertIn("Feb 29", embeds[0].description)
                self.assertEqual(fields, {
                    "🏆 FINAL PODIUM": "**#1 Solo** — 5,000,000 fans",
                    "🏁 CLUB FINISH": "Final rank unavailable",
                })

    async def test_requires_exact_closing_standings(self):
        with self.assertRaisesRegex(ValueError, "No closing standings.*2026-10-31"):
            await self._generate(date(2026, 10, 31), {date(2026, 10, 30): {"Solo": 1}})

    async def test_tied_podium_spans_embeds_without_truncating_finishers(self):
        closing = date(2026, 12, 31)
        names = [f"{i:02d}" + "🌟" * 98 for i in range(30)]
        embeds = await self._generate(closing, {closing: dict.fromkeys(names, 123_456)})

        self.assertGreater(len(embeds), 1)
        podium_lines = []
        for embed in embeds:
            embed_text = (embed.title or "") + (embed.description or "")
            self.assertLessEqual(len(embed.fields), 25)
            for field in embed.fields:
                embed_text += field.name + field.value
                self.assertLessEqual(len(field.value.encode("utf-16-le")) // 2, 1024)
                if field.name == "🏆 FINAL PODIUM":
                    podium_lines.extend(field.value.splitlines())
            self.assertLessEqual(len(embed_text.encode("utf-16-le")) // 2, 6000)
        self.assertEqual(podium_lines, [f"**#1 {name}** — 123,456 fans" for name in names])


class UmaMoeClosingRankTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_month_rank_is_used_after_december_rollover(self):
        scraper = UmaMoeAPIScraper("123")
        previous = {"members": [{
            "viewer_id": "1", "trainer_name": "Alpha", "daily_fans": list(range(1, 32)),
        }], "circle": {"last_month_rank": 137}}
        new_month = {"members": [], "circle": {"monthly_rank": 912, "last_month_rank": 83}}
        with (
            patch("scrapers.umamoe_api_scraper.datetime") as clock,
            patch.object(scraper, "_fetch_via_direct_api", side_effect=[previous, new_month]) as fetch,
        ):
            clock.now.side_effect = lambda tz=None: datetime(2027, 1, 1, 16, tzinfo=tz)
            await scraper.scrape()

        self.assertEqual([call.args for call in fetch.await_args_list], [(2026, 12), (2027, 1)])
        self.assertEqual(scraper.get_data_date(), date(2026, 12, 31))
        self.assertEqual(scraper.get_closing_month_rank(), 83)
        self.assertEqual(scraper.get_monthly_rank(), 912)

    async def test_missing_new_month_response_still_saves_previous_month_snapshot(self):
        scraper = UmaMoeAPIScraper("123")
        previous = {"members": [{
            "viewer_id": "1", "trainer_name": "Alpha", "daily_fans": list(range(1, 32)),
        }], "circle": {"last_month_rank": 137}}
        with (
            patch("scrapers.umamoe_api_scraper.datetime") as clock,
            patch.object(scraper, "_fetch_via_direct_api", side_effect=[previous, None]),
            patch.object(scraper, "_fetch_via_playwright", side_effect=RuntimeError("unavailable")),
        ):
            clock.now.side_effect = lambda tz=None: datetime(2026, 11, 1, 16, tzinfo=tz)
            members = await scraper.scrape()

        self.assertEqual(members["1"]["fans"][-1], 30)
        self.assertEqual(scraper.get_data_date(), date(2026, 10, 31))
        self.assertIsNone(scraper.get_closing_month_rank())


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
