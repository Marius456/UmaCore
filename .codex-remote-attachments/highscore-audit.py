"""Read-only audit of cached ENDCORE highscore API responses."""
import calendar
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from services.highscore_service import HighscoreService as H

source = json.loads(Path('.codex-remote-attachments/endcore-highscore-source.json').read_text(encoding='utf-8-sig'))
rows = []
monthly = []
leaders = defaultdict(dict)
names = defaultdict(set)
for chunk in source['months']:
    year, month = chunk['year'], chunk['month']
    last = calendar.monthrange(year, month)[1]
    for member in chunk['data'].get('members', []):
        name, vid = member['trainer_name'], str(member['viewer_id'])
        names[vid].add(name)
        valid = [(day, fans) for day, fans in enumerate(member['daily_fans'][:last], 1) if fans > 0]
        if not valid:
            continue
        for day, fans in valid:
            rows.append(dict(date=date(year, month, day), lifetime_fans=fans, trainer_name=name, trainer_id=vid))
            leaders[date(year, month, day)][vid] = (fans - valid[0][1], name)
        endpoint = member.get('next_month_start')
        if endpoint and endpoint > 0:
            rows.append(dict(date=date(year, month, last), lifetime_fans=endpoint, trainer_name=name, trainer_id=vid, is_end_of_month=True))
        total = (endpoint if endpoint and endpoint > 0 else valid[-1][1]) - valid[0][1]
        monthly.append(dict(name=name, total=total, month=f'{year}-{month:02}', start=valid[0][1], end=endpoint or valid[-1][1], first_day=valid[0][0]))
print('CURRENT ALGORITHM')
for method in ['_compute_best_daily_gain', '_compute_best_monthly_total', '_compute_longest_first_place_streak', '_compute_longest_first_place_streak_by_total']:
    print(method, getattr(H,method)(rows))
print('WITHIN-MONTH TOTALS', sorted(monthly,key=lambda r:r['total'],reverse=True)[:5])
print('KILUA', [r for r in monthly if r['name']=='Kilua'])
print('RENAMED', {vid:sorted(n) for vid,n in names.items() if len(n)>1})
best=None
previous=None
active=None
start=None
length=0
for day, gains in sorted(leaders.items()):
    high=max(v[0] for v in gains.values())
    winners=[vid for vid,v in gains.items() if v[0]==high]
    winner=winners[0] if len(winners)==1 and high>0 else None
    if winner is None:
        length=0; active=None
    elif winner == active and previous+timedelta(days=1)==day and (previous.year,previous.month)==(day.year,day.month):
        length+=1
    else:
        length=1; start=day; active=winner
    if length and (best is None or length>best['streak']):
        best=dict(name=gains[winner][1],streak=length,start=start,end=day)
    previous=day
print('REIGN WITHOUT LIFETIME TIEBREAK OR OLD-MONTH BASELINES',best)
