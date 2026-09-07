import asyncio
from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from bot.tasks import BotTasks
from models.club import Club
from models.quota_requirement import QuotaRequirement
from models.quota_history import QuotaHistory
from services import prediction_store
from services.leaderboard_report_service import (
    LeaderboardReportService,
    ReportEmbeds,
)
from services.quota_calculator import QuotaCalculator


CLUB_ID = UUID("11111111-1111-1111-1111-111111111111")
MEMBER_ID = UUID("44444444-4444-4444-4444-444444444444")


def make_club(*, guild_id=123):
    return Club(
        club_id=CLUB_ID,
        club_name="Test",
        scrape_url="https://example.invalid",
        circle_id="123",
        guild_id=guild_id,
        daily_quota=1_000_000,
        quota_period="daily",
        timezone="UTC",
        scrape_time=time(16),
        is_active=True,
        report_channel_id=None,
        alert_channel_id=None,
        monthly_info_channel_id=None,
        monthly_info_message_id=None,
        created_at=None,
        updated_at=None,
    )


class GuildIsolationTests(unittest.IsolatedAsyncioTestCase):
    def test_unassigned_club_is_not_a_cross_guild_wildcard(self):
        self.assertFalse(make_club(guild_id=None).belongs_to_guild(123))
        self.assertTrue(make_club(guild_id=123).belongs_to_guild(123))
        self.assertFalse(make_club(guild_id=123).belongs_to_guild(456))

    async def test_guild_queries_do_not_include_unassigned_clubs(self):
        with patch("models.club.db.fetch", new=AsyncMock(return_value=[])) as fetch:
            await Club.get_names_for_guild(123)

        query, guild_id = fetch.await_args.args
        self.assertNotIn("guild_id IS NULL", query)
        self.assertEqual(guild_id, 123)


class QuotaCalculationTests(unittest.TestCase):
    def test_prefetched_schedule_applies_midmonth_changes(self):
        result = QuotaCalculator.calculate_expected_fans_from_schedule(
            date(2026, 9, 1),
            date(2026, 9, 4),
            "daily",
            1_000_000,
            [
                {"effective_date": date(2026, 9, 3), "daily_quota": 2_000_000}
            ],
        )
        self.assertEqual(result, 6_000_000)

    def test_prefetched_schedule_uses_latest_change_before_join(self):
        result = QuotaCalculator.calculate_expected_fans_from_schedule(
            date(2026, 9, 5),
            date(2026, 9, 6),
            "weekly",
            700_000,
            [
                {"effective_date": date(2026, 9, 3), "daily_quota": 1_400_000}
            ],
        )
        self.assertEqual(result, 400_000)


class ConsecutiveDayTests(unittest.IsolatedAsyncioTestCase):
    async def test_calculator_breaks_streak_across_missing_calendar_day(self):
        history = [
            SimpleNamespace(date=date(2026, 9, 5), deficit_surplus=-1),
            SimpleNamespace(date=date(2026, 9, 3), deficit_surplus=-1),
        ]
        with patch.object(
            QuotaHistory,
            "get_last_n_days",
            new=AsyncMock(return_value=history),
        ):
            result = await QuotaCalculator()._calculate_days_behind(
                MEMBER_ID, -1, date(2026, 9, 6)
            )

        self.assertEqual(result, 2)

    async def test_history_counter_stops_at_recovery_day(self):
        rows = [
            {"date": date(2026, 9, 7), "deficit_surplus": -1},
            {"date": date(2026, 9, 6), "deficit_surplus": 1},
            {"date": date(2026, 9, 5), "deficit_surplus": -1},
        ]
        with patch(
            "models.quota_history.db.fetch", new=AsyncMock(return_value=rows)
        ):
            result = await QuotaHistory.check_consecutive_behind_days(
                MEMBER_ID, 10, date(2026, 9, 7)
            )

        self.assertEqual(result, 1)


class MonthlyResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_preserves_history_and_quota_audit_rows(self):
        calculator = QuotaCalculator()
        calculator._get_previous_cumulative_totals = AsyncMock(return_value={})
        calculator._detect_monthly_reset_from_scraped = MagicMock(return_value=True)
        calculator._auto_deactivate_missing_members = AsyncMock()

        with (
            patch("services.quota_calculator.db.execute", new=AsyncMock()) as execute,
            patch("services.quota_calculator.db.fetch", new=AsyncMock(return_value=[])),
            patch(
                "services.quota_calculator.Club.get_by_id",
                new=AsyncMock(return_value=make_club()),
            ),
        ):
            await calculator.process_scraped_data(
                CLUB_ID, {}, date(2026, 9, 1), 1
            )

        queries = [call.args[0] for call in execute.await_args_list]
        self.assertTrue(any("UPDATE members" in query for query in queries))
        self.assertFalse(any("DELETE FROM quota_history" in query for query in queries))
        self.assertFalse(any("DELETE FROM quota_requirements" in query for query in queries))


class QuotaRequirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_does_not_carry_prior_month_override_forward(self):
        with (
            patch(
                "models.quota_requirement.db.fetchval",
                new=AsyncMock(return_value=None),
            ) as fetchval,
            patch(
                "models.club.Club.get_by_id",
                new=AsyncMock(return_value=make_club()),
            ),
        ):
            result = await QuotaRequirement.get_quota_for_date(
                CLUB_ID, date(2026, 9, 7)
            )

        query = fetchval.await_args.args[0]
        self.assertIn("date_trunc('month'", query)
        self.assertEqual(result, 1_000_000)


class PredictionStoreTests(unittest.TestCase):
    def test_snapshot_is_uuid_keyed_atomic_and_validated(self):
        with TemporaryDirectory() as directory:
            with patch.object(prediction_store, "PREDICTIONS_DIR", directory):
                prediction_store.save_prediction_snapshot(
                    CLUB_ID,
                    "../../unsafe/name",
                    date(2026, 9, 7),
                    [{"challenger": "A", "target": "B"}],
                )
                loaded = prediction_store.load_prediction_snapshot(
                    CLUB_ID, date(2026, 9, 7)
                )

                self.assertEqual(loaded[0]["challenger"], "A")
                paths = list(Path(directory).iterdir())
                self.assertEqual(len(paths), 1)
                self.assertEqual(paths[0].parent, Path(directory))
                self.assertIn(str(CLUB_ID), paths[0].name)


class PredictionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_predictions_are_committed_only_by_delivery_hook(self):
        embeds = ReportEmbeds(
            MagicMock(),
            CLUB_ID,
            "Test",
            date(2026, 9, 7),
            [],
        )
        with patch(
            "services.leaderboard_report_service.save_prediction_snapshot"
        ) as save:
            await LeaderboardReportService.persist_delivered_predictions(embeds)

        save.assert_called_once_with(CLUB_ID, "Test", date(2026, 9, 7), [])

    async def test_failed_embed_delivery_stops_before_later_embeds(self):
        channel = SimpleNamespace(
            send=AsyncMock(side_effect=[None, RuntimeError("send failed")])
        )
        with self.assertRaises(RuntimeError):
            await BotTasks._send_embeds(channel, [object(), object(), object()])

        self.assertEqual(channel.send.await_count, 2)


class ScheduledTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_embed_lists_are_sent_individually(self):
        channel = SimpleNamespace(send=AsyncMock())
        embeds = [object(), object()]

        await BotTasks._send_embeds(channel, embeds)

        self.assertEqual(channel.send.await_count, 2)
        self.assertIs(channel.send.await_args_list[0].kwargs["embed"], embeds[0])
        self.assertIs(channel.send.await_args_list[1].kwargs["embed"], embeds[1])

    async def test_running_guard_is_cleared_after_failure(self):
        tasks = BotTasks(SimpleNamespace())
        club = make_club()
        tasks._running_club_ids.add(club.club_id)
        tasks.daily_check_for_club = AsyncMock(side_effect=RuntimeError("boom"))

        with self.assertRaises(RuntimeError):
            await tasks._run_scheduled_daily_check(club)

        self.assertNotIn(club.club_id, tasks._running_club_ids)

    async def test_shutdown_waits_for_background_task_cleanup(self):
        tasks = BotTasks(SimpleNamespace())
        cleanup_finished = asyncio.Event()

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleanup_finished.set()

        background = asyncio.create_task(worker())
        await asyncio.sleep(0)
        tasks._scheduled_tasks.add(background)
        tasks._running_club_ids.add(CLUB_ID)

        await tasks.stop_tasks()

        self.assertTrue(cleanup_finished.is_set())
        self.assertTrue(background.done())
        self.assertEqual(tasks._scheduled_tasks, set())
        self.assertEqual(tasks._running_club_ids, set())


if __name__ == "__main__":
    unittest.main()
