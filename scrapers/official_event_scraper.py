"""
Official Umamusume News Event Scraper using Playwright (headless browser)

Scrapes https://umamusume.com/news/?t=game for upcoming in-game events,
parses their start/end times, and returns structured Event objects.

Event classification by title:
  - "The story event"           -> story_event
  - "The race event Champions Meeting" -> champions_meeting
  - "Bonus Star Piece"          -> bonus_star_piece
  - "Spotlight"                 -> spotlight
  - anything else               -> unknown
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import List, Optional, Tuple, Set

from playwright.async_api import async_playwright, Page

from scrapers.umamoe_api_scraper import _setup_stealth_patches

logger = logging.getLogger(__name__)

URL = "https://umamusume.com/news/?t=game"

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
    "--single-process",  # Better for Docker containers
    "--disable-setuid-sandbox",  # Additional Docker safety
]

# Detect if running in Docker
IN_DOCKER = os.environ.get("RUNNING_IN_DOCKER") == "true" or os.path.exists("/.dockerenv")

# Longer timeouts for Docker environment
PAGE_LOAD_TIMEOUT = 90000 if IN_DOCKER else 60000  # 90s in Docker, 60s locally
DEFAULT_WAIT_TIMEOUT = 5000 if IN_DOCKER else 3000  # 5s in Docker, 3s locally


class EventType(str, Enum):
    """Classification of known in-game event types."""
    STORY_EVENT = "story_event"
    CHAMPIONS_MEETING = "champions_meeting"
    BONUS_STAR_PIECE = "bonus_star_piece"
    SPOTLIGHT = "spotlight"
    UNKNOWN = "unknown"


@dataclass
class Event:
    """A single upcoming in-game event parsed from the official news."""
    title: str
    type: EventType
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    url: str
    banner_image: Optional[str] = None


# ── Date Parsing ──────────────────────────────────────────────────────────────

_JAPANESE_ERA_MAP = {
    "\u4ee4\u548c": 2018,  # Reiwa
    "\u5e73\u6210": 1988,  # Heisei
}

_MONTH_NAMES_EN = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_ampm_time(time_str: str) -> Tuple[int, int]:
    """Parse '10:00 p.m.' or '9:59 p.m.' into (hour, minute) in 24h format."""
    t = time_str.strip().lower()
    pm = "p.m." in t or "pm" in t
    am = "a.m." in t or "am" in t
    clean = re.sub(r'\s*[ap]\.?m\.?', '', t).strip()
    parts = clean.split(':')
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    if pm and hour != 12:
        hour += 12
    if am and hour == 12:
        hour = 0
    return hour, minute


def _parse_date_jst(text: str) -> Optional[datetime]:
    """
    Parse date string to UTC datetime.

    Handles:
      - "10:00 p.m., Jun 25, 2026 (UTC)"   <- already UTC
      - "2026\u5e747\u670810\u65e5 12:00"  <- JST
      - "\u4ee4\u548c7\u5e747\u670810\u65e5"  <- JST
      - "2026-07-10"                         <- JST
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # English format with am/pm -> UTC (already UTC)
    en_match = re.match(
        r'(\d{1,2}[:]\d{2})\s*[ap]\.?m\.,\s*'
        r'([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})',
        text, re.IGNORECASE
    )
    if en_match:
        hour, minute = _parse_ampm_time(en_match.group(1))
        month = _MONTH_NAMES_EN.get(en_match.group(2)[:3])
        day = int(en_match.group(3))
        year = int(en_match.group(4))
        if month:
            try:
                return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            except Exception:
                pass

    # Japanese era: "\u4ee4\u548c7\u5e747\u670810\u65e5 12:00"
    era_match = re.match(
        r'([\u4ee4\u548c\u5e73\u6210]{1,2})(\d{1,2})\u5e74\s*(\d{1,2})\u6708\s*(\d{1,2})\u65e5'
        r'(?:\s+(\d{1,2})[:](\d{2}))?',
        text
    )
    if era_match:
        base = _JAPANESE_ERA_MAP.get(era_match.group(1))
        if base is not None:
            year = base + int(era_match.group(2))
            month = int(era_match.group(3))
            day = int(era_match.group(4))
            hour = int(era_match.group(5)) if era_match.group(5) else 0
            minute = int(era_match.group(6)) if era_match.group(6) else 0
            try:
                return datetime(year, month, day, hour, minute, tzinfo=timezone.utc) - timedelta(hours=9)
            except Exception:
                return None

    # Western: "2026\u5e747\u670810\u65e5"
    western_match = re.match(
        r'(\d{4})\u5e74\s*(\d{1,2})\u6708\s*(\d{1,2})\u65e5'
        r'(?:\s+(\d{1,2})[:](\d{2}))?',
        text
    )
    if western_match:
        try:
            dt = datetime(
                int(western_match.group(1)), int(western_match.group(2)), int(western_match.group(3)),
                int(western_match.group(4)) if western_match.group(4) else 0,
                int(western_match.group(5)) if western_match.group(5) else 0,
                tzinfo=timezone.utc
            )
            return dt - timedelta(hours=9)
        except Exception:
            return None

    # ISO: "2026-07-10 12:00"
    iso_match = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2})[:](\d{2}))?', text)
    if iso_match:
        try:
            dt = datetime(
                int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)),
                int(iso_match.group(4)) if iso_match.group(4) else 0,
                int(iso_match.group(5)) if iso_match.group(5) else 0,
                tzinfo=timezone.utc
            )
            return dt - timedelta(hours=9)
        except Exception:
            return None

    logger.warning(f"Could not parse date string: '{text}'")
    return None


