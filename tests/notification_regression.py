from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from services.notification_service import NotificationService


MEMBER_ID = UUID("33333333-3333-3333-3333-333333333333")


def member_status():
    return {
        "member": SimpleNamespace(member_id=MEMBER_ID, trainer_name="Trainer"),
        "history": SimpleNamespace(
            date=date(2026, 9, 7),
            cumulative_fans=1_000_000,
            expected_fans=2_000_000,
            deficit_surplus=-1_000_000,
            days_behind=1,
        ),
    }


class NotificationIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_claim_prevents_duplicate_dm(self):
        bot = SimpleNamespace(fetch_user=AsyncMock())
        link = SimpleNamespace(discord_user_id=123, member_id=MEMBER_ID)
        with (
            patch(
                "services.notification_service.UserLink.get_all_with_deficit_notifications",
                new=AsyncMock(return_value=[link]),
            ),
            patch(
                "services.notification_service.db.fetchval",
                new=AsyncMock(return_value=None),
            ) as claim,
        ):
            await NotificationService(bot).send_deficit_notifications(
                "Club", [member_status()], current_date=date(2026, 9, 7)
            )

        claim.assert_awaited_once()
        bot.fetch_user.assert_not_awaited()

    async def test_successful_dm_marks_claim_sent(self):
        user = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(fetch_user=AsyncMock(return_value=user))
        link = SimpleNamespace(discord_user_id=123, member_id=MEMBER_ID)
        with (
            patch(
                "services.notification_service.UserLink.get_all_with_deficit_notifications",
                new=AsyncMock(return_value=[link]),
            ),
            patch(
                "services.notification_service.db.fetchval",
                new=AsyncMock(return_value=MEMBER_ID),
            ),
            patch(
                "services.notification_service.db.execute", new=AsyncMock()
            ) as execute,
        ):
            await NotificationService(bot).send_deficit_notifications(
                "Club", [member_status()], current_date=date(2026, 9, 7)
            )

        user.send.assert_awaited_once()
        self.assertIn("SET sent_at = NOW()", execute.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
