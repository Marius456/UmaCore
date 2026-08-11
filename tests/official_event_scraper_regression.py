import unittest
from datetime import datetime, timezone

from scrapers.official_event_scraper import _extract_times_from_body, _parse_date_jst


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


if __name__ == "__main__":
    unittest.main()
