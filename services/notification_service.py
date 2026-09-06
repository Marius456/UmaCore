"""
Notification service for sending DMs to linked users
"""
import discord
from typing import List, Dict
import logging

from models import UserLink
from config.settings import COLOR_BEHIND

logger = logging.getLogger(__name__)


class NotificationService:
    """Handles sending DM notifications to linked users"""
    
    def __init__(self, bot):
        self.bot = bot
    
    async def send_deficit_notifications(self, club_name: str, members_data: List[Dict], current_date=None):
        """Send DM notifications to users who are behind quota (once per day)"""
        # Skip deficit notifications during the first 4 days of a new month
        # to avoid noisy alerts while fresh month data is still settling.
        if current_date is not None and current_date.day < 5:
            logger.info(
                f"Skipping deficit notifications for {club_name} "
                f"(day {current_date.day} < 5, new month data settling)"
            )
            return

        user_links = await UserLink.get_all_with_deficit_notifications()
        
        for item in members_data:
            member = item['member']
            history = item['history']
            
            # Only notify if actually behind
            if history.deficit_surplus >= 0:
                continue
            
            # Find if this member is linked to a Discord user
            user_link = None
            for link in user_links:
                if link.member_id == member.member_id:
                    user_link = link
                    break
            
            if not user_link:
                continue
            
            try:
                user = await self.bot.fetch_user(user_link.discord_user_id)
                
                deficit = abs(history.deficit_surplus)
                
                embed = discord.Embed(
                    title=f"⚠️ Behind Quota - {club_name}",
                    description=f"Your trainer **{member.trainer_name}** is currently behind quota.",
                    color=COLOR_BEHIND,
                    timestamp=discord.utils.utcnow()
                )
                
                # Progress bar
                if history.expected_fans > 0:
                    progress_pct = min(100, int((history.cumulative_fans / history.expected_fans) * 100))
                    filled = int(progress_pct / 5)
                    empty = 20 - filled
                    bar = "█" * filled + "░" * empty
                    
                    embed.add_field(
                        name="📉 Current Progress",
                        value=f"```\nCurrent:  {history.cumulative_fans:,} 👥\n"
                              f"Expected: {history.expected_fans:,} 👥\n"
                              f"━━━━━━━━━━━━━━━━━━━━\n"
                              f"Progress: {bar} {progress_pct}%\n```",
                        inline=False
                    )
                
                embed.add_field(
                    name="⚠️ Status",
                    value=f"**Deficit:** -{deficit:,} fans\n"
                          f"**Days Behind:** {history.days_behind} consecutive days",
                    inline=False
                )
                
                embed.set_footer(text=f"Use /my_status to check progress • {club_name}")
                
                await user.send(embed=embed)
                logger.info(f"Sent deficit notification to Discord user {user_link.discord_user_id} for {member.trainer_name} in {club_name}")
                
            except discord.Forbidden:
                logger.warning(f"Cannot send DM to user {user_link.discord_user_id} (DMs disabled)")
            except Exception as e:
                logger.error(f"Error sending deficit notification to {user_link.discord_user_id}: {e}")