"""
Administrative commands for quota management
"""
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta
import logging
import pytz
import asyncio

from scrapers import UmaMoeAPIScraper
from services import (
    MonthlyInfoService,
    QuotaCalculator,
    ReportGenerator,
    ScrapeContext,
    ScrapeLockUnavailableError,
)
from models import Member, QuotaRequirement, Club, ClubRankHistory

logger = logging.getLogger(__name__)


class AdminCommands(commands.Cog):
    """Administrative commands for quota management"""

    def __init__(self, bot):
        self.bot = bot
        self.quota_calculator = QuotaCalculator()
        self.report_generator = ReportGenerator()
        self.monthly_info_service = MonthlyInfoService()

    async def club_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for club names visible in this guild"""
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

    async def _update_monthly_info_board(self, club_obj: Club, current_date) -> bool:
        """Auto-update the monthly info board after quota changes"""
        try:
            channel_id, message_id = await club_obj.get_monthly_info_location()
            if channel_id and message_id:
                channel = self.bot.get_channel(channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(message_id)
                        updated_embed = await self.monthly_info_service.create_monthly_info_embed(
                            club_obj.club_id, club_obj.club_name, current_date, club_obj.quota_period
                        )
                        await message.edit(embed=updated_embed)
                        logger.info(f"Auto-updated monthly info board for {club_obj.club_name}")
                        return True
                    except discord.NotFound:
                        logger.warning(f"Monthly info message not found for {club_obj.club_name}")
                    except discord.Forbidden:
                        logger.error(f"No permission to edit monthly info message for {club_obj.club_name}")
                    except Exception as e:
                        logger.error(f"Error editing monthly info message: {e}")
        except Exception as e:
            logger.error(f"Error updating monthly info board: {e}")
        return False

    @app_commands.command(name="quota", description="Set the daily quota requirement")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_quota(self, interaction: discord.Interaction, amount: int, club: str):
        """Set the daily quota requirement"""
        await interaction.response.defer()

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(f"❌ Club '{club}' is not registered in this server.")
                return

            if amount <= 0:
                await interaction.followup.send("❌ Quota amount must be positive")
                return

            period_caps = {'daily': 10_000_000, 'weekly': 100_000_000, 'biweekly': 200_000_000}
            max_quota = period_caps.get(club_obj.quota_period, 10_000_000)
            if amount > max_quota:
                cap_label = f"{max_quota // 1_000_000}M"
                await interaction.followup.send(f"❌ Quota amount seems unreasonably high (>{cap_label} for {club_obj.quota_period} quota). Please check your input.")
                return

            club_tz = pytz.timezone(club_obj.timezone)
            current_datetime = datetime.now(club_tz)
            current_date = current_datetime.date()

            set_by = f"{interaction.user.name}#{interaction.user.discriminator}"
            await QuotaRequirement.create(
                club_id=club_obj.club_id,
                effective_date=current_date,
                daily_quota=amount,
                set_by=set_by
            )

            if amount >= 1_000_000:
                formatted = f"{amount / 1_000_000:.1f}M"
            elif amount >= 1_000:
                formatted = f"{amount / 1_000:.1f}K"
            else:
                formatted = str(amount)

            period_label = {'daily': 'day', 'weekly': 'week', 'biweekly': '2 weeks'}.get(club_obj.quota_period, 'day')
            period_name = {'daily': 'Daily', 'weekly': 'Weekly', 'biweekly': 'Biweekly'}.get(club_obj.quota_period, 'Daily')

            embed = discord.Embed(
                title=f"✅ Quota Updated - {club}",
                description=f"{period_name} quota has been set to **{formatted} fans/{period_label}**",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )

            embed.add_field(
                name="Effective Date",
                value=current_date.strftime('%Y-%m-%d'),
                inline=True
            )

            embed.add_field(
                name="Exact Amount",
                value=f"{amount:,} fans",
                inline=True
            )

            embed.add_field(
                name="Set By",
                value=set_by,
                inline=True
            )

            embed.add_field(
                name="ℹ️ Important",
                value="This quota applies from today onwards. Previous days are unaffected.",
                inline=False
            )

            await interaction.followup.send(embed=embed)
            logger.info(f"Quota set to {amount:,} for {club} by {set_by} effective {current_date}")

            # Auto-update monthly info board
            updated = await self._update_monthly_info_board(club_obj, current_date)
            if updated:
                await interaction.followup.send("✅ Monthly info board auto-updated!", ephemeral=True)

        except Exception as e:
            logger.error(f"Error in set_quota: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")

    @app_commands.command(name="update_monthly_info", description="Update the monthly info board")
    @app_commands.checks.has_permissions(administrator=True)
    async def update_monthly_info(self, interaction: discord.Interaction, club: str):
        """Update the existing monthly info board"""
        await interaction.response.defer()

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(f"❌ Club '{club}' is not registered in this server.")
                return

            channel_id, message_id = await club_obj.get_monthly_info_location()

            if not channel_id or not message_id:
                await interaction.followup.send(
                    f"❌ No monthly info board found for {club}. Use `/post_monthly_info` first."
                )
                return

            channel = self.bot.get_channel(channel_id)
            if not channel:
                await interaction.followup.send("❌ Channel not found. The board may have been deleted.")
                return

            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                await interaction.followup.send("❌ Message not found. Use `/post_monthly_info` to create a new one.")
                return

            club_tz = pytz.timezone(club_obj.timezone)
            current_datetime = datetime.now(club_tz)
            current_date = current_datetime.date()

            embed = await self.monthly_info_service.create_monthly_info_embed(
                club_obj.club_id,
                club_obj.club_name,
                current_date,
                club_obj.quota_period
            )

            await message.edit(embed=embed)
            await interaction.followup.send(f"✅ Monthly info board updated for {club}!")

        except Exception as e:
            logger.error(f"Error in update_monthly_info: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")

    @app_commands.command(name="quota_history", description="View quota changes this month")
    @app_commands.checks.has_permissions(administrator=True)
    async def quota_history(self, interaction: discord.Interaction, club: str):
        """View quota change history for the current month"""
        await interaction.response.defer()

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(f"❌ Club '{club}' is not registered in this server.")
                return

            club_tz = pytz.timezone(club_obj.timezone)
            current_datetime = datetime.now(club_tz)
            current_date = current_datetime.date()

            quota_reqs = await QuotaRequirement.get_all_for_month(
                club_obj.club_id, current_date.year, current_date.month
            )

            quota_period_label = {'daily': 'day', 'weekly': 'week', 'biweekly': '2 weeks'}.get(club_obj.quota_period, 'day')

            if not quota_reqs:
                embed = discord.Embed(
                    title=f"📊 Quota History - {club} - Current Month",
                    description=f"No quota changes this month.\n"
                                f"Using default: **{club_obj.daily_quota:,} fans/{quota_period_label}**",
                    color=discord.Color.blue(),
                    timestamp=discord.utils.utcnow()
                )
                await interaction.followup.send(embed=embed)
                return

            embed = discord.Embed(
                title=f"📊 Quota History - {club} - Current Month",
                description=f"Showing {len(quota_reqs)} quota change(s)",
                color=discord.Color.blue(),
                timestamp=discord.utils.utcnow()
            )

            for quota_req in quota_reqs:
                amount = quota_req.daily_quota
                if amount >= 1_000_000:
                    formatted = f"{amount / 1_000_000:.1f}M"
                elif amount >= 1_000:
                    formatted = f"{amount / 1_000:.1f}K"
                else:
                    formatted = str(amount)

                embed.add_field(
                    name=f"{quota_req.effective_date.strftime('%B %d, %Y')}",
                    value=f"**{formatted} fans/{quota_period_label}** ({amount:,})\n"
                          f"Set by: {quota_req.set_by or 'Unknown'}",
                    inline=False
                )

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in quota_history: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")

    @app_commands.command(name="delete_quota", description="Delete a specific quota requirement entry by date and amount")
    @app_commands.checks.has_permissions(administrator=True)
    async def delete_quota(self, interaction: discord.Interaction, club: str, date: str, amount: int):
        """Delete a specific quota requirement entry (use /quota_history to find the values)"""
        await interaction.response.defer()

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(f"❌ Club '{club}' is not registered in this server.")
                return

            try:
                effective_date = datetime.strptime(date, "%Y-%m-%d").date()
            except ValueError:
                await interaction.followup.send("❌ Invalid date format. Use YYYY-MM-DD")
                return

            deleted = await QuotaRequirement.delete_by_date_and_amount(
                club_obj.club_id, effective_date, amount
            )

            if deleted == 0:
                await interaction.followup.send(
                    f"❌ No quota requirement found for **{club}** on `{date}` with amount `{amount:,}`. "
                    f"Use `/quota_history` to see existing entries."
                )
                return

            if amount >= 1_000_000:
                formatted = f"{amount / 1_000_000:.1f}M"
            elif amount >= 1_000:
                formatted = f"{amount / 1_000:.1f}K"
            else:
                formatted = str(amount)

            embed = discord.Embed(
                title=f"✅ Quota Entry Deleted - {club}",
                description=f"Removed **{formatted} fans/day** effective `{date}`",
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(
                name="ℹ️ Next Steps",
                value="The bot will now use the next applicable quota entry. "
                      "Run `/quota_history` to verify, or `/force_check` to recalculate.",
                inline=False
            )
            embed.set_footer(text=f"Deleted by {interaction.user}")

            await interaction.followup.send(embed=embed)
            logger.info(f"Quota entry deleted for {club} ({amount:,} on {date}) by {interaction.user}")

            await self._update_monthly_info_board(club_obj, effective_date)

        except Exception as e:
            logger.error(f"Error in delete_quota: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")

    @app_commands.command(name="force_check", description="Manually trigger a quota check and report")
    @app_commands.checks.has_permissions(administrator=True)
    async def force_check(self, interaction: discord.Interaction, club: str):
        """Manually trigger the daily check"""
        await interaction.response.defer()
        scrape_context = None

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(f"❌ Club '{club}' is not registered in this server.")
                return

            scrape_context = ScrapeContext(
                club_obj.club_id, f"force_check_{club_obj.club_name}"
            )
            await scrape_context.__aenter__()

            report_channel = self.bot.get_channel(club_obj.report_channel_id)
            alert_channel = self.bot.get_channel(club_obj.alert_channel_id or club_obj.report_channel_id)

            if not report_channel:
                await interaction.followup.send(f"❌ Report channel not found for {club}. Use `/set_report_channel` first.")
                return

            if not alert_channel:
                alert_channel = report_channel

            club_tz = pytz.timezone(club_obj.timezone)
            current_datetime = datetime.now(club_tz)
            current_date = current_datetime.date()

            # Initialize Uma.moe API scraper with validation
            if not club_obj.circle_id:
                await interaction.followup.send(
                    f"❌ **Missing Circle ID for {club}**\n\n"
                    f"No circle_id has been set for this club.\n\n"
                    f"**To fix this:**\n"
                    f"Use `/edit_club club:{club} circle_id:<numeric_id>`\n\n"
                    f"**How to find your Circle ID:**\n"
                    f"1. Go to https://uma.moe/circles/\n"
                    f"2. Search for **{club}**\n"
                    f"3. Copy the number from the URL"
                )
                logger.error(f"No circle_id configured for {club_obj.club_name}")
                return

            if not club_obj.is_circle_id_valid():
                error_msg = club_obj.get_circle_id_help_message()
                await interaction.followup.send(error_msg)
                logger.error(f"Invalid circle_id format for {club}: '{club_obj.circle_id}'")
                return

            scraper = UmaMoeAPIScraper(club_obj.circle_id)
            await interaction.followup.send(f"Using Uma.moe API scraper for {club}...")
            logger.info(f"Using Uma.moe API scraper for {club_obj.club_name} (circle_id: {club_obj.circle_id})")

            # Scrape with retry logic
            max_retries = 3
            retry_delay = 10
            scraped_data = None
            current_day = None

            for attempt in range(1, max_retries + 1):
                try:
                    await interaction.followup.send(f"🔄 Scraping {club} (attempt {attempt}/{max_retries})...")
                    scraped_data = await scraper.scrape()
                    current_day = scraper.get_current_day()

                    if scraped_data:
                        break
                except Exception:
                    if attempt == max_retries:
                        raise
                    await interaction.followup.send(f"⚠️ Attempt {attempt} failed, retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2

            if not scraped_data:
                await interaction.followup.send("❌ Failed to scrape data after all retries")
                return

            # Use the scraper's data date in case of previous-month fallback (e.g. Day 1)
            data_date = scraper.get_data_date()
            if data_date:
                current_date = data_date
                logger.info(f"Using scraper's data date: {current_date} (previous-month fallback)")

            # Extract and persist club rank data
            rank_data = None
            monthly_rank = scraper.get_monthly_rank()
            last_month_rank = scraper.get_last_month_rank()
            yesterday_rank = scraper.get_yesterday_rank()

            if monthly_rank is not None:
                try:
                    await ClubRankHistory.save(club_obj.club_id, current_date, monthly_rank, monthly_rank)
                except Exception as e:
                    logger.error(f"Failed to save rank data for {club_obj.club_name}: {e}", exc_info=True)

                rank_data = {
                    'monthly_rank': monthly_rank,
                    'last_month_rank': last_month_rank,
                    'yesterday_rank': yesterday_rank,
                }
                logger.info(
                    f"Rank data for {club_obj.club_name}: "
                    f"monthly={monthly_rank}, yesterday={yesterday_rank}, "
                    f"last_month={last_month_rank}"
                )

            # Process scraped data
            await interaction.followup.send("⚙️ Processing data...")
            new_members, updated_members = await self.quota_calculator.process_scraped_data(
                club_obj.club_id, scraped_data, current_date, current_day,
                quota_period=club_obj.quota_period
            )

            # Generate and send daily reports
            status_summary = await self.quota_calculator.get_member_status_summary(
                club_obj.club_id, current_date, quota_period=club_obj.quota_period
            )

            effective_quota = await QuotaRequirement.get_quota_for_date(club_obj.club_id, current_date)
            daily_reports = await self.report_generator.create_daily_report(
                club_obj.club_name, effective_quota, status_summary, current_date,
                rank_data=rank_data, quota_period=club_obj.quota_period
            )

            for embed, files in daily_reports:
                await report_channel.send(embed=embed, files=files if files else None)

            # Auto-update monthly info board
            await self._update_monthly_info_board(club_obj, current_date)

            await interaction.followup.send(
                f"✅ Check complete for {club}: {updated_members} members updated, {new_members} new members"
            )

        except ScrapeLockUnavailableError:
            await interaction.followup.send(
                "⚠️ A sync or quota check is already running for this club."
            )
        except Exception as e:
            logger.error(f"Error in force_check: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")
        finally:
            if scrape_context and scrape_context.lock_acquired:
                try:
                    await scrape_context.__aexit__(None, None, None)
                except Exception:
                    logger.exception(
                        "Failed to release force-check lock for club %s",
                        club,
                    )

    @app_commands.command(name="add_member", description="Manually add a new member")
    @app_commands.checks.has_permissions(administrator=True)
    async def add_member(self, interaction: discord.Interaction,
                         trainer_name: str, join_date: str, club: str, trainer_id: str = None):
        """Manually add a member (format: YYYY-MM-DD)"""
        await interaction.response.defer()

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(f"❌ Club '{club}' is not registered in this server.")
                return

            join_date_obj = datetime.strptime(join_date, "%Y-%m-%d").date()

            if trainer_id:
                existing = await Member.get_by_trainer_id(club_obj.club_id, trainer_id)
            else:
                existing = await Member.get_by_name(club_obj.club_id, trainer_name)

            if existing:
                await interaction.followup.send(f"❌ Member '{trainer_name}' already exists in {club}")
                return

            await Member.create(club_obj.club_id, trainer_name, join_date_obj, trainer_id)

            await interaction.followup.send(
                f"✅ Added member to {club}: {trainer_name} (joined {join_date}, ID: {trainer_id or 'N/A'})"
            )

        except ValueError:
            await interaction.followup.send("❌ Invalid date format. Use YYYY-MM-DD")
        except Exception as e:
            logger.error(f"Error in add_member: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")

    @app_commands.command(name="deactivate_member", description="Manually deactivate a member")
    @app_commands.checks.has_permissions(administrator=True)
    async def deactivate_member(self, interaction: discord.Interaction, trainer_name: str, club: str):
        """Manually deactivate a member"""
        await interaction.response.defer()

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(f"❌ Club '{club}' is not registered in this server.")
                return

            member = await Member.get_by_name(club_obj.club_id, trainer_name)

            if not member:
                await interaction.followup.send(f"❌ Member '{trainer_name}' not found in {club}")
                return

            if not member.is_active:
                await interaction.followup.send(f"ℹ️ {trainer_name} is already inactive")
                return

            await member.deactivate(manual=True)

            embed = discord.Embed(
                title=f"✅ Member Manually Deactivated - {club}",
                description=f"**{trainer_name}** has been deactivated and will not be auto-reactivated.",
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(
                name="ℹ️ Note",
                value="This member will stay inactive even if they appear in scraped data. "
                      "Use `/activate_member` to reactivate them.",
                inline=False
            )

            await interaction.followup.send(embed=embed)
            logger.info(f"Manually deactivated member: {trainer_name} in {club} by {interaction.user}")

        except Exception as e:
            logger.error(f"Error in deactivate_member: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")

    @app_commands.command(name="activate_member", description="Reactivate a member")
    @app_commands.checks.has_permissions(administrator=True)
    async def activate_member(self, interaction: discord.Interaction, trainer_name: str, club: str):
        """Reactivate a deactivated member"""
        await interaction.response.defer()

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(f"❌ Club '{club}' is not registered in this server.")
                return

            member = await Member.get_by_name(club_obj.club_id, trainer_name)

            if not member:
                await interaction.followup.send(f"❌ Member '{trainer_name}' not found in {club}")
                return

            if member.is_active:
                await interaction.followup.send(f"ℹ️ {trainer_name} is already active")
                return

            await member.activate()

            embed = discord.Embed(
                title=f"✅ Member Reactivated - {club}",
                description=f"**{trainer_name}** has been reactivated.",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )

            await interaction.followup.send(embed=embed)
            logger.info(f"Reactivated member: {trainer_name} in {club} by {interaction.user}")

        except Exception as e:
            logger.error(f"Error in activate_member: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")

    @staticmethod
    async def _recalculate_days_behind(club_id, current_date):
        """Bulk-recalculate current-month streaks and return the row count."""
        from config.database import db as _db

        rows = await _db.fetch(
            """
            SELECT id, member_id, date, deficit_surplus
            FROM quota_history
            WHERE club_id = $1
              AND date >= date_trunc('month', $2::date)::date
              AND date <= $2
            ORDER BY member_id, date ASC
            """,
            club_id,
            current_date,
        )
        streaks = {}
        updates = []
        for row in rows:
            previous_date, consecutive = streaks.get(row['member_id'], (None, 0))
            is_adjacent = (
                previous_date is not None
                and row['date'] == previous_date + timedelta(days=1)
            )
            consecutive = (
                consecutive + 1 if row['deficit_surplus'] < 0 and is_adjacent
                else 1 if row['deficit_surplus'] < 0
                else 0
            )
            streaks[row['member_id']] = (row['date'], consecutive)
            updates.append((consecutive, row['id']))

        if updates:
            async with _db.transaction() as conn:
                await conn.executemany(
                    "UPDATE quota_history SET days_behind = $1 WHERE id = $2",
                    updates,
                )
        return len(updates)

    @app_commands.command(name="recalculate", description="Recalculate days-behind counts from current history")
    @app_commands.checks.has_permissions(administrator=True)
    async def recalculate(self, interaction: discord.Interaction, club: str):
        """Recalculate days_behind based on existing quota history"""
        await interaction.response.defer()

        try:
            club_obj = await Club.get_by_name(club, interaction.guild_id)
            if not club_obj:
                await interaction.followup.send(f"❌ Club '{club}' not found")
                return

            if not club_obj.belongs_to_guild(interaction.guild_id):
                await interaction.followup.send(f"❌ Club '{club}' is not registered in this server.")
                return

            club_tz = pytz.timezone(club_obj.timezone)
            current_date = datetime.now(club_tz).date()

            await interaction.followup.send(f"🔄 Recalculating for {club}...")

            async with ScrapeContext(
                club_obj.club_id, f"recalculate_{club_obj.club_name}"
            ):
                updated_entries = await self._recalculate_days_behind(
                    club_obj.club_id, current_date
                )

            embed = discord.Embed(
                title=f"✅ Recalculation Complete - {club}",
                description=(
                    f"**History entries updated:** {updated_entries}\n\n"
                    "Days-behind counts now reflect current data.\n"
                    "Run `/force_check` to generate a fresh report."
                ),
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"Recalculated by {interaction.user}")
            await interaction.followup.send(embed=embed)
            logger.info(f"Recalculation performed for {club} by {interaction.user}: "
                        f"{updated_entries} entries updated")

        except ScrapeLockUnavailableError:
            await interaction.followup.send(
                "⚠️ A sync or quota check is already running for this club."
            )
        except Exception as e:
            logger.error(f"Error in recalculate: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred. Please try again later.")

    # Register autocomplete for all club arguments
    set_quota.autocomplete('club')(club_autocomplete)
    update_monthly_info.autocomplete('club')(club_autocomplete)
    quota_history.autocomplete('club')(club_autocomplete)
    delete_quota.autocomplete('club')(club_autocomplete)
    force_check.autocomplete('club')(club_autocomplete)
    add_member.autocomplete('club')(club_autocomplete)
    deactivate_member.autocomplete('club')(club_autocomplete)
    activate_member.autocomplete('club')(club_autocomplete)
    recalculate.autocomplete('club')(club_autocomplete)


async def setup(bot):
    """Setup function for loading the cog"""
    await bot.add_cog(AdminCommands(bot))
