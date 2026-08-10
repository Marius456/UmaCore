"""
Survival Trivia game commands with button-based UI
"""
import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import random
import asyncio
import logging
from typing import Optional, List

from models.trivia_question import TriviaQuestion
from models.trivia_leaderboard import TriviaLeaderboardEntry

logger = logging.getLogger(__name__)

SEED_DATA_PATH = "data/trivia_questions.json"
QUESTION_TIMEOUT = 20.0  # Seconds per question


class TriviaButton(discord.ui.Button):
    """A single answer button in the trivia game"""

    def __init__(self, label: str, correct_answer: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.correct_answer = correct_answer

    async def callback(self, interaction: discord.Interaction):
        """Handle button click: mark answer, style buttons, stop the view"""
        view: TriviaButtonView = self.view
        view.selected_answer = self.label
        view.is_correct = (self.label == self.correct_answer)

        # Disable all buttons
        for child in view.children:
            child.disabled = True

        # Style buttons: green for correct, red for wrong selection
        for child in view.children:
            if child.label == view.question.correct_answer:
                child.style = discord.ButtonStyle.success
            elif child.label == self.label and not view.is_correct:
                child.style = discord.ButtonStyle.danger

        await interaction.response.edit_message(view=view)
        view.stop()


class TriviaButtonView(discord.ui.View):
    """View containing the 4 answer buttons for a trivia question"""

    def __init__(self, question: TriviaQuestion, author_id: int, timeout: float = QUESTION_TIMEOUT):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.question = question
        self.selected_answer: Optional[str] = None
        self.is_correct: Optional[bool] = None

        # Shuffle options for display
        options = question.options[:]
        random.shuffle(options)
        for option in options:
            self.add_item(TriviaButton(option, question.correct_answer))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the game author can interact with these buttons"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ This is not your trivia game!", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        """Handle timeout: disable all buttons"""
        self.disable_all_buttons()
        self.selected_answer = None
        self.is_correct = False
        self.stop()

    def disable_all_buttons(self):
        """Disable all buttons in the view"""
        for child in self.children:
            child.disabled = True


class TriviaCommands(commands.Cog):
    """Survival trivia game commands"""

    def __init__(self, bot):
        self.bot = bot
        self.active_sessions: dict[int, set[int]] = {}
        self._seed_loaded = False

    trivia = app_commands.Group(name="trivia", description="Survival trivia game commands")

    # ──────────────────────────────────────────────
    #  Cog lifecycle
    # ──────────────────────────────────────────────

    async def cog_load(self):
        """Load seed questions when the cog is loaded"""
        await self._load_seed_questions()

    async def _load_seed_questions(self):
        """Load seed questions from JSON if the trivia_questions table is empty"""
        if self._seed_loaded:
            return
        try:
            count = await TriviaQuestion.get_count()
            if count == 0:
                if not os.path.exists(SEED_DATA_PATH):
                    logger.warning(f"Seed data file not found: {SEED_DATA_PATH}")
                    return
                with open(SEED_DATA_PATH, 'r', encoding='utf-8') as f:
                    questions = json.load(f)
                for q in questions:
                    await TriviaQuestion.create(
                        question_text=q['question_text'],
                        options=q['options'],
                        correct_answer=q['correct_answer']
                    )
                logger.info(f"Loaded {len(questions)} seed trivia questions into the database")
            self._seed_loaded = True
        except Exception as e:
            logger.error(f"Failed to load seed trivia questions: {e}")

    # ──────────────────────────────────────────────
    #  Embed builders
    # ──────────────────────────────────────────────

    @staticmethod
    def _create_question_embed(question: TriviaQuestion, current_streak: int) -> discord.Embed:
        """Format a trivia question into an embed"""
        embed = discord.Embed(
            title=f"🎯 Question #{current_streak + 1}",
            description=question.question_text,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Current streak: {current_streak} | Choose wisely!")
        return embed

    @staticmethod
    def _create_result_embed(final_streak: int, is_new_record: bool) -> discord.Embed:
        """Format the end-of-game result embed"""
        if final_streak > 0:
            title = "🎉 Game Over!"
            description = f"You got **{final_streak}** question(s) right!"
            if is_new_record:
                description += "\n🌟 **New Personal Best!** 🎊"
            color = discord.Color.green()
        else:
            title = "😢 Game Over!"
            description = "Better luck next time!"
            color = discord.Color.red()

        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )
        return embed

    @staticmethod
    def _create_leaderboard_embed(entries: List[TriviaLeaderboardEntry]) -> discord.Embed:
        """Format the trivia leaderboard embed"""
        embed = discord.Embed(
            title="🏆 Trivia Leaderboard",
            color=discord.Color.gold()
        )

        if not entries:
            embed.description = "No one has played yet! Be the first with `/trivia play`!"
            return embed

        lines = []
        for i, entry in enumerate(entries, 1):
            if i == 1:
                medal = "🥇"
            elif i == 2:
                medal = "🥈"
            elif i == 3:
                medal = "🥉"
            else:
                medal = f"**{i}.**"

            lines.append(
                f"{medal} <@{entry.user_id}> — **{entry.highest_streak}** streak, "
                f"{entry.total_correct} total correct"
            )

        embed.description = "\n".join(lines)
        embed.set_footer(text="Play with /trivia play to get on the leaderboard!")
        return embed

    # ──────────────────────────────────────────────
    #  Core game loop
    # ──────────────────────────────────────────────

    async def _get_next_question(self, used_question_ids: set[int]) -> Optional[TriviaQuestion]:
        """Get an unused question, beginning a new cycle when the bank is exhausted."""
        question = await TriviaQuestion.get_random(excluded_ids=used_question_ids)
        if question is None and used_question_ids:
            used_question_ids.clear()
            question = await TriviaQuestion.get_random()

        if question is not None:
            used_question_ids.add(question.id)
        return question

    async def _start_game(self, interaction: discord.Interaction):
        """Core game loop: fetches questions, handles answers, manages streaks"""
        user_id = interaction.user.id

        # Prevent duplicate sessions
        if user_id in self.active_sessions:
            await interaction.followup.send(
                "⚠️ You already have an active trivia game! Please finish it first.",
                ephemeral=True
            )
            return

        used_question_ids: set[int] = set()
        self.active_sessions[user_id] = used_question_ids

        try:
            current_streak = 0
            total_correct = 0
            is_first = True
            message = None

            while True:
                # Fetch a random question that has not appeared in this cycle.
                question = await self._get_next_question(used_question_ids)
                if not question:
                    await interaction.followup.send(
                        "❌ No trivia questions available! Ask an admin to add some with `/trivia add`.",
                        ephemeral=True
                    )
                    return

                embed = self._create_question_embed(question, current_streak)
                view = TriviaButtonView(question, user_id, timeout=QUESTION_TIMEOUT)

                if is_first:
                    is_first = False
                    await interaction.edit_original_response(embed=embed, view=view)
                    message = await interaction.original_response()
                else:
                    # Brief pause before showing the next question
                    await asyncio.sleep(1.5)
                    await message.edit(embed=embed, view=view)

                # Wait for the user to answer (or timeout)
                await view.wait()

                if view.is_correct:
                    current_streak += 1
                    total_correct += 1
                else:
                    # Game over — update leaderboard and show result
                    if current_streak > 0:
                        entry = await TriviaLeaderboardEntry.upsert(
                            user_id, current_streak, total_correct
                        )
                        # Check if this is a new personal best record
                        is_new_record = (current_streak >= entry.highest_streak)
                    else:
                        is_new_record = False

                    result_embed = self._create_result_embed(current_streak, is_new_record)
                    await message.edit(embed=result_embed, view=view)
                    return

        finally:
            # Clean up session
            self.active_sessions.pop(user_id, None)

    # ──────────────────────────────────────────────
    #  Slash commands
    # ──────────────────────────────────────────────

    @trivia.command(name="play", description="Start a survival trivia game")
    async def play(self, interaction: discord.Interaction):
        """Start a new survival trivia game"""
        await interaction.response.defer(ephemeral=True)
        await self._start_game(interaction)

    @trivia.command(name="leaderboard", description="View the trivia leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        """Display the top 10 trivia players"""
        await interaction.response.defer()

        try:
            entries = await TriviaLeaderboardEntry.get_top(10)
            embed = self._create_leaderboard_embed(entries)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error in leaderboard command: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error fetching leaderboard: {str(e)}")

    @trivia.command(name="add", description="Add a trivia question (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        question="The trivia question text",
        correct_answer="The correct answer",
        wrong_option_1="First wrong answer option",
        wrong_option_2="Second wrong answer option",
        wrong_option_3="Third wrong answer option"
    )
    async def add_question(
        self,
        interaction: discord.Interaction,
        question: str,
        correct_answer: str,
        wrong_option_1: str,
        wrong_option_2: str,
        wrong_option_3: str
    ):
        """Add a new trivia question to the database (Admin only)"""
        await interaction.response.defer(ephemeral=True)

        options = [correct_answer, wrong_option_1, wrong_option_2, wrong_option_3]

        # Validate: no duplicate options
        if len(set(options)) != 4:
            await interaction.followup.send(
                "❌ All four options must be unique. Please provide 4 distinct answers.",
                ephemeral=True
            )
            return

        try:
            q = await TriviaQuestion.create(
                question_text=question,
                options=options,
                correct_answer=correct_answer
            )
            await interaction.followup.send(
                f"✅ Trivia question added successfully! (ID: {q.id})",
                ephemeral=True
            )
            logger.info(f"Admin {interaction.user.id} added trivia question #{q.id}")
        except Exception as e:
            logger.error(f"Error adding trivia question: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Failed to add question: {str(e)}",
                ephemeral=True
            )

    @add_question.error
    async def add_question_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Handle permission errors for the add command"""
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need administrator permissions to add trivia questions.",
                ephemeral=True
            )
        else:
            logger.error(f"Unexpected error in add_question: {error}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ An unexpected error occurred: {str(error)}",
                    ephemeral=True
                )


    @trivia.command(name="list", description="List all trivia questions (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def list_questions(self, interaction: discord.Interaction):
        """List every trivia question, including its answer options (Admin only)."""
        await interaction.response.defer(ephemeral=True)

        try:
            questions = await TriviaQuestion.get_all()
            if not questions:
                await interaction.followup.send("No trivia questions found.", ephemeral=True)
                return

            for start in range(0, len(questions), 10):
                batch = questions[start:start + 10]
                embed = discord.Embed(
                    title="Trivia Questions",
                    description=f"Showing questions {start + 1}-{start + len(batch)} of {len(questions)}.",
                    color=discord.Color.blue()
                )
                for question in batch:
                    options = "\n".join(
                        f"{'✅' if option == question.correct_answer else '▫️'} {option}"
                        for option in question.options
                    )
                    embed.add_field(
                        name=f"#{question.id} — {question.question_text[:240]}",
                        value=f"{options}\n**Correct:** {question.correct_answer}",
                        inline=False
                    )
                await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error listing trivia questions: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Failed to fetch questions: {str(e)}", ephemeral=True
            )

    @trivia.command(name="delete", description="Delete a trivia question (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(question_id="The ID of the question to delete")
    async def delete_question(self, interaction: discord.Interaction, question_id: int):
        """Delete one trivia question by ID (Admin only)."""
        await interaction.response.defer(ephemeral=True)

        try:
            deleted = await TriviaQuestion.delete(question_id)
            if deleted:
                await interaction.followup.send(
                    f"✅ Trivia question #{question_id} deleted.", ephemeral=True
                )
                logger.info(f"Admin {interaction.user.id} deleted trivia question #{question_id}")
            else:
                await interaction.followup.send(
                    f"❌ No trivia question found with ID #{question_id}.", ephemeral=True
                )
        except Exception as e:
            logger.error(f"Error deleting trivia question #{question_id}: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Failed to delete question: {str(e)}", ephemeral=True
            )


async def setup(bot):
    """Setup function for loading the cog"""
    await bot.add_cog(TriviaCommands(bot))
