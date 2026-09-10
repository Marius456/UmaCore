import asyncio
from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytz

from bot.tasks import BotTasks
from models.club import Club
from models.member import Member
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


class ScrapeBatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_roster_uses_bulk_reads_and_writes(self):
        member = SimpleNamespace(
            member_id=MEMBER_ID,
            club_id=CLUB_ID,
            trainer_id="123",
            trainer_name="Trainer",
            join_date=date(2026, 9, 1),
            is_active=True,
            manually_deactivated=False,
            last_seen=date(2026, 9, 6),
            missing_scrapes=0,
        )
        connection = SimpleNamespace(execute=AsyncMock(), executemany=AsyncMock())

        class Transaction:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_):
                return False

        calculator = QuotaCalculator()
        calculator._auto_deactivate_missing_members = AsyncMock()

        with (
            patch.object(
                Member, "get_all_for_club", new=AsyncMock(return_value=[member])
            ) as roster,
            patch.object(Member, "get_by_trainer_id", new=AsyncMock()) as lookup,
            patch.object(QuotaHistory, "create", new=AsyncMock()) as create_history,
            patch("services.quota_calculator.db.fetch", new=AsyncMock(side_effect=[[], []])),
            patch(
                "services.quota_calculator.db.transaction",
                return_value=Transaction(),
            ),
            patch.object(Club, "get_by_id", new=AsyncMock(return_value=make_club())),
        ):
            result = await calculator.process_scraped_data(
                CLUB_ID,
                {
                    "123": {
                        "trainer_id": "123",
                        "name": "Trainer",
                        "fans": [100],
                        "join_day": 1,
                    }
                },
                date(2026, 9, 7),
                7,
            )

        self.assertEqual(result, (0, 1))
        roster.assert_awaited_once_with(CLUB_ID)
        lookup.assert_not_awaited()
        create_history.assert_not_awaited()
        connection.execute.assert_awaited_once()
        connection.executemany.assert_awaited_once()
        self.assertEqual(len(connection.executemany.await_args.args[1]), 1)


class QuotaRequirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_day_quota_change_is_an_upsert(self):
        row = {
            "id": UUID("66666666-6666-6666-6666-666666666666"),
            "club_id": CLUB_ID,
            "effective_date": date(2026, 9, 7),
            "daily_quota": 2_000_000,
            "set_by": "admin",
        }
        with patch(
            "models.quota_requirement.db.fetchrow",
            new=AsyncMock(return_value=row),
        ) as fetchrow:
            requirement = await QuotaRequirement.create(
                CLUB_ID, date(2026, 9, 7), 2_000_000, "admin"
            )

        query = fetchrow.await_args.args[0]
        self.assertIn("ON CONFLICT (club_id, effective_date)", query)
        self.assertEqual(requirement.daily_quota, 2_000_000)

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


class StatusSummaryQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_summary_uses_one_set_based_database_query(self):
        row = {
            "member_id": MEMBER_ID,
            "club_id": CLUB_ID,
            "trainer_id": "123",
            "trainer_name": "Trainer",
            "join_date": date(2026, 9, 1),
            "is_active": True,
            "manually_deactivated": False,
            "last_seen": date(2026, 9, 7),
            "missing_scrapes": 0,
            "history_id": UUID("55555555-5555-5555-5555-555555555555"),
            "history_date": date(2026, 9, 7),
            "cumulative_fans": 800,
            "expected_fans": 700,
            "deficit_surplus": 100,
            "days_behind": 0,
            "previous_cumulative_fans": 650,
            "period_start_fans": None,
        }
        fetch = AsyncMock(return_value=[row])

        with patch("services.quota_calculator.db.fetch", new=fetch):
            summary = await QuotaCalculator().get_member_status_summary(
                CLUB_ID, date(2026, 9, 7)
            )

        fetch.assert_awaited_once()
        self.assertEqual(summary["total_members"], 1)
        self.assertEqual(summary["on_track"][0]["yesterday_cumulative_fans"], 650)
        self.assertEqual(summary["on_track"][0]["history"].cumulative_fans, 800)

    async def test_month_history_query_uses_indexable_date_bounds(self):
        fetch = AsyncMock(return_value=[])
        with patch("models.quota_history.db.fetch", new=fetch):
            await QuotaHistory.get_current_month_for_club(CLUB_ID, 2026, 12)

        query, club_id, month_start, next_month = fetch.await_args.args
        self.assertNotIn("date_part", query)
        self.assertEqual(club_id, CLUB_ID)
        self.assertEqual(month_start, date(2026, 12, 1))
        self.assertEqual(next_month, date(2027, 1, 1))


class ScheduledTaskTests(unittest.IsolatedAsyncioTestCase):
    def test_scheduled_leaderboard_passes_all_club_goal_fields(self):
        rank_data = {
            "club_rank": 6,
            "monthly_rank": 1_137,
            "fans_to_next_tier": 95_035_383,
            "fans_to_lower_tier": 338_997_978,
            "last_month_rank": 981,
        }

        self.assertEqual(
            BotTasks._club_goal_kwargs(rank_data),
            {
                "club_rank": 6,
                "monthly_rank": 1_137,
                "fans_to_next_tier": 95_035_383,
                "fans_to_lower_tier": 338_997_978,
            },
        )

    def test_daily_check_only_matches_configured_minute(self):
        scheduled_time = time(18, 0)

        self.assertFalse(
            BotTasks._is_daily_check_time(datetime(2026, 9, 7, 17, 59), scheduled_time)
        )
        self.assertTrue(
            BotTasks._is_daily_check_time(datetime(2026, 9, 7, 18, 0), scheduled_time)
        )
        self.assertFalse(
            BotTasks._is_daily_check_time(datetime(2026, 9, 7, 18, 1), scheduled_time)
        )
        self.assertFalse(
            BotTasks._is_daily_check_time(datetime(2026, 9, 7, 18, 44), scheduled_time)
        )

    def test_scheduled_report_loop_polls_every_minute(self):
        self.assertEqual(BotTasks.scheduled_report_check.minutes, 1.0)

    def test_daily_check_uses_club_local_time(self):
        instant = pytz.UTC.localize(datetime(2026, 9, 7, 18, 0))
        utc_now = instant.astimezone(pytz.timezone("UTC"))
        vilnius_now = instant.astimezone(pytz.timezone("Europe/Vilnius"))

        self.assertTrue(BotTasks._is_daily_check_time(utc_now, time(18, 0)))
        self.assertTrue(BotTasks._is_daily_check_time(vilnius_now, time(21, 0)))
        self.assertFalse(BotTasks._is_daily_check_time(vilnius_now, time(18, 0)))

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
