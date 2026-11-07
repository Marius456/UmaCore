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
import tempfile
from urllib.parse import urlparse
from dataclasses import dataclass, field, replace
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
OFFICIAL_EVENTS_PROXY = os.getenv(
    "OFFICIAL_EVENTS_PROXY", "http://100.111.216.3:8888"
).strip()
PROXY_CONNECT_TIMEOUT = 3.0
UNDATED_EVENT_RETENTION_DAYS = 30


async def _reachable_proxy(proxy_address: str) -> Optional[str]:
    """Return the configured proxy only when its TCP endpoint is reachable."""
    if not proxy_address:
        return None

    parsed = urlparse(proxy_address)
    if not parsed.hostname:
        logger.warning("Ignoring invalid OFFICIAL_EVENTS_PROXY value")
        return None

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        logger.warning("Ignoring OFFICIAL_EVENTS_PROXY with an invalid port")
        return None
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, port),
            timeout=PROXY_CONNECT_TIMEOUT,
        )
        return proxy_address
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning(
            "Official-events proxy %s:%s is unreachable (%s); trying direct connection",
            parsed.hostname,
            port,
            exc,
        )
        return None
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


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
    published_at: Optional[datetime] = None
    notified_clubs: list[str] = field(default_factory=list)

    @classmethod
    def from_record(cls, record: dict) -> Event:
        title = record.get("title") or ""
        try:
            event_type = EventType(record.get("type"))
        except ValueError:
            event_type = EventType.UNKNOWN
        if event_type == EventType.UNKNOWN:
            event_type = _classify_event(title)
        return cls(
            title=title,
            type=event_type,
            start_time=_read_event_time(record.get("start_time")),
            end_time=_read_event_time(record.get("end_time")),
            url=record.get("url") or "",
            banner_image=record.get("banner_image"),
            published_at=_read_event_time(record.get("published_at")),
            notified_clubs=record.get("notified_clubs") or [],
        )

    def to_record(self) -> dict:
        return {
            "title": self.title,
            "event_key": _canonical_event_key(self.title, self.type),
            "type": self.type.value,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "url": self.url,
            "banner_image": self.banner_image,
            "notified_clubs": self.notified_clubs,
        }


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
        r'(\d{1,2}[:]\d{2}\s*[ap]\.?m\.),\s*'
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


_PREVIEW_STATUS = re.compile(r"\s+(?:is\s+)?coming\s+soon!?$", re.IGNORECASE)
_EVENT_STATUS = re.compile(r"\s+(?:is here|has ended|out now)!?$", re.IGNORECASE)


def event_display_name(title: str) -> str:
    """Remove official announcement wording from an event title."""
    title = re.sub(r"^the\s+(?:story|race)\s+event\s+", "", title.strip(), flags=re.IGNORECASE)
    if _PREVIEW_STATUS.search(title):
        title = _PREVIEW_STATUS.sub("", title)
        title = re.sub(r"^new\s+(?=spotlight\b)", "", title, flags=re.IGNORECASE)
    return _EVENT_STATUS.sub("", title)


