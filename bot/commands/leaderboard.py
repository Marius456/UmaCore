"""
Leaderboard Report command — on-demand news-style embed of monthly
leaderboard position changes, streaks, rivalries, and records.
"""
import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
import pytz

from models import Club
from scrapers import UmaMoeAPIScraper
from services.leaderboard_report_service import LeaderboardReportService
from services.highscore_service import HighscoreService

logger = logging.getLogger(__name__)


class LeaderboardCommands(commands.Cog):
    """Commands for the leaderboard news-style report."""

    def __init__(self, bot):
        self.bot = bot

    async def club_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        try:
            club_names = await Club.get_names_for_guild(interaction.guild_id)
            return [
                app_commands.Choice(name=name, value=name)
                for name in club_names
                if current.lower() in name.lower()
            ][:25]
        except Exception as e:
            logger.error(f"Error in club autocomplete: {e}")
            return []

    @app_commands.command(
        name="leaderboard_report",
        description="Generate a news-style leaderboard report for the current month",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def leaderboard_report(
        self, interaction: discord.Interaction, club: str
    ):
        """
        Generate and post a leaderboard report embed analyzing position
        changes, streaks, rivalries, and records for the current month.
        """
        await interaction.response.defer()

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found.")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(
                    f"❌ Club '{club}' is not registered in this server."
                )
                return

            # Determine current year/month in the club's timezone
            club_tz = pytz.timezone(club_obj.timezone)
            now = datetime.now(club_tz)
            year, month = now.year, now.month

            tier_kwargs = {}
            if club_obj.circle_id:
                scraper = UmaMoeAPIScraper(club_obj.circle_id)
                tier_progress = await scraper.fetch_tier_progress(year, month)
                if tier_progress:
                    tier_kwargs.update(tier_progress)
                else:
                    logger.warning(
                        "Live uma.moe tier progress unavailable for %s; "
                        "omitting Club Goal",
                        club_obj.club_name,
                    )

            # Generate the report embeds (overflow fields split into followup embeds)
            embeds = await LeaderboardReportService.generate_leaderboard_report(
                club_obj.club_id,
                club_obj.club_name,
                year,
                month,
                **tier_kwargs,
            )

            # Send to the interaction channel
            await interaction.followup.send(embed=embeds[0])
            for extra_embed in embeds[1:]:
                await interaction.followup.send(embed=extra_embed)
            await LeaderboardReportService.persist_delivered_predictions(embeds)

            # Also post to the club's report channel if configured and different
            if (
                club_obj.report_channel_id
                and club_obj.report_channel_id != interaction.channel_id
            ):
                report_channel = self.bot.get_channel(club_obj.report_channel_id)
                if report_channel:
                    await report_channel.send(embed=embeds[0])
                    for extra_embed in embeds[1:]:
                        await report_channel.send(embed=extra_embed)
                    logger.info(
                        f"leaderboard_report crossposted to report channel "
                        f"{club_obj.report_channel_id} for {club}"
                    )

            logger.info(
                f"leaderboard_report sent for {club} ({year}-{month:02d}) "
                f"by {interaction.user}"
            )

        except ValueError as e:
            logger.warning(f"leaderboard_report data error: {e}")
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")
        except Exception as e:
            logger.error(
                f"Error in leaderboard_report for {club}: {e}", exc_info=True
            )
            await interaction.followup.send(
                "❌ An unexpected error occurred while generating the report. Please try again later."
            )

    @app_commands.command(
        name="club_highscores",
        description="Show all-time club highscores (best daily gain, monthly total, best rank)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def club_highscores(
        self, interaction: discord.Interaction, club: str
    ):
        """
        Generate and post an embed showing all-time historical highscores
        for the selected club.
        """
        await interaction.response.defer()

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found.")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(
                    f"❌ Club '{club}' is not registered in this server."
                )
                return

            # Generate the highscore embed (hybrid DB + API for complete history)
            embed = await HighscoreService.generate_highscore_embed(
                club_obj.club_id,
                club_obj.club_name,
                circle_id=club_obj.circle_id,
            )

            # Send to the interaction channel
            await interaction.followup.send(embed=embed)

            # Also post to the club's report channel if configured and different
            if (
                club_obj.report_channel_id
                and club_obj.report_channel_id != interaction.channel_id
            ):
                report_channel = self.bot.get_channel(club_obj.report_channel_id)
                if report_channel:
                    await report_channel.send(embed=embed)
                    logger.info(
                        f"club_highscores crossposted to report channel "
                        f"{club_obj.report_channel_id} for {club}"
                    )

            logger.info(
                f"club_highscores sent for {club} "
                f"by {interaction.user}"
            )

        except ValueError as e:
            logger.warning(f"club_highscores data error: {e}")
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")
        except Exception as e:
            logger.error(
                f"Error in club_highscores for {club}: {e}", exc_info=True
            )
            await interaction.followup.send(
                "❌ An unexpected error occurred while generating highscores. Please try again later."
            )

    club_highscores.autocomplete("club")(club_autocomplete)
    leaderboard_report.autocomplete("club")(club_autocomplete)


async def setup(bot):
    """Setup function for loading the cog."""
    await bot.add_cog(LeaderboardCommands(bot))
