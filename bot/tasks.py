"""
Scheduled tasks for the Discord bot
"""
import discord
from discord.ext import tasks
import json as json_mod
import os
import re
import tempfile
from datetime import datetime
import logging
import pytz
import asyncio

from models import Club, ClubRankHistory, QuotaRequirement
from scrapers import (
    UmaMoeAPIScraper, DataNotAvailableError,
    check_and_save as check_and_save_official_events,
)
from services.notification_service import NotificationService
from services.quota_calculator import QuotaCalculator
from services.report_generator import ReportGenerator
from services.scrape_lock_manager import ScrapeContext
from services.leaderboard_report_service import LeaderboardReportService
from config.settings import EVENTS_JSON_PATH

logger = logging.getLogger(__name__)

LONG_RETRY_INTERVAL_SECONDS = 10 * 60
LONG_RETRY_WINDOW_SECONDS = 6 * 60 * 60


class BotTasks:
    """Manages scheduled tasks for the bot"""

    def __init__(self, bot):
        self.bot = bot
        self.quota_calculator = QuotaCalculator()
        self.report_generator = ReportGenerator()
        self.notification_service = NotificationService(bot)

        # Track last run per club per day (club_id_YYYY-MM-DD -> True)
        self.last_runs = {}
        self._running_club_ids = set()
        self._scheduled_tasks = set()
        # Serializes event scraping with notification reads/writes of events.json.
        self._events_lock = asyncio.Lock()
        # Prevents duplicate Discord posts if a successful send is followed by
        # a transient failure while persisting the deduplication state.
        self._sent_event_notifications = set()

        logger.info("Multi-club tasks configured - will check all clubs hourly")

    def start_tasks(self):
        """Start all scheduled tasks"""
        self.hourly_check.start()
        self.hourly_event_notifications.start()
        self.daily_official_events_check.start()
        logger.info(
            "Scheduled tasks started (hourly reports, hourly event notifications, "
            "daily official event scraping)"
        )

    async def stop_tasks(self):
        """Cancel scheduled work and wait until it has finished unwinding."""
        loop_tasks = [
            loop.get_task()
            for loop in (
                self.hourly_check,
                self.hourly_event_notifications,
                self.daily_official_events_check,
            )
            if loop.get_task() is not None
        ]
        self.hourly_check.cancel()
        self.hourly_event_notifications.cancel()
        self.daily_official_events_check.cancel()
        scheduled_tasks = tuple(self._scheduled_tasks)
        for task in scheduled_tasks:
            task.cancel()
        current_task = asyncio.current_task()
        pending = {
            task for task in (*loop_tasks, *scheduled_tasks)
            if task is not current_task and not task.done()
        }
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._scheduled_tasks.clear()
        self._running_club_ids.clear()
        logger.info("Scheduled tasks stopped")

    @tasks.loop(hours=1)
    async def hourly_check(self):
        """Check every hour if it's time to run any club's daily report"""
        logger.info("=" * 80)
        logger.info("Hourly check - scanning all clubs...")
        logger.info("=" * 80)

        try:
            clubs = await Club.get_all_active()
            logger.info(f"Found {len(clubs)} active club(s)")

            active_run_keys = {
                f"{club.club_id}_{datetime.now(pytz.timezone(club.timezone)).date()}"
                for club in clubs
            }
            self.last_runs = {
                key: value
                for key, value in self.last_runs.items()
                if key in active_run_keys
            }

            for club in clubs:
                try:
                    club_tz = pytz.timezone(club.timezone)
                    now_in_club_tz = datetime.now(club_tz)
                    current_date = now_in_club_tz.date()

                    target_hour = club.scrape_time.hour
                    target_minute = club.scrape_time.minute

                    if (now_in_club_tz.hour == target_hour and
                            now_in_club_tz.minute >= target_minute):

                        run_key = f"{club.club_id}_{current_date}"
                        if self.last_runs.get(run_key):
                            logger.debug(f"{club.club_name}: Already ran today ({current_date})")
                            continue

                        if club.club_id in self._running_club_ids:
                            logger.debug(f"{club.club_name}: Daily check already running")
                            continue

                        logger.info(f"⏰ Time to check {club.club_name} ({now_in_club_tz.strftime('%H:%M')} {club.timezone})")

                        self._running_club_ids.add(club.club_id)
                        task = asyncio.create_task(
                            self._run_scheduled_daily_check(club, current_date)
                        )
                        self._scheduled_tasks.add(task)
                        task.add_done_callback(self._scheduled_task_done)
                    else:
                        logger.debug(
                            f"{club.club_name}: Not time yet "
                            f"(current: {now_in_club_tz.strftime('%H:%M')}, "
                            f"target: {target_hour:02d}:{target_minute:02d} {club.timezone})"
                        )

                except Exception as e:
                    logger.error(f"Error checking club {club.club_name}: {e}", exc_info=True)
                    continue

        except Exception as e:
            logger.error(f"Error in hourly_check: {e}", exc_info=True)

    def _scheduled_task_done(self, task: asyncio.Task):
        """Retain background tasks through completion and consume failures."""
        self._scheduled_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Scheduled daily check crashed", exc_info=error)

    async def _run_scheduled_daily_check(self, club: Club, run_date=None):
        """Run one scheduled check and always clear its in-process guard."""
        try:
            await self.daily_check_for_club(club, run_date=run_date)
        finally:
            self._running_club_ids.discard(club.club_id)

    @staticmethod
    async def _send_embeds(channel, embeds):
        """Send each embed returned by a report service in order."""
        for embed in embeds:
            await channel.send(embed=embed)

    async def daily_check_for_club(self, club: Club, run_date=None):
        """Daily quota check and report generation for a specific club"""
        logger.info("=" * 80)
        logger.info(f"Starting daily check for {club.club_name}")
        logger.info("=" * 80)

        if run_date is None:
            run_date = datetime.now(pytz.timezone(club.timezone)).date()

        try:
            async with ScrapeContext(club.club_id, f"tasks_{club.club_name}"):
                report_channel = self.bot.get_channel(club.report_channel_id)

                if not report_channel:
                    logger.error(f"Report channel {club.report_channel_id} not found for {club.club_name}")
                    return

                club_tz = pytz.timezone(club.timezone)
                current_datetime = datetime.now(club_tz)
                current_date = current_datetime.date()

                max_retries = 3
                retry_delay = 10

                scraped_data = None
                current_day = None
                last_error = None

                # STEP 1: Initialize Uma.moe API scraper with validation
                if not club.circle_id:
                    logger.error(f"No circle_id configured for {club.club_name}")
                    error_embed = self.report_generator.create_error_report(
                        club.club_name,
                        f"⚠️ **Missing Circle ID for {club.club_name}**\n\n"
                        f"No circle_id has been set for this club.\n\n"
                        f"**To fix this:**\n"
                        f"Use `/edit_club club:{club.club_name} circle_id:<numeric_id>`\n\n"
                        f"**How to find your Circle ID:**\n"
                        f"1. Go to https://uma.moe/circles/\n"
                        f"2. Search for **{club.club_name}**\n"
                        f"3. Copy the number from the URL"
                    )
                    await report_channel.send(embed=error_embed)
                    return

                if not club.is_circle_id_valid():
                    logger.error(f"Invalid circle_id format for {club.club_name}: '{club.circle_id}' (must be numeric)")
                    error_embed = self.report_generator.create_error_report(
                        club.club_name,
                        club.get_circle_id_help_message()
                    )
                    await report_channel.send(embed=error_embed)
                    return

                scraper = UmaMoeAPIScraper(club.circle_id)
                logger.info(f"Using Uma.moe API scraper for {club.club_name} (circle_id: {club.circle_id})")

                # STEP 2: Scrape with retries
                # First, do 3 fast retries with backoff (catches transient network errors)
                for attempt in range(1, max_retries + 1):
                    try:
                        logger.info(f"🔍 Scraping {club.club_name} (attempt {attempt}/{max_retries})...")
                        scraped_data = await scraper.scrape()
                        current_day = scraper.get_current_day()

                        if scraped_data:
                            logger.info(f"✅ Scraping successful for {club.club_name} ({len(scraped_data)} members found)")
                            break
                        else:
                            raise ValueError("Scraper returned empty data")

                    except DataNotAvailableError as e:
                        # Data not available yet — this is expected, will retry in long loop below
                        last_error = e
                        logger.warning(f"📡 Data not available yet for {club.club_name} (attempt {attempt}/{max_retries}): {e}")
                        if attempt < max_retries:
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 2

                    except Exception as e:
                        last_error = e
                        logger.error(f"❌ Scraping failed for {club.club_name} (attempt {attempt}/{max_retries}): {e}")

                        if attempt < max_retries:
                            logger.info(f"Retrying in {retry_delay} seconds...")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 2

                # STEP 3: If all fast retries failed with DataNotAvailableError, enter long retry loop
                if not scraped_data and isinstance(last_error, DataNotAvailableError):
                    loop = asyncio.get_running_loop()
                    retry_deadline = loop.time() + LONG_RETRY_WINDOW_SECONDS
                    logger.warning(
                        f"⏳ Data not yet available for {club.club_name} after {max_retries} fast retries. "
                        f"Entering a bounded 10-minute retry loop..."
                    )
                    while not scraped_data:
                        remaining = retry_deadline - loop.time()
                        local_date = datetime.now(club_tz).date()
                        if remaining <= 0 or local_date != run_date:
                            logger.warning(
                                "Stopping delayed retries for %s after the retry window/date ended",
                                club.club_name,
                            )
                            break
                        await asyncio.sleep(min(LONG_RETRY_INTERVAL_SECONDS, remaining))
                        if loop.time() >= retry_deadline:
                            break
                        try:
                            logger.info(f"🔍 Retrying scrape for {club.club_name} (10-min cycle)...")
                            scraped_data = await scraper.scrape()
                            current_day = scraper.get_current_day()
                            if scraped_data:
                                logger.info(f"✅ Scraping successful for {club.club_name} ({len(scraped_data)} members found)")
                                break
                            else:
                                raise ValueError("Scraper returned empty data")
                        except DataNotAvailableError as e:
                            logger.warning(f"📡 Data still not available for {club.club_name}. Waiting another 10 minutes...")
                            last_error = e
                        except Exception as e:
                            logger.error(f"❌ Scrape failed in 10-min retry loop for {club.club_name}: {e}")
                            last_error = e
                            # For non-DataNotAvailableError, exit the loop and report failure
                            break

                # STEP 3b: Handle scraping failure (all retries exhausted)
                if not scraped_data:
                    error_msg = (
                        f"Failed to scrape data after multiple retries.\n\n"
                        f"**Last error:** {str(last_error)}\n\n"
                        f"**Most likely cause:**\n"
                        f"• Data for current day not yet available on Uma.moe\n"
                        f"• Uma.moe typically updates around 15:10 UTC daily\n\n"
                        f"**Other possible causes:**\n"
                        f"• Uma.moe API is down or unreachable\n"
                        f"• Network timeout\n"
                        f"• Invalid circle_id\n\n"
                        f"**What to do:**\n"
                        f"• Wait a few hours and try `/force_check` again\n"
                        f"• Check uma.moe directly to verify data availability"
                    )
                    logger.error(f"Scraping failed for {club.club_name}: {error_msg}")

                    error_embed = self.report_generator.create_error_report(club.club_name, error_msg)
                    await report_channel.send(embed=error_embed)
                    await report_channel.send(
                        f"⚠️ **Manual intervention required for {club.club_name}!**\n"
                        f"Administrators can run `/force_check club:{club.club_name}` to retry manually."
                    )
                    return

                # Use the scraper's data date in case of previous-month fallback (e.g. Day 1)
                data_date = scraper.get_data_date()
                if data_date:
                    current_date = data_date
                    logger.info(f"Using scraper's data date: {current_date} (previous-month fallback)")

                # Extract and persist club rank data
                monthly_rank = scraper.get_monthly_rank()
                last_month_rank = scraper.get_last_month_rank()
                yesterday_rank = scraper.get_yesterday_rank()
                fans_to_next_tier = scraper.get_fans_to_next_tier()
                fans_to_lower_tier = scraper.get_fans_to_lower_tier()

                rank_data = None
                if monthly_rank is not None or (
                    fans_to_next_tier is not None
                    and fans_to_lower_tier is not None
                ):
                    rank_data = {
                        'monthly_rank': monthly_rank,
                        'last_month_rank': last_month_rank,
                        'yesterday_rank': yesterday_rank,
                        'fans_to_next_tier': fans_to_next_tier,
                        'fans_to_lower_tier': fans_to_lower_tier,
                    }

                if monthly_rank is not None:
                    try:
                        await ClubRankHistory.save(club.club_id, current_date, monthly_rank, monthly_rank)
                    except Exception as e:
                        logger.error(f"Failed to save rank data for {club.club_name}: {e}", exc_info=True)

                    logger.info(
                        f"Rank data for {club.club_name}: "
                        f"monthly={monthly_rank}, yesterday={yesterday_rank}, "
                        f"last_month={last_month_rank}"
                    )
                if fans_to_next_tier is not None:
                    logger.info(
                        f"Tier progress for {club.club_name}: "
                        f"fans_to_next_tier={fans_to_next_tier:,}, "
                        f"fans_to_lower_tier={fans_to_lower_tier:,}"
                    )

                # STEP 4: Process the scraped data
                try:
                    logger.info(f"⚙️ Processing scraped data for {club.club_name}...")
                    new_members, updated_members = await self.quota_calculator.process_scraped_data(
                        club.club_id, scraped_data, current_date, current_day,
                        quota_period=club.quota_period
                    )
                    logger.info(f"✅ Data processed for {club.club_name}: {updated_members} members updated, {new_members} new members")

                except Exception as e:
                    logger.error(f"❌ Error processing scraped data for {club.club_name}: {e}", exc_info=True)
                    error_embed = self.report_generator.create_error_report(
                        club.club_name,
                        "Data processing failed. Please try again later."
                    )
                    await report_channel.send(embed=error_embed)
                    return

                # STEP 5: Send DM notifications to linked users
                try:
                    # Send deficit notifications
                    status_summary = await self.quota_calculator.get_member_status_summary(
                        club.club_id, current_date, quota_period=club.quota_period
                    )
                    if status_summary['behind']:
                        logger.info(f"📨 Sending deficit notifications for {club.club_name}...")
                        await self.notification_service.send_deficit_notifications(club.club_name, status_summary['behind'], current_date=current_date)

                except Exception as e:
                    logger.error(f"❌ Error sending DM notifications for {club.club_name}: {e}", exc_info=True)

                # STEP 7: Generate and send reports
                try:
                    logger.info(f"📊 Generating daily report for {club.club_name}...")
                    status_summary = await self.quota_calculator.get_member_status_summary(
                        club.club_id, current_date, quota_period=club.quota_period
                    )

                    effective_quota = await QuotaRequirement.get_quota_for_date(club.club_id, current_date)
                    daily_reports = await self.report_generator.create_daily_report(
                        club.club_name, effective_quota, status_summary, current_date,
                        rank_data=rank_data, quota_period=club.quota_period
                    )

                    for embed, files in daily_reports:
                        await report_channel.send(embed=embed, files=files if files else None)

                    logger.info(f"✅ Daily report sent for {club.club_name} ({len(daily_reports)} embed(s))")

                except Exception as e:
                    logger.error(f"❌ Error generating/sending daily report for {club.club_name}: {e}", exc_info=True)
                    error_embed = self.report_generator.create_error_report(
                        club.club_name,
                        "Failed to generate the daily report. Please try again later."
                    )
                    await report_channel.send(embed=error_embed)

                # STEP 8.5: Generate and send leaderboard news report (after daily scrape)
                try:
                    if club.leaderboard_channel_id:
                        leaderboard_channel = self.bot.get_channel(club.leaderboard_channel_id)
                        if leaderboard_channel:
                            year, month = current_date.year, current_date.month

                            # Pass tier progress data from the scraper if available
                            tier_kwargs = {}
                            if rank_data:
                                tier_kwargs['fans_to_next_tier'] = rank_data.get('fans_to_next_tier')
                                tier_kwargs['fans_to_lower_tier'] = rank_data.get('fans_to_lower_tier')

                            embeds = await LeaderboardReportService.generate_leaderboard_report(
                                club.club_id, club.club_name, year, month,
                                **tier_kwargs,
                            )
                            await self._send_embeds(leaderboard_channel, embeds)
                            await LeaderboardReportService.persist_delivered_predictions(embeds)
                            logger.info(
                                f"Leaderboard report sent for {club.club_name} "
                                f"({len(embeds)} embed(s))"
                            )
                        else:
                            logger.error(f"Leaderboard channel {club.leaderboard_channel_id} not found for {club.club_name}")
                    else:
                        logger.debug(f"Leaderboard channel not configured for {club.club_name}, skipping leaderboard report")

                except ValueError as e:
                    logger.warning(f"Leaderboard report data error for {club.club_name}: {e}")
                except Exception as e:
                    logger.error(f"Error generating leaderboard report for {club.club_name}: {e}", exc_info=True)

                # Mark this club as successfully completed for today
                run_key = f"{club.club_id}_{run_date}"
                self.last_runs[run_key] = True
                logger.info(f"✅ Marked {club.club_name} as completed for {run_date}")

                # STEP 9: Final summary
                logger.info("=" * 80)
                logger.info(f"✅ Daily check complete for {club.club_name}!")
                logger.info(f"   • Members updated: {updated_members}")
                logger.info(f"   • New members: {new_members}")
                logger.info("=" * 80)

        except Exception as e:
            logger.error(f"Fatal error in daily check for {club.club_name}: {e}", exc_info=True)

            try:
                report_channel = self.bot.get_channel(club.report_channel_id)
                if report_channel:
                    error_embed = self.report_generator.create_error_report(
                        club.club_name,
                        "The daily check failed unexpectedly. Please try again later."
                    )
                    await report_channel.send(embed=error_embed)
            except Exception:
                pass

    @hourly_check.before_loop
    async def before_hourly_check(self):
        """Wait for bot to be ready before starting tasks"""
        await self.bot.wait_until_ready()
        logger.info("Bot ready, multi-club hourly check loop starting")

    # ── Event Notification Helpers ─────────────────────────────────────

    @staticmethod
    def _event_display_name(title: str) -> str:
        """Remove announcement status text from an event title used in alerts."""
        title = re.sub(
            r"\s+(?:is coming soon|is here|has ended|out now)!?$",
            "",
            title.strip(),
            flags=re.IGNORECASE,
        )
        return re.sub(r"^The (?:story|race) event\s+", "", title, flags=re.IGNORECASE)

    async def _notify_events_for_club(self, club: Club, event: dict, notif_type: str) -> bool:
        """
        Send an event notification embed to a club's events channel.
        Uses the 'notified_clubs' list in the event dict for dedup.
        
        Returns True if notification was sent, False if skipped.
        """
        if not club.events_channel_id:
            return False

        events_channel = self.bot.get_channel(club.events_channel_id)
        if not events_channel:
            logger.error(f"Events channel {club.events_channel_id} not found for {club.club_name}")
            return False

        # Check notified_clubs list in the JSON event data
        # Uses "{club_id}_{notif_type}" format to distinguish starting vs ending notifications
        notified_clubs = event.get("notified_clubs", [])
        dedup_key = f"{club.club_id}_{notif_type}"
        if dedup_key in notified_clubs:
            logger.debug(f"Club {club.club_id} already notified for '{event.get('title', '')[:60]}' ({notif_type})")
            return False

        title = event.get("title", "Unknown event")
        display_name = self._event_display_name(title)
        event_url = event.get("url", "")
        banner_image = event.get("banner_image")
        event_key = event.get("event_key") or display_name.lower()
        notification_key = f"{event_key}:{club.club_id}:{notif_type}"
        if notification_key in self._sent_event_notifications:
            logger.debug(f"Notification already sent in this run for '{event_key}' ({notif_type})")
            return False

        if notif_type == "starting":
            try:
                start_dt = datetime.fromisoformat(event["start_time"])
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=pytz.UTC)
            except (ValueError, TypeError, KeyError):
                return False

            embed = discord.Embed(
                title="⏰ Event Starting Soon",
                color=discord.Color.blue(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="📰 Event", value=display_name, inline=False)
            embed.add_field(
                name="📅 Starts",
                value=f"<t:{int(start_dt.timestamp())}:F> (<t:{int(start_dt.timestamp())}:R>)",
                inline=False
            )
        else:  # ending
            try:
                end_dt = datetime.fromisoformat(event["end_time"])
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=pytz.UTC)
            except (ValueError, TypeError, KeyError):
                return False

            embed = discord.Embed(
                title="⏳ Event Ending in 1 Day",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="📰 Event", value=display_name, inline=False)
            embed.add_field(
                name="📅 Ends",
                value=f"<t:{int(end_dt.timestamp())}:F> (<t:{int(end_dt.timestamp())}:R>)",
                inline=False
            )

        if event_url:
            embed.add_field(name="🔗 Link", value=event_url, inline=False)
        if banner_image:
            embed.set_image(url=banner_image)
        embed.set_footer(text="Source: Umamusume Official News")

        try:
            await events_channel.send(embed=embed)
            # Mark as notified and save back to JSON
            # Uses "{club_id}_{notif_type}" format to distinguish starting vs ending notifications
            notified_clubs.append(dedup_key)
            event["notified_clubs"] = notified_clubs
            self._sent_event_notifications.add(notification_key)
            if not await asyncio.to_thread(self._save_events_json):
                logger.warning(
                    f"Sent {notif_type} notification for '{title[:60]}', "
                    "but failed to persist its deduplication state"
                )
            logger.info(f"Sent {notif_type} notification to {club.club_name}: '{title[:60]}'")
            return True
        except Exception as e:
            logger.error(f"Error sending {notif_type} notification to {club.club_name}: {e}", exc_info=True)
            return False

    def _save_events_json(self) -> bool:
        """Save the current in-memory events data back to the JSON file."""
        try:
            if not hasattr(self, '_events_data') or not self._events_data:
                return False
            directory = os.path.dirname(EVENTS_JSON_PATH) or "."
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=directory,
                    prefix=".events-",
                    suffix=".tmp",
                    delete=False,
                ) as f:
                    temp_path = f.name
                    json_mod.dump(self._events_data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, EVENTS_JSON_PATH)
                temp_path = None
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
            return True
        except Exception as e:
            logger.error(f"Failed to save events JSON: {e}")
            return False

    @staticmethod
    def _load_events_json():
        """Read the event cache off the event-loop thread."""
        with open(EVENTS_JSON_PATH, "r", encoding="utf-8") as f:
            return json_mod.load(f)

    # ── Event Notifications ────────────────────────────────────────────

    async def event_notifications(self):
        """Serialize event notification checks with official-event scraping."""
        async with self._events_lock:
            await self._event_notifications_locked()

    async def _event_notifications_locked(self):
        """
        Check events.json for events starting or ending within 1.5 days
        and send notifications to each club's events channel before those events occur.

        Uses 'notified_clubs' per-event list (persisted in JSON) for dedup.
        Checks upcoming events within the next 36 hours for starts and the next
        24 hours for ending alerts.
        """
        logger.info("=" * 80)
        logger.info("Event notifications - checking events.json...")
        logger.info("=" * 80)

        try:
            if not os.path.exists(EVENTS_JSON_PATH):
                logger.warning(f"Events file not found: {EVENTS_JSON_PATH}")
                return

            self._events_data = await asyncio.to_thread(self._load_events_json)

            events = self._events_data.get("events", [])
            if not events:
                logger.info("No events found in events.json")
                return

            now = datetime.now(pytz.UTC)
            clubs = await Club.get_all_active()

            for event in events:
                title = event.get("title", "Unknown event")

                # Check if this event should be notified as "starting"
                start_str = event.get("start_time")
                if start_str:
                    try:
                        start_dt = datetime.fromisoformat(start_str)
                        if start_dt.tzinfo is None:
                            start_dt = start_dt.replace(tzinfo=pytz.UTC)
                        remaining = (start_dt - now).total_seconds()
                        # Within 36 hours in the future
                        if 0 <= remaining <= 129600:
                            for club in clubs:
                                await self._notify_events_for_club(club, event, "starting")
                    except (ValueError, TypeError):
                        logger.debug(f"Could not parse start_time for: {title[:40]}")

                # Check if this event should be notified as "ending"
                end_str = event.get("end_time")
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=pytz.UTC)
                        remaining = (end_dt - now).total_seconds()
                        # Within 24 hours in the future; never alert after an event ends.
                        if 0 <= remaining <= 86400:
                            for club in clubs:
                                await self._notify_events_for_club(club, event, "ending")
                    except (ValueError, TypeError):
                        logger.debug(f"Could not parse end_time for: {title[:40]}")

        except Exception as e:
            logger.error(f"Error in event_notifications: {e}", exc_info=True)

    # ── Daily Official Events Scraper Task ─────────────────────────────

    @tasks.loop(hours=1)
    async def hourly_event_notifications(self):
        """Check saved events hourly and post due notifications."""
        await self.event_notifications()

    @hourly_event_notifications.before_loop
    async def before_hourly_event_notifications(self):
        """Wait for Discord readiness before checking event notifications."""
        await self.bot.wait_until_ready()
        logger.info("Bot ready, hourly event notification loop starting")

    @tasks.loop(hours=24)
    async def daily_official_events_check(self):
        """
        Scrape official umamusume.com news for upcoming in-game events
        and save to JSON if new articles are detected.

        Runs once per day, checks for new event articles by comparing
        event titles against the previously-saved JSON file.
        
        Event notifications run independently every hour against the saved data.
        """
        logger.info("=" * 80)
        logger.info("Daily official events check - scraping news page...")
        logger.info("=" * 80)

        try:
            logger.info(f"Checking for new official events → {EVENTS_JSON_PATH}")
            async with self._events_lock:
                changed = await check_and_save_official_events(EVENTS_JSON_PATH)

            if changed:
                logger.info("✅ New official events detected and saved to JSON")
            else:
                logger.info("ℹ️ No new official events found (JSON unchanged)")
        except Exception as e:
            logger.error(f"Error in daily_official_events_check: {e}", exc_info=True)
            return

    @daily_official_events_check.before_loop
    async def before_daily_official_events_check(self):
        """Wait for bot to be ready before starting the events check task"""
        await self.bot.wait_until_ready()
        logger.info("Bot ready, daily official events check loop starting")

