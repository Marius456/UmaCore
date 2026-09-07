import unittest
from datetime import date
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
        fetch = AsyncMock(side_effect=[([], None), (older_rows, 10), ([], None)])

        with patch.object(HighscoreService, "_fetch_and_parse_api_month", new=fetch):
            rows, _ = await HighscoreService._fetch_all_months("123")

        self.assertEqual(rows, older_rows)
        self.assertEqual(fetch.await_count, 3)

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

        fetch = AsyncMock(side_effect=[([{"row": 1}], 10), ([], None)])
        with (
            patch("services.highscore_service.aiohttp.ClientSession", return_value=SessionContext()) as constructor,
            patch.object(HighscoreService, "_fetch_and_parse_api_month", new=fetch),
        ):
            await HighscoreService._fetch_all_months("123")

        constructor.assert_called_once()
        self.assertEqual(fetch.await_count, 2)
        self.assertIs(fetch.await_args_list[0].kwargs["session"], session)
        self.assertIs(fetch.await_args_list[1].kwargs["session"], session)


class HighscoreAlgorithmTests(unittest.TestCase):
    def test_leader_streaks_break_across_missing_calendar_days(self):
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
        self.assertEqual(total["streak"], 2)

    def test_total_leader_streak_does_not_carry_absent_member_forward(self):
        rows = [
            {"date": date(2026, 9, 1), "lifetime_fans": 1_000, "trainer_name": "A"},
            {"date": date(2026, 9, 2), "lifetime_fans": 1_200, "trainer_name": "A"},
            {"date": date(2026, 9, 1), "lifetime_fans": 100, "trainer_name": "B"},
            {"date": date(2026, 9, 2), "lifetime_fans": 150, "trainer_name": "B"},
            {"date": date(2026, 9, 3), "lifetime_fans": 200, "trainer_name": "B"},
        ]

        result = HighscoreService._compute_longest_first_place_streak_by_total(rows)

        self.assertEqual(result["name"], "A")
        self.assertEqual(result["streak"], 2)
        self.assertEqual(result["end_date"], date(2026, 9, 2))

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


if __name__ == "__main__":
    unittest.main()
