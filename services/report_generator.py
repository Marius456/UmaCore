"""
Discord report generation service
"""
import io
import asyncio
from datetime import date
from typing import Dict, List, Optional
from uuid import UUID
import discord
import logging
from config.settings import COLOR_BEHIND
from services.club_race_card import build_club_race, load_quota_report_history, race_card_html
from services.member_performance_card import build_member_performance, card_html
from services.quota_schedule import QuotaSchedule

logger = logging.getLogger(__name__)

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
        """Render a self-contained report PNG for Discord."""

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

    async def create_daily_report(
        self, club_name: str, daily_quota: int, status_summary: Dict, report_date: date,
        *, club_id: UUID, rank_data: Optional[Dict] = None, quota_period: str = "daily",
    ) -> List[discord.File]:
        """Render the club race card followed by the member-performance pages."""
        rank_data = rank_data or {}
        members = status_summary["on_track"] + status_summary["behind"]
        history = await load_quota_report_history(
            club_id, members, report_date, quota_period, daily_quota,
        )
        race = build_club_race(members, history, report_date, rank_data)
        daily_goal = daily_quota / QuotaSchedule.PERIOD_DAYS.get(quota_period, 1)
        race_html = race_card_html(
            club_name, report_date, daily_goal, status_summary, rank_data, history, race,
        )
        files = [await self._generate_table_image(race_html, "club_race_card.png")]

        rows = build_member_performance(
            members, history.points, history.schedule, report_date, history.month_end,
        )
        rows_per_card = 30
        for offset in range(0, len(rows), rows_per_card):
            page_number = offset // rows_per_card + 1
            html = card_html(
                club_name, report_date, status_summary["total_members"], daily_goal, rank_data,
                rows[offset:offset + rows_per_card], offset + 1,
            )
            files.append(await self._generate_table_image(html, f"member_performance_{page_number}.png"))

        return files

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
