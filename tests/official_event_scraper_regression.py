import unittest
import asyncio
from datetime import datetime, timezone
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scrapers.official_event_scraper import (
    Event,
    EventType,
    _canonical_event_key,
    _dedup_events,
    _extract_times_from_body,
    _parse_date_jst,
    check_and_save,
)


class OfficialEventTimeParsingTests(unittest.TestCase):
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
                changed = asyncio.run(check_and_save(str(path)))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(changed)
            self.assertEqual(saved["events"][0]["end_time"], "2026-08-10T21:59:00+00:00")
            self.assertEqual(saved["events"][0]["notified_clubs"], ["club-1_starting"])


if __name__ == "__main__":
    unittest.main()
