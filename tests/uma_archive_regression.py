import unittest
import re
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import discord
from discord.ext import commands

from bot.commands import setup
from bot.commands.uma_archive import UmaArchiveCommands
from models.uma_archive import UmaArchive


def interaction():
    return SimpleNamespace(
        guild_id=123, user=SimpleNamespace(id=456), namespace=SimpleNamespace(),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def entry(**kwargs):
    return dict(
        member_id=uuid4(), trainer_name="Trainer", club_name="Club", uma_name="Special Week",
        variant="Original", rank="UG", score=20000, position=1, participants=21,
        total_entries=11, observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc), **kwargs,
    )


class ArchiveQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_outfit_rank_ties_scope_and_personal_pagination_execute_sql(self):
        # SQLite supports the production window functions; remove PostgreSQL casts only.
        with sqlite3.connect(":memory:") as connection:
            connection.executescript("""
                CREATE TABLE clubs (club_id TEXT, guild_id INTEGER, is_active BOOLEAN);
                CREATE TABLE members (member_id TEXT, club_id TEXT, is_active BOOLEAN);
                CREATE TABLE uma_archive_scores (
                    member_id TEXT, uma_name TEXT, variant TEXT, rank TEXT,
                    score INTEGER, observed_at TEXT
                );
                INSERT INTO clubs VALUES ('local', 123, 1), ('foreign', 999, 1), ('inactive', 123, 0);
                INSERT INTO members VALUES
                    ('me', 'local', 1), ('tie', 'local', 1), ('lower', 'local', 1),
                    ('other-server', 'foreign', 1), ('former', 'local', 0), ('closed', 'inactive', 1);
                INSERT INTO uma_archive_scores VALUES
                    ('me', 'Uma', 'Original', 'S', 20000, '2026-10-01'),
                    ('tie', 'Uma', 'Original', 'S', 20000, '2026-10-01'),
                    ('lower', 'Uma', 'Original', 'A', 19000, '2026-10-01'),
                    ('other-server', 'Uma', 'Original', 'S', 30000, '2026-10-01'),
                    ('former', 'Uma', 'Original', 'S', 40000, '2026-10-01'),
                    ('closed', 'Uma', 'Original', 'S', 50000, '2026-10-01'),
                    ('me', 'Uma', 'Alternate', 'A', 18000, '2026-10-01');
            """)
            connection.row_factory = sqlite3.Row

            async def execute(query, *args):
                query = query.replace('::uuid', '').replace('::text', '').replace('::bigint', '')
                query = re.sub(r'\$(\d+)', r':p\1', query)
                parameters = {f'p{i}': value for i, value in enumerate(args, 1)}
                return [dict(row) for row in connection.execute(query, parameters)]

            with patch("models.uma_archive.db.fetch", side_effect=execute):
                rows = await UmaArchive.personal_scores(123, 'me')
                self.assertEqual([(r['position'], r['participants']) for r in rows], [(1, 3), (1, 1)])
                self.assertEqual(rows[0]['total_entries'], 2)
                lower = await UmaArchive.personal_scores(123, 'lower', 'Uma', 'Original')
                self.assertEqual(lower[0]['position'], 3)
                self.assertEqual(lower[0]['participants'], 3)
                self.assertEqual(await UmaArchive.personal_scores(123, 'other-server'), [])
                self.assertEqual(await UmaArchive.personal_scores(123, 'me', page=2), [])
                global_rows = await UmaArchive.personal_scores(None, 'me', 'Uma', 'Original')
                self.assertEqual(global_rows[0]['position'], 2)
                self.assertEqual(global_rows[0]['participants'], 4)
                foreign_rows = await UmaArchive.personal_scores(None, 'other-server')
                self.assertEqual(foreign_rows[0]['position'], 1)

    async def test_rank_scope_and_bound_filters_and_pagination(self):
        club_id = uuid4()
        with patch("models.uma_archive.db.fetch", new=AsyncMock(return_value=[])) as fetch:
            await UmaArchive.leaderboard(123, club_id, "Uma'", "Outfit", 3)
        query, *args = fetch.await_args.args
        self.assertEqual(args, [123, club_id, "Uma'", "Outfit", 20])
        self.assertNotIn("Uma'", query)
        self.assertIn("($1::bigint IS NULL OR c.guild_id = $1) AND c.is_active AND m.is_active", query)
        self.assertIn("DISTINCT ON (s.member_id)", query)
        self.assertIn("RANK() OVER (ORDER BY score DESC)", query)

    async def test_personal_rank_is_computed_before_member_filter(self):
        member_id = uuid4()
        with patch("models.uma_archive.db.fetchrow", new=AsyncMock()) as fetchrow:
            await UmaArchive.standing(123, member_id)
        query, *args = fetchrow.await_args.args
        self.assertEqual(args, [123, None, None, None, member_id])
        self.assertGreater(query.index("WHERE member_id = $5"), query.index("RANK() OVER"))
        with patch("models.uma_archive.db.fetch", new=AsyncMock()) as fetch:
            await UmaArchive.personal_scores(123, member_id, "Uma", None, 2)
        query, *args = fetch.await_args.args
        self.assertEqual(args, [123, None, "Uma", None, member_id, 10])
        self.assertIn("PARTITION BY s.uma_name, s.variant ORDER BY s.score DESC", query)
        self.assertGreater(query.index("WHERE member_id = $5"), query.index("RANK() OVER"))

    async def test_linked_member_lookup_is_server_scoped_and_preserves_unscanned_member(self):
        with patch("models.uma_archive.db.fetchrow", new=AsyncMock()) as fetchrow:
            await UmaArchive.linked_member(123, 456)
        query, *args = fetchrow.await_args.args
        self.assertEqual(args, [123, 456])
        self.assertIn("LEFT JOIN uma_archive_scans", query)
        self.assertIn("c.guild_id = $1", query)
        self.assertIn("c.is_active AND m.is_active", query)


class ArchiveCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_is_member_accessible_and_guild_only(self):
        async with commands.Bot(command_prefix="!", intents=discord.Intents.none()) as bot:
            with patch("bot.commands.trivia.TriviaCommands.cog_load", new=AsyncMock()):
                await setup(bot)
            group = bot.tree.get_command("uma")
            self.assertTrue(group.guild_only)
            self.assertEqual({c.name for c in group.commands}, {"leaderboard", "status"})
            for command in group.commands:
                self.assertEqual(command.checks, [])
                self.assertIsNone(command.default_permissions)

    async def test_overall_and_per_uma_use_filters_and_show_pagination(self):
        cog = UmaArchiveCommands(None)
        for uma, variant in [(None, None), ("Special Week", "Original")]:
            with self.subTest(uma=uma):
                ctx = interaction()
                with patch.object(UmaArchive, "leaderboard", new=AsyncMock(return_value=[entry()])) as fetch:
                    await cog.leaderboard.callback(cog, ctx, uma, variant, page=2)
                fetch.assert_awaited_once_with(None, None, uma, variant, 2)
                embed = ctx.followup.send.await_args.kwargs['embed']
                self.assertIn("Page 2/3", embed.footer.text)
                self.assertIn("20,000", embed.fields[0].value)
                self.assertIn("#1", embed.fields[0].name)
                self.assertIn("across Discord servers", embed.description)
                ctx.response.defer.assert_awaited_once_with()

    async def test_cross_server_club_is_rejected_before_score_query(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        foreign = SimpleNamespace(belongs_to_guild=lambda guild: False)
        with patch("bot.commands.uma_archive.Club.get_by_name", new=AsyncMock(return_value=foreign)), \
             patch.object(UmaArchive, "leaderboard", new=AsyncMock()) as fetch:
            await cog.leaderboard.callback(cog, ctx, club="Foreign")
        fetch.assert_not_awaited()
        self.assertIn("this server", ctx.followup.send.await_args.args[0])

    async def test_variant_without_uma_is_rejected(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        with patch.object(UmaArchive, "leaderboard", new=AsyncMock()) as fetch:
            await cog.leaderboard.callback(cog, ctx, variant="Original")
        fetch.assert_not_awaited()
        self.assertIn("Choose an Uma", ctx.followup.send.await_args.args[0])

    async def test_personal_status_is_private_and_shows_overall_and_outfit_rank(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        row = entry()
        member = {**row, "entry_count": 11}
        with patch.object(UmaArchive, "linked_member", new=AsyncMock(return_value=member)) as linked, \
             patch.object(UmaArchive, "personal_scores", new=AsyncMock(return_value=[row])) as scores, \
             patch.object(UmaArchive, "standing", new=AsyncMock(return_value=row)):
            await cog.status.callback(cog, ctx)
        linked.assert_awaited_once_with(None, 456)
        scores.assert_awaited_once_with(None, row['member_id'], None, None, 1)
        ctx.response.defer.assert_awaited_once_with(ephemeral=True)
        response = ctx.followup.send.await_args.kwargs
        self.assertTrue(response['ephemeral'])
        self.assertIn("Overall global rank", response['embed'].description)
        self.assertIn("Outfit rank", response['embed'].fields[0].value)
        self.assertIn("Page 1/2", response['embed'].footer.text)

    async def test_missing_link_unscanned_empty_scan_and_empty_filter(self):
        cog = UmaArchiveCommands(None)
        row = entry()
        for member, expected in [
            (None, "/link_trainer"),
            ({**row, "entry_count": 0, "observed_at": None}, "not been scanned"),
            ({**row, "entry_count": 0}, "contains no scores"),
            ({**row, "entry_count": 1}, "No scores on this page"),
        ]:
            with self.subTest(expected=expected):
                ctx = interaction()
                with patch.object(UmaArchive, "linked_member", new=AsyncMock(return_value=member)), \
                     patch.object(UmaArchive, "personal_scores", new=AsyncMock(return_value=[])):
                    await cog.status.callback(cog, ctx)
                self.assertIn(expected, ctx.followup.send.await_args.args[0])
                self.assertTrue(ctx.followup.send.await_args.kwargs['ephemeral'])

    async def test_empty_leaderboard_and_missing_schema_are_friendly(self):
        cog = UmaArchiveCommands(None)
        for failure in [False, True]:
            ctx = interaction()
            fetch = AsyncMock(side_effect=asyncpg.UndefinedTableError() if failure else None, return_value=[])
            with patch.object(UmaArchive, "leaderboard", new=fetch):
                await cog.leaderboard.callback(cog, ctx)
            expected = "not set up" if failure else "No archive scores"
            self.assertIn(expected, ctx.followup.send.await_args.args[0])

    async def test_autocomplete_scopes_filters_and_handles_unavailable_database(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        ctx.namespace.uma = "Special Week"
        with patch.object(UmaArchive, "choices", new=AsyncMock(return_value=[{'name': 'Original'}])) as fetch:
            choices = await cog.variant_autocomplete(ctx, "Orig")
        fetch.assert_awaited_once_with(None, None, "Special Week", "Orig", variants=True)
        self.assertEqual(choices[0].value, "Original")
        with patch.object(UmaArchive, "choices", new=AsyncMock(side_effect=asyncpg.UndefinedTableError())):
            self.assertEqual(await cog.uma_autocomplete(ctx, ""), [])
        ctx.namespace.uma = None
        with patch.object(UmaArchive, "choices", new=AsyncMock()) as fetch:
            self.assertEqual(await cog.variant_autocomplete(ctx, ""), [])
        fetch.assert_not_awaited()

    async def test_long_user_text_stays_inside_embed_limits_and_disables_mentions(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        row = entry()
        for key in ('trainer_name', 'club_name', 'uma_name', 'variant'):
            row[key] = "*_@everyone" * 40
        with patch.object(UmaArchive, "leaderboard", new=AsyncMock(return_value=[row] * 10)):
            await cog.leaderboard.callback(cog, ctx)
        result = ctx.followup.send.await_args.kwargs
        embed = result['embed']
        self.assertLessEqual(len(embed), 6000)
        for field in embed.fields:
            self.assertLessEqual(len(field.name), 256)
            self.assertLessEqual(len(field.value), 1024)
            self.assertNotIn("@everyone", field.name)
        self.assertFalse(result['allowed_mentions'].everyone)


if __name__ == "__main__":
    unittest.main()
