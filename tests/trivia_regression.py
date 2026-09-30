import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.commands.trivia import TriviaCommands
from models.trivia_question import TriviaQuestion
from models.trivia_leaderboard import TriviaLeaderboardEntry


class FakeInteraction:
    def __init__(self, user_id=123):
        self.user = SimpleNamespace(id=user_id)
        self.followup = SimpleNamespace(send=AsyncMock())
        self.edit_original_response = AsyncMock()
        self.original_response = AsyncMock()


class FakeMessage:
    def __init__(self, ephemeral=True):
        self.ephemeral = ephemeral
        self.edit = AsyncMock()


class TriviaQuestionSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_random_passes_excluded_question_ids_to_database(self):
        row = {
            "id": 3,
            "question_text": "Question",
            "options": ["A", "B", "C", "D"],
            "correct_answer": "A",
        }

        with patch("models.trivia_question.db.fetchrow", new=AsyncMock(return_value=row)) as fetchrow:
            question = await TriviaQuestion.get_random({1, 2})

        self.assertEqual(question.id, 3)
        query, excluded_ids = fetchrow.await_args.args
        self.assertIn("id = ANY($1::int[])", query)
        self.assertEqual(excluded_ids, [1, 2])

    async def test_session_history_tracks_questions_and_resets_after_full_cycle(self):
        commands = TriviaCommands(None)
        used_ids = set()
        first = TriviaQuestion(1, "First", ["A"], "A")
        second = TriviaQuestion(2, "Second", ["A"], "A")

        with patch.object(
            TriviaQuestion,
            "get_random",
            new=AsyncMock(side_effect=[first, second, None, first]),
        ) as get_random:
            self.assertIs(await commands._get_next_question(used_ids), first)
            self.assertEqual(used_ids, {1})
            self.assertIs(await commands._get_next_question(used_ids), second)
            self.assertEqual(used_ids, {1, 2})
            self.assertIs(await commands._get_next_question(used_ids), first)
            self.assertEqual(used_ids, {1})

        self.assertEqual(get_random.await_args_list[0].kwargs["excluded_ids"], set())
        self.assertEqual(get_random.await_args_list[1].kwargs["excluded_ids"], {1})
        self.assertEqual(get_random.await_args_list[2].kwargs["excluded_ids"], {1, 2})
        self.assertEqual(get_random.await_args_list[3].args, ())

    async def test_new_session_starts_with_empty_history(self):
        commands = TriviaCommands(None)
        self.assertEqual(commands.active_sessions, {})

    async def test_empty_question_bank_does_not_create_history(self):
        commands = TriviaCommands(None)
        used_ids = set()

        with patch.object(
            TriviaQuestion, "get_random", new=AsyncMock(return_value=None)
        ) as get_random:
            question = await commands._get_next_question(used_ids)

        self.assertIsNone(question)
        self.assertEqual(used_ids, set())
        get_random.assert_awaited_once_with(excluded_ids=set())


class TriviaVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_play_defers_ephemeral_and_starts_game(self):
        commands = TriviaCommands(None)
        commands._start_game = AsyncMock()
        interaction = FakeInteraction()
        interaction.response = SimpleNamespace(defer=AsyncMock())

        await TriviaCommands.play.callback(commands, interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        commands._start_game.assert_awaited_once_with(interaction)

    async def test_game_uses_one_ephemeral_original_message(self):
        commands = TriviaCommands(None)
        interaction = FakeInteraction()
        message = FakeMessage()
        interaction.original_response.return_value = message
        question = TriviaQuestion(1, "Question", ["A", "B", "C", "D"], "A")

        with patch.object(commands, "_get_next_question", new=AsyncMock(return_value=question)), \
             patch("bot.commands.trivia.TriviaButtonView.wait", new=AsyncMock(return_value=None)):
            await commands._start_game(interaction)

        interaction.edit_original_response.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()
        message.edit.assert_awaited_once()
        self.assertTrue(message.ephemeral)

    async def test_duplicate_session_message_is_ephemeral(self):
        commands = TriviaCommands(None)
        commands.active_sessions[123] = set()
        interaction = FakeInteraction()

        await commands._start_game(interaction)

        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])


class TriviaLeaderboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_tied_streak_is_not_reported_as_a_new_record(self):
        row = {
            "user_id": 123,
            "highest_streak": 5,
            "total_correct": 20,
            "last_played": None,
        }
        connection = SimpleNamespace(
            execute=AsyncMock(),
            fetchval=AsyncMock(return_value=5),
            fetchrow=AsyncMock(return_value=row),
        )

        class Transaction:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_):
                return False

        with patch("models.trivia_leaderboard.db.transaction", return_value=Transaction()):
            entry, is_new = await TriviaLeaderboardEntry.record_result(123, 5, 5)

        self.assertEqual(entry.highest_streak, 5)
        self.assertFalse(is_new)
        connection.execute.assert_awaited_once_with(
            "SELECT pg_advisory_xact_lock($1)", 123
        )
        connection.fetchval.assert_awaited_once()
        connection.fetchrow.assert_awaited_once()

    async def test_empty_question_bank_message_is_ephemeral(self):
        commands = TriviaCommands(None)
        interaction = FakeInteraction()

        with patch.object(commands, "_get_next_question", new=AsyncMock(return_value=None)):
            await commands._start_game(interaction)

        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()
