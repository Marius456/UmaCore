"""
Discord report generation service
"""
import io
import asyncio
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple
import discord
import logging
from config.settings import COLOR_BEHIND, COLOR_INFO
from services.member_performance_card import card_html, load_member_performance
from services.quota_schedule import QuotaSchedule

logger = logging.getLogger(__name__)

# Each embed in the report can carry optional file attachments (for table images)
ReportEmbed = Tuple[discord.Embed, List[discord.File]]

# Shared Playwright browser for rendering table images (lazy-initialised)
_playwright_browser = None
_playwright_context = None
_playwright = None
_playwright_lock = asyncio.Lock()


async def _ensure_playwright_browser_async():
    """
    Ensure a healthy shared Playwright browser instance exists.
    Reuse a connected browser. Renderers retry failed page operations using a
    fresh browser. Avoid creating and disposing a separate context for each probe.
    """
    global _playwright_browser, _playwright_context, _playwright

    # browser.new_page() owns a temporary context. Closing that context for every
    # health check can destabilize single-process Chromium during repeated renders.
    if _playwright_browser is not None:
        if _playwright_browser.is_connected():
            return _playwright_browser
        logger.warning("Playwright browser disconnected, re-launching...")
        await _close_playwright_browser_unlocked_async()

    from playwright.async_api import async_playwright

    if _playwright is None:
        _playwright = await async_playwright().start()

    _playwright_browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
        timeout=30000,
    )
    # Create a persistent context to avoid default-context corruption issues
    _playwright_context = await _playwright_browser.new_context()
    logger.info("Started shared Playwright browser for table image rendering")
    return _playwright_browser


async def _get_browser_context_async():
    """
    Get or create a persistent browser context from the shared browser.
    The context is reused across renders for better performance.
    """
    global _playwright_context
    browser = await _ensure_playwright_browser_async()
    if _playwright_context is None or not _playwright_context.browser:
        _playwright_context = await browser.new_context()
    return _playwright_context


async def _close_playwright_browser_async():
    """Close the shared Playwright browser (call on bot shutdown)."""
    async with _playwright_lock:
        await _close_playwright_browser_unlocked_async()


async def _close_playwright_browser_unlocked_async():
    """Close Playwright while the caller owns the lifecycle lock."""
    global _playwright_browser, _playwright_context, _playwright
    if _playwright_context:
        try:
            await _playwright_context.close()
        except Exception:
            pass
        _playwright_context = None
    if _playwright_browser:
        try:
            await _playwright_browser.close()
        except Exception:
            pass
        _playwright_browser = None
    if _playwright:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None
    logger.info("Closed shared Playwright browser for table image rendering")


