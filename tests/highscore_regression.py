import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.highscore_service import HighscoreService


class HighscoreFetchRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_current_month_does_not_hide_older_history(self):
        older_rows = [
            {
                "date": date(2026, 8, 1),
                "lifetime_fans": 100,
                "trainer_name": "Trainer",
            }
        ]
        fetch = AsyncMock(side_effect=[([], None), (older_rows, 10)] + [([], None)] * 1000)

        with patch.object(HighscoreService, "_fetch_and_parse_api_month", new=fetch):
            rows, _ = await HighscoreService._fetch_all_months("123")

        self.assertEqual(rows, older_rows)
        self.assertGreaterEqual(fetch.await_count, 3)

    async def test_transient_older_month_failure_does_not_return_partial_records(self):
        current_rows = [
            {
                "date": object(),
                "lifetime_fans": 100,
                "trainer_name": "Trainer",
            }
        ]
        fetch = AsyncMock(
            side_effect=[
                (current_rows, 10),
                RuntimeError("temporary upstream failure"),
            ]
        )

        with patch.object(HighscoreService, "_fetch_and_parse_api_month", new=fetch):
            with self.assertRaisesRegex(RuntimeError, "temporary upstream failure"):
                await HighscoreService._fetch_all_months("123")

        self.assertEqual(fetch.await_count, 2)

    async def test_month_scan_reuses_one_http_session(self):
        session = SimpleNamespace()

        class SessionContext:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *_):
                return False

        fetch = AsyncMock(side_effect=[([{"row": 1}], 10)] + [([], None)] * 1000)
        with (
            patch("services.highscore_service.aiohttp.ClientSession", return_value=SessionContext()) as constructor,
            patch.object(HighscoreService, "_fetch_and_parse_api_month", new=fetch),
        ):
            await HighscoreService._fetch_all_months("123")

        constructor.assert_called_once()
        self.assertGreaterEqual(fetch.await_count, 2)
        self.assertIs(fetch.await_args_list[0].kwargs["session"], session)
        self.assertIs(fetch.await_args_list[1].kwargs["session"], session)


class HighscoreAlgorithmTests(unittest.TestCase):
    def test_only_total_reign_bridges_short_missing_calendar_gap(self):
        rows = []
        for day, a_fans, b_fans in (
            (1, 100, 1_000),
            (2, 300, 1_050),
            (4, 500, 1_100),
            (5, 700, 1_150),
        ):
            rows.extend(
                [
                    {"date": date(2026, 9, day), "lifetime_fans": a_fans, "trainer_name": "A"},
                    {"date": date(2026, 9, day), "lifetime_fans": b_fans, "trainer_name": "B"},
                ]
            )

        daily = HighscoreService._compute_longest_first_place_streak(rows)
        total = HighscoreService._compute_longest_first_place_streak_by_total(rows)

        self.assertIsNone(daily)
        self.assertEqual(total["streak"], 4)
        self.assertEqual(total["inferred_days"], 1)

    def test_total_leader_streak_does_not_carry_absent_member_forward(self):
        rows = [
            {"date": date(2026, 9, 1), "lifetime_fans": 1_000, "trainer_name": "A"},
            {"date": date(2026, 9, 2), "lifetime_fans": 1_200, "trainer_name": "A"},
            {"date": date(2026, 9, 3), "lifetime_fans": 1_400, "trainer_name": "A"},
            {"date": date(2026, 9, 1), "lifetime_fans": 100, "trainer_name": "B"},
            {"date": date(2026, 9, 2), "lifetime_fans": 150, "trainer_name": "B"},
            {"date": date(2026, 9, 3), "lifetime_fans": 200, "trainer_name": "B"},
            {"date": date(2026, 9, 4), "lifetime_fans": 250, "trainer_name": "B"},
        ]

        result = HighscoreService._compute_longest_first_place_streak_by_total(rows)

        self.assertEqual(result["name"], "A")
        self.assertEqual(result["streak"], 2)
        self.assertEqual(result["end_date"], date(2026, 9, 3))

    def test_total_leader_streak_uses_indexed_daily_history(self):
        rows = []
        for day, a_fans, b_fans in (
            (1, 100, 1_000),
            (2, 300, 1_050),
            (3, 500, 1_100),
            (4, 700, 1_150),
        ):
            rows.extend(
                [
                    {"date": date(2026, 9, day), "lifetime_fans": a_fans, "trainer_name": "A"},
                    {"date": date(2026, 9, day), "lifetime_fans": b_fans, "trainer_name": "B"},
                ]
            )

        result = HighscoreService._compute_longest_first_place_streak_by_total(rows)

        self.assertEqual(result["name"], "A")
        self.assertEqual(result["streak"], 3)


class HighscoreCorrectionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def row(name, month, day, fans, endpoint=False):
        return dict(trainer_name=name, date=date(2026, month, day),
                    lifetime_fans=fans, is_end_of_month=endpoint)

    def test_endcore_monthly_record_excludes_kilua_absence(self):
        rows = [
            self.row("Kilua", 1, 1, 756634836),
            self.row("Kilua", 1, 31, 858294897),
            self.row("Kilua", 8, 2, 1974615724),
            self.row("Kilua", 8, 31, 2346487268),
            self.row("Kilua", 8, 31, 2358429057, True),
            self.row("NatsuRegis", 8, 1, 4285994240),
            self.row("NatsuRegis", 8, 31, 5318214542),
            self.row("NatsuRegis", 8, 31, 5341343259, True),
        ]
        # Full daily coverage is required to attribute the whole interval.
        for name in ("Kilua", "NatsuRegis"):
            start = next(r for r in rows if r["trainer_name"] == name and r["date"].month == 8)
            end = next(r for r in rows if r["trainer_name"] == name and r["date"] == date(2026, 8, 31))
            for day in range(start["date"].day + 1, 31):
                fans = start["lifetime_fans"] + (end["lifetime_fans"] - start["lifetime_fans"]) * (day - start["date"].day) // (31 - start["date"].day)
                rows.append(self.row(name, 8, day, fans))
        result = HighscoreService._compute_best_monthly_total(list(reversed(rows)))
        self.assertEqual(result["name"], "NatsuRegis")
        self.assertEqual(result["total"], 1055349019)
        self.assertFalse(result["incomplete"])

    def test_monthly_baseline_is_local_even_in_adjacent_months(self):
        rows = [self.row("A", 7, 31, 100), self.row("A", 8, 10, 1000),
                self.row("A", 8, 11, 1100)]
        self.assertEqual(HighscoreService._compute_best_monthly_total(rows)["total"], 100)

    def test_zero_baseline_day_does_not_extend_january_reign(self):
        rows = []
        for day in range(1, 32):
            rows.extend([self.row("NatsuRegis", 1, day, 10000 + day * 200),
                         self.row("B", 1, day, 100 + day * 50)])
        result = HighscoreService._compute_longest_first_place_streak_by_total(rows)
        self.assertEqual(result["streak"], 30)
        self.assertEqual(result["start_date"], date(2026, 1, 2))
        self.assertEqual(result["end_date"], date(2026, 1, 31))

    def test_positive_tie_breaks_reign_despite_different_lifetime_fans(self):
        rows = []
        for day, a, b in [(1, 1000, 100), (2, 1100, 150), (3, 1200, 300),
                          (4, 1400, 350), (5, 1600, 400)]:
            rows.extend([self.row("A", 9, day, a), self.row("B", 9, day, b)])
        result = HighscoreService._compute_longest_first_place_streak_by_total(rows)
        self.assertEqual(result["streak"], 2)
        self.assertEqual(result["start_date"], date(2026, 9, 4))

    def test_old_month_endpoint_cannot_inflate_rejoining_members_reign(self):
        rows = [self.row("A", 1, 31, 10, True)]
        for day, a, b in [(1, 1000, 100), (2, 1010, 150), (3, 1020, 200)]:
            rows.extend([self.row("A", 9, day, a), self.row("B", 9, day, b)])
        result = HighscoreService._compute_longest_first_place_streak_by_total(rows)
        self.assertEqual(result["name"], "B")
        self.assertEqual(result["streak"], 2)

    def test_month_endpoint_does_not_replace_real_daily_snapshot(self):
        rows = [self.row("A", 8, 30, 100), self.row("A", 8, 31, 200),
                self.row("A", 8, 31, 900, True), self.row("A", 9, 1, 900)]
        for ordered in (rows, list(reversed(rows))):
            result = HighscoreService._compute_best_daily_gain(ordered)
            self.assertEqual(result["delta"], 700)
            self.assertEqual(result["date"], date(2026, 9, 1))
            streak = HighscoreService._compute_longest_first_place_streak(ordered)
            self.assertEqual(streak["streak"], 2)

    async def test_embed_explains_recorded_rank_coverage(self):
        rows = [self.row("A", 9, 1, 100), self.row("A", 9, 2, 200)]
        rank = dict(best_club_rank=5, best_club_rank_date=date(2026, 9, 6),
                    club_rank_history_start=date(2026, 9, 1),
                    club_rank_history_end=date(2026, 9, 24))
        with (
            patch.object(HighscoreService, "_fetch_all_months",
                         new=AsyncMock(return_value=(rows, {(2026, 6): 2}))),
            patch("services.highscore_service.ClubRankHistory.get_best_rank",
                  new=AsyncMock(return_value=rank)),
        ):
            embed = await HighscoreService.generate_highscore_embed(None, "ENDCORE", "123")
        field = next(f for f in embed.fields if f.name == "👑 Best Recorded Club Rank")
        self.assertIn("September 01, 2026", field.value)
        self.assertIn("September 24, 2026", field.value)
        self.assertIn("source snapshot dates", embed.description)


