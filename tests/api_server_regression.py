import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from bot.api_server import _backfill_month, _recalculate_club
from bot.commands.admin import AdminCommands


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
        fetch = AsyncMock(
            side_effect=[
                [],
                [{"member_id": MEMBER_ID, "trainer_id": "123", "join_date": date(2026, 9, 1)}],
                [],
                [],
            ]
        )

        with patch("bot.api_server.db.fetch", new=fetch):
            count = await _backfill_month(club, scraped, 2026, 9)

        self.assertEqual(count, 0)
        self.assertEqual(fetch.await_count, 4)
        self.assertIn("UNNEST", fetch.await_args_list[-1].args[0])


class RecalculationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_calendar_day_resets_days_behind(self):
        club = make_club()
        today = date.today()
        month_start = today.replace(day=1)
        first = month_start
        third = month_start.replace(day=3)
        history = [
            {"id": 1, "member_id": MEMBER_ID, "join_date": month_start, "date": first, "cumulative_fans": 0},
            {"id": 2, "member_id": MEMBER_ID, "join_date": month_start, "date": third, "cumulative_fans": 0},
        ]
        connection = SimpleNamespace(executemany=AsyncMock())

        class Transaction:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_):
                return False

        with (
            patch(
                "bot.api_server.db.fetch",
                new=AsyncMock(side_effect=[[], history]),
            ),
            patch("bot.api_server.db.transaction", return_value=Transaction()),
        ):
            updated = await _recalculate_club(club)

        self.assertEqual(updated, 2)
        updates = connection.executemany.await_args.args[1]
        self.assertEqual([args[2] for args in updates], [1, 1])

    async def test_admin_recalculation_batches_updates(self):
        rows = [
            {"id": 1, "member_id": MEMBER_ID, "date": date(2026, 9, 1), "deficit_surplus": -1},
            {"id": 2, "member_id": MEMBER_ID, "date": date(2026, 9, 3), "deficit_surplus": -1},
        ]
        connection = SimpleNamespace(executemany=AsyncMock())

        class Transaction:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_):
                return False

        fetch = AsyncMock(return_value=rows)
        with (
            patch("config.database.db.fetch", new=fetch),
            patch("config.database.db.transaction", return_value=Transaction()),
        ):
            count = await AdminCommands._recalculate_days_behind(
                CLUB_ID, date(2026, 9, 7)
            )

        self.assertEqual(count, 2)
        fetch.assert_awaited_once()
        self.assertEqual(connection.executemany.await_args.args[1], [(1, 1), (1, 2)])


if __name__ == "__main__":
    unittest.main()
