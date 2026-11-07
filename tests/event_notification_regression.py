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
from services.club_cache import ActiveClubCache


class EventNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = SimpleNamespace(send=AsyncMock())
        self.bot = SimpleNamespace(get_channel=MagicMock(return_value=self.channel))
        self.cache = ActiveClubCache()
        self.tasks = BotTasks(self.bot, club_cache=self.cache)
        self.club = SimpleNamespace(
            club_id="club-1",
            club_name="Test Club",
            events_channel_id=123,
            is_active=True,
        )
        self.cache.load([self.club])
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

    def spotlight_articles(self, *, start_hours=-48, end_hours=23):
        launch = self.event(start_hours=start_hours, end_hours=end_hours)
        launch.update(
            title="Spotlight Support Card Scout",
            type="spotlight",
            event_key="spotlight:spotlight support card scout",
            url="https://umamusume.com/news/1132",
            banner_image="https://example.invalid/launch.png",
        )
        preview = {
            **launch,
            "title": "Spotlight Support Card Scout coming soon!",
            "event_key": "spotlight:spotlight support card scout coming soon",
            "url": "https://umamusume.com/news/1130",
            "banner_image": "https://example.invalid/preview.png",
            "notified_clubs": [],
        }
        return preview, launch

    async def test_spotlight_preview_and_launch_send_one_ending_reminder_per_club(self):
        preview, launch = self.spotlight_articles()
        preview["end_time"] = datetime.fromisoformat(launch["end_time"]).astimezone(
            pytz.timezone("Europe/Vilnius")
        ).isoformat()
        second_club = SimpleNamespace(
            club_id="club-2", club_name="Second Club", events_channel_id=456, is_active=True
        )
        self.cache.add(second_club)
        second_channel = SimpleNamespace(send=AsyncMock())
        self.bot.get_channel.side_effect = {123: self.channel, 456: second_channel}.get

        for records in ([preview, launch], [launch, preview]):
            with self.subTest(first=records[0]["url"]):
                self.write_events(records)
                self.channel.send.reset_mock()
                second_channel.send.reset_mock()
                tasks = BotTasks(self.bot, club_cache=self.cache)
                with patch("bot.tasks.EVENTS_JSON_PATH", str(self.events_path)):
                    await tasks.event_notifications()
                    await tasks.event_notifications()

                for channel in (self.channel, second_channel):
                    channel.send.assert_awaited_once()
                    embed = channel.send.await_args.kwargs["embed"]
                    self.assertEqual(embed.title, "⏳ Event Ending in 1 Day")
                    self.assertEqual(embed.fields[0].value, launch["title"])
                    self.assertEqual(embed.fields[2].value, launch["url"])
                    self.assertEqual(embed.image.url, launch["banner_image"])
                saved = json.loads(self.events_path.read_text(encoding="utf-8"))["events"]
                self.assertEqual(len(saved), 1)
                self.assertCountEqual(saved[0]["notified_clubs"], ["club-1_ending", "club-2_ending"])

    async def test_spotlight_preview_history_survives_restart_and_is_separate_by_phase(self):
        preview, launch = self.spotlight_articles(start_hours=1, end_hours=2)
        preview["notified_clubs"] = ["club-1_ending"]
        self.write_events([launch, preview])
        restarted = BotTasks(self.bot, club_cache=self.cache)

        with patch("bot.tasks.EVENTS_JSON_PATH", str(self.events_path)):
            await restarted.event_notifications()

        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.kwargs["embed"].title, "⏰ Event Starting Soon")
        saved = json.loads(self.events_path.read_text(encoding="utf-8"))["events"]
        self.assertEqual(saved[0]["notified_clubs"], ["club-1_ending", "club-1_starting"])

    async def test_later_spotlight_with_same_title_can_notify_in_the_same_process(self):
        _, first = self.spotlight_articles(end_hours=20)
        _, later = self.spotlight_articles(start_hours=-24, end_hours=23)
        later["url"] = "https://umamusume.com/news/1142"

        with patch("bot.tasks.EVENTS_JSON_PATH", str(self.events_path)):
            self.write_events([first])
            await self.tasks.event_notifications()
            self.write_events([later])
            await self.tasks.event_notifications()

        self.assertEqual(self.channel.send.await_count, 2)
        self.assertEqual(self.channel.send.await_args.kwargs["embed"].fields[2].value, later["url"])

    async def test_starting_notification_is_persisted_and_not_resent(self):
        self.write_events([self.event()])

        with (
            patch("bot.tasks.EVENTS_JSON_PATH", str(self.events_path)),
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
        ):
            await self.tasks.event_notifications()
            await self.tasks.event_notifications()

        self.assertEqual(self.channel.send.await_count, 2)
        saved = json.loads(self.events_path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["events"][0]["notified_clubs"],
            ["club-1_starting", "club-1_ending"],
        )

    async def test_ending_notification_uses_a_true_24_hour_threshold(self):
        within_24_hours = self.event(start_hours=48, end_hours=23)
        outside_24_hours = self.event(start_hours=48, end_hours=25)

        with (
            patch("bot.tasks.EVENTS_JSON_PATH", str(self.events_path)),
        ):
            self.write_events([within_24_hours])
            await self.tasks.event_notifications()
            self.assertEqual(self.channel.send.await_count, 1)

            self.channel.send.reset_mock()
            self.write_events([outside_24_hours])
            await self.tasks.event_notifications()
            self.channel.send.assert_not_awaited()

    async def test_successful_send_is_not_repeated_when_state_persistence_fails(self):
        self.write_events([self.event(start_hours=1, end_hours=72)])

        with (
            patch("bot.tasks.EVENTS_JSON_PATH", str(self.events_path)),
            patch.object(self.tasks, "_save_events_json", return_value=False),
        ):
            await self.tasks.event_notifications()
            await self.tasks.event_notifications()

        self.channel.send.assert_awaited_once()

    async def test_ended_event_does_not_send_or_record_an_ending_notification(self):
        self.write_events([self.event(start_hours=-48, end_hours=-1)])

        with (
            patch("bot.tasks.EVENTS_JSON_PATH", str(self.events_path)),
        ):
            await self.tasks.event_notifications()

        self.channel.send.assert_not_awaited()
        saved = json.loads(self.events_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["events"][0]["notified_clubs"], [])

    def test_event_display_name_removes_announcement_status(self):
        self.assertEqual(
            self.tasks._event_display_name(
                'The story event "Wings of Steam and Steel" is coming soon!'
            ),
            '"Wings of Steam and Steel"',
        )

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

    async def test_start_and_stop_manage_hourly_event_notification_loop(self):
        self.tasks.scheduled_report_check.start = MagicMock()
        self.tasks.hourly_event_notifications.start = MagicMock()
        self.tasks.daily_official_events_check.start = MagicMock()
        self.tasks.scheduled_report_check.cancel = MagicMock()
        self.tasks.hourly_event_notifications.cancel = MagicMock()
        self.tasks.daily_official_events_check.cancel = MagicMock()

        self.tasks.start_tasks()
        await self.tasks.stop_tasks()

        self.tasks.hourly_event_notifications.start.assert_called_once()
        self.tasks.hourly_event_notifications.cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
