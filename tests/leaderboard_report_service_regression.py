from datetime import date, timedelta
import unittest

from services.leaderboard_report_service import LeaderboardReportService


class TodayRecordsTests(unittest.TestCase):
    def test_includes_a_personal_best_tied_on_the_latest_day(self):
        latest = date(2026, 7, 10)
        records = LeaderboardReportService._compute_today_records(
            {
                "Trainer": [
                    {"date": latest - timedelta(days=2), "delta": 1_000_000},
                    {"date": latest, "delta": 1_000_000},
                ]
            },
            latest,
        )

        self.assertEqual(records["count"], 1)
        self.assertTrue(records["members"][0]["is_tie"])
        self.assertEqual(records["members"][0]["prev_best_delta"], 1_000_000)


class RareAchievementTests(unittest.TestCase):
    def test_does_not_count_window_gains_below_an_earlier_personal_best(self):
        latest = date(2026, 7, 10)
        daily_rankings = {latest: [{"name": "Trainer"}]}
        deltas = {
            "Trainer": [
                {"date": latest - timedelta(days=8), "delta": 10_000_000},
                {"date": latest - timedelta(days=3), "delta": 1_000_000},
                {"date": latest - timedelta(days=2), "delta": 2_000_000},
                {"date": latest - timedelta(days=1), "delta": 3_000_000},
            ]
        }

        achievements = LeaderboardReportService._compute_rare_achievements(
            daily_rankings, deltas, club_record=None, latest_date=latest
        )

        self.assertFalse(any(a.title == "PB Spree" for a in achievements))


if __name__ == "__main__":
    unittest.main()
