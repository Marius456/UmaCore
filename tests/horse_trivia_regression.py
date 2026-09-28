"""Photo gameplay and storage regressions; no live Discord or database credentials needed."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
from discord import app_commands
import pytest

from bot.commands.trivia import TriviaButtonView, TriviaCommands
from config.database import Database
from models.horse_trivia_question import (
    ASSET_DIR, MANIFEST_PATH, HorseTriviaQuestion, validate_https_url, validate_question,
)
from models.trivia_leaderboard import HorseTriviaLeaderboardEntry, TriviaLeaderboardEntry


def question(number=1, image="https://example.org/photo.jpg"):
    return HorseTriviaQuestion(
        number, ["Gold Ship", "Oguri Cap", "Mejiro McQueen", "Biwa Hayahide"],
        "Gold Ship", image, "https://example.org/source", "Photographer", "CC BY-SA 4.0",
        "https://creativecommons.org/licenses/by-sa/4.0/", "Cropped.",
    )


def interaction():
    message = SimpleNamespace(edit=AsyncMock())
    return SimpleNamespace(
        user=SimpleNamespace(id=123),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
        original_response=AsyncMock(return_value=message),
    )


@pytest.mark.parametrize("url", [
    "http://example.org/a.jpg", "file:///a.jpg", "https:///a", "javascript:alert(1)",
    "https://user:pass@example.org/a", "https://example.org:bad/a", "https://exa mple.org",
    "https://example.org/a\nb", "https://example.org/<a>", "https://[invalid/a",
])
def test_invalid_photo_urls(url):
    with pytest.raises(ValueError):
        validate_https_url(url)


@pytest.mark.parametrize("options", [
    ["A", "a", "B", "C"], ["A", " A ", "B", "C"], ["", "B", "C", "D"],
    ["A" * 81, "B", "C", "D"], ["A", "B", "C"],
])
def test_invalid_answer_options(options):
    with pytest.raises(ValueError):
        validate_question(options, "Photographer", "CC BY 4.0")


def test_starter_pack_is_complete_and_uses_image_urls():
    entries = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert len(entries) == 20
    assert len({entry["seed_key"] for entry in entries}) == 20
    assert len({entry["correct_answer"] for entry in entries}) == 20
    assert not list(ASSET_DIR.glob("*.jpg"))
    for entry in entries:
        validate_question(entry["options"], entry["author"], entry["license"])
        validate_https_url(entry["source_url"])
        assert entry["options"].count(entry["correct_answer"]) == 1
        validate_https_url(entry["image_reference"])
        assert entry["image_reference"].startswith("https://upload.wikimedia.org/")
        assert entry["modifications"]
        if entry["license"].startswith("CC"):
            assert entry["license_url"].startswith("https://creativecommons.org/")


class HorseGameTests(unittest.IsolatedAsyncioTestCase):
    async def run_game(self, questions, outcomes, *, send_error=None):
        cog = TriviaCommands(None)
        request = interaction()
        if send_error:
            request.original_response.return_value.edit.side_effect = send_error
        results = iter(outcomes)

        async def wait(view):
            outcome = next(results)
            if outcome is None:
                await view.on_timeout()
            else:
                view.is_correct = outcome
                view.selected_answer = view.question.correct_answer if outcome else "wrong"
            view.stop()

        with patch.object(cog, "_get_next_horse_question", new=AsyncMock(side_effect=questions)), \
             patch.object(TriviaButtonView, "wait", new=wait), \
             patch("bot.commands.trivia.asyncio.sleep", new=AsyncMock()), \
             patch.object(HorseTriviaLeaderboardEntry, "record_result", new=AsyncMock(return_value=(None, True))) as photo_scores, \
             patch.object(TriviaLeaderboardEntry, "record_result", new=AsyncMock()) as text_scores:
            try:
                await cog._start_game(request, horse=True)
            except RuntimeError:
                if not send_error:
                    raise
            self.assertEqual(cog.active_sessions, {})
            text_scores.assert_not_awaited()
            return request, photo_scores

    async def test_horse_command_defers_privately(self):
        cog = TriviaCommands(None)
        cog._start_game = AsyncMock()
        request = interaction()
        await TriviaCommands.horse.callback(cog, request)
        request.response.defer.assert_awaited_once_with(ephemeral=True)
        cog._start_game.assert_awaited_once_with(request, horse=True)

    async def test_correct_then_wrong_reveals_answer_and_only_records_photo_score(self):
        request, scores = await self.run_game([question(), question(2)], [True, False])
        scores.assert_awaited_once_with(123, 1, 1)
        first = request.edit_original_response.await_args.kwargs
        self.assertEqual(first["embed"].description, "Which horse is this?")
        self.assertEqual(first["view"].timeout, 20)
        edits = request.original_response.return_value.edit.await_args_list
        self.assertIn("Correct! **Gold Ship**", edits[0].kwargs["embed"].description)
        self.assertEqual(
            edits[0].kwargs["embed"].description,
            "Correct! **Gold Ship**\n\n[Source](<https://example.org/source>)",
        )
        final = edits[-1].kwargs
        self.assertIn("Incorrect. **Gold Ship**", final["embed"].description)
        self.assertIn("1** question", final["embed"].description)
        self.assertTrue(all(button.disabled for button in final["view"].children))

    async def test_timeout_reveals_correct_name_and_button_without_awarding_points(self):
        request, scores = await self.run_game([question()], [None])
        final = request.original_response.return_value.edit.await_args.kwargs
        self.assertIn("Time's up! **Gold Ship**", final["embed"].description)
        correct = next(button for button in final["view"].children if button.label == "Gold Ship")
        self.assertEqual(correct.style, discord.ButtonStyle.success)
        self.assertTrue(all(button.disabled for button in final["view"].children))
        scores.assert_not_awaited()

    async def test_rounds_embed_urls_without_file_uploads(self):
        urls = [f"https://example.org/photo-{i}.jpg" for i in range(3)]
        with patch("bot.commands.trivia.discord.File", side_effect=AssertionError("No uploads")):
            request, _ = await self.run_game(
                [question(i, url) for i, url in enumerate(urls)], [True, True, False],
            )
        first = request.edit_original_response.await_args.kwargs
        edits = request.original_response.return_value.edit.await_args_list
        for kwargs, url in zip([first, edits[1].kwargs, edits[3].kwargs], urls):
            self.assertEqual(kwargs["embed"].image.url, url)
            self.assertEqual(kwargs["attachments"], [])

    async def test_empty_bank_ends_privately_without_scoring(self):
        request, scores = await self.run_game([None], [])
        self.assertIn("No usable horse photos", request.edit_original_response.await_args.kwargs["embed"].description)
        scores.assert_not_awaited()

    async def test_bank_disappearing_preserves_earned_points(self):
        request, scores = await self.run_game([question(), None], [True])
        scores.assert_awaited_once_with(123, 1, 1)
        self.assertIn("No usable horse photos", request.edit_original_response.await_args.kwargs["embed"].description)

    async def test_invalid_image_url_skipped_without_penalty_or_infinite_loop(self):
        missing = question(1, "not-present.jpg")
        good = question(2)
        with patch.object(HorseTriviaQuestion, "get_random", new=AsyncMock(side_effect=[missing, good, None, None])) as select:
            cog = TriviaCommands(None)
            request = interaction()
            async def wait(view):
                view.is_correct = True
                view.stop()
            with patch.object(TriviaButtonView, "wait", new=wait), \
                 patch.object(HorseTriviaLeaderboardEntry, "record_result", new=AsyncMock(return_value=(None, True))) as scores:
                await cog._start_game(request, horse=True)
            scores.assert_awaited_once_with(123, 1, 1)
            self.assertEqual(select.await_args_list[1].args[0], {1})
            self.assertEqual(select.await_args_list[-1].args[0], {1})
            self.assertEqual(cog.active_sessions, {})

    async def test_all_image_urls_invalid_terminates(self):
        request, scores = await self.run_game([question(1, "missing.jpg"), None], [])
        self.assertIn("No usable horse photos", request.edit_original_response.await_args.kwargs["embed"].description)
        scores.assert_not_awaited()

    async def test_send_failure_preserves_earned_points(self):
        _, scores = await self.run_game([question()], [True], send_error=RuntimeError("Discord unavailable"))
        scores.assert_awaited_once_with(123, 1, 1)

    async def test_selection_cycles_without_repeating_until_exhausted(self):
        cog = TriviaCommands(None)
        used, unavailable = set(), {99}
        with patch.object(HorseTriviaQuestion, "get_random", new=AsyncMock(side_effect=[question(), question(2), None, question()])) as select:
            for expected in [1, 2, 1]:
                self.assertEqual((await cog._get_next_horse_question(used, unavailable)).id, expected)
        self.assertEqual([call.args[0] for call in select.await_args_list], [{99}, {1, 99}, {1, 2, 99}, {99}])

    async def test_modes_share_duplicate_session_guard(self):
        cog = TriviaCommands(None)
        cog.active_sessions[123] = {7}
        for horse in [True, False]:
            request = interaction()
            await cog._start_game(request, horse=horse)
            self.assertTrue(request.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(cog.active_sessions[123], {7})

    async def test_only_owner_can_answer_and_choices_are_shuffled_without_mutation(self):
        q = question()
        with patch("bot.commands.trivia.random.shuffle", side_effect=lambda options: options.reverse()):
            view = TriviaButtonView(q, 123)
        self.assertEqual([button.label for button in view.children], list(reversed(q.options)))
        self.assertEqual(q.options[0], "Gold Ship")
        other = interaction()
        other.user.id = 999
        self.assertFalse(await view.interaction_check(other))
        self.assertTrue(other.response.send_message.await_args.kwargs["ephemeral"])
        self.assertTrue(await view.interaction_check(interaction()))
        view.stop()

    async def test_double_click_cannot_change_first_answer(self):
        view = TriviaButtonView(question(), 123)
        correct = next(button for button in view.children if button.label == "Gold Ship")
        wrong = next(button for button in view.children if button.label != "Gold Ship")
        await asyncio.gather(correct.callback(interaction()), wrong.callback(interaction()))
        self.assertTrue(view.is_correct)
        self.assertEqual(view.selected_answer, "Gold Ship")

    async def test_score_failure_is_not_retried_and_releases_session(self):
        cog = TriviaCommands(None)
        async def wait(view):
            view.is_correct = True
            view.stop()
        with patch.object(cog, "_get_next_horse_question", new=AsyncMock(side_effect=[question(), None])), \
             patch.object(TriviaButtonView, "wait", new=wait), \
             patch.object(HorseTriviaLeaderboardEntry, "record_result", new=AsyncMock(side_effect=RuntimeError("DB"))) as scores:
            with self.assertRaises(RuntimeError):
                await cog._start_game(interaction(), horse=True)
            scores.assert_awaited_once()
        self.assertEqual(cog.active_sessions, {})


class HorseStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_create_trims_names_and_stores_correct_answer(self):
        row = asdict(question())
        with patch("models.horse_trivia_question.db.fetchrow", new=AsyncMock(return_value=row)) as fetch:
            await HorseTriviaQuestion.create(
                options=[" Gold Ship ", "Oguri Cap", "Mejiro McQueen", "Biwa Hayahide"],
                image_url=row["image_reference"], source_url=row["source_url"],
                author=row["author"], license=row["license"],
            )
        self.assertEqual(fetch.await_args.args[1][0], "Gold Ship")
        self.assertEqual(fetch.await_args.args[2], "Gold Ship")
        self.assertIn("INSERT INTO horse_trivia_questions", fetch.await_args.args[0])

    async def test_deleted_seed_is_not_recreated_on_restart(self):
        rows = {}
        async def execute(query, *args):
            if "INSERT INTO" in query:
                self.assertIn("ON CONFLICT (seed_key) DO NOTHING", query)
                rows.setdefault(args[0], {"deleted": False})
            else:
                self.assertNotIn("deleted_at =", query)
                self.assertIn("AND image_reference = $8", query)
        conn = SimpleNamespace(execute=AsyncMock(side_effect=execute))
        @asynccontextmanager
        async def transaction():
            yield conn
        with patch("models.horse_trivia_question.db.transaction", transaction):
            await HorseTriviaQuestion.seed()
            first_key = next(iter(rows))
            rows[first_key]["deleted"] = True
            await HorseTriviaQuestion.seed()
        self.assertEqual(len(rows), 20)
        self.assertTrue(rows[first_key]["deleted"])

    async def test_delete_soft_deletes_and_queries_exclude_tombstones(self):
        with patch("models.horse_trivia_question.db.fetchrow", new=AsyncMock(return_value={"id": 7})) as fetch:
            self.assertTrue(await HorseTriviaQuestion.delete(7))
            self.assertIn("SET deleted_at = NOW()", fetch.await_args.args[0])
            fetch.return_value = None
            self.assertFalse(await HorseTriviaQuestion.delete(7))
            await HorseTriviaQuestion.get_random({7})
            self.assertIn("deleted_at IS NULL", fetch.await_args.args[0])
            self.assertEqual(fetch.await_args.args[1], [7])

    async def test_photo_score_upsert_and_personal_best_use_separate_table(self):
        row = dict(user_id=123, highest_streak=5, total_correct=12, last_played=None)
        conn = SimpleNamespace(execute=AsyncMock(), fetchval=AsyncMock(return_value=5), fetchrow=AsyncMock(return_value=row))
        @asynccontextmanager
        async def transaction():
            yield conn
        with patch("models.trivia_leaderboard.db.transaction", transaction):
            entry, is_new = await HorseTriviaLeaderboardEntry.record_result(123, 5, 5)
        self.assertIsInstance(entry, HorseTriviaLeaderboardEntry)
        self.assertFalse(is_new)
        conn.execute.assert_awaited_once_with("SELECT pg_advisory_xact_lock($1)", 123)
        self.assertIn("FROM horse_trivia_leaderboard", conn.fetchval.await_args.args[0])
        self.assertIn("INSERT INTO horse_trivia_leaderboard", conn.fetchrow.await_args.args[0])
        self.assertEqual(TriviaLeaderboardEntry.table, "trivia_leaderboard")

    async def test_schema_is_additive_for_existing_trivia(self):
        conn = SimpleNamespace(execute=AsyncMock())
        @asynccontextmanager
        async def acquire():
            yield conn
        database = Database("")
        database.pool = SimpleNamespace(acquire=acquire)
        await database.initialize_schema()
        sql = conn.execute.await_args.args[0]
        for table in ["horse_trivia_questions", "horse_trivia_leaderboard", "trivia_questions", "trivia_leaderboard"]:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
            self.assertNotIn(f"DROP TABLE {table}", sql)
            self.assertNotIn(f"ALTER TABLE {table}", sql)

    async def test_seed_migrates_legacy_references_without_overwriting_custom_urls(self):
        entries = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        rows = {
            entries[0]["seed_key"]: {"image": "001.jpg", "deleted": False},
            entries[1]["seed_key"]: {"image": "002.jpg", "deleted": True},
            entries[2]["seed_key"]: {"image": "https://example.org/custom.jpg", "deleted": False},
        }
        async def execute(query, *args):
            if "INSERT INTO" in query:
                rows.setdefault(args[0], {"image": args[3], "deleted": False})
            else:
                self.assertIn("WHERE seed_key = $1 AND image_reference = $8", query)
                self.assertNotIn("deleted_at =", query)
                self.assertNotIn("options =", query)
                if rows[args[0]]["image"] == args[7]:
                    rows[args[0]]["image"] = args[1]
        conn = SimpleNamespace(execute=AsyncMock(side_effect=execute))
        @asynccontextmanager
        async def transaction():
            yield conn
        with patch("models.horse_trivia_question.db.transaction", transaction):
            await HorseTriviaQuestion.seed()
            await HorseTriviaQuestion.seed()
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[entries[0]["seed_key"]]["image"], entries[0]["image_reference"])
        self.assertEqual(rows[entries[1]["seed_key"]]["image"], entries[1]["image_reference"])
        self.assertTrue(rows[entries[1]["seed_key"]]["deleted"])
        self.assertEqual(rows[entries[2]["seed_key"]]["image"], "https://example.org/custom.jpg")


class HorseCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_commands_require_administrator(self):
        request = interaction()
        request.permissions = discord.Permissions.none()
        for command in [TriviaCommands.horse_add, TriviaCommands.horse_list, TriviaCommands.horse_delete]:
            for check in command.checks:
                with self.assertRaises(app_commands.MissingPermissions):
                    check(request)
        request.permissions = discord.Permissions(administrator=True)
        for command in [TriviaCommands.horse_add, TriviaCommands.horse_list, TriviaCommands.horse_delete]:
            self.assertTrue(command.checks[0](request))

    async def test_admin_validation_error_is_private(self):
        request = interaction()
        await TriviaCommands.horse_add.callback(
            TriviaCommands(None), request, "http://example.org/a.jpg", "https://example.org/source",
            "Author", "CC BY 4.0", "A", "B", "C", "D",
        )
        self.assertIn("HTTPS", request.followup.send.await_args.args[0])
        self.assertTrue(request.followup.send.await_args.kwargs["ephemeral"])

    async def test_photo_leaderboard_is_public_and_separate(self):
        request = interaction()
        with patch.object(HorseTriviaLeaderboardEntry, "get_top", new=AsyncMock(return_value=[])) as photo, \
             patch.object(TriviaLeaderboardEntry, "get_top", new=AsyncMock()) as text:
            await TriviaCommands.horse_leaderboard.callback(TriviaCommands(None), request)
        request.response.defer.assert_awaited_once_with()
        photo.assert_awaited_once_with(10)
        text.assert_not_awaited()
        embed = request.followup.send.await_args.kwargs["embed"]
        self.assertIn("Horse Photo", embed.title)
        self.assertIn("/trivia horse", embed.description)
