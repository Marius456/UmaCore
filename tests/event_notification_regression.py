import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

from bot.tasks import BotTasks


class EventNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = SimpleNamespace(send=AsyncMock())
        self.bot = SimpleNamespace(get_channel=MagicMock(return_value=self.channel))
        self.tasks = BotTasks(self.bot)
        self.club = SimpleNamespace(
            club_id="club-1",
            club_name="Test Club",
            events_channel_id=123,
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.events_path = Path(self.temp_dir.name) / "events.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_events(self, events):
        self.events_path.write_text(json.dumps({"events": events}), encoding="utf-8")

    def event(self, *, start_hours=1, end_hours=72):
        now = datetime.now(pytz.UTC)
        return {
            "title": "Test event",
            "start_time": (now + timedelta(hours=start_hours)).isoformat(),
            "end_time": (now + timedelta(hours=end_hours)).isoformat(),
            "url": "https://example.invalid/event",
            "notified_clubs": [],
        }

    async def test_starting_notification_is_persisted_and_not_resent(self):
        self.write_events([self.event()])

        with (
            patch("bot.tasks.EVENTS_JSON_PATH", str(self.events_path)),
            patch("bot.tasks.Club.get_all_active", new=AsyncMock(return_value=[self.club])),
        ):
            await self.tasks.event_notifications()
            await self.tasks.event_notifications()

        self.channel.send.assert_awaited_once()
        saved = json.loads(self.events_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["events"][0]["notified_clubs"], ["club-1_starting"])

    async def test_starting_and_ending_notifications_use_separate_dedup_keys(self):
        self.write_events([self.event(start_hours=1, end_hours=2)])

        with (
            patch("bot.tasks.EVENTS_JSON_PATH", str(self.events_path)),
            patch("bot.tasks.Club.get_all_active", new=AsyncMock(return_value=[self.club])),
        ):
            await self.tasks.event_notifications()
            await self.tasks.event_notifications()

        self.assertEqual(self.channel.send.await_count, 2)
        saved = json.loads(self.events_path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["events"][0]["notified_clubs"],
            ["club-1_starting", "club-1_ending"],
        )

    async def test_ended_event_does_not_send_or_record_an_ending_notification(self):
        self.write_events([self.event(start_hours=-48, end_hours=-1)])

        with (
            patch("bot.tasks.EVENTS_JSON_PATH", str(self.events_path)),
            patch("bot.tasks.Club.get_all_active", new=AsyncMock(return_value=[self.club])),
        ):
            await self.tasks.event_notifications()

        self.channel.send.assert_not_awaited()
        saved = json.loads(self.events_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["events"][0]["notified_clubs"], [])

    async def test_daily_scraper_waits_for_the_same_event_lock(self):
        scrape = AsyncMock(return_value=False)

        with patch("bot.tasks.check_and_save_official_events", new=scrape):
            async with self.tasks._events_lock:
                run = asyncio.create_task(
                    self.tasks.daily_official_events_check.coro(self.tasks)
                )
                await asyncio.sleep(0)
                scrape.assert_not_awaited()
            await run

        scrape.assert_awaited_once()

    def test_start_and_stop_manage_hourly_event_notification_loop(self):
        self.tasks.hourly_check.start = MagicMock()
        self.tasks.hourly_event_notifications.start = MagicMock()
        self.tasks.daily_official_events_check.start = MagicMock()
        self.tasks.hourly_check.cancel = MagicMock()
        self.tasks.hourly_event_notifications.cancel = MagicMock()
        self.tasks.daily_official_events_check.cancel = MagicMock()

        self.tasks.start_tasks()
        self.tasks.stop_tasks()

        self.tasks.hourly_event_notifications.start.assert_called_once()
        self.tasks.hourly_event_notifications.cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
