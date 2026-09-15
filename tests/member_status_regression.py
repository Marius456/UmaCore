import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.commands.member import MemberCommands
from services.member_status_card import card_html, chart_svg, compact, render_card
from services.member_status_service import build_status, load_member_status
from services.trainer_profile_service import (
    TrainerProfile, TrainerProfileClient, portrait_url, profile_portrait_url,
)


def record(day, fans, expected=8_000_000):
    return SimpleNamespace(date=day, cumulative_fans=fans, expected_fans=expected,
                           deficit_surplus=fans - expected, days_behind=int(fans < expected))


def member():
    return SimpleNamespace(member_id="member", club_id="club", trainer_id="711119194083",
                           trainer_name="OnlyRex", join_date=date(2026, 8, 1),
                           is_active=True, manually_deactivated=False)


def club():
    return SimpleNamespace(club_id="club", circle_id="123", club_name="UMA Vault",
                           quota_period="daily")


def sample_status():
    latest = record(date(2026, 9, 13), 279_500_000, 104_000_000)
    history = [record(date(2026, 9, day), int(day / 13 * latest.cumulative_fans))
               for day in range(1, 14)]
    profile = TrainerProfile({
        "trainer": {"team_evaluation_point": 295600, "follower_num": 486, "rank_score": 4200000},
        "fan_history": {"monthly": [{"year": 2026, "month": 9, "rank": 187}],
                        "rolling": {"gain_30d": 674900000}, "alltime": {"rank": 99}},
        "circle_history": [{"year": 2026, "month": 9, "circle_id": 123, "circle_rank": 34}],
    }, datetime(2026, 9, 15, 12, tzinfo=timezone.utc))
    return build_status(member(), club(), latest, history, 8_000_000, profile)


