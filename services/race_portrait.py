"""Fetch optional Club Race Card portraits from GameTora's trainee gallery."""

import asyncio
from base64 import b64encode
from collections import OrderedDict
from time import monotonic

import aiohttp


_CACHE_TTL = 3600
_CACHE_SIZE = 64
_MAX_BYTES = 512 * 1024
_portraits: OrderedDict[int, tuple[float, str]] = OrderedDict()


async def load_race_portrait(card_id: int | None) -> str | None:
    """Return an embeddable PNG, or None if the portrait cannot be fetched."""
    if type(card_id) is not int or not 100_000 <= card_id <= 999_999:
        return None

    cached = _portraits.get(card_id)
    if cached and monotonic() - cached[0] < _CACHE_TTL:
        _portraits.move_to_end(card_id)
        return cached[1]

    url = f"https://media.gametora.com/umamusume/characters/portrait/trainee/256/{card_id}.png"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200 or response.content_type != "image/png":
                    return None
                body = bytearray()
                async for chunk in response.content.iter_chunked(32 * 1024):
                    body.extend(chunk)
                    if len(body) > _MAX_BYTES:
                        return None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

    if not (body.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            and body.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82")):
        return None
    portrait = "data:image/png;base64," + b64encode(body).decode("ascii")
    _portraits[card_id] = (monotonic(), portrait)
    _portraits.move_to_end(card_id)
    if len(_portraits) > _CACHE_SIZE:
        _portraits.popitem(last=False)
    return portrait
