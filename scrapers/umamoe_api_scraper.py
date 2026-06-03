"""
Uma.moe API scraper for club data fetching

Uses Playwright to bypass Cloudflare's browser_proof_required challenge
by first visiting the main uma.moe page (which sets Cloudflare cookies),
then fetching the API endpoint.
"""
from typing import Dict, Optional, List
import logging
import calendar
import json
import asyncio
from datetime import datetime, date, timezone, timedelta

from playwright.async_api import async_playwright, Error as PlaywrightError

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# Shared browser instance across scrape calls (lazy-initialised, reused for performance)
_browser = None
_playwright = None


async def _get_browser():
    """
    Get or create a shared Playwright browser instance.
    The browser is kept alive across calls to avoid the overhead of launching
    a new browser for every scrape. It is closed when the bot shuts down.
    """
    global _browser, _playwright
    if _browser is None or not _browser.is_connected():
        if _playwright is None:
            _playwright = await async_playwright().start()
        try:
            _browser = await _playwright.chromium.launch(headless=True)
        except PlaywrightError as e:
            message = str(e)
            if "Executable doesn't exist" in message or "playwright install" in message.lower():
                raise RuntimeError(
                    "Playwright Chromium is not installed. Run 'python -m playwright install chromium' "
                    "or 'playwright install chromium' after installing dependencies."
                ) from e
            raise
        logger.info("Started shared Playwright browser instance for Uma.moe API")
    return _browser


async def _close_browser():
    """Close the shared browser instance (call on bot shutdown)."""
    global _browser, _playwright
    try:
        if _browser:
            await _browser.close()
    except Exception:
        pass
    try:
        if _playwright:
            await _playwright.stop()
    except Exception:
        pass
    _browser = None
    _playwright = None
    logger.info("Closed shared Playwright browser instance")