class StatusDataTests(unittest.TestCase):
    def test_profile_mapping_and_matching_month(self):
        status = sample_status()
        self.assertEqual((status.team_rating, status.followers, status.rank_score), (295600, 486, 4200000))
        self.assertEqual((status.monthly_rank, status.alltime_rank, status.circle_rank), (187, 99, 34))
        self.assertEqual(status.gain_30d, 674900000)
        self.assertEqual(status.percent, 268)
        self.assertAlmostEqual(status.average, 279500000 / 13)

    def test_missing_fields_wrong_month_and_different_circle(self):
        latest = record(date(2026, 9, 13), 10)
        profile = TrainerProfile({
            "trainer": {"follower_num": True, "rank_score": -1},
            "fan_history": {"monthly": [{"year": 2026, "month": 8, "rank": 1}],
                            "alltime": None, "rolling": []},
            "circle_history": [{"year": 2026, "month": 9, "circle_id": 456, "circle_rank": 1}],
        }, datetime.now(timezone.utc))
        status = build_status(member(), club(), latest, [latest], 8, profile)
        self.assertIsNone(status.followers)
        self.assertIsNone(status.rank_score)
        self.assertIsNone(status.monthly_rank)
        self.assertIsNone(status.circle_rank)
        self.assertIsNone(status.alltime_rank)

    def test_gaps_resets_membership_and_best_day(self):
        person = member()
        person.join_date = date(2026, 8, 30)
        records = [record(date(2026, 8, 29), 1), record(date(2026, 8, 30), 20),
                   record(date(2026, 8, 31), 30), record(date(2026, 9, 1), 2),
                   record(date(2026, 9, 3), 100), record(date(2026, 9, 4), 105)]
        for r in records:
            r.deficit_surplus = 0
        status = build_status(person, club(), records[-1], records, 8)
        self.assertEqual(status.best_day, 10)
        self.assertEqual(status.streak, 2)
        self.assertEqual(status.days_active, 5)
        self.assertEqual(status.average, 105 / 4)
        self.assertEqual(len(status.points), 3)
        svg = chart_svg(status)
        self.assertEqual(svg.count('opacity=".20"'), 1)  # only Sep 3–4 are connected

    def test_midmonth_join_and_zero_target(self):
        person = member()
        person.join_date = date(2026, 9, 10)
        latest = record(date(2026, 9, 13), 40, 0)
        status = build_status(person, club(), latest, [latest], 0)
        self.assertEqual(status.average, 10)
        self.assertIsNone(status.percent)
        self.assertIsNone(status.best_day)
        self.assertEqual(status.badge, "NO QUOTA")
        self.assertIn("circle", chart_svg(status))
        self.assertIn("No fan history", chart_svg(replace(status, points=[])))

    def test_quota_periods_and_inactive_state(self):
        for period, label in (("weekly", "Weekly Quota"), ("biweekly", "Biweekly Quota")):
            group = club()
            group.quota_period = period
            latest = record(date(2026, 9, 1), 0)
            status = build_status(member(), group, latest, [latest], 56_000_000)
            self.assertEqual(status.quota_label, label)
            self.assertEqual(status.badge, "BEHIND QUOTA")
            self.assertEqual(status.streak, 0)
            self.assertEqual(replace(status, active=False).badge, "INACTIVE")

    def test_html_escaping_and_large_progress(self):
        status = replace(sample_status(), name='<script>alert("x")</script>日本語', club_name="A&B")
        html = card_html(status)
        self.assertNotIn('<script>', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("A&amp;B", html)
        self.assertIn("width:100%", html)
        self.assertIn("268%", html)
        self.assertEqual(compact(8000000), "8M")
        self.assertEqual(compact(-175500000, True), "−175.5M")
        self.assertEqual(compact(None), "—")


class ProfileClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_response_size_limit(self):
        async def chunks(size):
            yield b"0123456789"
            yield b"0123456789"
        response = SimpleNamespace(content=SimpleNamespace(iter_chunked=chunks))
        with self.assertRaises(ValueError):
            await TrainerProfileClient._read_limited(response, 15)

    async def test_cache_ttl_eviction_and_negative_cache(self):
        client = TrainerProfileClient()
        client.MAX_ENTRIES = 2
        client._fetch = AsyncMock(return_value=None)
        with patch("services.trainer_profile_service.time.monotonic", return_value=0):
            await client.fetch("1")
            await client.fetch("1")
            client._fetch.assert_awaited_once()
        with patch("services.trainer_profile_service.time.monotonic", return_value=301):
            await client.fetch("1")
            await client.fetch("2")
            await client.fetch("3")
        self.assertNotIn("1", client._cache)
        self.assertEqual(len(client._cache), 2)
        self.assertEqual(client._fetch.await_count, 4)

    async def test_timeout_and_invalid_id(self):
        client = TrainerProfileClient()
        client._fetch = AsyncMock(side_effect=asyncio.TimeoutError)
        self.assertIsNone(await client.fetch("123"))
        self.assertIsNone(await client.fetch("../123"))
        client._fetch.assert_awaited_once_with("123")

    async def test_http_key_private_profile_and_mapping(self):
        response = MagicMock(status=403)
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get.return_value = response
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        with patch("services.trainer_profile_service.aiohttp.ClientSession", return_value=session), \
                patch("services.trainer_profile_service.UMAMOE_API_KEY", "test-key"):
            client = TrainerProfileClient()
            self.assertIsNone(await client._fetch("123"))
            self.assertEqual(session.get.call_args.kwargs["headers"], {"X-API-Key": "test-key"})
            response.status = 200
            client._read_limited = AsyncMock(return_value=b'{"trainer":{"account_id":"123"},"fan_history":{}}')
            result = await client._fetch("123")
            self.assertIsInstance(result, TrainerProfile)
            self.assertEqual(result.data["trainer"]["account_id"], "123")
            self.assertIsNone(result.portrait)
            client._read_limited.return_value = b'{"trainer":{"account_id":"456"}}'
            self.assertIsNone(await client._fetch("123"))

    def test_portrait_mapping(self):
        self.assertEqual(portrait_url(100101), "https://uma.moe/assets/images/character_stand/chara_stand_100101.webp")
        self.assertIsNone(portrait_url(None))
        self.assertIsNone(portrait_url("../../file"))

    def test_dress_and_card_namespaces_are_distinct(self):
        data = {"trainer": {"leader_chara_dress_id": 100430},
                "inheritance": {"main_parent_id": 101401}}
        self.assertEqual(profile_portrait_url(data), portrait_url(101401))
        data["veterans"] = [{"race_cloth_id": 100430, "card_id": 100401}]
        self.assertEqual(profile_portrait_url(data), portrait_url(100401))
        data["veterans"] = []
        data["inheritance"] = None
        self.assertIsNone(profile_portrait_url(data))


class StatusCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_previous_membership_history_is_not_reused(self):
        latest = record(date(2026, 7, 31), 100)
        with patch("services.member_status_service.QuotaHistory.get_latest_for_member", AsyncMock(return_value=latest)):
            self.assertIsNone(await load_member_status(member()))

    async def test_no_history_does_not_render(self):
        cog = MemberCommands(None)
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
        with patch("bot.commands.member.load_member_status", AsyncMock(return_value=None)), \
                patch("bot.commands.member.render_card", AsyncMock()) as renderer:
            await cog._send_member_status(interaction, member())
        renderer.assert_not_awaited()
        self.assertIn("No quota data", interaction.followup.send.call_args.args[0])

    async def test_unlinked_command_preserves_linking_requirement(self):
        cog = MemberCommands(None)
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()),
                                      response=SimpleNamespace(defer=AsyncMock()),
                                      user=SimpleNamespace(id=1))
        with patch("bot.commands.member.UserLink.get_by_discord_id", AsyncMock(return_value=None)), \
                patch("bot.commands.member.load_member_status", AsyncMock()) as loader:
            await cog.my_status.callback(cog, interaction)
        loader.assert_not_awaited()
        self.assertIn("/link_trainer", interaction.followup.send.call_args.args[0])

    async def test_commands_send_card(self):
        for command in ("my_status", "member_status"):
            cog = MemberCommands(None)
            interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                          followup=SimpleNamespace(send=AsyncMock()),
                                          user=SimpleNamespace(id=1), guild_id=1)
            group = club()
            group.belongs_to_guild = lambda guild: True
            with patch("bot.commands.member.UserLink.get_by_discord_id", AsyncMock(return_value=SimpleNamespace(member_id="member"))), \
                    patch("bot.commands.member.Member.get_by_id", AsyncMock(return_value=member())), \
                    patch("bot.commands.member.Member.get_by_name", AsyncMock(return_value=member())), \
                    patch("bot.commands.member.Club.get_by_name", AsyncMock(return_value=group)), \
                    patch("bot.commands.member.load_member_status", AsyncMock(return_value=sample_status())), \
                    patch("bot.commands.member.render_card", AsyncMock(return_value=b"png")):
                if command == "my_status":
                    await cog.my_status.callback(cog, interaction)
                else:
                    await cog.member_status.callback(cog, interaction, "OnlyRex", "UMA Vault")
            interaction.response.defer.assert_awaited_once_with()
            self.assertEqual(interaction.followup.send.call_args.kwargs["file"].filename, "member-status.png")

    async def test_render_fallback_and_missing_member(self):
        cog = MemberCommands(None)
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
        with patch("bot.commands.member.load_member_status", AsyncMock(return_value=sample_status())), \
                patch("bot.commands.member.render_card", AsyncMock(side_effect=RuntimeError("browser unavailable"))):
            await cog._send_member_status(interaction, member())
        self.assertIn("QUOTA MET", interaction.followup.send.call_args.kwargs["embed"].title)
        await cog._send_member_status(interaction, None)
        self.assertIn("no longer exists", interaction.followup.send.call_args.args[0])

    async def test_quota_uses_record_date_and_local_circle_rank(self):
        latest = record(date(2026, 8, 31), 100)
        with patch("services.member_status_service.QuotaHistory.get_latest_for_member", AsyncMock(return_value=latest)), \
                patch("services.member_status_service.Club.get_by_id", AsyncMock(return_value=club())), \
                patch("services.member_status_service.QuotaRequirement.get_quota_for_date", AsyncMock(return_value=8)) as quota, \
                patch("services.member_status_service.QuotaHistory.get_for_member_range", AsyncMock(return_value=[latest])), \
                patch("services.member_status_service.ClubRankHistory.get_previous", AsyncMock(return_value=SimpleNamespace(date=latest.date, monthly_rank=23))), \
                patch("services.member_status_service.profile_client.fetch", AsyncMock(return_value=None)):
            status = await load_member_status(member())
        quota.assert_awaited_once_with("club", latest.date)
        self.assertEqual(status.circle_rank, 23)


class CardRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_closes_failed_and_successful_pages(self):
        page = AsyncMock()
        screenshot = AsyncMock(side_effect=[RuntimeError("browser died"), b"png"])
        page.locator = MagicMock(return_value=SimpleNamespace(screenshot=screenshot))
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        with patch("services.report_generator._get_browser_context_async", AsyncMock(return_value=context)), \
                patch("services.report_generator._close_playwright_browser_unlocked_async", AsyncMock()) as restart, \
                patch("services.report_generator._playwright_lock", asyncio.Lock()):
            self.assertEqual(await render_card(sample_status()), b"png")
        self.assertEqual(page.close.await_count, 2)
        restart.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