class ReportGenerator:
    """Generates Discord embed reports"""

    @staticmethod
    def format_number(num: int) -> str:
        """Format number with commas"""
        return f"{num:,}"

    @staticmethod
    def format_fans_short(num: int) -> str:
        """Format fan count in short form (e.g., 1.5M)"""
        if abs(num) >= 1_000_000:
            return f"{num / 1_000_000:.1f}M"
        elif abs(num) >= 1_000:
            return f"{num / 1_000:.1f}K"
        return str(num)

    async def _generate_table_image(self, html: str, filename: str) -> discord.File:
        """Render a self-contained member-performance PNG for Discord."""

        async def render_once() -> bytes:
            context = await _get_browser_context_async()
            page = await context.new_page()
            try:
                await page.set_content(html, wait_until="load", timeout=15000)
                return await page.locator("#card").screenshot(type="png", timeout=15000)
            finally:
                await page.close()

        # A retry must not close a browser another card is currently using.
        async with _playwright_lock:
            try:
                screenshot_bytes = await render_once()
            except Exception:
                logger.warning("Playwright render failed, retrying once", exc_info=True)
                await _close_playwright_browser_unlocked_async()
                screenshot_bytes = await render_once()

        return discord.File(io.BytesIO(screenshot_bytes), filename=filename)

    async def create_daily_report(self, club_name: str, daily_quota: int, status_summary: Dict,
                                   report_date: date,
                                   rank_data: Optional[Dict] = None,
                                   quota_period: str = 'daily') -> List[ReportEmbed]:
        """
        Create the main daily report embeds.

        Returns a list of (embed, [files]) tuples.
        """
        report_items = []

        period_info = status_summary.get('period_info')

        # Build description based on quota period
        period_labels = {'daily': 'day', 'weekly': 'week', 'biweekly': '2 weeks'}
        period_label = period_labels.get(quota_period, 'day')
        quota_line = f"**Quota:** {self.format_fans_short(daily_quota)} fans per {period_label}"

        next_date = report_date + timedelta(days=1)
        date_range = (f"{report_date.strftime('%B %d')}, 16:00 CEST"
                      f" ~ {next_date.strftime('%B %d')}, 16:00 CEST")
        description = f"**Date:** {date_range}\n{quota_line}"

        if period_info:
            p_num = period_info['period_number']
            p_total = period_info['total_periods']
            p_start = period_info['period_start'].strftime('%b %d')
            p_end = period_info['period_end'].strftime('%b %d')
            description += f"\n**Period:** {period_info['quota_label'].capitalize()} {p_num} of {p_total} ({p_start} – {p_end})"

        # Summary embed
        summary_embed = discord.Embed(
            title=f"📊 Daily Quota Report - {club_name}",
            description=description,
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow()
        )

        total = status_summary['total_members']
        on_track_count = len(status_summary['on_track'])
        behind_count = len(status_summary['behind'])

        # Build summary text
        summary_text = (
            f"**Total Members:** {total}\n"
            f"✅ On Track: {on_track_count}\n"
            f"⚠️ Behind: {behind_count}"
        )

        summary_embed.add_field(
            name="📈 Summary",
            value=summary_text,
            inline=False
        )

        if rank_data and rank_data.get('monthly_rank') is not None:
            rank_text = self._format_rank_section(rank_data)
            summary_embed.add_field(
                name="🏆 Club Rankings",
                value=rank_text,
                inline=False
            )

        summary_embed.set_footer(text=f"Umamusume Quota Tracker - {club_name}")
        report_items.append((summary_embed, []))

        members = status_summary["on_track"] + status_summary["behind"]
        if members:
            rows = await load_member_performance(members, report_date, quota_period, daily_quota)
            daily_goal = daily_quota / QuotaSchedule.PERIOD_DAYS.get(quota_period, 1)
            rows_per_card = 30
            for offset in range(0, len(rows), rows_per_card):
                page_number = offset // rows_per_card + 1
                filename = f"member_performance_{page_number}.png"
                html = card_html(
                    club_name, report_date, total, daily_goal, rank_data,
                    rows[offset:offset + rows_per_card], offset + 1,
                )
                file = await self._generate_table_image(html, filename)
                title = "Member Performance"
                if page_number > 1:
                    title += f" (continued {page_number})"
                embed = discord.Embed(title=title, color=0x96D000)
                embed.set_image(url=f"attachment://{filename}")
                report_items.append((embed, [file]))

        return report_items

    def _format_member_line(self, item: Dict, is_behind: bool, quota_period: str = 'daily') -> str:
        """Format a single member line for on-track or behind sections"""
        member = item['member']
        history = item['history']

        if quota_period != 'daily' and 'period_start_fans' in item and 'period_info' in item:
            period_info = item['period_info']
            period_fans = history.cumulative_fans - item['period_start_fans']
            period_quota = period_info['period_quota']
            period_label = period_info['quota_label']  # 'week' or 'biweek'

            if is_behind:
                deficit = abs(history.deficit_surplus)
                return (f"**{member.trainer_name}**: "
                        f"{self.format_fans_short(period_fans)}/{self.format_fans_short(period_quota)} this {period_label} "
                        f"(-{self.format_fans_short(deficit)} overall)")
            else:
                surplus = history.deficit_surplus
                return (f"**{member.trainer_name}**: "
                        f"{self.format_fans_short(period_fans)}/{self.format_fans_short(period_quota)} this {period_label} "
                        f"(+{self.format_fans_short(surplus)} overall)")

        # Default daily format
        if is_behind:
            deficit = abs(history.deficit_surplus)
            days_behind = history.days_behind
            days_text = f"{days_behind} day{'s' if days_behind != 1 else ''}"
            return f"**{member.trainer_name}**: -{self.format_fans_short(deficit)} ({days_text} behind)"
        else:
            surplus = history.deficit_surplus
            return f"**{member.trainer_name}**: +{self.format_fans_short(surplus)} ({self.format_number(history.cumulative_fans)} total)"

    def _split_into_sections(self, items: List[Dict], formatter, max_length: int = 1000) -> List[str]:
        """Split a list of items into text sections that fit within Discord's character limits"""
        sections = []
        current_section = []
        current_length = 0

        for item in items:
            line = formatter(item)
            line_length = len(line) + 1  # +1 for newline

            if current_length + line_length > max_length and current_section:
                sections.append("\n".join(current_section))
                current_section = [line]
                current_length = line_length
            else:
                current_section.append(line)
                current_length += line_length

        if current_section:
            sections.append("\n".join(current_section))

        return sections if sections else ["*No members*"]

    def _format_rank_section(self, rank_data: Dict) -> str:
        """Format the club/monthly rank lines for the summary embed."""
        monthly_rank = rank_data.get('monthly_rank')
        last_month_rank = rank_data.get('last_month_rank')
        yesterday_rank = rank_data.get('yesterday_rank')

        lines = []

        # Club Rank line — delta vs yesterday (available directly from API)
        if monthly_rank is not None:
            if yesterday_rank is not None:
                delta = yesterday_rank - monthly_rank  # positive = improved (lower number = better)
                if delta > 0:
                    change = f"(↑{delta} since yesterday)"
                elif delta < 0:
                    change = f"(↓{abs(delta)} since yesterday)"
                else:
                    change = "(no change)"
                lines.append(f"Club Rank: #{monthly_rank} {change}")
            else:
                lines.append(f"Club Rank: #{monthly_rank}")

        # Monthly Rank line — last month comparison comes directly from the API
        if monthly_rank is not None:
            monthly_line = f"Monthly Rank: #{monthly_rank}"
            if last_month_rank is not None:
                monthly_line += f" | Last Month: #{last_month_rank}"
            lines.append(monthly_line)

        return "\n".join(lines) if lines else "*No rank data available*"

    def create_kick_alert(self, club_name: str, members_to_kick: List) -> List[discord.Embed]:
        """
        Create alert embeds for members who need to be kicked.

        Returns a list of embeds, split across multiple if needed.
        """
        items = [{"member": m} for m in members_to_kick]
        sections = self._split_into_sections(
            items,
            lambda item: (
                f"❌ **{item['member'].trainer_name}**: "
                f"Joined {item['member'].join_date.strftime('%Y-%m-%d')} — "
                f"still behind quota"
            ),
            max_length=1000
        )

        embeds = []
        for idx, section in enumerate(sections):
            title = f"🚨 KICK ALERT - {club_name}" if idx == 0 else f"🚨 KICK ALERT - {club_name} (continued {idx + 1})"
            description = (
                f"The following members need to be kicked:\n\n{section}"
                if idx == 0 else section
            )
            embed = discord.Embed(
                title=title,
                description=description,
                color=COLOR_BEHIND,
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"Manual kick required - {club_name}")
            embeds.append(embed)

        return embeds

    def create_error_report(self, club_name: str, error_message: str) -> discord.Embed:
        """Create an error report embed"""
        embed = discord.Embed(
            title=f"❌ Error During Daily Check - {club_name}",
            description=error_message,
            color=0xFF0000,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Please check logs for details - {club_name}")
        return embed