# ── Event Classification ─────────────────────────────────────────────────────

def _classify_event(title: str) -> EventType:
    """Classify an event by its title using known patterns."""
    if not title:
        return EventType.UNKNOWN
    t = title.strip()
    if "Bonus Star Piece" in t:
        return EventType.BONUS_STAR_PIECE
    if t.startswith("The story event"):
        return EventType.STORY_EVENT
    if t.startswith("The race event Champions Meeting"):
        return EventType.CHAMPIONS_MEETING
    # Spotlight: must mention scout-related terms
    if any(kw in t for kw in ("Spotlight Scout", "Spotlight Pretty Derby", "Spotlight Support Card")):
        return EventType.SPOTLIGHT
    return EventType.UNKNOWN


# ── Title Cleaning ───────────────────────────────────────────────────────────

def _clean_title(raw: str) -> str:
    """
    Clean article card text to extract the event title.

    Card text looks like:
      "Game\\n2026/06/25 22:00 (UTC)\\nNew Spotlight Scouts out now!...\\n\\nDetails"
    We take the longest meaningful line that isn't boilerplate.
    """
    lines = [l.strip() for l in raw.split('\n') if l.strip()]
    # Prefer the longest line that isn't a known keyword
    candidates = [
        l for l in lines
        if l not in ("Game", "Details", "Top", "News")
        and not re.match(r'^\d{4}[/-]\d{2}[/-]\d{2}', l)
        and not re.match(r'^\d{1,2}[:]\d{2}', l)
    ]
    if candidates:
        return max(candidates, key=len)
    return max(lines, key=len) if lines else raw


# ── Article Detail Extraction ────────────────────────────────────────────────

async def _extract_banner_image(page: Page) -> Optional[str]:
    """
    Extract banner image URL from an article page.
    
    Tries multiple selectors in order:
      1. Article banner image class (news-detail__image)
      2. Open Graph meta tag (og:image)
      3. Article header/banner images
    
    Returns absolute URL or None if not found.
    """
    # Try article banner image class first (most reliable for umamusume.com)
    banner_selectors = [
        '.news-detail__image',
        'article img[src*="news"]',
        'article img[src*="banner"]',
    ]
    for selector in banner_selectors:
        try:
            elem = page.locator(selector).first
            if await elem.count() > 0:
                url = await elem.get_attribute("src")
                if url:
                    if not url.startswith("http"):
                        url = f"https://umamusume.com{url}" if url.startswith("/") else f"https://umamusume.com/{url}"
                    logger.info(f"Found banner image via {selector}: {url}")
                    return url
        except Exception:
            continue
    
    # Try Open Graph image as fallback
    og_image_selectors = [
        'meta[property="og:image"]',
        'meta[name="twitter:image"]',
    ]
    for selector in og_image_selectors:
        try:
            elem = page.locator(selector).first
            if await elem.count() > 0:
                url = await elem.get_attribute("content")
                if url:
                    if not url.startswith("http"):
                        url = f"https://umamusume.com{url}" if url.startswith("/") else f"https://umamusume.com/{url}"
                    logger.info(f"Found banner image via {selector}: {url}")
                    return url
        except Exception:
            continue
    
    logger.debug("No banner image found on article page")
    return None


