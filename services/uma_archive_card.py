"""Portrait rows and game grade icons rendered with the shared status-card browser."""

import asyncio
import base64
import hashlib
from collections import OrderedDict
from functools import lru_cache
from html import escape
import json
from pathlib import Path
import re
import time
import unicodedata

import aiohttp

from services.trainer_profile_service import portrait_url


def portrait_name_key(name):
    # OCR and catalogues differ on spaces and punctuation (T.M. Opera O / T M Opera O).
    normalized = unicodedata.normalize('NFKC', str(name)).casefold()
    return ''.join(character for character in normalized if character.isalnum())


@lru_cache(maxsize=1)
def portrait_ids():
    roster = Path(__file__).resolve().parents[1] / 'assets/horse_trivia/gametora_support_roster.json'
    # The archive has names, not card IDs. Use each character's original outfit portrait.
    return {portrait_name_key(row['name']): row['char_id'] * 100 + 1
            for row in json.loads(roster.read_text('utf-8'))['horses']}


def uma_portrait(name):
    return portrait_url(portrait_ids().get(portrait_name_key(name)))


def grade_icon(grade):
    grade = str(grade).strip().upper()
    tiers = ['G', 'F', 'E', 'D', 'C', 'B', 'A', 'S', 'SS']
    standard = re.fullmatch(r'(SS|[GFEDCBAS])(\+)?', grade)
    ultra = re.fullmatch(r'U([GFEDCBAS])([0-9]?)', grade)
    if standard:
        index = tiers.index(standard[1]) * 2 + bool(standard[2])
    elif ultra:
        index = 18 + tiers.index(ultra[1]) * 10 + int(ultra[2] or 0)
    else:
        return None
    return f'https://uma.moe/assets/images/icon/ranks/utx_txt_rank_{index:02d}.webp'


_cache = OrderedDict()


async def image_data(urls):
    """Fetch only our fixed asset URLs, with bounded requests and a small memory cache."""
    result = {}
    semaphore = asyncio.Semaphore(6)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
        async def fetch(url):
            cached = _cache.get(url)
            if cached and time.monotonic() - cached[0] < 600:
                result[url] = cached[1]
                _cache.move_to_end(url)
                return
            data = None
            try:
                async with semaphore, session.get(url, allow_redirects=False) as response:
                    if response.status == 200 and response.content_type == 'image/webp':
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > 512 * 1024:
                                raise ValueError('Asset too large')
                        if body[:4] == b'RIFF' and body[8:12] == b'WEBP':
                            data = 'data:image/webp;base64,' + base64.b64encode(body).decode()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass
            _cache[url] = (time.monotonic(), data)
            while len(_cache) > 256:
                _cache.popitem(last=False)
            result[url] = data

        await asyncio.gather(*(fetch(url) for url in sorted(set(urls) - {None})))
    return result


