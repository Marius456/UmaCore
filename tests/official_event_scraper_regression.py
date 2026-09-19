import unittest
import asyncio
from datetime import datetime, timedelta, timezone
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from scrapers.official_event_scraper import (
    Event,
    EventType,
    _canonical_event_key,
    _dedup_events,
    _extract_card_published_at,
    _extract_times_from_body,
    _parse_date_jst,
    _retain_relevant_events,
    _reachable_proxy,
    _wait_for_news_cards,
    check_and_save,
)


class OfficialEventTimeParsingTests(unittest.TestCase):
    def test_extracts_utc_publication_time_from_news_card(self):
        parsed = _extract_card_published_at(
            "Game\n2026/09/18 22:00 (UTC)\nA news title\nDetails"
        )

        self.assertEqual(parsed, datetime(2026, 9, 18, 22, 0, tzinfo=timezone.utc))

    def test_news_readiness_timeout_propagates_instead_of_returning_empty_events(self):
        page = MagicMock()
        page.locator.return_value.first.wait_for = AsyncMock(
            side_effect=TimeoutError("News cards never appeared")
        )
        with self.assertRaisesRegex(TimeoutError, "News cards never appeared"):
            asyncio.run(_wait_for_news_cards(page))

    def test_news_readiness_waits_for_visible_dynamic_cards(self):
        page = MagicMock()
        page.locator.return_value.first.wait_for = AsyncMock()
        with patch("scrapers.official_event_scraper.PAGE_LOAD_TIMEOUT", 90000):
            asyncio.run(_wait_for_news_cards(page))
        page.locator.return_value.first.wait_for.assert_awaited_once_with(
            state="visible", timeout=90000
        )

    def test_parses_pm_timestamp_as_utc_evening(self):
        parsed = _parse_date_jst("9:59 p.m., Aug 10, 2026 (UTC)")

        self.assertEqual(parsed, datetime(2026, 8, 10, 21, 59, tzinfo=timezone.utc))

    def test_extracts_the_official_article_914_schedule(self):
        start, end = _extract_times_from_body(
            "Event Availability Period\n"
            "10:00 p.m., Jul 27 - 9:59 p.m., Aug 10, 2026 (UTC)"
        )

        self.assertEqual(start, datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 8, 10, 21, 59, tzinfo=timezone.utc))

    def test_status_variants_share_one_canonical_event(self):
        scheduled = Event(
            title='The story event "Wings of Steam and Steel" is coming soon!',
            type=EventType.STORY_EVENT,
            start_time=datetime(2026, 7, 27, 22, tzinfo=timezone.utc),
            end_time=datetime(2026, 8, 10, 21, 59, tzinfo=timezone.utc),
            url="https://umamusume.com/news/914",
        )
        ended = Event(
            title='The story event "Wings of Steam and Steel" has ended!',
            type=EventType.STORY_EVENT,
            start_time=None,
            end_time=None,
            url="https://umamusume.com/news/916",
        )

        merged = _dedup_events([ended, scheduled])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].start_time, scheduled.start_time)
        self.assertEqual(merged[0].end_time, scheduled.end_time)
        self.assertEqual(
            _canonical_event_key(ended.title, ended.type),
            _canonical_event_key(scheduled.title, scheduled.type),
        )

    def test_unreachable_proxy_falls_back_to_direct_connection(self):
        async def check():
            with patch(
                "scrapers.official_event_scraper.asyncio.open_connection",
                new=AsyncMock(side_effect=OSError("offline")),
            ):
                return await _reachable_proxy("http://100.111.216.3:8888")

        self.assertIsNone(asyncio.run(check()))

    def test_known_event_is_refreshed_and_notification_history_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.json"
            path.write_text(
                json.dumps({
                    "events": [{
                        "title": 'The story event "Wings of Steam and Steel" is coming soon!',
                        "type": "story_event",
                        "start_time": "2026-07-27T22:00:00+00:00",
                        "end_time": "2026-08-10T09:59:00+00:00",
                        "url": "https://umamusume.com/news/914",
                        "notified_clubs": ["club-1_starting"],
                    }]
                }),
                encoding="utf-8",
            )
            refreshed = Event(
                title='The story event "Wings of Steam and Steel" has ended!',
                type=EventType.STORY_EVENT,
                start_time=datetime(2026, 7, 27, 22, tzinfo=timezone.utc),
                end_time=datetime(2026, 8, 10, 21, 59, tzinfo=timezone.utc),
                url="https://umamusume.com/news/916",
            )

            with patch(
                "scrapers.official_event_scraper.scrape_official_events",
                new=AsyncMock(return_value=[refreshed]),
            ):
                changed = asyncio.run(
                    check_and_save(
                        str(path),
                        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
                    )
                )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(changed)
            self.assertEqual(saved["events"][0]["end_time"], "2026-08-10T21:59:00+00:00")
            self.assertEqual(saved["events"][0]["notified_clubs"], ["club-1_starting"])

    def test_saved_event_has_a_stable_canonical_key(self):
        event = Event(
            title='The story event "Wings of Steam and Steel" is coming soon!',
            type=EventType.STORY_EVENT,
            start_time=None,
            end_time=None,
            url="https://umamusume.com/news/914",
        )

        self.assertEqual(
            _canonical_event_key(event.title, event.type),
            "story_event:wings of steam and steel",
        )

    def test_retention_keeps_active_and_recent_undated_events(self):
        now = datetime(2026, 9, 19, tzinfo=timezone.utc)
        active = Event(
            title="Active",
            type=EventType.SPOTLIGHT,
            start_time=now - timedelta(days=40),
            end_time=now + timedelta(hours=1),
            url="https://example.invalid/active",
            published_at=now - timedelta(days=45),
        )
        recent_undated = Event(
            title="Recent announcement",
            type=EventType.CHAMPIONS_MEETING,
            start_time=None,
            end_time=None,
            url="https://example.invalid/recent",
            published_at=now - timedelta(days=30),
        )

        retained = _retain_relevant_events([active, recent_undated], now=now)

        self.assertEqual(retained, [active, recent_undated])

    def test_retention_prunes_ended_and_stale_undated_events(self):
        now = datetime(2026, 9, 19, tzinfo=timezone.utc)
        ended = Event(
            title="Ended",
            type=EventType.STORY_EVENT,
            start_time=now - timedelta(days=10),
            end_time=now - timedelta(seconds=1),
            url="https://example.invalid/ended",
        )
        stale_undated = Event(
            title="Stale announcement",
            type=EventType.CHAMPIONS_MEETING,
            start_time=None,
            end_time=None,
            url="https://example.invalid/stale",
            published_at=now - timedelta(days=30, seconds=1),
        )

        self.assertEqual(
            _retain_relevant_events([ended, stale_undated], now=now),
            [],
        )

    def test_retention_keeps_undated_event_when_publication_time_is_unknown(self):
        event = Event(
            title="Legacy announcement",
            type=EventType.CHAMPIONS_MEETING,
            start_time=None,
            end_time=None,
            url="https://example.invalid/legacy",
        )

        self.assertEqual(_retain_relevant_events([event]), [event])

    def test_pruning_only_does_not_report_a_new_event(self):
        now = datetime(2026, 9, 19, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.json"
            path.write_text(
                json.dumps({
                    "events": [{
                        "title": "Bonus Star Piece rewards in Career!",
                        "type": "bonus_star_piece",
                        "start_time": "2026-09-01T00:00:00+00:00",
                        "end_time": "2026-09-02T00:00:00+00:00",
                        "url": "https://umamusume.com/news/1",
                        "notified_clubs": [],
                    }]
                }),
                encoding="utf-8",
            )

            with patch(
                "scrapers.official_event_scraper.scrape_official_events",
                new=AsyncMock(return_value=[]),
            ):
                changed = asyncio.run(check_and_save(str(path), now=now))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(changed)
            self.assertEqual(saved["events"], [])


if __name__ == "__main__":
    unittest.main()
