"""Build and lay out the daily quota report's member-performance card."""

from base64 import b64encode
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from pathlib import Path

from config.settings import CLUB_TIERS
from services.member_status_service import daily_gains
from services.report_palette import REPORT_PALETTE


RANK_ICONS = Path(__file__).resolve().parents[1] / "assets" / "circle_ranks"


@dataclass(frozen=True)
class MemberPerformance:
    name: str
    fans: int
    target: int
    day: int
    activity: dict[date, int]
    month_average: float
    surplus: int
    required_per_day: int | None
    status: str


def build_member_performance(members, points, schedule, report_date, month_end):
    sorted_members = sorted(members, key=lambda entry: (
        -entry["history"].cumulative_fans,
        entry["member"].trainer_name.casefold(),
        str(entry["member"].member_id),
    ))
    days_remaining = (month_end - report_date).days
    month_start = report_date.replace(day=1)
    baseline_date = report_date - timedelta(days=7)
    rows = []
    for entry in sorted_members:
        member, history = entry["member"], entry["history"]
        fans = history.cumulative_fans
        month_days = max(1, (report_date - max(member.join_date, month_start)).days + 1)
        target = schedule.expected(member.join_date, month_end)
        remaining = target - fans
        if remaining <= 0:
            status = "Done"
            required_per_day = 0
        else:
            status = "Behind" if history.deficit_surplus < 0 else "Ahead"
            required_per_day = (
                (remaining + days_remaining - 1) // days_remaining if days_remaining else None
            )
        baseline_start = max(member.join_date, baseline_date)
        activity = dict(daily_gains(
            (day, total) for day, total in points.get(member.member_id, {}).items()
            if baseline_start <= day <= report_date
        ))
        rows.append(MemberPerformance(
            member.trainer_name, fans, target,
            fans - entry.get("yesterday_cumulative_fans", 0), activity, fans / month_days,
            history.deficit_surplus, required_per_day, status,
        ))
    return rows


def format_fans(value, *, signed=False):
    if value is None:
        return "—"
    sign = "−" if value < 0 else "+" if signed else ""
    magnitude = abs(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if magnitude >= divisor:
            return f"{sign}{magnitude / divisor:.2f}{suffix}"
    return f"{sign}{magnitude:.0f}"


def _sparkline(activity, report_date):
    if not activity:
        return '<span class="no-activity">—</span>'
    low, high = min(activity.values()), max(activity.values())
    points = [
        (day, 102 - (report_date - day).days * 16, 19 - (gain - low) / (high - low or 1) * 15)
        for day, gain in activity.items()
    ]
    marks = ['<path class="spark-base" d="M6 20H102"/>']
    for (day, x, y), (next_day, next_x, next_y) in zip(points, points[1:]):
        if next_day - day == timedelta(days=1):
            marks.append(f'<path class="spark-line" d="M{x} {y:.2f}L{next_x} {next_y:.2f}"/>')
    marks.extend(f'<circle cx="{x}" cy="{y:.2f}" r="1.35"/>' for _, x, y in points)
    return (
        f'<svg viewBox="0 0 108 24" aria-hidden="true">{"".join(marks)}</svg>'
        f'<small>{len(activity)}/7 days</small>'
    )


def rank_badge(tier_code):
    badge = ""
    if type(tier_code) is int and 1 <= tier_code <= len(CLUB_TIERS):
        tier = CLUB_TIERS[tier_code - 1]
        icon_path = RANK_ICONS / f"utx_ico_circle_rank_{tier_code:02d}.webp"
        try:
            icon = b64encode(icon_path.read_bytes()).decode("ascii")
        except FileNotFoundError:
            badge = f'<span class="tier-fallback">{tier}</span>'
        else:
            badge = f'<img class="tier" alt="{tier}" src="data:image/webp;base64,{icon}">'
    return badge


def card_html(club_name, report_date, count, daily_goal, rank_data, rows, start_index):
    rank_data = rank_data or {}
    badge = rank_badge(rank_data.get("club_rank"))
    rank = rank_data.get("monthly_rank")
    rank_text = f'<div class="rank">#{rank:,}</div>' if rank is not None else ""
    table_rows = []
    for index, row in enumerate(rows, start_index):
        status_class = row.status.lower()
        progress = 100 if row.target <= 0 else min(100, max(0, row.fans / row.target * 100))
        pace_class = "negative" if row.surplus < 0 else "positive"
        table_rows.append(f"""
            <tr class="{status_class}">
              <td class="position">{index}</td>
              <th class="member" scope="row">{escape(row.name)}</th>
              <td class="target">
                <div>{format_fans(row.fans)} / {format_fans(row.target)}</div>
                <div class="track"><div class="progress" style="width:{progress:.2f}%"></div></div>
              </td>
              <td>{format_fans(row.day, signed=True)}</td>
              <td class="activity">{_sparkline(row.activity, report_date)}</td>
              <td>{format_fans(row.month_average)}</td>
              <td class="{pace_class}">{format_fans(row.surplus, signed=True)}</td>
              <td>{format_fans(row.required_per_day)}</td>
              <td><span class="pill">{row.status}</span></td>
            </tr>""")

    date_label = f"{report_date:%B} {report_date.day}, {report_date.year}"
    member_label = "member" if count == 1 else "members"
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Member performance</title>
<style>
{REPORT_PALETTE}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--canvas); color: var(--text); font: 600 16px Arial, sans-serif; }}
#card {{ width: 1000px; padding: 14px 25px 35px; }}
header {{ display: flex; justify-content: space-between; align-items: flex-start;
          border-bottom: 5px solid var(--accent); padding-bottom: 8px; margin-bottom: 14px; gap: 16px; }}
