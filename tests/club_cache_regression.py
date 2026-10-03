import asyncio
import json
import tempfile
import unittest
from copy import copy
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytz

from bot.tasks import BotTasks
from models.club import Club
from services.club_cache import ActiveClubCache
from tests.core_regression import make_club


class ClubCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cache = ActiveClubCache()
        self.club = make_club()
        self.model_cache = patch("models.club.active_club_cache", self.cache)
        self.model_cache.start()
        self.addCleanup(self.model_cache.stop)

    async def test_create_load_and_empty_cache_do_not_reload(self):
        tasks = BotTasks(SimpleNamespace(), club_cache=self.cache)
        with patch.object(Club, "get_all_active", new=AsyncMock(return_value=[])) as fetch:
            self.assertFalse(self.cache.loaded)
            await tasks.initialize_club_cache()
            self.assertTrue(self.cache.loaded)
            await tasks.initialize_club_cache()
            for _ in range(3):
                await tasks.scheduled_report_check.coro(tasks)
            fetch.assert_awaited_once()
        self.assertEqual(self.cache.active_clubs(), ())
        with patch("models.club.db.fetchrow", new=AsyncMock(return_value=vars(self.club))):
            created = await Club.create("Test", "https://example.invalid")
        self.assertEqual(self.cache.active_clubs()[0], created)
        self.assertIsNot(self.cache.active_clubs()[0], created)

    async def test_model_edits_patch_only_changed_fields(self):
        self.cache.load([self.club])
        stale = copy(self.club)
        with patch("models.club.db.execute", new=AsyncMock()):
            await self.club.update_settings(scrape_time="18:20", timezone="Europe/Vilnius")
            await stale.set_channels(report_channel_id=42, events_channel_id=99)
            await stale.set_monthly_info_location(100, 200)
            await stale.deactivate()
            self.assertEqual(self.cache.active_clubs(), ())
            await stale.update_settings(daily_quota=200)
            await stale.activate()
            cached = self.cache.active_clubs()[0]
            self.assertEqual(cached.scrape_time, time(18, 20))
            self.assertEqual(cached.timezone, "Europe/Vilnius")
            self.assertEqual(cached.daily_quota, 200)
            self.assertEqual(cached.report_channel_id, 42)
            self.assertEqual(cached.events_channel_id, 99)
            self.assertEqual(cached.monthly_info_channel_id, 100)
            self.assertEqual(cached.monthly_info_message_id, 200)
            await stale.delete()
            self.assertEqual(self.cache.active_clubs(), ())

    async def test_failed_writes_leave_cache_unchanged(self):
        self.cache.load([self.club])
        operations = [
            lambda: self.club.update_settings(timezone="Europe/Vilnius"),
            lambda: self.club.set_channels(report_channel_id=42),
            lambda: self.club.set_monthly_info_location(100, 200),
            self.club.deactivate,
            self.club.activate,
            self.club.delete,
            lambda: Club.create("Other", "https://example.invalid"),
        ]
        with (
            patch("models.club.db.execute", new=AsyncMock(side_effect=RuntimeError("offline"))),
            patch("models.club.db.fetchrow", new=AsyncMock(side_effect=RuntimeError("offline"))),
        ):
            for operation in operations:
                with self.assertRaises(RuntimeError):
                    await operation()
                self.assertEqual(self.cache.active_clubs(), (self.club,))

    async def test_startup_replays_edits_and_deletes_during_load(self):
        tasks = BotTasks(SimpleNamespace(), club_cache=self.cache)
        deleted = copy(self.club)
        deleted.club_id = "deleted"
        added = copy(self.club)
        added.club_id = "added"

        async def load_rows():
            with patch("models.club.db.execute", new=AsyncMock()):
                await self.club.set_channels(events_channel_id=99)
                await deleted.delete()
            self.cache.add(added)
            return [make_club(), deleted]

        with patch.object(Club, "get_all_active", side_effect=load_rows):
            await tasks.initialize_club_cache()
        cached = {club.club_id: club for club in self.cache.active_clubs()}
        self.assertEqual(cached[self.club.club_id].events_channel_id, 99)
        self.assertIn("added", cached)
        self.assertNotIn("deleted", cached)

    async def test_startup_retries_with_capped_backoff_then_loads_once(self):
        tasks = BotTasks(SimpleNamespace(), club_cache=self.cache)
        with (
            patch.object(Club, "get_all_active", new=AsyncMock(
                side_effect=[RuntimeError("offline")] * 8 + [[self.club]]
            )) as fetch,
            patch("bot.tasks.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            await tasks.initialize_club_cache()
            await tasks.initialize_club_cache()
        self.assertEqual(fetch.await_count, 9)
        self.assertEqual([call.args[0] for call in sleep.await_args_list],
                         [5, 10, 20, 40, 80, 160, 300, 300])
        self.assertEqual(self.cache.active_clubs(), (self.club,))

    async def test_idle_schedule_and_event_checks_never_fetch_clubs(self):
        now = datetime.now(pytz.UTC)
        self.club.scrape_time = time((now.hour + 1) % 24, now.minute)
        self.cache.load([self.club])
        tasks = BotTasks(SimpleNamespace(), club_cache=self.cache)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps({"events": [{"title": "No dates"}]}))
            with (
                patch("bot.tasks.EVENTS_JSON_PATH", str(path)),
                patch.object(Club, "get_all_active", new=AsyncMock()) as fetch,
            ):
                for _ in range(3):
                    await tasks.scheduled_report_check.coro(tasks)
                    await tasks.event_notifications()
                fetch.assert_not_awaited()

    async def test_due_club_runs_once_and_receives_updated_settings(self):
        now = datetime.now(pytz.UTC)
        self.club.scrape_time = time(now.hour, now.minute)
        self.cache.load([self.club])
        with patch("models.club.db.execute", new=AsyncMock()):
            await self.club.set_channels(report_channel_id=42)
        tasks = BotTasks(SimpleNamespace(), club_cache=self.cache)
        started = asyncio.Event()
        release = asyncio.Event()

        async def check(club, run_date):
            self.assertEqual(club.report_channel_id, 42)
            started.set()
            await release.wait()
            tasks.last_runs[f"{club.club_id}_{run_date}"] = True

        tasks.daily_check_for_club = AsyncMock(side_effect=check)
        await tasks.scheduled_report_check.coro(tasks)
        await started.wait()
        await tasks.scheduled_report_check.coro(tasks)
        release.set()
        await asyncio.gather(*tasks._scheduled_tasks)
        await tasks.scheduled_report_check.coro(tasks)
        tasks.daily_check_for_club.assert_awaited_once()

    async def test_snapshot_is_detached_and_shutdown_cancels_startup_retry(self):
        self.cache.load([self.club])
        snapshot = self.cache.active_clubs()[0]
        snapshot.report_channel_id = 999
        self.assertIsNone(self.cache.active_clubs()[0].report_channel_id)
        tasks = BotTasks(SimpleNamespace(), club_cache=ActiveClubCache())
        attempted = asyncio.Event()

        async def fail():
            attempted.set()
            raise RuntimeError("offline")

        with patch.object(Club, "get_all_active", side_effect=fail):
            tasks._cache_load_task = asyncio.create_task(tasks.initialize_club_cache())
            await attempted.wait()
            await tasks.stop_tasks()
        self.assertTrue(tasks._cache_load_task.cancelled())

    async def test_report_and_event_loops_wait_for_the_shared_startup_load(self):
        tasks = BotTasks(SimpleNamespace(wait_until_ready=AsyncMock()), club_cache=self.cache)
        loading = asyncio.Event()
        release = asyncio.Event()

        async def load_rows():
            loading.set()
            await release.wait()
            return [self.club]

        with patch.object(Club, "get_all_active", side_effect=load_rows) as fetch:
            tasks._cache_load_task = asyncio.create_task(tasks.initialize_club_cache())
            report_ready = asyncio.create_task(tasks.before_scheduled_report_check())
            event_ready = asyncio.create_task(tasks.before_hourly_event_notifications())
            await loading.wait()
            await asyncio.sleep(0)
            self.assertFalse(report_ready.done())
            self.assertFalse(event_ready.done())
            release.set()
            await asyncio.gather(report_ready, event_ready)
            fetch.assert_awaited_once()
        self.assertTrue(self.cache.loaded)


if __name__ == "__main__":
    unittest.main()
