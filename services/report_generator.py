"""
Discord report generation service
"""
import io
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple
import discord
import logging
import plotly.graph_objects as go
import plotly.io as pio
from tabulate import tabulate

from config.settings import COLOR_ON_TRACK, COLOR_BEHIND, COLOR_INFO

logger = logging.getLogger(__name__)

# Each embed in the report can carry optional file attachments (for table images)
ReportEmbed = Tuple[discord.Embed, List[discord.File]]

# Shared Playwright browser for rendering table images (lazy-initialised)
_playwright_browser = None
_playwright_context = None
_playwright = None


async def _ensure_playwright_browser_async():
    """
    Ensure a healthy shared Playwright browser instance exists.
    Performs a real health check (not just is_connected()) and re-launches
    if the browser has died. Returns the browser instance.
    """
    global _playwright_browser, _playwright_context, _playwright

    # If we have a browser, do a real health check by trying to use it
    if _playwright_browser is not None:
        try:
            # Quick health check — try to create and close a page
            page = await _playwright_browser.new_page()
            await page.close()
            return _playwright_browser
        except Exception:
            logger.warning("Playwright browser is dead, re-launching...")
            await _close_playwright_browser_async()

    from playwright.async_api import async_playwright

    if _playwright is None:
        _playwright = await async_playwright().start()

    _playwright_browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
            "--no-zygote",
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

    async def _generate_table_image(self, headers: List[str], rows: List[List], title_color: str, filename: str) -> discord.File:
        """Generate a styled table image using Plotly + Playwright screenshot and return it as a Discord file attachment."""
        # Convert hex color like 0x00FF00 to "#00FF00" format
        hex_str = f"#{title_color:06X}" if isinstance(title_color, int) else title_color

        fig = go.Figure(data=[go.Table(
            header=dict(
                values=headers,
                fill_color=hex_str,
                font=dict(color='white', size=14, family='Arial'),
                align='center',
                height=32
            ),
            cells=dict(
                values=[[str(row[i]) for row in rows] for i in range(len(headers))],
                fill_color=['white', '#f8f9fa'],
                font=dict(color='#2c3e50', size=13, family='Arial'),
                align=['center', 'left', 'center', 'center', 'center', 'center'],
                height=30,
                line_color='#dcdcdc',
                line_width=1
            )
        )])

        fig.update_layout(
            margin=dict(l=10, r=10, t=10, b=10),
            paper_bgcolor='white',
            width=750,
            height=40 + (len(rows) * 33),
            font=dict(family='Arial')
        )

        # Render the figure to HTML, then screenshot with Playwright
        html_str = pio.to_html(fig, include_plotlyjs='cdn', full_html=True)

        # Use persistent context for better reliability
        context = await _get_browser_context_async()
        page = await context.new_page()
        try:
            await page.set_content(html_str, wait_until='networkidle')
            # Wait a brief moment for the plotly.js render to complete
            await page.wait_for_timeout(500)

            # Locate the plotly graph div and take a screenshot of it
            plot_div = page.locator('.plotly-graph-div')
            screenshot_bytes = await plot_div.screenshot(timeout=15000)

            buf = io.BytesIO(screenshot_bytes)
            buf.seek(0)
            return discord.File(buf, filename=filename)
        except Exception as e:
            # If the browser died mid-render, try once more with a fresh launch
            logger.warning(f"Playwright render failed (will retry once): {e}")
            await _close_playwright_browser_async()
            context = await _get_browser_context_async()
            page = await context.new_page()
            try:
                await page.set_content(html_str, wait_until='networkidle')
                await page.wait_for_timeout(500)
                plot_div = page.locator('.plotly-graph-div')
                screenshot_bytes = await plot_div.screenshot(timeout=15000)
                buf = io.BytesIO(screenshot_bytes)
                buf.seek(0)
                return discord.File(buf, filename=filename)
            finally:
                await page.close()
        finally:
            await page.close()

    def _prepare_table_data(self, members_list: List[Dict], start_index: int = 1, daily_quota: int = 0) -> List[List]:
        """Converts the member dicts into a list of lists for tabulate"""
        table_rows = []
        for idx, item in enumerate(members_list, start_index):
            member = item['member']
            history = item['history']

            # Daily calculation
            yesterday_fans = item.get('yesterday_cumulative_fans', 0)
            daily_progress = history.cumulative_fans - yesterday_fans
            daily_str = f"+{self.format_fans_short(daily_progress)}" if daily_progress >= 0 else f"-{self.format_fans_short(abs(daily_progress))}"

            # Avg Calculation
            month_start = date(history.date.year, history.date.month, 1)
            days_active = (history.date - month_start).days + 1
            avg_per_day = history.cumulative_fans // days_active if days_active > 0 else 0

            # Carry = net surplus/deficit compared to expected quota for this month
            # Fans reset monthly in Umamusume, so cumulative_fans already equals this month's fans.
            # No need to subtract month_start_fans (which would be last month's total).
            carry_fans = history.deficit_surplus
            carry_str = f"+{self.format_fans_short(carry_fans)}" if carry_fans >= 0 else f"-{self.format_fans_short(abs(carry_fans))}"

            # We truncate the name to 12 chars to prevent table blowout on mobile
            name = (member.trainer_name[:12] + '..') if len(member.trainer_name) > 13 else member.trainer_name

            table_rows.append([
                idx,
                name,
                daily_str,
                carry_str,
                self.format_fans_short(avg_per_day),
                self.format_fans_short(history.cumulative_fans)
            ])
        return table_rows

    def _prepare_behind_table_data(self, members_list: List[Dict], start_index: int = 1) -> List[List]:
        """Converts behind-quota member dicts into sorted rows with deficit as Carry"""
        table_rows = []
        for item in members_list:
            member = item['member']
            history = item['history']

            # Daily calculation
            yesterday_fans = item.get('yesterday_cumulative_fans', 0)
            daily_progress = history.cumulative_fans - yesterday_fans
            daily_str = f"+{self.format_fans_short(daily_progress)}" if daily_progress >= 0 else f"-{self.format_fans_short(abs(daily_progress))}"

            # Carry = deficit (negative, how far behind they are)
            deficit = abs(history.deficit_surplus) if history.deficit_surplus < 0 else 0
            carry_str = f"-{self.format_fans_short(deficit)}"

            # Avg Calculation
            month_start = date(history.date.year, history.date.month, 1)
            days_active = (history.date - month_start).days + 1
            avg_per_day = history.cumulative_fans // days_active if days_active > 0 else 0

            # We truncate the name to 12 chars to prevent table blowout on mobile
            name = (member.trainer_name[:12] + '..') if len(member.trainer_name) > 13 else member.trainer_name

            table_rows.append([
                0,  # placeholder, will be set after sorting
                name,
                daily_str,
                carry_str,
                self.format_fans_short(avg_per_day),
                self.format_fans_short(history.cumulative_fans),
                deficit  # hidden sort key
            ])

        # Sort by deficit ascending (smallest debt first)
        table_rows.sort(key=lambda r: r[6])

        # Assign continuous numbers after sorting
        for i, row in enumerate(table_rows, start_index):
            row[0] = i

        # Remove the sort key before returning
        return [row[:6] for row in table_rows]

    def _split_data_into_chunks(self, data: List[List], max_rows_per_chunk: int = 20) -> List[List[List]]:
        """Split table data into chunks that each fit within a single image."""
        if not data:
            return []

        chunks = []
        for i in range(0, len(data), max_rows_per_chunk):
            chunks.append(data[i:i + max_rows_per_chunk])
        return chunks

    def _split_table_into_sections(self, members_list: List[Dict], max_length: int = 3500, start_index: int = 1, is_behind: bool = False, daily_quota: int = 0, carry_col_name: str = "Carry") -> List[str]:
        """Splits data into chunks while maintaining table formatting
        (kept for backward compatibility with any remaining text-table usage)"""
        if not members_list:
            return ["*No members*"]

        if is_behind:
            all_data = self._prepare_behind_table_data(members_list, start_index)
        else:
            all_data = self._prepare_table_data(members_list, start_index, daily_quota)
        headers = ["#", "Name", "Daily", carry_col_name, "Avg", "Total"]

        sections = []
        current_chunk = []

        for row in all_data:
            # Check if adding this row exceeds the limit
            temp_chunk = current_chunk + [row]
            temp_table = tabulate(temp_chunk, headers=headers, tablefmt="simple", stralign="right")

            if len(temp_table) > max_length and current_chunk:
                sections.append(tabulate(current_chunk, headers=headers, tablefmt="simple", stralign="right"))
                current_chunk = [row]
            else:
                current_chunk.append(row)

        if current_chunk:
            sections.append(tabulate(current_chunk, headers=headers, tablefmt="simple", stralign="right"))

        return sections

    async def _generate_table_embeds(self, title: str, color: int, headers: List[str],
                                      data_rows: List[List], image_prefix: str) -> List[ReportEmbed]:
        """Generate one or more embeds with table images from prepared data rows."""
        if not data_rows:
            return []

        chunks = self._split_data_into_chunks(data_rows, max_rows_per_chunk=20)
        embeds_with_files = []

        for idx, chunk in enumerate(chunks):
            embed_title = title if idx == 0 else f"{title} (continued {idx + 1})"
            image_filename = f"{image_prefix}_{idx}.png"

            # Generate the image
            file = await self._generate_table_image(headers, chunk, color, image_filename)

            embed = discord.Embed(
                title=embed_title,
                color=color,
                timestamp=discord.utils.utcnow()
            )
            # Attach image to embed using attachment:// URL
            embed.set_image(url=f"attachment://{image_filename}")

            embeds_with_files.append((embed, [file]))

        return embeds_with_files

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

        # On Track images
        if status_summary['on_track']:
            on_track_data = self._prepare_table_data(
                status_summary['on_track'],
                start_index=1,
                daily_quota=daily_quota
            )
            table_embeds = await self._generate_table_embeds(
                title="✅ On Track",
                color=COLOR_ON_TRACK,
                headers=["#", "Name", "Daily", "Surplus", "Avg", "Total"],
                data_rows=on_track_data,
                image_prefix="on_track"
            )
            report_items.extend(table_embeds)

        # Behind images
        if status_summary['behind']:
            behind_data = self._prepare_behind_table_data(
                status_summary['behind'],
                start_index=on_track_count + 1
            )
            table_embeds = await self._generate_table_embeds(
                title="⚠️ Behind Quota",
                color=COLOR_BEHIND,
                headers=["#", "Name", "Daily", "Deficit", "Avg", "Total"],
                data_rows=behind_data,
                image_prefix="behind"
            )
            report_items.extend(table_embeds)

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