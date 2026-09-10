"""A private command guide for Discord members."""

import discord
from discord import app_commands
from discord.ext import commands


class HelpCommands(commands.Cog):
    """Show the member guide without accessing external services."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="View UmaCore's member command guide")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="UmaCore Help",
            description=(
                "Start with `/link_trainer` using your exact in-game trainer name and club. "
                "Link your account before viewing personal status or managing notifications.\n\n"
                "Use club commands in your server. Arguments in <angle brackets> are required; "
                "choose their values in Discord's command options."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Your account",
            value=(
                "`/link_trainer <trainer_name> <club>` — Link Discord to your trainer.\n"
                "`/unlink` — Remove your trainer link.\n"
                "`/my_status` — View your quota progress and deficit or surplus.\n"
                "`/notification_settings` — View or change your DM notification preferences."
            ),
            inline=False,
        )
        embed.add_field(
            name="Clubs and progress",
            value=(
                "`/list_clubs` — View clubs registered in this server.\n"
                "`/member_status <trainer_name> <club>` — View a member's quota status.\n"
                "`/progress_chart <club>` — View this month's fan progression chart.\n"
                "`/previous_month <club>` — View last month's final fan stats."
            ),
            inline=False,
        )
        embed.add_field(
            name="Gacha and trivia",
            value=(
                "`/gacha` — Show current GameTora gacha banners.\n"
                "`/trivia play` — Start a survival trivia game.\n"
                "`/trivia leaderboard` — View the top trivia players."
            ),
            inline=False,
        )
        embed.add_field(
            name="Information",
            value="`/privacy` — View the privacy policy and terms of service.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)