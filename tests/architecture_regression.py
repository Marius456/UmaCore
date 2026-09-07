import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.commands.common import Club, ClubAutocompleteMixin
from services.quota_schedule import QuotaSchedule, advance_days_behind


class QuotaPolicyTests(unittest.TestCase):
    def test_schedule_and_streak_rules_are_transport_independent(self):
        schedule = QuotaSchedule(
            date(2026, 9, 7),
            "daily",
            100,
            [{"effective_date": date(2026, 9, 3), "daily_quota": 200}],
        )

        self.assertEqual(schedule.expected(date(2026, 9, 2), date(2026, 9, 4)), 500)
        self.assertEqual(
            advance_days_behind(date(2026, 9, 3), 3, date(2026, 9, 5), -1),
            1,
        )

    def test_schedule_rejects_cross_month_use(self):
        schedule = QuotaSchedule(date(2026, 9, 1), "daily", 100, [])
        with self.assertRaises(ValueError):
            schedule.expected(date(2026, 9, 1), date(2026, 10, 1))


class ClubAutocompleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_autocomplete_is_guild_scoped_and_casefolded(self):
        mixin = ClubAutocompleteMixin()
        interaction = SimpleNamespace(guild_id=123)
        with patch.object(
            Club,
            "get_names_for_guild",
            new=AsyncMock(return_value=["Straße", "Other"]),
        ) as names:
            choices = await mixin.club_autocomplete(interaction, "STRASSE")

        names.assert_awaited_once_with(123)
        self.assertEqual([choice.value for choice in choices], ["Straße"])


if __name__ == "__main__":
    unittest.main()