class UmaMoeAPIScraper(BaseScraper):
    """Scraper using Uma.moe API for fast data retrieval"""

    CLOUDFLARE_TIMEOUT = 45  # seconds to wait for Cloudflare challenge to resolve

    def __init__(self, circle_id: str):
        self.circle_id = circle_id
        self.base_url = "https://uma.moe/api/v4/circles"
        self.current_day_count = 1
        # Track which year/month was actually fetched (differs from now() on Day 1)
        self._fetched_year = None
        self._fetched_month = None
        # Set to a date object when the scraper fell back to the previous month;
        # None when the fetched data matches the current calendar date.
        self._data_date: Optional[date] = None
        # Club/monthly rank fields from the API response (nested inside "circle" key)
        self._monthly_rank: Optional[int] = None
        self._last_month_rank: Optional[int] = None
        self._yesterday_rank: Optional[int] = None
        super().__init__(self.base_url)

    async def _fetch_json_via_page(self, page, url: str) -> Optional[dict]:
        """
        Navigate to the given URL and extract the JSON response body from <pre>.
        Returns parsed dict or None on failure.
        """
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self.CLOUDFLARE_TIMEOUT * 1000)
            if response is None:
                logger.error("No response received for %s", url)
                return None

            status = response.status
            if status != 200:
                body_text = await page.locator("pre").text_content() or ""
                logger.error(f"Uma.moe API returned status {status} for {url}: {body_text[:200]}")
                return None

            # Give any remaining dynamic content a moment to settle
            await asyncio.sleep(1)

            body_text = await page.locator("pre").text_content()
            if not body_text:
                logger.error("Empty response body for %s", url)
                return None

            return json.loads(body_text)

        except Exception as e:
            logger.error(f"Request failed for {url}: {e}")
            return None

    async def _fetch_api_data(self, year: int, month: int) -> dict:
        """
        Fetch API data by opening a Playwright page, visiting uma.moe first
        (to satisfy Cloudflare's browser_proof_required challenge), then
        navigating to the API endpoint.

        Returns the full API response dict. Raises on failure.
        """
        browser = await _get_browser()
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        try:
            # Step 1: Visit the main uma.moe page to solve the Cloudflare challenge.
            # This sets the necessary cookies/tokens for subsequent API calls.
            logger.info("Visiting uma.moe main page to satisfy Cloudflare challenge...")
            await page.goto("https://uma.moe/", wait_until="domcontentloaded", timeout=self.CLOUDFLARE_TIMEOUT * 1000)

            # Wait for the page to fully settle after challenge resolution
            await page.wait_for_load_state("networkidle", timeout=self.CLOUDFLARE_TIMEOUT * 1000)
            logger.info("Cloudflare challenge resolved successfully")

            # Step 2: Build the API URL and fetch data
            api_url = (
                f"{self.base_url}"
                f"?circle_id={self.circle_id}"
                f"&year={year}"
                f"&month={month}"
            )
            logger.info(f"Fetching API data from: {api_url}")

            data = await self._fetch_json_via_page(page, api_url)
            if data is None:
                raise ValueError(f"API request failed for {year}-{month:02d}")

            return data

        finally:
            await context.close()

    async def scrape(self) -> Dict[str, Dict]:
        """
        Scrape club data from Uma.moe API.

        On Day 1 the new month hasn't populated yet, so we fetch the previous
        month as the primary data source. We also fetch the current month and
        use its index 0 as the true endpoint per member.

        On Day 2+, we check if current day data exists (Uma.moe updates ~15:10 UTC).
        If not, we fall back to previous day to avoid reading zeros.

        Returns:
            Dict mapping viewer_id -> member data
        """
        try:
            now = datetime.now()
            year = now.year
            month = now.month

            # Determine which month to use as primary data source
            if now.day == 1:
                if month == 1:
                    year -= 1
                    month = 12
                else:
                    month -= 1
                last_day = calendar.monthrange(year, month)[1]
                self._data_date = date(year, month, last_day)
                logger.info(f"Day 1 detected: fetching previous month ({year}-{month:02d}) as primary source, data date: {self._data_date}")

            self._fetched_year = year
            self._fetched_month = month

            logger.info(f"Fetching data from Uma.moe API for circle {self.circle_id}...")

            # Primary fetch: the month we're actually reporting on
            primary_data = await self._fetch_api_data(year, month)
            if not primary_data:
                raise ValueError(f"Primary API request failed for {year}-{month:02d}")

            # On Day 1, also fetch current month for endpoint correction
            endpoint_members = None
            endpoint_data = None
            if now.day == 1:
                endpoint_data = await self._fetch_api_data(now.year, now.month)
                if endpoint_data and "members" in endpoint_data:
                    endpoint_members = endpoint_data.get("members", [])
                    logger.info(f"Fetched {len(endpoint_members)} members from {now.year}-{now.month:02d} for endpoint correction")
                else:
                    logger.warning("Could not fetch current month for endpoint correction — using previous month's last snapshot")

            # Extract club ranks from the "circle" sub-object.
            # On Day 1 prefer the current-month endpoint (more timely), fall back to primary.
            # Note: the top-level "club_rank" field is a tier bracket (not a position rank);
            # the actual position ranks live inside response["circle"].
            rank_source = (endpoint_data if (now.day == 1 and endpoint_data) else primary_data) or {}
            circle_data = rank_source.get("circle") or {}
            self._monthly_rank = circle_data.get("monthly_rank")
            self._last_month_rank = circle_data.get("last_month_rank")
            self._yesterday_rank = circle_data.get("yesterday_rank")
            logger.info(
                f"Club ranks: monthly_rank={self._monthly_rank}, "
                f"last_month_rank={self._last_month_rank}, "
                f"yesterday_rank={self._yesterday_rank}"
            )

            # Detect end-of-month JST rollover: uma.moe always returns the CURRENT
            # competition period's rank fields regardless of the year/month query param.
            # After ~15:00 UTC on the last day of the month, uma.moe has already switched
            # to next month's competition internally, so ALL rank fields reflect the new
            # (nearly empty) period. Drop them entirely so the rank section is omitted.
            now_utc = datetime.now(timezone.utc)
            now_jst = now_utc + timedelta(hours=9)
            last_day_of_month = calendar.monthrange(now_utc.year, now_utc.month)[1]
            if now_utc.day == last_day_of_month and now_jst.month != now_utc.month:
                logger.warning(
                    "End-of-month JST rollover detected: uma.moe rank fields already reflect "
                    "the new competition period. Dropping all rank data to avoid false display."
                )
                self._monthly_rank = None
                self._last_month_rank = None
                self._yesterday_rank = None

            if not primary_data or "members" not in primary_data:
                logger.error("API response missing 'members' field")
                raise ValueError("Invalid API response structure")

            members = primary_data.get("members", [])
            logger.info(f"API returned {len(members)} members")

            if not members:
                logger.warning("No members found in API response")
                return {}

            # Pass calendar_day to _parse_api_data so it can check if data exists
            parsed_data = self._parse_api_data(members, endpoint_members=endpoint_members, calendar_day=now.day)
            logger.info(f"Successfully parsed {len(parsed_data)} active members from API")

            return parsed_data

        except Exception as e:
            logger.error(f"Error during Uma.moe API scraping: {e}")
            raise

    def _parse_api_data(self, members: list, endpoint_members: Optional[List] = None, calendar_day: int = None) -> Dict[str, Dict]:
        """
        Parse API member data into scraper format.

        Uma.moe returns LIFETIME cumulative fans. Converts to monthly by
        subtracting each member's starting lifetime fans (fans at join).

        Uma.moe updates around 15:10 UTC with yesterday's data, so we check
        if current day data exists before using it.

        Args:
            members: List of member dicts from the primary (previous) month
            endpoint_members: Member list from current month (Day 1 only)
            calendar_day: Current calendar day for data availability checking

        Returns:
            Dict mapping viewer_id -> member data
        """
        parsed_data = {}

        now = datetime.now()

        if now.day == 1:
            # Day 1: Fetched previous month, use last day of that month
            current_day = calendar.monthrange(self._fetched_year, self._fetched_month)[1]
            logger.info(f"Day 1 fallback: using day {current_day} (last day of {self._fetched_year}-{self._fetched_month:02d})")
        else:
            # Day 2+: Check if current day data exists
            current_day = calendar_day if calendar_day else now.day
            current_day_index = current_day - 1

            # Check if current day data exists by sampling active members
            data_exists = False
            if members:
                # Find a member with recent activity to check data availability
                for member in members:
                    sample_fans = member.get("daily_fans", [])
                    if sample_fans and len(sample_fans) > current_day_index and sample_fans[current_day_index] > 0:
                        data_exists = True
                        logger.debug(f"Found current day data in member {member.get('trainer_name')}")
                        break

            if not data_exists:
                fallback_day = now.day - 1
                fallback_idx = fallback_day - 1   # 0-based index for fallback day
                prev_idx = fallback_day - 2       # 0-based index for the day before that

                # When falling back past day 1, verify the fallback data is genuinely fresh.
                # Uma.moe sometimes copies the previous day's values as a placeholder before
                # publishing the real update (~15:10 UTC). Detect this: if no member shows
                # fan growth between day fallback_day-1 and fallback_day, it's stale.
                if fallback_day >= 2 and prev_idx >= 0:
                    relevant = [
                        m for m in members
                        if len(m.get("daily_fans", [])) > fallback_idx
                        and len(m.get("daily_fans", [])) > prev_idx
                        and m["daily_fans"][fallback_idx] > 0
                    ]
                    any_growth = any(
                        m["daily_fans"][fallback_idx] > m["daily_fans"][prev_idx]
                        for m in relevant
                    )
                    if relevant and not any_growth:
                        raise ValueError(
                            f"Day {fallback_day} data appears stale — fan counts are unchanged from "
                            f"day {fallback_day - 1} for all sampled members. "
                            f"Uma.moe likely hasn't published today's update yet (typically ~15:10 UTC)."
                        )

                current_day = fallback_day
                logger.warning(
                    f"Current day {now.day} data not available yet (Uma.moe updates ~15:10 UTC). "
                    f"Using day {current_day} data."
                )
                # Slot 'current_day' (index current_day-1) holds competition results from
                # day current_day-1 (published the following day at ~15:10 UTC).
                # Use that competition date so expected quota is calculated correctly.
                self._data_date = date(now.year, now.month, max(1, current_day - 1))
            else:
                # Current day data exists (Day 5 on Feb 5 = Feb 4 competition)
                # current_day = day number to read from array
                # _data_date = actual competition date it represents
                current_day = now.day
                self._data_date = date(now.year, now.month, now.day - 1)
                logger.info(f"Day {current_day} data is available (represents day {now.day - 1} competition results)")

        self.current_day_count = current_day

        # Build endpoint lookup for Day 1 correction
        endpoint_totals = {}
        if endpoint_members:
            for m in endpoint_members:
                vid = m.get("viewer_id")
                fans = m.get("daily_fans", [])
                if vid and fans and len(fans) > 0 and fans[0] > 0:
                    endpoint_totals[str(vid)] = fans[0]
            logger.info(f"Endpoint correction available for {len(endpoint_totals)} members")

        for member in members:
            viewer_id = member.get("viewer_id")
            trainer_name = member.get("trainer_name")
            lifetime_fans = member.get("daily_fans", [])

            if not viewer_id or not trainer_name:
                logger.warning(f"Skipping member with missing data: viewer_id={viewer_id}, name={trainer_name}")
                continue

            # Skip members who left the club (0 fans on current day)
            current_day_index = current_day - 1
            if current_day_index >= len(lifetime_fans):
                logger.warning(f"Current day {current_day} exceeds array length for {trainer_name}")
                continue

            current_day_lifetime_fans = lifetime_fans[current_day_index]
            if current_day_lifetime_fans == 0:
                logger.debug(f"Skipping inactive member (left club): {trainer_name} (ID: {viewer_id})")
                continue

            viewer_id_str = str(viewer_id)

            # Detect join day (first non-zero value) and starting lifetime fans
            join_day = 1
            starting_lifetime_fans = 0

            for idx, fans in enumerate(lifetime_fans[:current_day], start=1):
                if fans > 0:
                    join_day = idx
                    starting_lifetime_fans = fans
                    break

            # Convert lifetime cumulative fans to monthly cumulative fans
            monthly_fans = []
            for day_idx in range(current_day):
                lifetime_total = lifetime_fans[day_idx]

                if lifetime_total == 0:
                    fans_this_month = 0
                else:
                    fans_this_month = lifetime_total - starting_lifetime_fans

                monthly_fans.append(fans_this_month)

            # Day 1 endpoint correction
            if endpoint_totals and viewer_id_str in endpoint_totals:
                endpoint_lifetime = endpoint_totals[viewer_id_str]
                if endpoint_lifetime >= starting_lifetime_fans:
                    corrected_monthly = endpoint_lifetime - starting_lifetime_fans
                    if corrected_monthly > monthly_fans[-1]:
                        logger.debug(
                            f"Endpoint correction for {trainer_name}: "
                            f"{monthly_fans[-1]:,} → {corrected_monthly:,} "
                            f"(+{corrected_monthly - monthly_fans[-1]:,} recovered)"
                        )
                        monthly_fans[-1] = corrected_monthly
                else:
                    logger.warning(
                        f"Endpoint correction skipped for {trainer_name}: "
                        f"endpoint lifetime ({endpoint_lifetime:,}) < starting ({starting_lifetime_fans:,})"
                    )

            parsed_data[viewer_id_str] = {
                "name": trainer_name,
                "trainer_id": viewer_id_str,
                "fans": monthly_fans,
                "join_day": join_day
            }

            logger.debug(
                f"Parsed {trainer_name}: joined day {join_day}, "
                f"lifetime: {starting_lifetime_fans:,} → {current_day_lifetime_fans:,}, "
                f"monthly: {monthly_fans[-1]:,}"
            )

        return parsed_data

    def get_current_day(self) -> int:
        """Get the current day number"""
        return self.current_day_count

    def get_data_date(self) -> Optional[date]:
        """
        Returns the date the scraped data belongs to when fallback was used,
        or None when the data matches today.
        """
        return self._data_date

    def get_monthly_rank(self) -> Optional[int]:
        """Return the club's current monthly position rank (from circle.monthly_rank)."""
        return self._monthly_rank

    def get_last_month_rank(self) -> Optional[int]:
        """Return the club's previous month position rank (from circle.last_month_rank)."""
        return self._last_month_rank

    def get_yesterday_rank(self) -> Optional[int]:
        """Return the club's rank as of yesterday (from circle.yesterday_rank)."""
        return self._yesterday_rank


__all__ = ['UmaMoeAPIScraper', '_close_browser']