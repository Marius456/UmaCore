import asyncio, json
import aiohttp
from config.settings import UMAMOE_API_KEY, DATABASE_URL
from config.database import db
from models import ClubRankHistory
from uuid import UUID
async def main():
    result = {'months': []}
    async with aiohttp.ClientSession(headers={'X-API-Key': UMAMOE_API_KEY}, timeout=aiohttp.ClientTimeout(total=30)) as s:
        for y,m in [(2026,m) for m in range(9,0,-1)] + [(2025,m) for m in range(12,5,-1)]:
            async with s.get(f'https://uma.moe/api/v4/circles?circle_id=168472480&year={y}&month={m}') as r:
                r.raise_for_status()
                result['months'].append({'year':y,'month':m,'data':await r.json()})
    db.url = DATABASE_URL
    await db.connect()
    result['best_rank'] = await ClubRankHistory.get_best_rank(UUID('a0ba3dcf-fc85-4c66-8bb2-baeb5572f737'))
    result['rank_coverage'] = dict(await db.fetchrow('SELECT min(date), max(date), count(*) FROM club_rank_history WHERE club_id=$1', UUID('a0ba3dcf-fc85-4c66-8bb2-baeb5572f737')))
    await db.disconnect()
    print(json.dumps(result,default=str))
asyncio.run(main())

