"""Optional uma.moe profile enrichment; failures must not hide local quota data."""
import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import time

import aiohttp

from config.settings import UMAMOE_API_KEY

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrainerProfile:
    data: dict
    fetched_at: datetime
    portrait: str | None = None


def portrait_url(card_id) -> str | None:
    # Verified against uma-moe/umamoe-frontend profile-helpers.ts:getCardImage
    # and the public character catalog. The deployed assets use WebP.
    if type(card_id) is not int or not 100000 <= card_id <= 999999:
        return None
    return f"https://uma.moe/assets/images/character_stand/chara_stand_{card_id}.webp"


def profile_portrait_url(data) -> str | None:
    """Resolve the separate dress/card namespaces without guessing outfit numbers.

    Profile veterans explicitly pair race_cloth_id with card_id. When no unique
    match exists, use inheritance.main_parent_id, as uma.moe's profile header does.
    Source: uma-moe/umamoe-frontend profile-header/profile-header.component.ts.
    """
    trainer = data.get("trainer")
    dress_id = trainer.get("leader_chara_dress_id") if isinstance(trainer, dict) else None
    veterans = data.get("veterans")
    candidates = {
        v["card_id"] for v in veterans
        if isinstance(v, dict) and type(dress_id) is int
        and v.get("race_cloth_id") == dress_id and portrait_url(v.get("card_id"))
    } if isinstance(veterans, list) else set()
    if len(candidates) == 1:
        return portrait_url(candidates.pop())
    inheritance = data.get("inheritance")
    return portrait_url(inheritance.get("main_parent_id")) if isinstance(inheritance, dict) else None


class TrainerProfileClient:
    TTL = 300
    MAX_ENTRIES = 128
    MAX_PROFILE_BYTES = 4 * 1024 * 1024
    MAX_PORTRAIT_BYTES = 512 * 1024

    def __init__(self):
        self._cache = OrderedDict()
        self._lock = asyncio.Lock()

    async def fetch(self, trainer_id: str | None) -> TrainerProfile | None:
        trainer_id = str(trainer_id or "")
        if not trainer_id.isascii() or not trainer_id.isdigit():
            return None
        async with self._lock:
            cached = self._cache.get(trainer_id)
            if cached and time.monotonic() - cached[0] < self.TTL:
                self._cache.move_to_end(trainer_id)
                return cached[1]
            result = None
            try:
                # Bound the entire enrichment, including the optional image.
                result = await asyncio.wait_for(self._fetch(trainer_id), timeout=10)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
                logger.warning("Trainer profile unavailable for %s", trainer_id)
            self._cache[trainer_id] = (time.monotonic(), result)
            self._cache.move_to_end(trainer_id)
            while len(self._cache) > self.MAX_ENTRIES:
                self._cache.popitem(last=False)
            return result

    @staticmethod
    async def _read_limited(response, limit):
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > limit:
                raise ValueError("Profile asset exceeds size limit")
        return bytes(body)

    async def _fetch(self, trainer_id):
        import json

        headers = {"X-API-Key": UMAMOE_API_KEY} if UMAMOE_API_KEY else {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(
                f"https://uma.moe/api/v4/user/profile/{trainer_id}",
                headers=headers, allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return None
                data = json.loads(await self._read_limited(response, self.MAX_PROFILE_BYTES))
            if not isinstance(data, dict) or not isinstance(data.get("trainer"), dict):
                return None
            if str(data["trainer"].get("account_id")) != trainer_id:
                return None
            fetched_at = datetime.now(timezone.utc)
            portrait = None
            url = profile_portrait_url(data)
            if url:
                try:
                    # Do not send the API key to asset requests.
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=2), allow_redirects=False,
                    ) as response:
                        if response.status == 200 and response.content_type == "image/webp":
                            body = await self._read_limited(response, self.MAX_PORTRAIT_BYTES)
                            if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
                                portrait = "data:image/webp;base64," + base64.b64encode(body).decode()
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
            # Keep only data consumed by the card, not the potentially large veteran roster.
            return TrainerProfile(
                {key: data.get(key) for key in ("trainer", "fan_history", "circle_history")},
                fetched_at, portrait,
            )


profile_client = TrainerProfileClient()