def _canonical_event_key(title: str, event_type: EventType) -> str:
    """Return the name shared by announcement-status variants."""
    key = event_display_name(title).lower()
    key = re.sub(r"[^\w\s]+", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    return f"{event_type.value}:{key}"


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _read_event_time(value: object) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            return _as_utc(datetime.fromisoformat(value))
        except (ValueError, OverflowError):
            pass
    return None


def _event_schedule(event: Event) -> tuple[Optional[datetime], Optional[datetime]]:
    return _as_utc(event.start_time), _as_utc(event.end_time)


def _shares_event_schedule(event: Event, other: Event) -> bool:
    shared_times = [
        (first, second)
        for first, second in zip(_event_schedule(event), _event_schedule(other))
        if first is not None and second is not None
    ]
    return bool(shared_times) and all(first == second for first, second in shared_times)


def _spotlight_article_rank(event: Event) -> tuple[int, datetime, str]:
    title = event.title.strip()
    if re.search(r"\s+has ended!?$", title, re.IGNORECASE):
        status = 0
    elif _PREVIEW_STATUS.search(title):
        status = 1
    else:
        status = 2
    return status, _as_utc(event.published_at) or datetime.min.replace(tzinfo=timezone.utc), event.url


def _fill_event_details(current: Event, fallback: Event) -> Event:
    return replace(
        current,
        start_time=current.start_time or fallback.start_time,
        end_time=current.end_time or fallback.end_time,
        published_at=current.published_at or fallback.published_at,
        banner_image=current.banner_image or fallback.banner_image,
    )


def _merge_spotlight_details(current: Event, fallback: Event) -> Event:
    preferred = current
    if current.url != fallback.url:
        preferred = max((current, fallback), key=_spotlight_article_rank)
    publication_times = [
        _as_utc(event.published_at) for event in (current, fallback) if event.published_at
    ]
    return replace(
        preferred,
        url=preferred.url or current.url or fallback.url,
        banner_image=preferred.banner_image or current.banner_image or fallback.banner_image,
        start_time=current.start_time or fallback.start_time,
        end_time=current.end_time or fallback.end_time,
        published_at=max(publication_times, default=None),
        notified_clubs=list(dict.fromkeys(current.notified_clubs + fallback.notified_clubs)),
    )


@dataclass
class _SpotlightOccurrence:
    event: Event
    urls: set[str]


def _find_spotlight_occurrence(
    event: Event, occurrences: list[_SpotlightOccurrence], urls: set[str]
) -> Optional[_SpotlightOccurrence]:
    key = _canonical_event_key(event.title, event.type)
    same_name = [
        occurrence for occurrence in occurrences
        if _canonical_event_key(occurrence.event.title, occurrence.event.type) == key
    ]
    for occurrence in same_name:
        if urls & occurrence.urls:
            return occurrence
    compatible = [
        occurrence for occurrence in same_name if _shares_event_schedule(event, occurrence.event)
    ]
    return compatible[0] if len(compatible) == 1 else None


def _spotlight_occurrences(events: list[Event]) -> list[_SpotlightOccurrence]:
    """Group complete schedules first so a partial schedule cannot pick an arbitrary run."""
    occurrences: list[_SpotlightOccurrence] = []
    for event in sorted(events, key=lambda e: bool(e.start_time) + bool(e.end_time), reverse=True):
        urls = {event.url} if event.url else set()
        match = _find_spotlight_occurrence(event, occurrences, urls)
        if match is None:
            occurrences.append(_SpotlightOccurrence(event, urls))
        else:
            match.event = _merge_spotlight_details(match.event, event)
            match.urls.update(urls)
    return occurrences


def merge_saved_spotlight_events(records: list[dict]) -> list[dict]:
    """Normalize legacy Spotlight records before checking reminder eligibility."""
    spotlight = []
    other = []
    for record in records:
        event = Event.from_record(record)
        if event.type == EventType.SPOTLIGHT:
            spotlight.append(event)
        else:
            other.append(record)
    return other + [occurrence.event.to_record() for occurrence in _spotlight_occurrences(spotlight)]


def spotlight_reminder_key(record: dict) -> tuple[str, Optional[datetime], Optional[datetime]]:
    event = Event.from_record(record)
    return _canonical_event_key(event.title, event.type), *_event_schedule(event)


# ── Title Cleaning ───────────────────────────────────────────────────────────

def _clean_title(raw: str) -> str:
    """
    Clean article card text to extract the event title.

    Card text looks like:
      "Game\\n2026/06/25 22:00 (UTC)\\nNew Spotlight Scouts out now!...\\n\\nDetails"
    We take the longest meaningful line that isn't boilerplate.
    """
    lines = [line.strip() for line in raw.split('\n') if line.strip()]
    # Prefer the longest line that isn't a known keyword
    candidates = [
        line for line in lines
        if line not in ("Game", "Details", "Top", "News")
        and not re.match(r'^\d{4}[/-]\d{2}[/-]\d{2}', line)
        and not re.match(r'^\d{1,2}[:]\d{2}', line)
    ]
    if candidates:
        return max(candidates, key=len)
    return max(lines, key=len) if lines else raw


def _extract_card_published_at(raw: str) -> Optional[datetime]:
    """Extract the publication timestamp displayed on a news-list card."""
    match = re.search(
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})"
        r"(?:\s+(\d{1,2})[:](\d{2}))?"
        r"(?:\s*\((UTC|JST)\))?",
        raw,
        re.IGNORECASE,
    )
    if not match:
        return None

    try:
        published_at = datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4) or 0),
            int(match.group(5) or 0),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None

    # The site normally labels card timestamps as UTC. Treat an unlabeled or
    # explicitly JST timestamp consistently with the detail-page date parser.
    if (match.group(6) or "JST").upper() == "JST":
        published_at -= timedelta(hours=9)
    return published_at


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

async def _wait_for_news_cards(page: Page) -> None:
    """Wait for client-rendered news; a load failure must not become an empty feed."""
    await page.locator(
        "a[href^='/news/'], a[class*='newsList_cardLink'], a[href*='/news/article/']"
    ).first.wait_for(state="visible", timeout=PAGE_LOAD_TIMEOUT)


