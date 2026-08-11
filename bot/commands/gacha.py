"""
Gacha commands cog - displays current gacha banners from GameTora
"""
import discord
from discord import app_commands
from discord.ext import commands
import logging

from scrapers import scrape_gacha_banners

logger = logging.getLogger(__name__)


class GachaCommands(commands.Cog):
    """Commands for viewing current gacha banners"""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="gacha", description="Show current gacha banners from GameTora")
    @app_commands.default_permissions(send_messages=True)
    async def gacha(self, interaction: discord.Interaction):
        """Fetch and display current gacha banners"""
        await interaction.response.defer()

        try:
            banners = await scrape_gacha_banners()

            if not banners:
                await interaction.followup.send("❌ No gacha banners found or failed to fetch data.")
                return

            embeds = []
            for banner in banners:
                embed = discord.Embed(
                    title=f"🎴 {banner.banner_type}",
                    color=discord.Color.blue(),
                    timestamp=discord.utils.utcnow()
                )

                embed.add_field(
                    name="📅 Duration",
                    value=f"{banner.start_date} – {banner.end_date}",
                    inline=False
                )

                items_text = ""
                for item in banner.items:
                    line_parts = [f"**{item.name}**"]
                    if item.variant:
                        line_parts.append(f"({item.variant})")
                    if item.is_new:
                        line_parts.append("🆕 **New**")
                    if item.rate is not None:
                        line_parts.append(f"`{item.rate}%`")
                    items_text += "• " + " ".join(line_parts) + "\n"

                embed.add_field(
                    name="⭐ Rate Up",
                    value=items_text.strip(),
                    inline=False
                )

                embed.set_footer(text="Source: GameTora")
                embeds.append(embed)

            # Single message with all banner embeds
            await interaction.followup.send(embeds=embeds)

        except Exception as e:
            logger.error(f"Error in gacha command: {e}", exc_info=True)
            await interaction.followup.send("❌ Unable to fetch gacha data right now. Please try again later.")


async def setup(bot):
    """Setup function for loading the cog"""
    await bot.add_cog(GachaCommands(bot))
