"""Shared Discord command concerns that do not belong in domain services."""

import asyncio
import logging
import time

import discord
from discord import app_commands

from models import Club

logger = logging.getLogger(__name__)


class ClubAutocompleteMixin:
    """Provide consistent guild-scoped club autocomplete for command cogs."""

    _autocomplete_cache_ttl = 60.0
    _autocomplete_timeout = 1.5

    async def club_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cache = getattr(self, "_club_autocomplete_cache", {})
        self._club_autocomplete_cache = cache
        now = time.monotonic()
        cached = cache.get(interaction.guild_id)

        try:
            if cached and now - cached[0] < self._autocomplete_cache_ttl:
                club_names = cached[1]
            else:
                club_names = await asyncio.wait_for(
                    Club.get_names_for_guild(interaction.guild_id),
                    timeout=self._autocomplete_timeout,
                )
                cache[interaction.guild_id] = (time.monotonic(), club_names)
        except asyncio.TimeoutError:
            logger.warning(
                "Club autocomplete database lookup timed out for guild %s",
                interaction.guild_id,
            )
            club_names = cached[1] if cached else []
        except Exception:
            logger.exception("Error loading guild-scoped club autocomplete")
            return []
        needle = current.casefold()
        return [
            app_commands.Choice(name=name, value=name)
            for name in club_names
            if needle in name.casefold()
        ][:25]
