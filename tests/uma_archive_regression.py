import unittest
import asyncio
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
from bot.commands.uma_archive import ArchivePaginationView, UmaArchiveCommands
from models.uma_archive import UmaArchive


def interaction():
    return SimpleNamespace(
        guild_id=123, user=SimpleNamespace(id=456), namespace=SimpleNamespace(),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
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
                ALTER TABLE clubs ADD COLUMN club_name TEXT DEFAULT 'Club';
                ALTER TABLE members ADD COLUMN trainer_name TEXT DEFAULT 'Trainer';
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
                overall = await UmaArchive.leaderboard(None)
                self.assertEqual(len(overall), 5)
                self.assertEqual(sum(r['member_id'] == 'me' for r in overall), 2)
                self.assertEqual([r['position'] for r in overall], [1, 2, 2, 4, 5])
                self.assertEqual(overall[0]['participants'], 5)
                filtered = await UmaArchive.leaderboard(None, uma='Uma')
                self.assertEqual(len(filtered), 4)
                self.assertEqual(sum(r['member_id'] == 'me' for r in filtered), 1)

            async def execute_one(query, *args):
                rows = await execute(query, *args)
                return rows[0] if rows else None

            with patch('models.uma_archive.db.fetchrow', side_effect=execute_one):
                standing = await UmaArchive.standing(None, 'me')
                self.assertEqual(standing['score'], 20000)
                self.assertEqual(standing['position'], 2)
                self.assertEqual(standing['participants'], 5)

    async def test_rank_scope_and_bound_filters_and_pagination(self):
        club_id = uuid4()
        with patch("models.uma_archive.db.fetch", new=AsyncMock(return_value=[])) as fetch:
            await UmaArchive.leaderboard(123, club_id, "Uma'", "Outfit", 3)
        query, *args = fetch.await_args.args
        self.assertEqual(args, [123, club_id, "Uma'", "Outfit", 20])
        self.assertNotIn("Uma'", query)
        self.assertIn("($1::bigint IS NULL OR c.guild_id = $1) AND c.is_active AND m.is_active", query)
        self.assertIn("WHERE $3::text IS NULL OR member_choice = 1", query)
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
    async def asyncSetUp(self):
        # Exercise the text fallback offline unless a test explicitly supplies a rendered image.
        self.renderer = patch('bot.commands.uma_archive.render_card',
                              new=AsyncMock(side_effect=RuntimeError('Browser unavailable')))
        self.renderer.start()
        self.addCleanup(self.renderer.stop)

    async def test_rendered_card_has_portraits_and_grade_icons_and_no_scan_times(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        row = entry()
        with patch.object(UmaArchive, 'leaderboard', new=AsyncMock(return_value=[row])), \
             patch('bot.commands.uma_archive.render_card', new=AsyncMock(return_value=b'png')) as renderer:
            await cog.leaderboard.callback(cog, ctx)
        response = ctx.followup.send.await_args.kwargs
        self.assertEqual(response['file'].filename, 'uma-archive.png')
        self.assertNotIn('embed', response)
        self.assertNotIn('content', response)
        self.assertFalse(response['ephemeral'])
        self.assertEqual(renderer.await_args.args[0], [row])
        self.assertNotIn('<t:', str(renderer.await_args.args))
        self.assertNotIn('Scanned', str(renderer.await_args.args))
        self.addCleanup(response['view'].stop)
        response['file'].close()

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
                response = ctx.followup.send.await_args.kwargs
                self.assertNotIn('embed', response)
                content = response['content']
                self.assertIn("Page 2/3", content)
                self.assertIn("20,000", content)
                self.assertIn("#1", content)
                self.assertIn("across Discord servers", content)
                ctx.response.defer.assert_awaited_once_with()
                view = ctx.followup.send.await_args.kwargs['view']
                self.assertFalse(view.previous.disabled)
                self.assertFalse(view.next.disabled)
                view.stop()

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

    async def test_personal_status_is_public_and_shows_overall_and_outfit_rank(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        row = entry()
        member = {**row, "entry_count": 11}
        with patch.object(UmaArchive, "linked_member", new=AsyncMock(return_value=member)) as linked, \
             patch.object(UmaArchive, "personal_scores", new=AsyncMock(return_value=[row])) as scores, \
             patch.object(UmaArchive, "standing", new=AsyncMock(return_value=row)):
            await cog.status.callback(cog, ctx)
        linked.assert_awaited_once_with(None, 456)
        scores.assert_awaited_once_with(None, row['member_id'], None, None, 1)
        ctx.response.defer.assert_awaited_once_with()
        response = ctx.followup.send.await_args.kwargs
        self.assertFalse(response['ephemeral'])
        self.assertNotIn('embed', response)
        self.assertIn("Overall global rank", response['content'])
        self.assertIn("Outfit rank", response['content'])
        self.assertIn("Page 1/2", response['content'])
        self.assertNotIn('<t:', response['content'])
        self.assertNotIn('Latest complete scan:', response['content'])
        self.addCleanup(response['view'].stop)

    async def test_personal_image_is_public_without_embed(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        row = entry()
        with patch.object(UmaArchive, 'linked_member', new=AsyncMock(return_value={**row, 'entry_count': 11})), \
             patch.object(UmaArchive, 'personal_scores', new=AsyncMock(return_value=[row])), \
             patch.object(UmaArchive, 'standing', new=AsyncMock(return_value=row)), \
             patch('bot.commands.uma_archive.render_card', new=AsyncMock(return_value=b'png')) as renderer:
            await cog.status.callback(cog, ctx)
        response = ctx.followup.send.await_args.kwargs
        self.addCleanup(response['view'].stop)
        self.assertFalse(response['ephemeral'])
        self.assertNotIn('embed', response)
        self.assertEqual(response['file'].filename, 'uma-archive.png')
        self.assertTrue(renderer.await_args.kwargs['personal'])
        self.assertIn('Overall global rank', renderer.await_args.args[2])

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

    async def test_long_user_text_fallback_keeps_all_rows_within_content_limit(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        row = entry()
        for key in ('trainer_name', 'club_name', 'uma_name', 'variant'):
            row[key] = "*_@everyone" * 40
        rows = [{**row, 'position': position} for position in range(1, 11)]
        with patch.object(UmaArchive, "leaderboard", new=AsyncMock(return_value=rows)):
            await cog.leaderboard.callback(cog, ctx)
        result = ctx.followup.send.await_args.kwargs
        self.addCleanup(result['view'].stop)
        self.assertNotIn('embed', result)
        content = result['content']
        self.assertLessEqual(len(content), 2000)
        self.assertEqual(content.count('20,000'), 10)
        for position in range(1, 11):
            self.assertIn(f'#{position} ·', content)
        self.assertIn('Page 1/3', content)
        self.assertNotIn('@everyone', content)
        self.assertFalse(result['allowed_mentions'].everyone)


class ArchivePaginationTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self, page=1, pages=3, *, personal=False):
        cog = UmaArchiveCommands(None)
        cog._render_card = AsyncMock(return_value=None)
        cog._error = AsyncMock()
        embed = discord.Embed(title='Page')
        loader = AsyncMock(return_value=(embed, [entry()], pages))
        view = ArchivePaginationView(cog, 456, page, pages, loader, personal=personal)
        self.addCleanup(view.stop)
        return view, cog, loader

    async def test_buttons_match_reference_and_disable_at_boundaries(self):
        view, _, _ = self.make_view()
        self.assertEqual([b.label for b in view.children], ['◀', '▶'])
        self.assertTrue(view.previous.disabled)
        self.assertFalse(view.next.disabled)
        view.page = 3
        view._update_buttons()
        self.assertFalse(view.previous.disabled)
        self.assertTrue(view.next.disabled)
        view.page, view.pages = 1, 1
        view._update_buttons()
        self.assertTrue(all(b.disabled for b in view.children))

    async def test_next_then_previous_edit_same_message_and_clear_old_image_on_fallback(self):
        view, _, loader = self.make_view()
        ctx = interaction()
        await view.next.callback(ctx)
        loader.assert_awaited_with(2)
        self.assertEqual(view.page, 2)
        ctx.response.defer.assert_awaited_once()
        self.assertEqual(ctx.edit_original_response.await_args.kwargs['attachments'], [])
        self.assertIsNone(ctx.edit_original_response.await_args.kwargs['embed'])
        self.assertIn('Page', ctx.edit_original_response.await_args.kwargs['content'])
        self.assertIs(ctx.edit_original_response.await_args.kwargs['view'], view)
        ctx.followup.send.assert_not_awaited()
        await view.previous.callback(ctx)
        loader.assert_awaited_with(1)
        self.assertTrue(view.previous.disabled)

    async def test_image_attachment_is_replaced_and_error_preserves_page(self):
        import io

        view, cog, _ = self.make_view()
        file = discord.File(io.BytesIO(b'png'), 'uma-archive.png')
        self.addCleanup(file.fp.close)
        cog._render_card.return_value = file
        ctx = interaction()
        await view.next.callback(ctx)
        self.assertEqual(ctx.edit_original_response.await_args.kwargs['attachments'], [file])
        self.assertIsNone(ctx.edit_original_response.await_args.kwargs['embed'])
        self.assertIsNone(ctx.edit_original_response.await_args.kwargs['content'])
        cog._render_card.return_value = None
        ctx.edit_original_response.side_effect = RuntimeError('Edit failed')
        await view.next.callback(ctx)
        self.assertEqual(view.page, 2)
        self.assertEqual(view.pages, 3)
        self.assertFalse(view.next.disabled)
        cog._error.assert_awaited_once()

    async def test_image_fallback_and_recovery_clear_previous_message_content(self):
        import io

        view, cog, _ = self.make_view()
        first = discord.File(io.BytesIO(b'first'), 'uma-archive.png')
        recovered = discord.File(io.BytesIO(b'recovered'), 'uma-archive.png')
        cog._render_card.side_effect = [first, None, recovered]
        ctx = interaction()
        await view.next.callback(ctx)
        await view.previous.callback(ctx)
        await view.next.callback(ctx)
        image, fallback, recovery = [call.kwargs for call in ctx.edit_original_response.await_args_list]
        self.assertEqual(image['attachments'], [first])
        self.assertIsNone(image['content'])
        self.assertEqual(fallback['attachments'], [])
        self.assertIn('Page', fallback['content'])
        self.assertEqual(recovery['attachments'], [recovered])
        self.assertIsNone(recovery['content'])
        for response in (image, fallback, recovery):
            self.assertIsNone(response['embed'])

    async def test_only_original_user_can_control_pages(self):
        view, _, _ = self.make_view(personal=True)
        ctx = interaction()
        self.assertTrue(await view.interaction_check(ctx))
        ctx.user.id = 999
        ctx.response.send_message = AsyncMock()
        self.assertFalse(await view.interaction_check(ctx))
        self.assertTrue(ctx.response.send_message.await_args.kwargs['ephemeral'])

    async def test_timeout_disables_controls_and_edits_original_message(self):
        view, _, _ = self.make_view()
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(all(b.disabled for b in view.children))
        view.message.edit.assert_awaited_once_with(view=view)

    async def test_concurrent_clicks_advance_without_duplicate_pages(self):
        view, _, loader = self.make_view()
        await asyncio.gather(view.next.callback(interaction()), view.next.callback(interaction()))
        self.assertEqual([c.args[0] for c in loader.await_args_list], [2, 3])
        self.assertEqual(view.page, 3)
        self.assertTrue(view.next.disabled)

    async def test_leaderboard_buttons_preserve_all_filters(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        club_id = uuid4()
        cog._club = AsyncMock(return_value=club_id)
        with patch.object(UmaArchive, 'leaderboard', new=AsyncMock(return_value=[entry()])) as fetch, \
             patch('bot.commands.uma_archive.render_card', new=AsyncMock(return_value=b'png')) as renderer:
            await cog.leaderboard.callback(cog, ctx, 'Special Week', 'Original', 'Club')
            view = ctx.followup.send.await_args.kwargs['view']
            self.addCleanup(view.stop)
            await view.next.callback(ctx)
        fetch.assert_awaited_with(None, club_id, 'Special Week', 'Original', 2)
        self.assertIn('Page 2/3', renderer.await_args.args[3])

    async def test_personal_buttons_stop_if_link_changes(self):
        cog, ctx = UmaArchiveCommands(None), interaction()
        row = entry()
        member = {**row, 'entry_count': 11}
        with patch.object(UmaArchive, 'linked_member', new=AsyncMock(side_effect=[member, None])), \
             patch.object(UmaArchive, 'personal_scores', new=AsyncMock(return_value=[row])) as scores, \
             patch.object(UmaArchive, 'standing', new=AsyncMock(return_value=row)), \
             patch('bot.commands.uma_archive.render_card', new=AsyncMock(return_value=b'png')):
            await cog.status.callback(cog, ctx)
            view = ctx.followup.send.await_args.kwargs['view']
            self.addCleanup(view.stop)
            await view.next.callback(ctx)
        scores.assert_awaited_once()
        ctx.edit_original_response.assert_not_awaited()
        self.assertIn('link has changed', ctx.followup.send.await_args.args[0])
        self.assertTrue(ctx.followup.send.await_args.kwargs['ephemeral'])


if __name__ == "__main__":
    unittest.main()
