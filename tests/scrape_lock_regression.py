import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from services.scrape_lock_manager import ScrapeContext, ScrapeLockManager


CLUB_ID = UUID("22222222-2222-2222-2222-222222222222")


class LockOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_releases_only_its_unique_ownership_token(self):
        with (
            patch.object(
                ScrapeLockManager, "acquire_lock", new=AsyncMock(return_value=True)
            ) as acquire,
            patch.object(
                ScrapeLockManager, "release_lock", new=AsyncMock(return_value=True)
            ) as release,
        ):
            async with ScrapeContext(CLUB_ID, "worker") as context:
                token = context.locked_by

        self.assertTrue(token.startswith("worker:"))
        self.assertLessEqual(len(token), 100)
        acquire.assert_awaited_once_with(CLUB_ID, token)
        release.assert_awaited_once_with(CLUB_ID, token)

    async def test_owner_scoped_release_cannot_delete_a_replacement_lock(self):
        with patch(
            "services.scrape_lock_manager.db.execute",
            new=AsyncMock(return_value="DELETE 0"),
        ) as execute:
            released = await ScrapeLockManager.release_lock(CLUB_ID, "old-token")

        query, club_id, token = execute.await_args.args
        self.assertIn("locked_by = $2", query)
        self.assertEqual((club_id, token), (CLUB_ID, "old-token"))
        self.assertFalse(released)

    async def test_heartbeat_cancels_work_after_lock_is_lost(self):
        context = ScrapeContext(CLUB_ID, "worker")
        context._owner_task = SimpleNamespace(cancel=MagicMock())

        with (
            patch.object(ScrapeLockManager, "LOCK_TIMEOUT_MINUTES", 0),
            patch.object(
                ScrapeLockManager, "refresh_lock", new=AsyncMock(return_value=False)
            ),
            patch("services.scrape_lock_manager.asyncio.sleep", new=AsyncMock()),
        ):
            await context._heartbeat()

        context._owner_task.cancel.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