async def _collect_article_cards(page: Page) -> List[Tuple[str, str, Optional[datetime]]]:
    """
    From the news list page, collect all visible article cards.
    Returns list of (title, url, published_at) tuples.
    """
    cards = []

    # The current site links directly to numeric article paths such as
    # ``/news/914``. Keep the older selectors as fallbacks because the site
    # has changed its card markup more than once.
    for selector in [
        "a[href^='/news/']",
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

                # Exclude the news index, pagination links, and unrelated
                # navigation cards. Article pages currently use /news/<id>.
                if not re.search(
                    r"^https://umamusume\.com/news/(?:article/)?\d+/?(?:[?#].*)?$",
                    href,
                    re.IGNORECASE,
                ):
                    continue

                if any(existing_url == href for _, existing_url, _ in cards):
                    continue
                cards.append((title, href, _extract_card_published_at(raw)))
            except Exception:
                continue

        if cards:
            break

    logger.info(f"Collected {len(cards)} visible article card(s)")
    return cards


async def scrape_official_events(
    known_titles: Optional[Set[str]] = None,
    existing_events: Optional[list[dict] | dict[str, dict]] = None,
    now: Optional[datetime] = None,
) -> List[Event]:
    """
    Scrape upcoming in-game events from the official Umamusume news page.

    Strategy:
      1. Load the news list page, click "View More" to expand
      2. Collect all visible article card titles + URLs
      3. For each matching event article, navigate directly to its URL
         unless it was previously recorded with a past end time
      4. Extract start/end times from the article body

    Args:
        known_titles: Optional set of saved titles retained for caller compatibility.
        existing_events: Previously saved event records, used to avoid loading
                         detail pages for events already known to have ended.
        now: Current time override used by tests and retention checks.

    Returns:
        List of Event dataclass instances.
    """
    events: List[Event] = []
    if isinstance(existing_events, dict):
        existing_events = [{"title": title, **record} for title, record in existing_events.items()]
    scrape_now = now or datetime.now(timezone.utc)
    if scrape_now.tzinfo is None:
        scrape_now = scrape_now.replace(tzinfo=timezone.utc)
    existing_by_title = {}
    existing_by_key = {}
    existing_by_url = {}
    for record in existing_events or []:
        event = Event.from_record(record)
        if event.type == EventType.SPOTLIGHT:
            if event.url:
                existing_by_url[event.url] = event
        else:
            existing_by_title[event.title] = event
            existing_by_key[_canonical_event_key(event.title, event.type)] = event

    async with async_playwright() as p:
        proxy_address = await _reachable_proxy(OFFICIAL_EVENTS_PROXY)
        launch_options = {
            "headless": True,
            "args": LAUNCH_ARGS,
            "timeout": 30000,
        }
        if proxy_address:
            launch_options["proxy"] = {"server": proxy_address}
        browser = await p.chromium.launch(
            **launch_options,
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
            await _wait_for_news_cards(page)

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
            for title, url, published_at in cards:
                event_type = _classify_event(title)
                if event_type == EventType.UNKNOWN:
                    logger.debug(f"Skipping non-event: '{title[:50]}'")
                    continue

                if event_type == EventType.SPOTLIGHT:
                    previous = existing_by_url.get(url)
                else:
                    previous = existing_by_title.get(title) or existing_by_key.get(
                        _canonical_event_key(title, event_type)
                    )
                if previous and previous.end_time and previous.end_time < scrape_now:
                    logger.debug(f"Skipping expired event article: '{title[:50]}'")
                    continue

                # Classification only makes this an event article; whether the
                # underlying event is new is decided after canonical deduplication
                # against events.json in check_and_save(). Calling every article
                # "new" here made normal restart refreshes look like discoveries.
                logger.info(f"Processing event article: '{title[:50]}' ({event_type.value})")

                # Navigate directly to the article URL
                if not url:
                    events.append(Event(
                        title=title,
                        type=event_type,
                        start_time=None,
                        end_time=None,
                        url="",
                        published_at=published_at,
                    ))
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
                    published_at=published_at,
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

def _load_existing_events(path: str) -> list[dict]:
    """Load saved records, including recurring events that reuse a title."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [record for record in json.load(f).get("events", []) if record.get("title")]
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load existing events: {e}")
        return []


def _save_events(events: List[Event], path: str) -> None:
    """Write events to JSON file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    output = {
        "events": [event.to_record() for event in events],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    directory = os.path.dirname(path) or "."
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
            json.dump(output, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    logger.info(f"Saved {len(events)} events to {path}")


def _dedup_events(events: List[Event]) -> List[Event]:
    """Collapse announcement variants of the same scheduled event."""
    grouped: dict[str, Event] = {}
    spotlight = []

    for e in events:
        if e.type == EventType.SPOTLIGHT:
            spotlight.append(e)
            continue
        key = _canonical_event_key(e.title, e.type)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = e
            continue

        # Prefer the variant with a parsed schedule, while filling any missing
        # fields from the other article variant.
        if e.start_time and e.end_time and not (existing.start_time and existing.end_time):
            e.banner_image = e.banner_image or existing.banner_image
            if existing.published_at and (
                not e.published_at or existing.published_at > e.published_at
            ):
                e.published_at = existing.published_at
            grouped[key] = e
        else:
            existing.start_time = existing.start_time or e.start_time
            existing.end_time = existing.end_time or e.end_time
            existing.banner_image = existing.banner_image or e.banner_image
            if e.published_at and (
                not existing.published_at or e.published_at > existing.published_at
            ):
                existing.published_at = e.published_at
        logger.debug(f"Merged article variant '{e.title[:50]}' into event '{key}'")

    return list(grouped.values()) + [occurrence.event for occurrence in _spotlight_occurrences(spotlight)]


def _retain_relevant_events(
    events: List[Event], now: Optional[datetime] = None
) -> List[Event]:
    """Keep active/upcoming events and recent undated announcements."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=UNDATED_EVENT_RETENTION_DAYS)
    retained: List[Event] = []

    for event in events:
        if event.end_time is not None:
            end_time = event.end_time
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
            if end_time >= now:
                retained.append(event)
            else:
                logger.info(f"Pruned ended event: '{event.title[:50]}'")
            continue

        if event.published_at is not None:
            published_at = event.published_at
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            if published_at >= cutoff:
                retained.append(event)
            else:
                logger.info(f"Pruned stale undated event: '{event.title[:50]}'")
            continue

        # Existing JSON from before published_at was introduced cannot be aged
        # safely, so retain it until a future scrape supplies usable metadata.
        retained.append(event)

    return retained


async def check_and_save(json_path: str, now: Optional[datetime] = None) -> bool:
    """
    Run scraper once; save if new events detected.

    Previously ended events are skipped before detail-page navigation. After
    scraping, missing detail data is merged from the saved file and expired or
    stale undated events are pruned before the file is replaced.
    """
    records = _load_existing_events(json_path)
    existing = [Event.from_record(record) for record in records]

    # Pass known titles for logging/compatibility. Known articles are still
    # re-parsed so schedule corrections and parser fixes take effect.
    raw_events = await scrape_official_events(
        known_titles={event.title for event in existing},
        existing_events=records,
        now=now,
    )
    saved_by_title = {}
    saved_by_key = {}
    notified_by_key: dict[str, list[str]] = {}
    saved_spotlight = []
    for event in existing:
        if event.type == EventType.SPOTLIGHT:
            saved_spotlight.append(event)
        else:
            key = _canonical_event_key(event.title, event.type)
            saved_by_title[event.title] = event
            saved_by_key[key] = event
            notified_by_key.setdefault(key, []).extend(event.notified_clubs)

    refreshed: list[tuple[Event, bool]] = []
    for event in _dedup_events([e for e in raw_events if e.type != EventType.SPOTLIGHT]):
        key = _canonical_event_key(event.title, event.type)
        previous = saved_by_title.get(event.title) or saved_by_key.get(key)
        if previous:
            event = _fill_event_details(event, previous)
        event.notified_clubs = list(dict.fromkeys(notified_by_key.get(key, [])))
        refreshed.append((event, key in saved_by_key))

    saved_occurrences = _spotlight_occurrences(saved_spotlight)
    saved_by_article = {
        (event.url, _canonical_event_key(event.title, event.type)): event
        for event in saved_spotlight if event.url
    }
    current_spotlight = []
    for event in raw_events:
        if event.type == EventType.SPOTLIGHT:
            previous = saved_by_article.get((event.url, _canonical_event_key(event.title, event.type)))
            if previous:
                event = _fill_event_details(event, previous)
                event.notified_clubs = list(dict.fromkeys(event.notified_clubs + previous.notified_clubs))
            current_spotlight.append(event)

    current_occurrences = _spotlight_occurrences(current_spotlight)
    for occurrence in current_occurrences:
        event = occurrence.event
        previous = _find_spotlight_occurrence(event, saved_occurrences, occurrence.urls)
        if previous and _find_spotlight_occurrence(
            previous.event, current_occurrences, previous.urls
        ) is not occurrence:
            previous = None
        if previous:
            event = _merge_spotlight_details(event, previous.event)
        refreshed.append((event, previous is not None))

    logger.info(f"Dedup: {len(raw_events)} raw -> {len(refreshed)} unique event(s)")
    events = _retain_relevant_events([event for event, _ in refreshed], now=now)
    new_events = {id(event) for event, known in refreshed if not known}
    new_titles = [event.title for event in events if id(event) in new_events]
    if new_titles:
        logger.info(f"New event(s): {', '.join(new_titles)}")
    else:
        logger.info(f"No new events ({len(events)} known)")
    _save_events(events, json_path)
    return bool(new_titles)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(check_and_save("data/events.json"))