def _extract_times_from_body(body_text: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Extract start/end times from article body text.

    Looks for patterns like:
      - "10:00 p.m., Jun 25 - 9:59 p.m., Jul 4, 2026 (UTC)"
      - "2026\u5e747\u670810\u65e5 12:00 \uff5e 2026\u5e747\u670825\u65e5 11:59"
    """
    start = None
    end = None

    # English range: "10:00 p.m., Jun 25 - 9:59 p.m., Jul 4, 2026 (UTC)"
    en_range = re.search(
        r'(\d{1,2}[:]\d{2}\s*[ap]\.?m\.,\s*[A-Z][a-z]+\s+\d{1,2})'
        r'\s*[\u2013-]\s*'
        r'(\d{1,2}[:]\d{2}\s*[ap]\.?m\.,\s*[A-Z][a-z]+\s+\d{1,2},\s*\d{4})',
        body_text, re.IGNORECASE
    )
    if en_range:
        start_str = en_range.group(1)
        end_str = en_range.group(2)
        year_m = re.search(r',\s*(\d{4})\)?$', end_str)
        if year_m:
            start = _parse_date_jst(f"{start_str}, {year_m.group(1)}")
            end = _parse_date_jst(end_str)

    # Japanese range
    if start is None:
        jp_range = re.search(
            r'(\d{4}\u5e74\s*\d{1,2}\u6708\s*\d{1,2}\u65e5'
            r'(?:\s+\d{1,2}[:]\d{2})?)'
            r'\s*[\uff5e~]\s*'
            r'(\d{4}\u5e74\s*\d{1,2}\u6708\s*\d{1,2}\u65e5'
            r'(?:\s+\d{1,2}[:]\d{2})?)',
            body_text
        )
        if jp_range:
            start = _parse_date_jst(jp_range.group(1))
            end = _parse_date_jst(jp_range.group(2))

    # Fallback: individual labels
    if start is None:
        for pat in [r'Start\s*[:]\s*(.+?)(?:\n|$)', r'\u958b\u59cb\s*[:]\s*(.+?)(?:\n|$)', r'\u958b\u50ac\u671f\u9593\s*[:]\s*(.+?)(?:\n|$)']:
            m = re.search(pat, body_text, re.IGNORECASE)
            if m:
                start = _parse_date_jst(m.group(1).strip())
                break
    if end is None:
        for pat in [r'End\s*[:]\s*(.+?)(?:\n|$)', r'\u7d42\u4e86\s*[:]\s*(.+?)(?:\n|$)']:
            m = re.search(pat, body_text, re.IGNORECASE)
            if m:
                end = _parse_date_jst(m.group(1).strip())
                break

    return start, end


# ── Main Scraping Function ───────────────────────────────────────────────────

async def _collect_article_cards(page: Page) -> List[Tuple[str, str]]:
    """
    From the news list page, collect all visible article cards.
    Returns list of (title, url) tuples.
    """
    cards = []

    # Try several selectors for article cards
    for selector in [
        "a[class*='newsList_cardLink']",
        "a[class*='card']",
        "article a",
        "a[href*='/news/article/']",
    ]:
        links = page.locator(selector)
        count = await links.count()
        if count == 0:
            continue

        logger.info(f"Found {count} article links with selector '{selector}'")
        for i in range(count):
            link = links.nth(i)
            try:
                if not await link.is_visible(timeout=1000):
                    continue
                raw = (await link.inner_text()).strip()
                if not raw:
                    continue
                title = _clean_title(raw)
                href = await link.get_attribute("href") or ""
                if href and not href.startswith("http"):
                    href = f"https://umamusume.com{href}"
                cards.append((title, href))
            except Exception:
                continue

        if cards:
            break

    logger.info(f"Collected {len(cards)} visible article card(s)")
    return cards


async def scrape_official_events(known_titles: Optional[Set[str]] = None) -> List[Event]:
    """
    Scrape upcoming in-game events from the official Umamusume news page.

    Strategy:
      1. Load the news list page, click "View More" to expand
      2. Collect all visible article card titles + URLs
      3. For each matching event article, navigate directly to its URL
         (unless the title is already in known_titles, in which case
          skip the detail page navigation and return a placeholder event)
      4. Extract start/end times from the article body

    Args:
        known_titles: Optional set of event titles that are already saved.
                      Events with these titles will skip detail-page navigation.

    Returns:
        List of Event dataclass instances.
    """
    events: List[Event] = []
    known_titles = known_titles or set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=LAUNCH_ARGS, timeout=30000,
        )
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
        await _setup_stealth_patches(page)

        try:
            # ── Step 1: Load news list and expand ──────────────────────
            logger.info(f"Loading news page: {URL}")
            await page.goto(URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            await page.wait_for_timeout(DEFAULT_WAIT_TIMEOUT)

            view_more_selectors = [
                "button:has-text('View More')",
                "button:has-text('\u3082\u3063\u3068\u898b\u308b')",
                "text=View More",
                "text=\u3082\u3063\u3068\u898b\u308b",
            ]
            for _ in range(3):
                clicked = False
                for sel in view_more_selectors:
                    try:
                        btn = page.locator(sel).first
                        if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                            await btn.click()
                            await page.wait_for_timeout(2000)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    break

            # ── Step 2: Collect all visible article cards ─────────────
            cards = await _collect_article_cards(page)
            
            # Debug: save page HTML and screenshot if no cards found (especially in Docker)
            if not cards:
                debug_dir = "debug_scraper"
                os.makedirs(debug_dir, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                try:
                    # Save screenshot
                    screenshot_path = f"{debug_dir}/no_cards_{timestamp}.png"
                    await page.screenshot(path=screenshot_path, full_page=True)
                    logger.warning(f"No article cards found. Screenshot saved to {screenshot_path}")
                    
                    # Save HTML content
                    html_path = f"{debug_dir}/no_cards_{timestamp}.html"
                    html_content = await page.content()
                    with open(html_path, "w", encoding="utf-8") as f:
                        f.write(html_content)
                    logger.warning(f"Page HTML saved to {html_path}")
                    
                    # Log some useful info
                    body_text = await page.locator("body").inner_text()
                    logger.warning(f"Page body text preview: {body_text[:500]}")
                    
                except Exception as debug_err:
                    logger.error(f"Failed to save debug info: {debug_err}")
                
                logger.warning("No article cards found on the page")
                return events

            # ── Step 3: Classify and process each card ─────────────────
            for title, url in cards:
                event_type = _classify_event(title)
                if event_type == EventType.UNKNOWN:
                    logger.debug(f"Skipping non-event: '{title[:50]}'")
                    continue

                if title in known_titles:
                    logger.debug(f"Skipping already-known event: '{title[:50]}'")
                    events.append(Event(title=title, type=event_type, start_time=None, end_time=None, url=url))
                    continue

                logger.info(f"Processing new event: '{title[:50]}' ({event_type.value})")

                # Navigate directly to the article URL
                if not url:
                    events.append(Event(title=title, type=event_type, start_time=None, end_time=None, url=""))
                    continue

                try:
                    article_timeout = PAGE_LOAD_TIMEOUT
                    await page.goto(url, wait_until="domcontentloaded", timeout=article_timeout)
                    await page.wait_for_timeout(DEFAULT_WAIT_TIMEOUT)
                    body_text = await page.locator("body").inner_text()
                    start_time, end_time = _extract_times_from_body(body_text)
                    banner_image = await _extract_banner_image(page)
                except Exception as e:
                    logger.warning(f"Failed to load article {url}: {e}")
                    start_time, end_time = None, None
                    banner_image = None

                events.append(Event(
                    title=title,
                    type=event_type,
                    start_time=start_time,
                    end_time=end_time,
                    url=url,
                    banner_image=banner_image,
                ))

                if start_time or end_time:
                    logger.info(f"  -> {start_time.isoformat() if start_time else '?'} / {end_time.isoformat() if end_time else '?'}")
                else:
                    logger.info("  -> no date info")

                # Brief pause between requests
                await page.wait_for_timeout(500)

            logger.info(f"Scraping complete: {len(events)} event(s) found")

        except Exception as e:
            logger.error(f"Error scraping official events: {e}")
            raise
        finally:
            await page.close()
            await browser.close()

    return events


# ── State Management (Dedup) ─────────────────────────────────────────────────

def _load_known_titles(path: str) -> Set[str]:
    """Load known event titles from saved JSON."""
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {e.get("title", "") for e in data.get("events", []) if e.get("title")}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load known titles: {e}")
        return set()


def _load_existing_events(path: str) -> dict[str, dict]:
    """
    Load full existing event data from saved JSON, keyed by title.
    
    Returns a dict: {title: {type, start_time, end_time, url, banner_image}}
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        result = {}
        for e in data.get("events", []):
            title = e.get("title")
            if title:
                result[title] = e
        return result
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load existing events: {e}")
        return {}


def _save_events(events: List[Event], path: str, merged_notified: Optional[dict[str, list]] = None) -> None:
    """Write events to JSON file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    merged_notified = merged_notified or {}
    output = {
        "events": [
            {
                "title": e.title,
                "type": e.type.value,
                "start_time": e.start_time.isoformat() if e.start_time else None,
                "end_time": e.end_time.isoformat() if e.end_time else None,
                "url": e.url,
                "banner_image": e.banner_image,
                "notified_clubs": merged_notified.get(e.title, []),
            }
            for e in events
        ],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {len(events)} events to {path}")


def _dedup_events(events: List[Event]) -> List[Event]:
    """
    Remove duplicate events that represent the same underlying game event.

    Dedup strategy:
      1. Group by (type, start_time, end_time) — if all three match, keep the first.
      2. For events with null times, prefer the title that is more informative
         (e.g. "out now" over "coming soon", longer title over shorter).
    """
    seen: Set[Tuple] = set()
    result: List[Event] = []

    for e in events:
        if e.start_time and e.end_time:
            key = (e.type.value, e.start_time.isoformat(), e.end_time.isoformat())
        else:
            # For null-time events, use a fuzzy key: type + normalized title prefix
            # Normalize: lowercase, remove common boilerplate
            norm = e.title.lower().replace("!", "").replace("?", "").strip()
            # Take first 40 chars as a fingerprint
            key = (e.type.value, "null", norm[:40])

        if key in seen:
            logger.debug(f"Dedup: skipping '{e.title[:50]}' (duplicate of existing event)")
            continue
        seen.add(key)
        result.append(e)

    return result


async def check_and_save(json_path: str) -> bool:
    """
    Run scraper once; save if new events detected.

    For events already in the JSON file, the scraper skips detail-page
    navigation. After scraping, existing detail data (start_time, end_time,
    banner_image) is merged back into those placeholder events so the saved
    file always has complete information for all events.
    """
    known_titles = _load_known_titles(json_path)
    existing = _load_existing_events(json_path)

    # Pass known titles so the scraper skips detail navigation for them
    raw_events = await scrape_official_events(known_titles=known_titles)
    events = _dedup_events(raw_events)
    logger.info(f"Dedup: {len(raw_events)} raw -> {len(events)} unique event(s)")

    # Merge existing detail data back into placeholder events (known titles
    # were returned with null start/end/banner from the skip logic)
    for e in events:
        if e.title in existing and e.start_time is None and e.end_time is None:
            existing_data = existing[e.title]
            # Parse stored ISO strings back into datetime objects
            start_str = existing_data.get("start_time")
            end_str = existing_data.get("end_time")
            if start_str:
                try:
                    e.start_time = datetime.fromisoformat(start_str)
                except (ValueError, TypeError):
                    pass
            if end_str:
                try:
                    e.end_time = datetime.fromisoformat(end_str)
                except (ValueError, TypeError):
                    pass
            if not e.banner_image:
                e.banner_image = existing_data.get("banner_image")
            logger.debug(f"Merged existing detail data for '{e.title[:50]}'")

    # Collect existing notified_clubs data keyed by title
    merged_notified = {}
    for title, existing_data in existing.items():
        clubs = existing_data.get("notified_clubs", [])
        if clubs:
            merged_notified[title] = clubs

    current = {e.title for e in events}
    new_titles = current - known_titles
    if new_titles:
        logger.info(f"New event(s): {', '.join(new_titles)}")
        _save_events(events, json_path, merged_notified=merged_notified)
        return True
    else:
        logger.info(f"No new events ({len(events)} known)")
        # Still save to persist any notified_clubs updates made by the notification task
        _save_events(events, json_path, merged_notified=merged_notified)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(check_and_save("data/events.json"))
