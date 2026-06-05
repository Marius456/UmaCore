"""
Uma.moe API scraper for club data fetching

Uses Playwright with system Chrome in non-headless mode to bypass
Cloudflare's browser_proof_required challenge, with stealth patches
and persistent cookie storage for robust scraping.
"""
from typing import Dict, Optional, List
import logging
import calendar
import json
import asyncio
import os
import subprocess
from datetime import datetime, date, timezone, timedelta

from playwright.async_api import async_playwright, Error as PlaywrightError
from playwright.async_api import BrowserContext, Page

from scrapers.base_scraper import BaseScraper
from config.settings import PLAYWRIGHT_HEADLESS, PLAYWRIGHT_COOKIE_DIR

logger = logging.getLogger(__name__)

# Shared browser instance across scrape calls (lazy-initialised, reused for performance)
_browser = None
_browser_context = None
_playwright = None
_orphan_pids = []


def _get_chrome_path() -> Optional[str]:
    """
    Detect the system Chrome executable path on Windows.
    Returns None if not found, in which case Playwright's bundled Chromium is used.
    """
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"),
                     "Google\\Chrome\\Application\\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"),
                     "Google\\Chrome\\Application\\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Google\\Chrome\\Application\\chrome.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            logger.info(f"Found system Chrome at: {path}")
            return path
    logger.warning("System Chrome not found in common locations; will fall back to Playwright bundled Chromium")
    return None


def _get_cookie_dir() -> str:
    """Return the persistent cookie storage directory, creating it if needed."""
    cookie_dir = PLAYWRIGHT_COOKIE_DIR
    os.makedirs(cookie_dir, exist_ok=True)
    logger.debug(f"Cookie directory: {os.path.abspath(cookie_dir)}")
    return cookie_dir


async def _setup_stealth_patches(page: Page) -> None:
    """
    Apply JavaScript-based stealth patches to evade Cloudflare headless detection.
    These patches run before any page script executes.
    """
    await page.add_init_script("""
        // Override navigator.webdriver
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });

        // Override navigator.plugins to return a non-empty array
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });

        // Override navigator.languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en']
        });

        // Override navigator.hardwareConcurrency
        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => 8
        });

        // Override permissions query to avoid detection
        if (navigator.permissions) {
            const originalQuery = navigator.permissions.query;
            navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ||
                parameters.name === 'geolocation' ||
                parameters.name === 'camera' ||
                parameters.name === 'microphone'
            ) ? Promise.resolve({ state: 'denied' }) : originalQuery(parameters);
        }

        // Override chrome.runtime if it exists (real Chrome has it)
        if (window.chrome && window.chrome.runtime) {
            Object.defineProperty(window.chrome.runtime, 'id', {
                get: () => 'abcdefghijklmnop'
            });
        }

        // Add missing chrome properties that real Chrome has
        if (window.chrome) {
            if (!window.chrome.app) window.chrome.app = {};
            if (!window.chrome.csi) window.chrome.csi = () => {};
            if (!window.chrome.loadTimes) window.chrome.loadTimes = () => {};
        }
    """)


async def _get_browser_context() -> BrowserContext:
    """
    Get or create a shared persistent Playwright browser context.
    The context stores Cloudflare clearance cookies so they survive restarts.
    """
    global _browser_context, _browser
    if _browser_context is None or not _browser_context.browser or not _browser_context.browser.is_connected():
        browser = await _get_browser()
        cookie_dir = _get_cookie_dir()
        _browser_context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            storage_state=os.path.join(cookie_dir, "storage_state.json") if os.path.exists(
                os.path.join(cookie_dir, "storage_state.json")) else None,
        )
        logger.info("Created persistent Playwright browser context (non-headless)")
    return _browser_context


