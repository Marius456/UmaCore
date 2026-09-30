import asyncio
import time
import unittest
from unittest.mock import AsyncMock

from events.client import GametoraClient, _FILES


class EventsClientCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_share_one_upstream_refresh(self):
        client = GametoraClient()
        client._fetch = AsyncMock(return_value={key: str(index) for index, key in enumerate(_FILES)})
        client._fetch_file = AsyncMock(return_value=[])

        first, second = await asyncio.gather(
            client.get_events_data(),
            client.get_events_data(),
        )

        self.assertIs(first, second)
        client._fetch.assert_awaited_once()
        self.assertEqual(client._fetch_file.await_count, len(_FILES))

    async def test_fresh_cache_avoids_all_upstream_calls(self):
        client = GametoraClient()
        cached = {"cached": True}
        client._cache = cached
        client._cache_at = time.monotonic()
        client._fetch = AsyncMock()

        result = await client.get_events_data()

        self.assertIs(result, cached)
        client._fetch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
