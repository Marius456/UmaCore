"""
Member status and user linking commands
"""
import discord
from discord import app_commands
from discord.ext import commands
import io
import logging

from models import Member, UserLink, Club
from services.member_status_service import load_member_status
from services.member_status_card import render_card, fallback_embed
from .common import ClubAutocompleteMixin

logger = logging.getLogger(__name__)


class MemberCommands(ClubAutocompleteMixin, commands.Cog):
    """Member status and user linking commands"""

    club_autocomplete = ClubAutocompleteMixin.club_autocomplete
    
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="link_trainer", description="Link your Discord account to your trainer")
    async def link_trainer(self, interaction: discord.Interaction, trainer_name: str, club: str):
        """Link your Discord account to a trainer"""
        await interaction.response.defer(ephemeral=True)
        
        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(
                    f"❌ Club '{club}' not found.",
                    ephemeral=True
                )
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(
                    f"❌ Club '{club}' is not registered in this server.",
                    ephemeral=True,
                )
                return
            
            member = await Member.get_by_name(club_obj.club_id, trainer_name)
            
            if not member:
                await interaction.followup.send(
                    f"❌ Trainer '{trainer_name}' not found in {club}. Make sure the name matches exactly.",
                    ephemeral=True
                )
                return
            
            # Check if already linked to another trainer
            existing_link = await UserLink.get_by_discord_id(interaction.user.id)
            if existing_link:
                existing_member = await Member.get_by_id(existing_link.member_id)
                if existing_member and existing_member.member_id == member.member_id:
                    await interaction.followup.send(
                        f"ℹ️ You're already linked to **{trainer_name}** in **{club}**",
                        ephemeral=True
                    )
                    return
                else:
                    # Unlink from old trainer
                    await UserLink.delete(interaction.user.id)
                    old_name = existing_member.trainer_name if existing_member else "a deleted member"
                    logger.info(f"Unlinked user {interaction.user.id} from {old_name}")
            
            # Create link
            await UserLink.create(
                discord_user_id=interaction.user.id,
                member_id=member.member_id,
                notify_on_deficit=False
            )
            
            embed = discord.Embed(
                title="✅ Trainer Linked!",
                description=f"Your Discord account is now linked to **{trainer_name}** in **{club}**",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            
            embed.add_field(
                name="🔔 Notifications Enabled",
                value="• **Deficit Alerts:** ❌ Disabled",
                inline=False
            )
            
            embed.add_field(
                name="💡 Next Steps",
                value="• Use `/my_status` to check your progress\n"
                      "• Use `/notification_settings` to customize alerts\n"
                      "• Use `/unlink` to remove the link",
                inline=False
            )
            
            embed.set_footer(text="You'll receive DMs when important events happen")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"User {interaction.user.id} linked to {trainer_name} in {club}")
            
        except Exception as e:
            logger.error(f"Error in link_trainer: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.", ephemeral=True)
    
    @app_commands.command(name="unlink", description="Unlink your Discord account from your trainer")
    async def unlink(self, interaction: discord.Interaction):
        """Unlink your Discord account"""
        await interaction.response.defer(ephemeral=True)
        
        try:
            user_link = await UserLink.get_by_discord_id(interaction.user.id)
            
            if not user_link:
                await interaction.followup.send(
                    "ℹ️ You don't have a linked trainer",
                    ephemeral=True
                )
                return
            
            member = await Member.get_by_id(user_link.member_id)
            await UserLink.delete(interaction.user.id)
            
            embed = discord.Embed(
                title="✅ Trainer Unlinked",
                description=f"Your Discord account has been unlinked from **{member.trainer_name}**",
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            
            embed.add_field(
                name="ℹ️ What this means",
                value="You will no longer receive DM notifications about quota status.",
                inline=False
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"User {interaction.user.id} unlinked from {member.trainer_name}")
            
        except Exception as e:
            logger.error(f"Error in unlink: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.", ephemeral=True)
    
    @app_commands.command(name="notification_settings", description="Manage your notification preferences")
    async def notification_settings(self, interaction: discord.Interaction, 
                                   deficit_alerts: bool = None):
        """Manage notification settings"""
        await interaction.response.defer(ephemeral=True)
        
        try:
            user_link = await UserLink.get_by_discord_id(interaction.user.id)
            
            if not user_link:
                await interaction.followup.send(
                    "❌ You need to link a trainer first using `/link_trainer`",
                    ephemeral=True
                )
                return
            
            # If no settings provided, show current settings
            if deficit_alerts is None:
                member = await Member.get_by_id(user_link.member_id)
                
                embed = discord.Embed(
                    title="🔔 Notification Settings",
                    description=f"Settings for **{member.trainer_name}**",
                    color=discord.Color.blue(),
                    timestamp=discord.utils.utcnow()
                )
                
                deficit_status = "✅ Enabled" if user_link.notify_on_deficit else "❌ Disabled"
                
                embed.add_field(
                    name="Current Settings",
                    value=f"**⚠️ Deficit Alerts:** {deficit_status}",
                    inline=False
                )
                
                embed.add_field(
                    name="ℹ️ How to change",
                    value="Use `/notification_settings deficit_alerts:True` or similar to update settings",
                    inline=False
                )
                
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # Update settings
            new_deficit_setting = deficit_alerts if deficit_alerts is not None else user_link.notify_on_deficit
            
            await user_link.update_notifications(new_deficit_setting)
            
            member = await Member.get_by_id(user_link.member_id)
            
            embed = discord.Embed(
                title="✅ Settings Updated",
                description=f"Notification settings for **{member.trainer_name}** have been updated",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            
            deficit_status = "✅ Enabled" if new_deficit_setting else "❌ Disabled"
            
            embed.add_field(
                name="New Settings",
                value=f"**⚠️ Deficit Alerts:** {deficit_status}",
                inline=False
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"User {interaction.user.id} updated notification settings")
            
        except Exception as e:
            logger.error(f"Error in notification_settings: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.", ephemeral=True)
    
    @app_commands.command(name="my_status", description="View your own quota status")
    async def my_status(self, interaction: discord.Interaction):
        """View your own linked trainer status"""
        await interaction.response.defer()
        
        try:
            user_link = await UserLink.get_by_discord_id(interaction.user.id)
            
            if not user_link:
                await interaction.followup.send(
                    "❌ You haven't linked a trainer yet. Use `/link_trainer` to get started!"
                )
                return
            
            member = await Member.get_by_id(user_link.member_id)
            await self._send_member_status(interaction, member)
            
        except Exception as e:
            logger.error(f"Error in my_status: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")
    
    @app_commands.command(name="member_status", description="View status of a specific member")
    async def member_status(self, interaction: discord.Interaction, trainer_name: str, club: str):
        """Get detailed status for a specific member"""
        await interaction.response.defer()
        
        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(
                    f"❌ Club '{club}' is not registered in this server."
                )
                return
            
            member = await Member.get_by_name(club_obj.club_id, trainer_name)
            
            if not member:
                await interaction.followup.send(f"❌ Member '{trainer_name}' not found in {club}")
                return
            
            await self._send_member_status(interaction, member)
            
        except Exception as e:
            logger.error(f"Error in member_status: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")
    
    async def _send_member_status(self, interaction: discord.Interaction, member: Member):
        """Send the shared status card, retaining text delivery if rendering fails."""
        if member is None:
            await interaction.followup.send(
                "Your linked trainer no longer exists. Use `/link_trainer` to link again."
            )
            return
        status = await load_member_status(member)
        if status is None:
            await interaction.followup.send(f"No quota data found for {member.trainer_name}")
            return
        try:
            png = await render_card(status)
        except Exception:
            logger.warning("Status card rendering failed; sending text fallback", exc_info=True)
            await interaction.followup.send(embed=fallback_embed(status))
            return
        attachment = discord.File(io.BytesIO(png), filename="member-status.png")
        try:
            await interaction.followup.send(file=attachment)
        finally:
            attachment.close()

    # Apply autocomplete
    link_trainer.autocomplete('club')(club_autocomplete)
    member_status.autocomplete('club')(club_autocomplete)