class ReignGapTests(unittest.IsolatedAsyncioTestCase):
    def rows(self, observations):
        rows = []
        for day, a, b in observations:
            for name, fans in (("A", a), ("B", b)):
                if fans is not None:
                    rows.append(dict(date=date.fromisoformat(day), trainer_name=name,
                                     lifetime_fans=fans))
        for name in ("A", "B"):
            member_rows = [r for r in rows if r["trainer_name"] == name and r["lifetime_fans"] > 0]
            if member_rows and member_rows[0]["date"].day > 1:
                first = member_rows[0]
                for day in range(1, first["date"].day):
                    rows.append(dict(date=first["date"].replace(day=day), trainer_name=name,
                                     lifetime_fans=first["lifetime_fans"]))
        return rows

    def compute(self, observations):
        return HighscoreService._compute_longest_first_place_streak_by_total(
            self.rows(observations))

    def test_month_reset_bridges_matching_leaders(self):
        result = self.compute([
            ("2026-08-30", 100, 100), ("2026-08-31", 300, 150),
            ("2026-09-01", 400, 200), ("2026-09-02", 600, 250),
        ])
        self.assertEqual(result["streak"], 3)
        self.assertEqual(result["inferred_days"], 1)
        self.assertEqual(result["start_date"], date(2026, 8, 31))

    def test_zero_member_snapshot_is_unknown_even_if_competitor_present(self):
        result = self.compute([
            ("2026-09-01", 100, 100), ("2026-09-02", 300, 150),
            ("2026-09-03", 0, 200), ("2026-09-04", 700, 250),
        ])
        self.assertEqual(result["name"], "A")
        self.assertEqual(result["streak"], 3)
        self.assertEqual(result["inferred_days"], 1)

    def test_two_day_gap_can_bridge_but_three_cannot(self):
        for resume, expected in ((5, 5), (6, 2)):
            with self.subTest(resume=resume):
                result = self.compute([
                    ("2026-09-01", 100, 100), ("2026-09-02", 300, 150),
                    (f"2026-09-{resume:02}", 700, 250),
                    (f"2026-09-{resume + 1:02}", 900, 300),
                ])
                self.assertEqual(result["streak"], expected)
                self.assertEqual(result["inferred_days"], 2 if resume == 5 else 0)

    def test_different_leader_after_gap_does_not_inherit_days(self):
        result = self.compute([
            ("2026-09-01", 100, 100), ("2026-09-02", 300, 150),
            ("2026-09-04", 400, 900), ("2026-09-05", 450, 2000),
            ("2026-09-06", 500, 2100),
        ])
        self.assertEqual(result["name"], "B")
        self.assertEqual(result["streak"], 2)
        self.assertEqual(result["inferred_days"], 0)

    def test_trailing_unknown_days_remain_pending(self):
        result = self.compute([
            ("2026-08-29", 100, 100), ("2026-08-30", 300, 150),
            ("2026-08-31", 500, 200), ("2026-09-01", 600, 250),
            ("2026-09-02", 600, 250),
        ])
        self.assertEqual(result["streak"], 2)
        self.assertEqual(result["end_date"], date(2026, 8, 31))
        self.assertEqual(result["inferred_days"], 0)

    def test_verified_other_leader_breaks_reign(self):
        result = self.compute([
            ("2026-09-01", 100, 100), ("2026-09-02", 300, 150),
            ("2026-09-03", 350, 500), ("2026-09-04", 900, 550),
            ("2026-09-05", 1100, 600),
        ])
        self.assertEqual(result["streak"], 2)
        self.assertEqual(result["start_date"], date(2026, 9, 4))
        self.assertEqual(result["inferred_days"], 0)

    def test_future_snapshot_cannot_extend_reign(self):
        today = datetime.now(timezone.utc).date()
        rows = [dict(date=today + timedelta(days=offset), trainer_name="A",
                     lifetime_fans=100 + (offset + 3) * 100)
                for offset in (-3, -2, -1, 0, 1)]
        # Keep this test independent of the calendar month's baseline reset.
        with patch("services.highscore_service.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 26, tzinfo=timezone.utc)
            for index, row in enumerate(rows):
                row["date"] = date(2026, 9, 23) + timedelta(days=index)
            result = HighscoreService._compute_longest_first_place_streak_by_total(rows)
        self.assertEqual(result["end_date"], date(2026, 9, 26))
        self.assertEqual(result["streak"], 3)

    async def test_embed_labels_inferred_days(self):
        rows = self.rows([
            ("2026-08-30", 100, 100), ("2026-08-31", 300, 150),
            ("2026-09-01", 400, 200), ("2026-09-02", 600, 250),
        ])
        with (
            patch.object(HighscoreService, "_fetch_all_months",
                         new=AsyncMock(return_value=(rows, {}))),
            patch("services.highscore_service.ClubRankHistory.get_best_rank",
                  new=AsyncMock(return_value=None)),
        ):
            embed = await HighscoreService.generate_highscore_embed(None, "Test", "123")
        field = next(f for f in embed.fields if f.name == "👑 Longest #1 Reign")
        self.assertIn("Includes 1 inferred day(s)", field.value)
        self.assertIn("at most 2 days", field.value)


class HighscoreDataQualityTests(unittest.IsolatedAsyncioTestCase):
    def rows(self, series):
        return [dict(date=date(2026, 9, day), trainer_name=name,
                     trainer_id=name, lifetime_fans=fans)
                for name, values in series.items() for day, fans in values]

    def test_unrelated_gap_cannot_hide_confirmed_overtake(self):
        rows = self.rows({
            "A": [(1,100),(2,300),(3,350),(4,900),(5,1100)],
            "B": [(1,100),(2,150),(3,500),(4,550),(5,600)],
            "C": [(1,100),(2,101),(4,102),(5,103)],
        })
        result = HighscoreService._compute_longest_first_place_streak_by_total(rows)
        self.assertEqual(result["name"], "A")
        self.assertEqual(result["start_date"], date(2026, 9, 4))
        self.assertEqual(result["streak"], 2)

    def test_long_and_unresolved_outages_do_not_award_competitor_reign(self):
        for returning in (True, False):
            with self.subTest(returning=returning):
                a = [(1,100),(2,1000)] + ([(6,1100)] if returning else [])
                rows = self.rows({"A": a, "B": [(1,100),(2,150),(3,200),(4,250),(5,300),(6,350)]})
                self.assertIsNone(HighscoreService._compute_longest_first_place_streak_by_total(rows))
                self.assertIsNone(HighscoreService._compute_longest_first_place_streak(rows))

    def test_adjacent_endpoint_recovers_lost_month_start(self):
        rows = self.rows({"A": [(2,200),(3,210),(4,220)],
                          "B": [(1,100),(2,110),(3,130),(4,150)]})
        rows.append(dict(date=date(2026,8,31), trainer_name="A", trainer_id="A",
                         lifetime_fans=100, is_end_of_month=True))
        total = HighscoreService._compute_best_monthly_total(rows)
        self.assertEqual(total["name"], "A")
        self.assertEqual(total["total"], 120)
        self.assertTrue(total["incomplete"])
        reign = HighscoreService._compute_longest_first_place_streak_by_total(rows)
        self.assertEqual(reign["name"], "A")
        self.assertEqual(reign["streak"], 3)

    def test_drop_and_recovery_cannot_generate_false_daily_record(self):
        rows = self.rows({"A": [(1,1000),(2,100),(3,1100),(4,1150)]})
        result = HighscoreService._compute_best_daily_gain(rows)
        self.assertEqual(result["delta"], 50)
        self.assertEqual(result["date"], date(2026,9,4))

    def test_same_name_distinct_ids_and_rename(self):
        rows = self.rows({"1": [(1,100),(2,200),(3,300)], "2": [(1,10000),(2,10001),(3,10002)]})
        for row in rows:
            row["trainer_name"] = "Same name"
        rows[2]["trainer_name"] = "Renamed"
        daily = HighscoreService._compute_best_daily_gain(rows)
        self.assertEqual(daily["delta"], 100)
        self.assertEqual(daily["name"], "Renamed")
        self.assertEqual(HighscoreService._compute_best_monthly_total(rows)["total"], 200)
        self.assertEqual(HighscoreService._compute_longest_first_place_streak_by_total(rows)["streak"], 2)

    def test_rejoin_does_not_credit_unobserved_outside_club_gains(self):
        rows = self.rows({"A": [(1,100),(2,200),(10,1000),(11,1100)]})
        result = HighscoreService._compute_best_monthly_total(rows)
        self.assertEqual(result["total"], 200)
        self.assertTrue(result["incomplete"])

    def test_inferred_reign_is_separate_from_verified(self):
        rows = self.rows({"A": [(1,100),(2,200),(4,300),(5,400)],
                          "B": [(1,100),(2,101),(4,102),(5,103)]})
        estimate = HighscoreService._compute_longest_first_place_streak_by_total(rows)
        verified = HighscoreService._compute_longest_first_place_streak_by_total(rows, verified_only=True)
        self.assertEqual(estimate["streak"], 4)
        self.assertEqual(estimate["inferred_days"], 1)
        self.assertEqual(verified["streak"], 2)
        self.assertEqual(verified["inferred_days"], 0)

    async def test_empty_historical_month_does_not_stop_scan(self):
        old_rows = self.rows({"A": [(1,100)]})
        async def fetch(circle, year, month, **kwargs):
            return (old_rows, 10) if (year, month) == (2025, 6) else ([], None)
        with patch.object(HighscoreService, "_fetch_and_parse_api_month", side_effect=fetch) as mocked:
            rows, _ = await HighscoreService._fetch_all_months("123")
        self.assertEqual(rows, old_rows)
        self.assertEqual(mocked.await_args.args[1:], (2025, 6))

    async def test_parser_keeps_ids_rejects_invalid_rank_and_future_endpoint(self):
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def json(self):
                return {"circle": {"monthly_rank": 0}, "members": [
                    {"viewer_id": 123, "trainer_name": "A", "daily_fans": [100,0,None,200],
                     "next_month_start": 500},
                    {"trainer_name": "No ID", "daily_fans": [900]},
                ]}
        session = SimpleNamespace(get=lambda url: Response())
        with patch("services.highscore_service.datetime") as clock:
            clock.now.return_value = datetime(2026,9,3,tzinfo=timezone.utc)
            rows, rank = await HighscoreService._fetch_and_parse_api_month("1",2026,9,session=session)
        self.assertIsNone(rank)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trainer_id"], "123")

    def test_month_without_baseline_cannot_create_verified_competitor(self):
        rows = self.rows({"A": [(3,1000),(4,1100)],
                          "B": [(1,100),(2,200),(3,300),(4,400)]})
        result = HighscoreService._compute_longest_first_place_streak_by_total(rows)
        self.assertIsNone(result)


class HighscoreCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_crosspost_requires_explicit_option(self):
        from bot.commands.leaderboard import LeaderboardCommands
        for post in (False, True):
            with self.subTest(post=post):
                channel = SimpleNamespace(send=AsyncMock())
                bot = SimpleNamespace(get_channel=lambda channel_id: channel)
                cog = LeaderboardCommands(bot)
                interaction = SimpleNamespace(
                    guild_id=1, channel_id=2, user="Tester",
                    response=SimpleNamespace(defer=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()),
                )
                club = SimpleNamespace(club_id=None, club_name="Test", circle_id="123",
                                       report_channel_id=3, belongs_to_guild=lambda guild: True)
                with (
                    patch("bot.commands.leaderboard.Club.get_by_name", new=AsyncMock(return_value=club)),
                    patch.object(HighscoreService, "generate_highscore_embed", new=AsyncMock(return_value="embed")),
                ):
                    await cog.club_highscores.callback(cog, interaction, "Test", post_to_report=post)
                interaction.followup.send.assert_awaited_once_with(embed="embed")
                self.assertEqual(channel.send.await_count, int(post))


if __name__ == "__main__":
    unittest.main()
