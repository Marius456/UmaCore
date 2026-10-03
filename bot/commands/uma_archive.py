"""Member-facing Discord leaderboards for imported Uma Hall of Fame archives."""

import asyncio
import logging

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

from models import Club
from models.uma_archive import UmaArchive
from .common import ClubAutocompleteMixin

logger = logging.getLogger(__name__)


def label(value, limit=100):
    escaped = discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))
    return escaped[:limit].rstrip("\\")


def score_line(row):
    return (
        f"**{row['score']:,}** · {label(row['rank'], 10)}\n"
        f"{label(row['uma_name'])} — {label(row['variant'])}\n"
        f"Scanned <t:{int(row['observed_at'].timestamp())}:R>"
    )


class UmaArchiveCommands(ClubAutocompleteMixin, commands.Cog):
    uma = app_commands.Group(name="uma", description="Uma archive scores and rankings",
                             guild_only=True)
    club_autocomplete = ClubAutocompleteMixin.club_autocomplete

    def __init__(self, bot):
        self.bot = bot

    async def _club(self, interaction, club):
        if club is None:
            return None
        result = await Club.get_by_name(club, interaction.guild_id)
        if not result or not result.belongs_to_guild(interaction.guild_id):
            raise ValueError("Choose a club registered in this server.")
        return result.club_id

    @staticmethod
    def _validate_filter(uma, variant):
        if variant and not uma:
            raise ValueError("Choose an Uma before filtering by outfit.")

    async def _error(self, interaction, error, *, private=False):
        if isinstance(error, ValueError):
            message = str(error)
        elif isinstance(error, asyncpg.UndefinedTableError):
            message = "Archive scores are not set up yet. Ask the bot owner to import Hall of Fame scans."
        else:
            logger.error("Uma archive command failed", exc_info=(type(error), error, error.__traceback__))
            message = "Unable to load archive scores right now. Please try again later."
        await interaction.followup.send(message, ephemeral=private,
                                        allowed_mentions=discord.AllowedMentions.none())

    @uma.command(name="leaderboard", description="View overall or per-Uma archive score rankings")
    @app_commands.describe(uma="Uma name; omit for overall rankings",
                           variant="Outfit; omit to include all outfits",
                           club="Club in this server; omit to include all scanned clubs across servers",
                           page="Results page (10 trainers per page)")
    async def leaderboard(self, interaction: discord.Interaction, uma: str | None = None,
                          variant: str | None = None, club: str | None = None,
                          page: app_commands.Range[int, 1, 10000] = 1):
        await interaction.response.defer()
        try:
            self._validate_filter(uma, variant)
            club_id = await self._club(interaction, club)
            rows = await UmaArchive.leaderboard(None, club_id, uma, variant, page)
            if not rows:
                await interaction.followup.send(
                    "No archive scores on this page for these filters. Try page 1 or another Uma/club."
                )
                return
            title = "Uma Archive — " + (label(uma, 70) if uma else "Overall")
            description = (
                "Each trainer's highest recorded score; equal scores share a rank.\n"
                + (f"Club: {label(club)}" if club else "All scanned clubs across Discord servers")
                + (f" · Outfit: {label(variant)}" if variant else " · All outfits")
            )
            embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
            for row in rows:
                embed.add_field(
                    name=f"#{row['position']} · {label(row['trainer_name'])} · {label(row['club_name'])}",
                    value=score_line(row), inline=False,
                )
            pages = (rows[0]['participants'] + 9) // 10
            embed.set_footer(text=f"Page {page}/{pages} · {rows[0]['participants']} trainers · "
                             "Latest complete scans, not live scores · /uma status for your ranks")
            await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            await self._error(interaction, error)

    @uma.command(name="status", description="Privately view your archive scores and global rankings")
    @app_commands.describe(uma="Uma name; omit to view your whole archive",
                           variant="Outfit; omit to include all outfits",
                           page="Results page (10 scores per page)")
    async def status(self, interaction: discord.Interaction, uma: str | None = None,
                     variant: str | None = None, page: app_commands.Range[int, 1, 10000] = 1):
        await interaction.response.defer(ephemeral=True)
        try:
            self._validate_filter(uma, variant)
            member = await UmaArchive.linked_member(None, interaction.user.id)
            if not member:
                raise ValueError("Use /link_trainer in your club's server to link an active trainer first.")
            if member['observed_at'] is None:
                raise ValueError("Your archive has not been scanned yet. Ask the bot owner to import your scan.")
            if member['entry_count'] == 0:
                raise ValueError("Your latest complete archive scan contains no scores.")
            rows = await UmaArchive.personal_scores(
                None, member['member_id'], uma, variant, page,
            )
            if not rows:
                raise ValueError("No scores on this page for these filters. Try page 1 or another Uma.")
            standing = await UmaArchive.standing(None, member['member_id'], uma, variant)
            description = f"{label(member['trainer_name'])} · {label(member['club_name'])}\n"
            if standing:
                description += (
                    f"{'Filtered' if uma else 'Overall'} global rank: "
                    f"**#{standing['position']} / {standing['participants']}** "
                    f"· Best score: **{standing['score']:,}**\n"
                )
            description += f"Latest complete scan: <t:{int(member['observed_at'].timestamp())}:f>"
            embed = discord.Embed(title="Your Uma Archive", description=description,
                                  color=discord.Color.blurple())
            for row in rows:
                embed.add_field(
                    name=f"{label(row['uma_name'])} — {label(row['variant'])}",
                    value=f"**{row['score']:,}** · {label(row['rank'], 10)}\n"
                          f"Outfit rank: **#{row['position']} / {row['participants']}** trainers",
                    inline=False,
                )
            pages = (rows[0]['total_entries'] + 9) // 10
            embed.set_footer(text=f"Page {page}/{pages} · {rows[0]['total_entries']} scores · "
                             "Ranks across all scanned clubs; ties share a rank")
            await interaction.followup.send(embed=embed, ephemeral=True,
                                             allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            await self._error(interaction, error, private=True)

    async def _choices(self, interaction, current, *, variants=False):
        try:
            uma = getattr(interaction.namespace, "uma", None) if variants else None
            if variants and not uma:
                return []
            async def fetch_choices():
                club_id = await self._club(interaction, getattr(interaction.namespace, "club", None))
                return await UmaArchive.choices(
                    None, club_id, uma, current, variants=variants,
                )

            rows = await asyncio.wait_for(fetch_choices(), timeout=1.5)
            # Discord string choices have a 100-character name and value limit.
            return [app_commands.Choice(name=row['name'], value=row['name'])
                    for row in rows if 0 < len(row['name']) <= 100]
        except Exception:
            logger.debug("Archive autocomplete unavailable", exc_info=True)
            return []

    async def uma_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._choices(interaction, current)

    async def variant_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._choices(interaction, current, variants=True)

    leaderboard.autocomplete("club")(club_autocomplete)
    leaderboard.autocomplete("uma")(uma_autocomplete)
    leaderboard.autocomplete("variant")(variant_autocomplete)
    status.autocomplete("uma")(uma_autocomplete)
    status.autocomplete("variant")(variant_autocomplete)
