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
from models.horse_trivia_question import HorseTriviaQuestion
from models.trivia_leaderboard import TriviaLeaderboardEntry, HorseTriviaLeaderboardEntry
from services.horse_image_url import image_url_for_game

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
        if view.is_finished() or view.selected_answer is not None:
            await interaction.response.defer()
            return
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

    def __init__(
        self, question: TriviaQuestion | HorseTriviaQuestion,
        author_id: int, timeout: float = QUESTION_TIMEOUT,
    ):
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
        if self.selected_answer is not None:
            return
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
        try:
            await HorseTriviaQuestion.seed()
        except Exception:
            logger.exception("Failed to seed horse-photo trivia")

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
    def _create_question_embed(
        question: TriviaQuestion | HorseTriviaQuestion, current_streak: int,
    ) -> discord.Embed:
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
        # Pass a snapshot because this set is mutated after the query.
        question = await TriviaQuestion.get_random(
            excluded_ids=set(used_question_ids)
        )
        if question is None and used_question_ids:
            used_question_ids.clear()
            question = await TriviaQuestion.get_random()

        if question is not None:
            used_question_ids.add(question.id)
        return question

    async def _get_next_horse_question(self, used_ids: set[int], unavailable_ids: set[int]):
        question = await HorseTriviaQuestion.get_random(used_ids | unavailable_ids)
        if question is None and used_ids:
            used_ids.clear()
            question = await HorseTriviaQuestion.get_random(unavailable_ids)
        if question is not None:
            used_ids.add(question.id)
        return question

    @staticmethod
    def _reveal_horse(embed, question, view):
        embed = embed.copy()
        outcome = "Correct!" if view.is_correct else (
            "Time's up!" if view.selected_answer is None else "Incorrect."
        )
        embed.description = f"{outcome} **{discord.utils.escape_markdown(question.correct_answer)}**"
        embed.description += f"\n\n[Source](<{question.source_url}>)"
        for button in view.children:
            button.disabled = True
            if button.label == question.correct_answer:
                button.style = discord.ButtonStyle.success
        return embed

    async def _start_game(self, interaction: discord.Interaction, *, horse: bool = False):
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
        unavailable_ids: set[int] = set()
        current_streak = 0
        total_correct = 0
        score_attempted = False
        view = None
        score_model = HorseTriviaLeaderboardEntry if horse else TriviaLeaderboardEntry

        async def save_score():
            nonlocal score_attempted
            if total_correct and not score_attempted:
                # Never retry an ambiguous database failure and risk counting twice.
                score_attempted = True
                _, is_new = await score_model.record_result(user_id, current_streak, total_correct)
                return is_new
            return False

        try:
            is_first = True
            message = None

            while True:
                # Fetch a random question that has not appeared in this cycle.
                question = (
                    await self._get_next_horse_question(used_question_ids, unavailable_ids)
                    if horse else await self._get_next_question(used_question_ids)
                )
                if not question:
                    if horse:
                        is_new_record = await save_score()
                        result = self._create_result_embed(current_streak, is_new_record)
                        result.description += (
                            "\nNo usable horse photos remain. Ask an admin to use `/trivia horse_add`."
                        )
                        await interaction.edit_original_response(
                            embed=result, view=None, attachments=[]
                        )
                        return
                    await interaction.followup.send(
                        "❌ No trivia questions available! Ask an admin to add some with `/trivia add`.",
                        ephemeral=True
                    )
                    return

                embed = self._create_question_embed(question, current_streak)
                edits = {}
                if horse:
                    embed.title = f"🐎 Horse #{current_streak + 1}"
                    embed.set_footer(text=f"Current streak: {current_streak} | 20 seconds to choose")
                    try:
                        embed.set_image(url=image_url_for_game(question.image_reference))
                    except ValueError:
                        logger.warning("Skipping invalid horse photo URL #%s", question.id)
                        unavailable_ids.add(question.id)
                        continue
                    edits["attachments"] = []

                if not is_first:
                    await asyncio.sleep(3.0 if horse else 1.5)
                view = TriviaButtonView(question, user_id, timeout=QUESTION_TIMEOUT)
                if is_first:
                    is_first = False
                    await interaction.edit_original_response(embed=embed, view=view, **edits)
                    message = await interaction.original_response()
                else:
                    await message.edit(embed=embed, view=view, **edits)

                # Wait for the user to answer (or timeout)
                await view.wait()

                if horse:
                    embed = self._reveal_horse(embed, question, view)

                if view.is_correct:
                    current_streak += 1
                    total_correct += 1
                    if horse:
                        embed.set_footer(text=f"Current streak: {current_streak} | Next photo shortly")
                        await message.edit(embed=embed, view=view)
                else:
                    # Game over — update leaderboard and show result
                    is_new_record = await save_score()
                    result_embed = self._create_result_embed(current_streak, is_new_record)
                    if horse:
                        result_embed.description += f"\n\n{embed.description}"
                        result_embed.set_image(url=embed.image.url)
                    await message.edit(embed=result_embed, view=view)
                    return

        finally:
            try:
                if view is not None:
                    view.disable_all_buttons()
                    view.stop()
                # Preserve earned photo points if fetching/sending the next round fails.
                if horse:
                    await save_score()
            finally:
                self.active_sessions.pop(user_id, None)

    # ──────────────────────────────────────────────
    #  Slash commands
    # ──────────────────────────────────────────────

    @trivia.command(name="play", description="Start a survival trivia game")
    async def play(self, interaction: discord.Interaction):
        """Start a new survival trivia game"""
        await interaction.response.defer(ephemeral=True)
        await self._start_game(interaction)

    @trivia.command(name="horse", description="Guess real racehorses from photos")
    async def horse(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self._start_game(interaction, horse=True)
        except Exception:
            logger.exception("Horse-photo game failed")
            await interaction.followup.send(
                "The photo game could not continue. Please try again later.", ephemeral=True
            )

    @trivia.command(name="horse_leaderboard", description="View the horse-photo trivia leaderboard")
    async def horse_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            entries = await HorseTriviaLeaderboardEntry.get_top(10)
            embed = self._create_leaderboard_embed(entries)
            embed.title = "🐎 Horse Photo Leaderboard"
            embed.set_footer(text="Play with /trivia horse to get on the leaderboard!")
            if not entries:
                embed.description = "No one has played yet! Be the first with `/trivia horse`!"
            await interaction.followup.send(embed=embed)
        except Exception:
            logger.exception("Unable to fetch horse-photo leaderboard")
            await interaction.followup.send("Unable to fetch horse-photo scores. Please try again later.")

    @trivia.command(name="horse_add", description="Add a horse photo and four answers (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        image_url="Stable, public HTTPS image URL (not an expiring attachment link)",
        source_url="HTTPS source page for identity and attribution",
        author="Photographer or rights holder", license="Reuse license, e.g. CC BY-SA 4.0",
        correct_answer="Correct horse name", wrong_option_1="First wrong horse name",
        wrong_option_2="Second wrong horse name", wrong_option_3="Third wrong horse name",
    )
    async def horse_add(
        self, interaction: discord.Interaction, image_url: str, source_url: str,
        author: str, license: str, correct_answer: str,
        wrong_option_1: str, wrong_option_2: str, wrong_option_3: str,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            image_url_for_game(image_url)
            question = await HorseTriviaQuestion.create(
                options=[correct_answer, wrong_option_1, wrong_option_2, wrong_option_3],
                image_url=image_url, source_url=source_url, author=author, license=license,
            )
            await interaction.followup.send(f"Horse photo added (ID: {question.id}).", ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception("Unable to add horse photo")
            await interaction.followup.send("Unable to add the photo. Please try again later.", ephemeral=True)

    @trivia.command(name="horse_list", description="List horse photos and answers (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def horse_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            questions = await HorseTriviaQuestion.get_all()
            if not questions:
                await interaction.followup.send("No horse photos found.", ephemeral=True)
            for start in range(0, len(questions), 5):
                embed = discord.Embed(title="Horse Photo Questions", color=discord.Color.blue())
                for question in questions[start:start + 5]:
                    choices = ", ".join(discord.utils.escape_markdown(x) for x in question.options)
                    embed.add_field(
                        name=f"#{question.id} — {question.correct_answer}",
                        value=f"{choices}\n[Source](<{question.source_url}>)", inline=False,
                    )
                await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception:
            logger.exception("Unable to list horse photos")
            await interaction.followup.send("Unable to list horse photos. Please try again later.", ephemeral=True)

    @trivia.command(name="horse_delete", description="Delete a horse photo (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def horse_delete(self, interaction: discord.Interaction, question_id: int):
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await HorseTriviaQuestion.delete(question_id)
            text = f"Horse photo #{question_id} deleted." if deleted else "No horse photo found with that ID."
            await interaction.followup.send(text, ephemeral=True)
        except Exception:
            logger.exception("Unable to delete horse photo")
            await interaction.followup.send("Unable to delete the photo. Please try again later.", ephemeral=True)

    @horse_add.error
    @horse_list.error
    @horse_delete.error
    async def horse_admin_error(self, interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need administrator permissions to manage horse photos.", ephemeral=True
            )
        else:
            logger.error("Horse admin command failed: %s", error, exc_info=error)
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender("Unable to manage horse photos. Please try again later.", ephemeral=True)

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
            await interaction.followup.send("❌ Unable to fetch the leaderboard right now. Please try again later.")

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
                "❌ Failed to add the question. Please try again later.",
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
                "❌ An unexpected error occurred. Please try again later.",
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
                "❌ Failed to fetch questions. Please try again later.", ephemeral=True
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
                "❌ Failed to delete the question. Please try again later.", ephemeral=True
            )


async def setup(bot):
    """Setup function for loading the cog"""
    await bot.add_cog(TriviaCommands(bot))