async def _get_browser():
    """
    Get or create a shared Playwright browser instance using system Chrome.
    Uses non-headless mode to bypass Cloudflare's headless detection.
    Falls back to Playwright's bundled Chromium if system Chrome is not found.
    """
    global _browser, _playwright
    if _browser is not None and _browser.is_connected():
        return _browser

    if _playwright is None:
        _playwright = await async_playwright().start()

    chrome_path = _get_chrome_path()
    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-web-security",
        "--disable-features=IsolateOrigins,site-per-process",
        "--window-size=1920,1080",
        "--window-position=-32000,-32000",  # Off-screen positioning to avoid visible window popup
    ]
    try:
        if chrome_path and os.name == 'nt':
            # Use system Chrome with channel detection
            _browser = await _playwright.chromium.launch(
                headless=False,
                executable_path=chrome_path,
                args=launch_args,
                timeout=30000,
            )
            logger.info("Started system Chrome (non-headless) via Playwright")
        elif PLAYWRIGHT_HEADLESS:
            _browser = await _playwright.chromium.launch(
                headless=True,
                args=launch_args,
                timeout=30000,
            )
            logger.info("Started Playwright bundled Chromium (headless, forcing headless mode via config)")
        else:
            # Try channel='chrome' which uses system Chrome by channel detection
            try:
                _browser = await _playwright.chromium.launch(
                    headless=False,
                    channel='chrome',
                    args=launch_args,
                    timeout=30000,
                )
                logger.info("Started system Chrome (non-headless, channel=chrome)")
            except Exception as channel_err:
                logger.warning(f"Could not launch system Chrome via channel='chrome': {channel_err}")
                # Fallback: try with bundled Chromium in headless mode (last resort)
                _browser = await _playwright.chromium.launch(
                    headless=True,
                    args=launch_args,
                    timeout=30000,
                )
                logger.warning("Falling back to Playwright bundled Chromium (headless mode)")

    except PlaywrightError as e:
        message = str(e)
        if "Executable doesn't exist" in message or "playwright install" in message.lower():
            raise RuntimeError(
                "Playwright Chromium is not installed. Run 'python -m playwright install chromium' "
                "or 'playwright install chromium' after installing dependencies."
            ) from e
        raise
    except Exception as e:
        logger.error(f"Failed to launch browser: {e}")
        # Last-resort fallback to headless bundled Chromium
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
            timeout=30000,
        )
        logger.warning("Fallback: launched Playwright bundled Chromium in headless mode after error")

    # Track Chrome process PID for cleanup
    try:
        if hasattr(_browser, 'process') and _browser.process:
            pid = _browser.process.pid
            if pid:
                _orphan_pids.append(pid)
                logger.debug(f"Tracking browser process PID: {pid}")
    except Exception:
        pass

    return _browser


async def _close_browser():
    """Close the shared browser instance (call on bot shutdown)."""
    global _browser, _browser_context, _playwright, _orphan_pids

    # Save storage state (cookies + localStorage) before closing
    if _browser_context:
        try:
            cookie_dir = _get_cookie_dir()
            storage_path = os.path.join(cookie_dir, "storage_state.json")
            await _browser_context.storage_state(path=storage_path)
            logger.info(f"Saved browser storage state to {storage_path}")
        except Exception as e:
            logger.warning(f"Failed to save storage state: {e}")

    # Close browser context
    if _browser_context:
        try:
            await _browser_context.close()
        except Exception:
            pass
        _browser_context = None

    # Close browser
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None

    # Stop Playwright
    if _playwright:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None

    # Force-kill any orphaned Chrome processes (Windows)
    if _orphan_pids and os.name == 'nt':
        for pid in set(_orphan_pids):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    timeout=5,
                )
                logger.debug(f"Killed orphaned Chrome process PID: {pid}")
            except Exception:
                pass
    _orphan_pids.clear()

    logger.info("Closed shared Playwright browser instance")


