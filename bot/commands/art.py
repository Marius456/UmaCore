"""
Art generation command using ComfyUI
"""
import discord
from discord import app_commands
from discord.ext import commands
import io
import logging

from config.settings import ART_COMMAND_COST
from services.comfyui_service import generate_image, ComfyUIError
from services.art_balance_service import (
    get_balance,
    get_balance_info,
    deduct_balance,
    InsufficientBalanceError,
)

logger = logging.getLogger(__name__)

# Maximum prompt length enforced by Discord (1-6000 chars for app_command strings)
MAX_PROMPT_LENGTH = 6000


def _fmt_fans(value: int) -> str:
    """Format a fan count into a human-readable string."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    elif value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return str(value)


class ArtCommands(commands.Cog):
    """Commands for AI art generation via ComfyUI"""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="art",
        description="Generate an image using AI. Costs 250K fans.",
    )
    async def art(self, interaction: discord.Interaction, prompt: str):
        """
        Send a prompt to ComfyUI and return the generated image.

        Usage: /art <prompt>
        Example: /art 1girl, Umamuseme Oguri Cap eating burger, masterpiece
        """
        prompt = prompt.strip()
        if not prompt:
            await interaction.response.send_message(
                "❌ Please provide a prompt for the image generation.",
                ephemeral=True,
            )
            return

        # Check if user is linked (has a balance entry)
        balance_info = get_balance_info(interaction.user.id)
        if not balance_info:
            await interaction.response.send_message(
                "❌ You need to link your account first to use `/art`.\n"
                "Use `/link` to connect your Discord account to your club member profile.",
                ephemeral=True,
            )
            return

        # Check if user has enough fans
        current_balance = get_balance(interaction.user.id)
        if current_balance < ART_COMMAND_COST:
            await interaction.response.send_message(
                f"❌ Insufficient fans to generate art.\n\n"
                f"**Your balance:** {_fmt_fans(current_balance)} fans\n"
                f"**Cost:** {_fmt_fans(ART_COMMAND_COST)} fans\n"
                f"**Needed:** {_fmt_fans(ART_COMMAND_COST - current_balance)} more fans",
                ephemeral=True,
            )
            return

        # Defer the interaction since generation takes time
        await interaction.response.defer()

        try:
            image_bytes = await generate_image(prompt)
        except ComfyUIError as e:
            logger.error(f"ComfyUI generation failed: {e}")
            error_msg = str(e).lower()
            is_connection_error = any(
                kw in error_msg
                for kw in ("failed to connect", "cannot connect", "semaphore", "timed out")
            )

            if is_connection_error:
                embed = discord.Embed(
                    title="🖥️ ComfyUI Server Unreachable",
                    description=(
                        "The image generation server appears to be **offline or unresponsive**.\n\n"
                        "This can happen if the server went to sleep or lost network connection.\n"
                        "Please try again in a few minutes."
                    ),
                    color=0xE74C3C,
                    timestamp=discord.utils.utcnow(),
                )
                embed.set_footer(text="💡 Tip: Make sure the ComfyUI server is powered on and connected to the network.")
            else:
                embed = discord.Embed(
                    title="⚠️ Image Generation Failed",
                    description=(
                        "Something went wrong while generating your image.\n"
                        "Please try again or use a different prompt."
                    ),
                    color=0xF39C12,
                    timestamp=discord.utils.utcnow(),
                )
                embed.set_footer(text=f"Error details have been logged.")

            await interaction.followup.send(embed=embed)
            return
        except Exception as e:
            logger.error(f"Unexpected error in /art command: {e}", exc_info=True)
            embed = discord.Embed(
                title="❌ Unexpected Error",
                description=(
                    "An unexpected error occurred during image generation.\n"
                    "Please try again later."
                ),
                color=0xE74C3C,
                timestamp=discord.utils.utcnow(),
            )
            await interaction.followup.send(embed=embed)
            return

        # Deduct balance after successful generation
        try:
            remaining = deduct_balance(interaction.user.id, ART_COMMAND_COST)
        except InsufficientBalanceError as e:
            # This shouldn't happen since we checked above, but handle gracefully
            logger.error(f"Balance deduction failed after generation: {e}")
            await interaction.followup.send(
                f"❌ Image generated but failed to deduct balance: {e}\n"
                "The image is still attached below.",
            )
            # Still send the image even if deduction fails
            file = discord.File(
                io.BytesIO(image_bytes),
                filename=f"art_{interaction.id}.png",
            )
            embed = discord.Embed(
                title="🎨 Generated Art",
                description=f"**Prompt:** {prompt[:1024]}",
                color=0x9B59B6,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_image(url=f"attachment://art_{interaction.id}.png")
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            await interaction.followup.send(embed=embed, file=file)
            return

        # Wrap the image bytes in a Discord File attachment
        file = discord.File(
            io.BytesIO(image_bytes),
            filename=f"art_{interaction.id}.png",
        )

        embed = discord.Embed(
            title="🎨 Generated Art",
            description=f"**Prompt:** {prompt[:1024]}",
            color=0x9B59B6,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_image(url=f"attachment://art_{interaction.id}.png")
        embed.set_footer(
            text=(
                f"Requested by {interaction.user.display_name} · "
                f"Balance: {_fmt_fans(remaining)} fans"
            )
        )

        await interaction.followup.send(embed=embed, file=file)
        logger.info(
            f"/art command used by {interaction.user} "
            f"(prompt: {prompt[:80]}{'...' if len(prompt) > 80 else ''}, "
            f"remaining balance: {remaining:,})"
        )


async def setup(bot):
    """Setup function for loading the cog"""
    await bot.add_cog(ArtCommands(bot))