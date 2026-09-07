"""Shared Discord command concerns that do not belong in domain services."""

import logging

import discord
from discord import app_commands

from models import Club

logger = logging.getLogger(__name__)


class ClubAutocompleteMixin:
    """Provide consistent guild-scoped club autocomplete for command cogs."""

    async def club_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            club_names = await Club.get_names_for_guild(interaction.guild_id)
        except Exception:
            logger.exception("Error loading guild-scoped club autocomplete")
            return []
        needle = current.casefold()
        return [
            app_commands.Choice(name=name, value=name)
            for name in club_names
            if needle in name.casefold()
        ][:25]