def card_html(rows, title, subtitle, footer, images, *, personal=False):
    def text(value):
        return escape(str(value)[:200])

    parts = []
    for row in rows:
        portrait = images.get(uma_portrait(row['uma_name']))
        icon = images.get(grade_icon(row['rank']))
        avatar = (f'<img src="{portrait}" alt="">' if portrait else
                  text(''.join(word[0] for word in row['uma_name'].split()[:2])))
        grade = f'<img class="grade" src="{icon}" alt="{text(row["rank"])}">' if icon else ''
        name = row['uma_name'] if personal else row['trainer_name']
        club = '' if personal else f'<span class="club"> · {text(row["club_name"])}</span>'
        detail = (text(row['variant']) if personal else
                  f'{text(row["uma_name"])} · {text(row["variant"])}')
        position = (f'#{row["position"]} / {row["participants"]}' if personal
                    else f'#{row["position"]}')
        parts.append(f'''<section class="row"><div class="position">{position}</div>
            <div class="portrait">{avatar}</div><div class="identity"><h2>{text(name)}{club}</h2>
            <div class="muted">{detail}</div></div>
            <div class="grade-box">{grade}<span>{text(row['rank'])}</span></div>
            <div class="score">{row['score']:,}</div></section>''')
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
        * {{box-sizing:border-box}} body {{margin:0;background:#0b0b0e;color:#e7e4ef;
            font-family:Arial,sans-serif}} #card {{width:1024px;padding:24px 32px}}
        header {{padding-bottom:16px;border-bottom:3px solid #2397d1;margin-bottom:16px}}
        h1 {{font-size:32px;margin:0 0 8px;overflow-wrap:anywhere}}
        .subtitle {{color:#a098b1;font-size:17px;line-height:24px;white-space:pre-line;
            overflow-wrap:anywhere}}
        .row {{display:flex;align-items:center;gap:16px;padding:10px 16px;
            border-radius:14px;height:80px;margin-bottom:6px;background:#14131a}}
        .row:nth-child(even) {{background:#1b1922}}
        .position {{width:{'160' if personal else '72'}px;flex-shrink:0;color:#f7cc35;
            font-size:{'19' if personal else '24'}px;font-weight:bold;white-space:nowrap;
            overflow:hidden;text-overflow:ellipsis}}
        .portrait {{width:60px;height:60px;flex-shrink:0;border-radius:50%;overflow:hidden;
            background:#292337;border:2px solid #2397d1;display:flex;align-items:center;
            justify-content:center;font-size:24px;color:#aca2c5}}
        .portrait img {{width:100%;height:100%;object-fit:cover;transform:scale(1.6);
            transform-origin:50% 25%}}
        .identity {{flex:1;min-width:0}} h2 {{font-size:23px;line-height:28px;margin:0 0 4px;
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
        .club {{font-size:16px;font-weight:normal;color:#a098b1}}
        .muted {{font-size:16px;line-height:20px;color:#a098b1;white-space:nowrap;
            overflow:hidden;text-overflow:ellipsis}}
        .grade-box {{width:80px;flex-shrink:0;text-align:center}} .grade {{width:76px;
            height:44px;object-fit:contain}} .grade-box span {{display:block;font-size:13px;color:#a098b1}}
        .score {{width:112px;flex-shrink:0;text-align:right;font-size:26px;font-weight:bold;color:#f7cc35}}
        footer {{margin-top:14px;font-size:15px;line-height:22px;color:#8b829d;
            overflow-wrap:anywhere}}
        </style></head><body><main id="card"><header><h1>{text(title)}</h1>
        <div class="subtitle">{text(subtitle)}</div></header>{''.join(parts)}
        <footer>{text(footer)}</footer></main></body></html>'''


_render_cache = OrderedDict()
_render_cache_lock = asyncio.Lock()
RENDER_CACHE_TTL = 300
RENDER_CACHE_MAX_BYTES = 32 * 1024 * 1024


async def render_card(rows, title, subtitle, footer, *, personal=False):
    # Cache by visible content, not user or page alone, so changed scores/ranks never
    # reuse an old image. Discord files are created separately for every upload.
    fields = ('uma_name', 'variant', 'rank', 'score', 'position', 'participants')
    if not personal:
        fields += ('trainer_name', 'club_name')
    payload = [title, subtitle, footer, personal,
               [[row[field] for field in fields] for row in rows]]
    key = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    def cached_image():
        cached = _render_cache.get(key)
        if cached and time.monotonic() - cached[0] < RENDER_CACHE_TTL:
            _render_cache.move_to_end(key)
            return cached[1]
        if cached:
            _render_cache.pop(key)
        return None

    cached = cached_image()
    if cached is not None:
        return cached
    async with _render_cache_lock:
        cached = cached_image()
        if cached is not None:
            return cached
        image = await _render_uncached(rows, title, subtitle, footer, personal=personal)
        if len(image) <= RENDER_CACHE_MAX_BYTES:
            _render_cache[key] = (time.monotonic(), image)
            while len(_render_cache) > 64 or sum(len(v[1]) for v in _render_cache.values()) > RENDER_CACHE_MAX_BYTES:
                _render_cache.popitem(last=False)
        return image


async def _render_uncached(rows, title, subtitle, footer, *, personal=False):
    from services import report_generator as reports

    urls = [url for row in rows for url in (uma_portrait(row['uma_name']), grade_icon(row['rank']))]
    try:
        images = await asyncio.wait_for(image_data(urls), timeout=6)
    except asyncio.TimeoutError:
        images = {}
    html = card_html(rows, title, subtitle, footer, images, personal=personal)

    async def render_once():
        context = await reports._get_browser_context_async()
        page = await context.new_page()
        try:
            await page.set_viewport_size({'width': 1024, 'height': 1400})
            await page.route('**/*', lambda route: route.abort())
            await page.set_content(html, wait_until='load', timeout=15000)
            await page.evaluate('''async () => {
                await document.fonts.ready;
                for (const image of document.images) {
                    try { await image.decode(); } catch { image.remove(); }
                }
            }''')
            return await page.locator('#card').screenshot(type='png', timeout=15000)
        finally:
            await page.close()

    async with reports._playwright_lock:
        try:
            return await render_once()
        except Exception:
            await reports._close_playwright_browser_unlocked_async()
            return await render_once()