h1 {{ margin: 0 0 4px; font-size: 30px; overflow-wrap: anywhere; }}
.subtitle, .goal, .rank, small {{ color: var(--muted); }}
.subtitle {{ font-size: 16px; text-transform: uppercase; }}
.goal {{ margin-top: 4px; font-size: 14px; }}
.standing {{ margin: 3px 13px 0 0; text-align: center; }}
.tier {{ display: block; margin: auto; width: 68px; height: 68px; object-fit: contain; }}
.tier-fallback {{ color: var(--accent); font-size: 30px; }}
.rank {{ margin-top: 3px; font-size: 14px; }}
table {{ width: 100%; table-layout: fixed; border-spacing: 0 1px; }}
th, td {{ padding: 0 5px; white-space: nowrap; text-align: center; }}
thead th {{ height: 27px; background: #283823; color: var(--accent);
            font-size: 12px; text-transform: uppercase; }}
thead th:first-child {{ border-radius: 4px 0 0 4px; }}
thead th:last-child {{ border-radius: 0 4px 4px 0; }}
tbody th, tbody td {{ height: 51px; background: var(--panel); }}
tbody tr:nth-child(even) > * {{ background: #20282e; }}
.member, .target, .activity {{ text-align: left; }}
th.member {{ overflow: hidden; text-overflow: ellipsis; }}
.target {{ padding-right: 14px; }}
.position, .positive {{ color: var(--gain); }}
.negative, .behind .position {{ color: var(--loss); }}
.track {{ height: 6px; width: 100%; margin-top: 9px; border-radius: 5px; background: var(--grid); }}
.progress {{ height: 100%; border-radius: inherit; background: var(--gain); }}
.activity svg {{ display: block; width: 100%; height: 24px; fill: var(--gain); }}
.spark-base {{ stroke: var(--grid); stroke-width: .7; }}
.spark-line {{ fill: none; stroke: var(--gain); stroke-width: 1.3; }}
small {{ display: block; margin: 0 0 2px 4px; font-size: 10px; font-style: italic; }}
.no-activity {{ display: block; text-align: center; color: var(--muted); }}
.pill {{ display: inline-block; width: 84px; padding: 3px 0; border-radius: 20px;
         background: #193a40; color: #b0e7e8; font-size: 12px; }}
.done .pill {{ background: #215c39; color: #b2f2c8; }}
.behind .pill {{ background: #4c2a31; color: #ffc4c0; }}
</style>
<main id="card">
  <header>
    <div>
      <h1>{escape(club_name)}</h1>
      <div class="subtitle">{date_label}</div>
      <div class="goal">{count} {member_label} · Goal {format_fans(daily_goal)}/day</div>
    </div>
    <div class="standing">{badge}{rank_text}</div>
  </header>
  <table aria-label="Member performance">
    <colgroup>
      <col style="width:3%"><col style="width:17%"><col style="width:21%">
      <col style="width:9%"><col style="width:11%"><col style="width:9%">
      <col style="width:10%"><col style="width:9%"><col style="width:11%">
    </colgroup>
    <thead><tr>
      <th scope="col">#</th><th scope="col" class="member">Member</th>
      <th scope="col" class="target">Month / Target</th><th scope="col">Day</th>
      <th scope="col" class="activity">7D Activity</th><th scope="col">Month Avg</th>
      <th scope="col">Vs Target</th><th scope="col">Req/Day</th><th scope="col">Status</th>
    </tr></thead>
    <tbody>{"".join(table_rows)}</tbody>
  </table>
</main></html>"""
