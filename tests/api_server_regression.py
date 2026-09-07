import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from bot.api_server import _backfill_month, _recalculate_club


CLUB_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
MEMBER_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def make_club(**overrides):
    values = {
        "club_id": CLUB_ID,
        "daily_quota": 100,
        "quota_period": "daily",
        "timezone": "UTC",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class BackfillRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflicted_insert_is_not_reported_as_backfilled(self):
        club = make_club()
        scraped = {"123": {"join_day": 1, "fans": [0, 50]}}

        with (
            patch("bot.api_server.db.fetch", new=AsyncMock(side_effect=[[], []])),
            patch(
                "bot.api_server.db.fetchrow",
                new=AsyncMock(
                    return_value={"member_id": MEMBER_ID, "join_date": date(2026, 9, 1)}
                ),
            ),
            patch("bot.api_server.db.fetchval", new=AsyncMock(return_value=None)),
        ):
            count = await _backfill_month(club, scraped, 2026, 9)

        self.assertEqual(count, 0)


class RecalculationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_calendar_day_resets_days_behind(self):
        club = make_club()
        today = date.today()
        month_start = today.replace(day=1)
        first = month_start
        third = month_start.replace(day=3)
        member = {"member_id": MEMBER_ID, "join_date": month_start}
        history = [
            {"id": 1, "date": first, "cumulative_fans": 0},
            {"id": 2, "date": third, "cumulative_fans": 0},
        ]
        execute = AsyncMock()

        with (
            patch(
                "bot.api_server.db.fetch",
                new=AsyncMock(side_effect=[[], [member], history]),
            ),
            patch("bot.api_server.db.execute", new=execute),
        ):
            updated = await _recalculate_club(club)

        self.assertEqual(updated, 2)
        self.assertEqual([call.args[3] for call in execute.await_args_list], [1, 1])


if __name__ == "__main__":
    unittest.main()
