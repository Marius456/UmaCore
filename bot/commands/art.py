"""
Art generation command using ComfyUI
"""
import discord
from discord import app_commands
from discord.ext import commands
import io
import logging

from services.comfyui_service import generate_image, ComfyUIError

logger = logging.getLogger(__name__)

# Maximum prompt length enforced by Discord (1-6000 chars for app_command strings)
MAX_PROMPT_LENGTH = 6000


class ArtCommands(commands.Cog):
    """Commands for AI art generation via ComfyUI"""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="art",
        description="Generate an image using AI. Provide a text prompt describing what you want.",
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

        # Defer the interaction since generation takes time
        await interaction.response.defer()

        try:
            image_bytes = await generate_image(prompt)
        except ComfyUIError as e:
            logger.error(f"ComfyUI generation failed: {e}")
            await interaction.followup.send(
                f"❌ Image generation failed: {e}",
            )
            return
        except Exception as e:
            logger.error(f"Unexpected error in /art command: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ An unexpected error occurred during image generation.",
            )
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
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")

        await interaction.followup.send(embed=embed, file=file)
        logger.info(
            f"/art command used by {interaction.user} "
            f"(prompt: {prompt[:80]}{'...' if len(prompt) > 80 else ''})"
        )


async def setup(bot):
    """Setup function for loading the cog"""
    await bot.add_cog(ArtCommands(bot))