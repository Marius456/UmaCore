import asyncio, json, subprocess
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch
from services.highscore_service import HighscoreService as H

source=json.loads(Path('.codex-remote-attachments/paragon-highscore-source.json').read_text(encoding='utf-8-sig'))
class Response:
    status=200
    def __init__(self,data): self.data=data
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def json(self): return self.data
class Session:
    def __init__(self,data): self.data=data
    def get(self,url): return Response(self.data)
async def main():
    rows=[]; ranks={}; identities={}
    for c in source['months']:
        parsed,rank=await H._fetch_and_parse_api_month('514456673',c['year'],c['month'],session=Session(c['data']))
        rows.extend(parsed); ranks[(c['year'],c['month'])]=rank
        print('MONTH',c['year'],c['month'],'members',len(c['data'].get('members',[])),'rank',rank)
        for m in c['data'].get('members',[]):
            identities.setdefault(m['trainer_name'],set()).add(m['viewer_id'])
    old={}
    code=subprocess.check_output(['git','-c','safe.directory=D:/Projects/UmaCore','show','HEAD:services/highscore_service.py'],text=True,encoding='utf-8')
    exec(compile(code,'previous_highscore_service.py','exec'),old)
    for method in ['_compute_best_daily_gain','_compute_best_monthly_total','_compute_longest_first_place_streak','_compute_longest_first_place_streak_by_total']:
        print(method,'OLD',getattr(old['HighscoreService'],method)(rows),'NEW',getattr(H,method)(rows))
    rank=source['best_rank']
    for key in ('best_club_rank_date','best_monthly_rank_date'):
        if rank.get(key): rank[key]=date.fromisoformat(rank[key])
    rank['club_rank_history_start']=date.fromisoformat(source['rank_coverage']['min'])
    rank['club_rank_history_end']=date.fromisoformat(source['rank_coverage']['max'])
    with patch.object(H,'_fetch_all_months',new=AsyncMock(return_value=(rows,ranks))), patch('services.highscore_service.ClubRankHistory.get_best_rank',new=AsyncMock(return_value=rank)):
        embed=await H.generate_highscore_embed(None,'Paragon','514456673')
    print('EMBED',json.dumps(embed.to_dict(),ensure_ascii=False))
    print('DUPLICATE NAMES',{k:list(v) for k,v in identities.items() if len(v)>1})
    Path('.codex-remote-attachments/paragon-highscore-result.json').write_text(json.dumps(embed.to_dict(),ensure_ascii=False,indent=2),encoding='utf-8')
asyncio.run(main())
