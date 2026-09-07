"""
GameTora API client for Uma Musume Global events and gacha banners.

Data flow:
  1. Fetch manifest -> get current file hashes
  2. Fetch hashed data files (layout, gacha, events)
  3. Filter by current timestamp to find active content
"""
import asyncio
import time
import logging

import aiohttp

logger = logging.getLogger(__name__)

MANIFEST_URL = "https://gametora.com/data/manifests/umamusume.json"
DATA_BASE = "https://gametora.com/data/umamusume"

# Files to fetch from the manifest (EN/Global)
_FILES = [
    "en/layout_data",
    "en/gacha/char-standard",
    "en/gacha/support-standard",
    "en/missions/storyevents",      # has proper EN names (storyEventEn)
    "en/events/champions-meeting",
    "en/events/legend-race",
    "en/missions/limited",          # limited-time mission events (ms timestamps)
]

CACHE_TTL_SECONDS = 5 * 60


def _normalize_ts(ts: int) -> int:
    """Convert millisecond timestamps to seconds if needed."""
    return ts // 1000 if ts > 10 ** 11 else ts


def _get_start(item: dict) -> int:
    raw = item.get("startDate") or item.get("start") or item.get("start_date") or 0
    return _normalize_ts(raw)


def _get_end(item: dict) -> int:
    raw = item.get("endDate") or item.get("end") or item.get("end_date") or 0
    return _normalize_ts(raw)


def _get_name(item: dict) -> str:
    return (
        item.get("name_en")
        or item.get("name")
        or item.get("title_en")
        or item.get("title")
        or f"Event #{item.get('id', '?')}"
    )


class GametoraClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict | None = None
        self._cache_at = 0.0
        self._cache_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "UmaCore-Bot/1.0"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _fetch(self, url: str) -> dict | list:
        session = await self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _fetch_file(self, manifest: dict, key: str) -> dict | list | None:
        hash_ = manifest.get(key)
        if not hash_:
            logger.warning(f"Key '{key}' not found in manifest")
            return None
        url = f"{DATA_BASE}/{key}.{hash_}.json"
        return await self._fetch(url)

    async def get_events_data(self) -> dict:
        """Return cached event data, coalescing concurrent refreshes."""
        now = time.monotonic()
        if self._cache is not None and now - self._cache_at < CACHE_TTL_SECONDS:
            return self._cache

        async with self._cache_lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache_at < CACHE_TTL_SECONDS:
                return self._cache
            data = await self._fetch_events_data()
            self._cache = data
            self._cache_at = time.monotonic()
            return data

    async def _fetch_events_data(self) -> dict:
        """
        Fetch and return all current Global event/gacha data.

        Returns a dict with:
          layout            - en/layout_data (current featured chars/supports)
          char_banner       - active char-standard gacha entry (or None)
          support_banner    - active support-standard gacha entry (or None)
          story_events      - list of currently active story events
          champions_meeting - active champions meeting entry (or None)
          legend_race       - active legend race entry (or None)
          limited_missions  - list of currently active limited mission events
        """
        now = int(time.time())

        manifest = await self._fetch(MANIFEST_URL)

        results = await asyncio.gather(
            *[self._fetch_file(manifest, key) for key in _FILES],
            return_exceptions=True,
        )

        (
            layout,
            char_gacha,
            support_gacha,
            story_events,
            champ_meeting,
            legend_race,
            limited_missions,
        ) = results

        # Log any fetch failures but don't crash
        for key, result in zip(_FILES, results):
            if isinstance(result, Exception):
                logger.error(f"Failed to fetch '{key}': {result}")

        def _safe_list(v):
            return v if isinstance(v, list) else []

        def _safe_dict(v):
            return v if isinstance(v, dict) else {}

        def _find_active(items):
            for item in _safe_list(items):
                if isinstance(item, dict) and _get_start(item) <= now <= _get_end(item):
                    return item
            return None

        def _filter_active(items):
            return [
                item
                for item in _safe_list(items)
                if isinstance(item, dict) and _get_start(item) <= now <= _get_end(item)
            ]

        # en/missions/limited wraps events in {"events": [...]}
        limited_events_raw = (
            limited_missions.get("events", [])
            if isinstance(limited_missions, dict)
            else _safe_list(limited_missions)
        )

        return {
            "layout": _safe_dict(layout),
            "char_banner": _find_active(char_gacha),
            "support_banner": _find_active(support_gacha),
            "story_events": _filter_active(story_events),
            "champions_meeting": _find_active(champ_meeting),
            "legend_race": _find_active(legend_race),
            "limited_missions": _filter_active(limited_events_raw),
        }


def char_image_url(card_id: int) -> str:
    """
    Build the gametora character card standing art URL.
    Pattern: chara_stand_{first4digits}_{card_id}.png
    e.g. card_id=103301 -> chara_stand_1033_103301.png
    """
    prefix = card_id // 100
    return f"https://gametora.com/images/umamusume/characters/chara_stand_{prefix}_{card_id}.png"


def support_image_url(support_id: int) -> str:
    """
    Build the gametora support card full art URL.
    Pattern: tex_support_card_{id}.png
    e.g. support_id=30010 -> tex_support_card_30010.png
    """
    return f"https://gametora.com/images/umamusume/supports/tex_support_card_{support_id}.png"
