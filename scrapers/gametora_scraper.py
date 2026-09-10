"""
GameTora Gacha Scraper using Playwright (headless browser)

Scrapes https://gametora.com/umamusume/gacha for current gacha banner information
including rate-up characters/supports, their rates, and availability windows.
"""
from typing import List, Optional
import logging
import re

from playwright.async_api import async_playwright

from scrapers.umamoe_api_scraper import _setup_stealth_patches

logger = logging.getLogger(__name__)

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-web-security",
    "--disable-features=IsolateOrigins,site-per-process",
    "--window-size=1920,1080",
]

GACHA_URL = "https://gametora.com/umamusume/gacha"


class GachaItem:
    """Represents a single rate-up item on a gacha banner."""
    def __init__(self, name: str, variant: Optional[str] = None,
                 is_new: bool = False, rate: Optional[float] = None,
                 card_type: Optional[str] = None):
        self.name = name
        self.variant = variant           # e.g. "Camping", "SSR Guts", "SSR Wit"
        self.is_new = is_new             # Has "New" badge
        self.rate = rate                 # Pull rate percentage (e.g. 0.75)
        self.card_type = card_type       # e.g. "SSR", "SR" for support cards

    def __repr__(self) -> str:
        parts = [self.name]
        if self.variant:
            parts.append(f"({self.variant})")
        if self.is_new:
            parts.append("New")
        if self.rate is not None:
            parts.append(f"{self.rate}%")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "variant": self.variant,
            "is_new": self.is_new,
            "rate": self.rate,
            "card_type": self.card_type,
        }


class GachaBanner:
    """Represents a single gacha banner (Character or Support Card)."""
    def __init__(self, banner_id: str, banner_type: str,
                 start_date: str, end_date: str,
                 items: List[GachaItem]):
        self.banner_id = banner_id
        self.banner_type = banner_type    # "Character Gacha" or "Support Card Gacha"
        self.start_date = start_date
        self.end_date = end_date
        self.items = items

    def __repr__(self) -> str:
        items_str = ", ".join(str(item) for item in self.items)
        return (f"[{self.banner_type}] {self.start_date} – {self.end_date}: {items_str}")

    def to_dict(self) -> dict:
        return {
            "banner_id": self.banner_id,
            "banner_type": self.banner_type,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "items": [item.to_dict() for item in self.items],
        }


def _parse_rate(text: str) -> Optional[float]:
    """Extract a percentage value from text like 'New, 0.75%'."""
    match = re.search(r'([\d.]+)%', text)
    if match:
        return float(match.group(1))
    return None


def _has_new_badge(text: str) -> bool:
    """Check if text contains 'New' badge indicator."""
    return bool(re.search(r'\bNew\b', text, re.IGNORECASE))


def _parse_name_with_variant(text: str) -> tuple:
    """Parse a name like 'Taiki Shuttle (Camping)' into (name, variant)."""
    match = re.match(r'^(.+?)\s*\(([^)]+)\)\s*$', text.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return text.strip(), None


def _parse_banner_text(banner_id: str, text: str) -> Optional[GachaBanner]:
    """Parse one banner from its visible text without relying on generated CSS classes."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3 or lines[0] not in {"Character Gacha", "Support Card Gacha"}:
        return None

    banner_type = lines[0]
    date_match = re.match(r"^(.+?)\s*[–-]\s*(.+?)$", lines[1])
    start_date = date_match.group(1).strip() if date_match else ""
    end_date = date_match.group(2).strip() if date_match else ""

    items = []
    for line in lines[2:]:
        item_match = re.match(r"^(.+?)\s+((?:New,\s*)?[\d.]+%)$", line, re.IGNORECASE)
        if not item_match:
            continue

        name, variant = _parse_name_with_variant(item_match.group(1))
        rate_text = item_match.group(2)
        card_type = None
        if variant and banner_type == "Support Card Gacha":
            type_match = re.match(r"(SSR|SR|R)\s", variant)
            if type_match:
                card_type = type_match.group(1)

        items.append(GachaItem(
            name=name,
            variant=variant,
            is_new=_has_new_badge(rate_text),
            rate=_parse_rate(rate_text),
            card_type=card_type,
        ))

    if not items:
        return None

    return GachaBanner(
        banner_id=banner_id,
        banner_type=banner_type,
        start_date=start_date,
        end_date=end_date,
        items=items,
    )


async def scrape_gacha_banners() -> List[GachaBanner]:
    """
    Scrape current gacha banners from GameTora.

    Uses Playwright headless browser to render the JavaScript-heavy page,
    then parses the DOM for banner data.

    Returns:
        List of GachaBanner objects with all rate-up information.
    """
    banners = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=LAUNCH_ARGS, timeout=30000)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = await context.new_page()

        # Apply stealth patches to avoid detection
        await _setup_stealth_patches(page)

        try:
            logger.info(f"Loading {GACHA_URL}...")
            # Use domcontentloaded instead of networkidle because gametora.com loads many
            # third-party ad/analytics scripts that never stop fetching, causing networkidle
            # to time out. The gacha data is server-side rendered by Next.js, so the full
            # HTML with banner information is available immediately after DOM is ready.
            await page.goto(GACHA_URL, wait_until="domcontentloaded", timeout=60000)

            # Dismiss cookie consent popup (InMobi CMP) which can overlay banner content
            try:
                consent_accept = page.locator("#qc-cmp2-ui #accept-btn")
                if await consent_accept.is_visible(timeout=5000):
                    await consent_accept.click()
                    logger.info("Dismissed cookie consent popup (Agree)")
                    await page.wait_for_timeout(1000)
                else:
                    logger.debug("Cookie consent popup not found — may already be dismissed")
            except Exception as e:
                logger.debug(f"Cookie consent popup handling (non-critical): {e}")

            # Generated styled-components class names change on every GameTora rebuild.
            # Anchor to the stable heading and direct child banner IDs instead.
            current_banners = page.get_by_role(
                "heading", name="Current banners", exact=True
            )
            await current_banners.wait_for(state="visible", timeout=30000)

            # Extra wait for all banner data to fully render
            await page.wait_for_timeout(2000)

            # Find all banner containers
            banner_elements = await current_banners.locator(
                "xpath=following-sibling::div[1]/div[@id]"
            ).all()
            logger.info(f"Found {len(banner_elements)} banner(s) on page")

            for el in banner_elements:
                try:
                    banner = await _parse_banner_element(el)
                    if banner:
                        banners.append(banner)
                except Exception as e:
                    logger.warning(f"Failed to parse a banner element: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error scraping GameTora gacha page: {e}")
            raise
        finally:
            await page.close()
            await browser.close()

    return banners


async def _parse_banner_element(el) -> Optional[GachaBanner]:
    """Parse a single banner container element into a GachaBanner object."""
    banner_id = await el.get_attribute("id") or "unknown"
    return _parse_banner_text(banner_id, await el.inner_text())