class UmaMoeAPIScraper(BaseScraper):
    """Scraper using Uma.moe API for fast data retrieval"""

    CLOUDFLARE_TIMEOUT = 60  # seconds to wait for Cloudflare challenge to resolve

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

    async def _fetch_json_via_fetch(self, page, url: str) -> Optional[dict]:
        """
        Fetch JSON from the given URL using JavaScript fetch() within the page context.
        This preserves Cloudflare Turnstile proof (page.goto() would lose it).
        Returns parsed dict or None on failure.
        """
        try:
            result = await page.evaluate("""
                async (url) => {
                    try {
                        const resp = await fetch(url);
                        const body = await resp.text();
                        let parsed = null;
                        try { parsed = JSON.parse(body); } catch(e) {}
                        return {
                            status: resp.status,
                            body: body,
                            ok: resp.ok,
                            error: null
                        };
                    } catch (e) {
                        return { status: 0, body: '', ok: false, error: e.message };
                    }
                }
            """, url)

            if result.get("error"):
                logger.error(f"Fetch failed for {url}: {result['error']}")
                return None

            status = result.get("status")
            if status != 200:
                body_preview = (result.get("body") or "")[:200]
                logger.error(f"Uma.moe API returned status {status} for {url}: {body_preview}")
                return None

            body = result.get("body")
            if not body:
                logger.error("Empty response body for %s", url)
                return None

            return json.loads(body)

        except Exception as e:
            logger.error(f"Request failed for {url}: {e}")
            return None

    async def _fetch_api_data(self, year: int, month: int) -> dict:
        """
        Fetch API data using a persistent non-headless browser context.

        Shows a brief Chrome window (positioned off-screen) to satisfy
        Cloudflare's browser_proof_required challenge. After initial
        resolution, cookies are persisted and reused for subsequent calls.

        Returns the full API response dict. Raises on failure.
        """
        context = await _get_browser_context()
        page = await context.new_page()

        try:
            # Apply stealth patches before any navigation
            await _setup_stealth_patches(page)

            # Step 1: Visit the main uma.moe page to solve the Cloudflare challenge.
            # This sets the necessary cookies/tokens for subsequent API calls.
            logger.info("Visiting uma.moe main page to satisfy Cloudflare challenge...")
            await page.goto("https://uma.moe/", wait_until="domcontentloaded", timeout=self.CLOUDFLARE_TIMEOUT * 1000)

            # Wait for the page to fully settle after challenge resolution
            try:
                await page.wait_for_load_state("networkidle", timeout=self.CLOUDFLARE_TIMEOUT * 1000)
            except Exception as e:
                logger.warning(f"Network idle wait timed out for main page (may be okay): {e}")

            # Verify we got past Cloudflare by checking page content
            page_title = await page.title()
            page_text = await page.locator("body").text_content() or ""
            logger.info(f"Main page loaded: title='{page_title[:80]}', content length={len(page_text)}")

            # Check if we're still stuck on a Cloudflare challenge page
            if "Just a moment" in page_text[:500] or "checking your browser" in page_text[:500].lower():
                logger.warning("Cloudflare challenge may still be in progress or blocking access")
                # Give it a bit more time
                await asyncio.sleep(10)
                page_text = await page.locator("body").text_content() or ""
                if "Just a moment" in page_text[:500]:
                    logger.error("Cloudflare challenge still present after extended wait — page may be blocked")
                else:
                    logger.info("Cloudflare challenge resolved after extended wait")
            else:
                logger.info("Cloudflare challenge appears resolved (main page loaded successfully)")

            # Step 2: Dismiss cookie consent popup (Angular overlay on uma.moe)
            # The API endpoint won't return data while this overlay is present.
            try:
                reject_btn = page.locator("button.consent-btn.reject")
                if await reject_btn.is_visible(timeout=5000):
                    await reject_btn.click()
                    logger.info("Dismissed cookie consent popup (Reject All)")
                    await asyncio.sleep(1)  # Give overlay animation time to disappear
                else:
                    logger.debug("Cookie consent popup not found — may already be dismissed")
            except Exception as e:
                logger.debug(f"Cookie consent popup handling (non-critical): {e}")

            # Step 3: Build the API URL and fetch data
            api_url = (
                f"{self.base_url}"
                f"?circle_id={self.circle_id}"
                f"&year={year}"
                f"&month={month}"
            )
            logger.info(f"Fetching API data from: {api_url}")

            data = await self._fetch_json_via_fetch(page, api_url)
            if data is None:
                raise ValueError(f"API request failed for {year}-{month:02d}")

            # Save cookies/storage state for future reuse
            try:
                cookie_dir = _get_cookie_dir()
                storage_path = os.path.join(cookie_dir, "storage_state.json")
                await context.storage_state(path=storage_path)
                logger.info(f"Saved storage state after successful fetch to {storage_path}")
            except Exception as e:
                logger.warning(f"Failed to save storage state after fetch: {e}")

            return data

        finally:
            await page.close()

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