import unittest
from unittest.mock import AsyncMock, patch

from services.highscore_service import HighscoreService


class HighscoreFetchRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_older_month_failure_does_not_return_partial_records(self):
        current_rows = [
            {
                "date": object(),
                "lifetime_fans": 100,
                "trainer_name": "Trainer",
            }
        ]
        fetch = AsyncMock(
            side_effect=[
                (current_rows, 10),
                RuntimeError("temporary upstream failure"),
            ]
        )

        with patch.object(HighscoreService, "_fetch_and_parse_api_month", new=fetch):
            with self.assertRaisesRegex(RuntimeError, "temporary upstream failure"):
                await HighscoreService._fetch_all_months("123")

        self.assertEqual(fetch.await_count, 2)


if __name__ == "__main__":
    unittest.main()
