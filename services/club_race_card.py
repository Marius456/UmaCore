"""Club race card data and offline HTML rendering for the daily quota report."""

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from statistics import mean
from uuid import UUID

from config.database import db
from models import Club
from services.member_performance_card import format_fans, rank_badge
from services.quota_schedule import QuotaSchedule
from services.report_palette import REPORT_PALETTE


@dataclass
class QuotaReportHistory:
    month_end: date
    schedule: QuotaSchedule
    points: dict[UUID, dict[date, int]]
    ranks: dict[int, int]


async def load_quota_report_history(club_id, members, report_date, quota_period, effective_quota):
    month_start = report_date.replace(day=1)
    month_end = report_date.replace(day=calendar.monthrange(report_date.year, report_date.month)[1])
    default_quota = effective_quota
    requirements = []
    points = defaultdict(dict)
    if members:
        club = await Club.get_by_id(club_id)
        if club:
            default_quota = club.daily_quota
        requirements = await db.fetch(
            """SELECT effective_date, daily_quota FROM quota_requirements
               WHERE club_id = $1 AND effective_date BETWEEN $2 AND $3""",
            club_id, month_start, month_end,
        )
        snapshots = await db.fetch(
            """SELECT member_id, date, cumulative_fans FROM quota_history
               WHERE club_id = $1 AND date BETWEEN $2 AND $3 AND member_id = ANY($4::uuid[])""",
            club_id, month_start, report_date, [entry["member"].member_id for entry in members],
        )
        for snapshot in snapshots:
            points[snapshot["member_id"]][snapshot["date"]] = snapshot["cumulative_fans"]
        for entry in members:
            points[entry["member"].member_id][report_date] = entry["history"].cumulative_fans

    ranks = await db.fetch(
        """SELECT date, monthly_rank FROM club_rank_history
           WHERE club_id = $1 AND date BETWEEN $2 AND $3 AND monthly_rank > 0""",
        club_id, month_start, report_date,
    )
    return QuotaReportHistory(
        month_end, QuotaSchedule(report_date, quota_period, default_quota, requirements),
        dict(points), {row["date"].day: row["monthly_rank"] for row in ranks},
    )


@dataclass
class ClubRace:
    totals: dict[int, int]
    targets: dict[int, int]
    gains: dict[int, int]
    daily_targets: dict[int, int]
    ranks: dict[int, int]
    rank_movement: int | None
    daily_leader: tuple[str, int] | None
    monthly_leader: tuple[str, int] | None


def build_club_race(members, history, report_date, rank_data):
    """Track a fixed report roster, with gaps for incomplete daily observations."""
    roster = [entry["member"] for entry in members]
    totals, targets, gains, daily_targets = {}, {}, {}, {}
    previous_total, previous_target = 0, 0
    for day in range(1, report_date.day + 1):
        current = report_date.replace(day=day)
        eligible = [member for member in roster if member.join_date <= current]
        if not eligible:
            continue
        target = sum(history.schedule.expected(member.join_date, current) for member in eligible)
        targets[day] = target
        daily_targets[day] = target - previous_target
        previous_target = target
        values = [history.points.get(member.member_id, {}).get(current) for member in eligible]
        total = None
        if None not in values:
            total = sum(values)
            totals[day] = total
            if previous_total is not None:
                gains[day] = total - previous_total
        previous_total = total

    daily_candidates = []
    previous_date = report_date - timedelta(days=1)
    for member in roster:
        points = history.points.get(member.member_id, {})
        today = points.get(report_date)
        previous = 0 if report_date.day == 1 or member.join_date == report_date else points.get(previous_date)
        if today is not None and previous is not None:
            daily_candidates.append((member.trainer_name, today - previous))
    monthly_candidates = [
        (entry["member"].trainer_name, entry["history"].cumulative_fans) for entry in members
    ]

    ranks = {day: rank for day, rank in history.ranks.items() if day <= report_date.day}
    latest = rank_data.get("monthly_rank")
    if type(latest) is int and latest > 0:
        ranks[report_date.day] = latest
    yesterday = rank_data.get("yesterday_rank")
    if report_date.day > 1 and type(yesterday) is int and yesterday > 0:
        ranks[report_date.day - 1] = yesterday
    today_rank, prior_rank = ranks.get(report_date.day), ranks.get(report_date.day - 1)
    movement = prior_rank - today_rank if today_rank is not None and prior_rank is not None else None
    return ClubRace(
        totals, targets, gains, daily_targets, ranks, movement,
        _front_runner(daily_candidates), _front_runner(monthly_candidates),
    )


def _front_runner(candidates):
    return min(candidates, key=lambda item: (-item[1], item[0].casefold(), item[0]), default=None)


def _line_chart(day, actual, target=None):
    is_rank = target is None
    series = [("observed", actual)]
    if not is_rank:
        series.insert(0, ("goal-line", target))
    values = [value for _, points in series for value in points.values()]
    if not values:
        return '<div class="chart-empty">No history available</div>'

    width = 346 if is_rank else 932
    left, right, top, bottom = (55 if is_rank else 72), width - 14, 15, 157
    if is_rank:
        low, high = min(values), max(max(values), min(values) + 3)
    else:
        low, high = min(0, min(values)), max(1, max(values))

    def x(day_number):
        return left + (day_number - 1) * (right - left) / max(day - 1, 1)

    def y(value):
        fraction = (value - low) / (high - low)
        return top + fraction * (bottom - top) if is_rank else bottom - fraction * (bottom - top)

    marks = []
    for step in range(4):
        value = low + (high - low) * step / 3
        if is_rank:
            value = round(value)
        line_y = y(value)
        label = f"#{value:,}" if is_rank else format_fans(value)
        marks.append(f'<path class="gridline" d="M{left} {line_y:.1f}H{right}"/>')
        marks.append(f'<text x="{left - 8}" y="{line_y + 4:.1f}" text-anchor="end">{label}</text>')
    for day_number in sorted({1, (day + 1) // 2, day}):
        marks.append(f'<text x="{x(day_number):.1f}" y="{bottom + 17}" text-anchor="middle">{day_number}</text>')
    for style, points in series:
        plotted = sorted(points.items())
        paths = []
        previous = None
        for number, value in plotted:
            command = "L" if previous is not None and number == previous + 1 else "M"
            paths.append(f"{command}{x(number):.1f} {y(value):.1f}")
            previous = number
        if paths:
            marks.append(f'<path class="{style}" d="{"".join(paths)}"/>')
        if style == "observed" or len(plotted) == 1:
            marks.extend(
                f'<circle class="{style}-dot" cx="{x(number):.1f}" cy="{y(value):.1f}" r="2.7"/>'
                for number, value in plotted
            )
    label = "Club rank by day" if is_rank else "Recorded club fans and quota goal by day"
    return f'<svg viewBox="0 0 {width} 181" role="img" aria-label="{label}">{"".join(marks)}</svg>'


RHYTHM_LEGEND = (
    ("missing", "No data"), ("zero", "No gain"), ("loss", "Loss"), ("upcoming", "Upcoming"),
    ("low", "<50%"), ("near", "50–<100%"), ("on", "100–<150%"), ("high", "150%+"),
)


def _rhythm_cell(day, report_day, gain, goal):
    if day > report_day:
        return "upcoming", ""
    if gain is None:
        return "missing", "—"
    if gain == 0:
        return "zero", "0.0×"
    if gain < 0:
        return "loss", f"−{-gain / goal:.1f}×" if goal else "Loss"
    if not goal:
        return "missing", "—"
    ratio = gain / goal
    if ratio < 0.5:
        style = "low"
    elif ratio < 1:
        style = "near"
    elif ratio < 1.5:
        style = "on"
    else:
        style = "high"
    return style, f"{ratio:.1f}×"


def _rhythm_calendar(report_date, month_end, race):
    cells = [f'<b>{day}</b>' for day in calendar.day_abbr]
    cells.extend("<span></span>" for _ in range(report_date.replace(day=1).weekday()))
    for day in range(1, month_end.day + 1):
        style, value = _rhythm_cell(
            day, report_date.day, race.gains.get(day), race.daily_targets.get(day),
        )
        cells.append(f'<div class="cell {style}">{day}<span>{value}</span></div>')
    legend = "".join(
        f'<div><i class="{style}"></i>{escape(label)}</div>' for style, label in RHYTHM_LEGEND
    )
    return f'<div class="calendar">{"".join(cells)}</div><div class="calendar-legend">{legend}</div>'


def _stat(title, value, note="", color=""):
    detail = f"<small>{note}</small>" if note else ""
    return f'<section><h2>{title}</h2><strong class="{color}">{value}</strong>{detail}</section>'


def _leader(title, leader):
    name, fans = leader if leader else ("—", None)
    return (f'<section><h2>{title}</h2><div class="leader">'
            f'<span>{escape(name)}</span><strong>{format_fans(fans)}</strong></div></section>')


def race_card_html(club_name, report_date, daily_goal, status_summary, rank_data, history, race):
    day = report_date.day
    active = status_summary["active_members"]
    reporting = status_summary["total_members"]
    missing = active - reporting
    on_track = len(status_summary["on_track"])
    quota_check = f"{on_track}/{active} on target" if active else "—"
    rank = race.ranks.get(day)
    rank_text = f'<div class="rank">#{rank:,}</div>' if rank is not None else ""
    last_rank = rank_data.get("last_month_rank")
    last_month = f"Last month #{last_rank:,}" if type(last_rank) is int and last_rank > 0 else ""
    movement = race.rank_movement
    movement_text = "—" if movement is None else f"{movement:+,}" if movement else "0"
    gain = race.gains.get(day)
    pace = [value for observed_day, value in race.gains.items() if day - 6 <= observed_day <= day]
    goal_note = (
        f"Daily club goal {format_fans(race.daily_targets.get(day))} · Target based on report roster"
    )
    date_label = f"{report_date:%B} {day}, {report_date.year}"
    member_label = "member" if active == 1 else "members"
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Club Race Card</title>
<style>
{REPORT_PALETTE}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--canvas); color: var(--text); font: 600 15px Arial, sans-serif; }}
#card {{ width: 1024px; padding: 20px 25px 28px; }}
header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 18px;
          padding: 10px 16px 20px 0; border-bottom: 5px solid var(--accent); margin-bottom: 15px; }}
h1 {{ margin: 6px 0 8px; font-size: 40px; overflow-wrap: anywhere; }}
.subtitle {{ font-size: 17px; }}
.subtitle, .quota, h2, .caption, small, .rank, .calendar b, .calendar-legend {{ color: var(--muted); }}
.quota {{ margin-top: 5px; }}
.standing {{ flex: none; text-align: center; }}
.tier {{ display: block; margin: auto; width: 68px; height: 68px; object-fit: contain; }}
.tier-fallback {{ color: var(--accent); font-size: 30px; }}
.rank {{ margin-top: 2px; }}
section {{ min-width: 0; padding: 12px 15px; border-radius: 12px; background: var(--panel); }}
.metrics, .panels, .highlights {{ display: grid; gap: 12px; }}
.metrics {{ grid-template-columns: repeat(4, 1fr); }}
.metrics section {{ min-height: 86px; }}
.metrics strong {{ display: block; font-size: 29px; margin: 3px 0; }}
.positive, .leader strong, .quota-result {{ color: var(--gain); }}
.negative {{ color: var(--loss); }}
h2 {{ margin: 0; font-size: 13px; text-transform: uppercase; }}
h3 {{ margin: 0 0 5px; text-transform: uppercase; font-size: 19px; }}
small {{ display: block; font-size: 12px; }}
.caption {{ font-size: 14px; }}
.month, .panels, .highlights {{ margin-top: 14px; }}
.month-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 15px; }}
.chart-legend {{ display: flex; gap: 26px; padding: 4px 8px 0 0; color: var(--muted); font-size: 13px; }}
.chart-legend i {{ display: inline-block; width: 24px; margin-right: 6px; vertical-align: middle;
                   border-top: 2px solid var(--gain); }}
.chart-legend .goal-key {{ border-top: 2px dashed var(--goal-line); }}
svg {{ width: 100%; display: block; margin-top: 7px; }}
svg text {{ fill: var(--muted); font: 600 13px Arial, sans-serif; }}
.gridline {{ stroke: var(--grid); }}
.observed {{ fill: none; stroke: var(--gain); stroke-width: 2.5; }}
.observed-dot {{ fill: var(--gain); }}
.goal-line {{ fill: none; stroke: var(--goal-line); stroke-width: 1.8; stroke-dasharray: 14 9; }}
.goal-line-dot {{ fill: var(--goal-line); }}
.chart-empty {{ display: grid; height: 185px; place-content: center; color: var(--muted); }}
.panels {{ grid-template-columns: 1.44fr 1fr; }}
.calendar {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 5px 8px;
             margin-top: 10px; }}
.calendar b {{ text-align: center; text-transform: uppercase; font-size: 11px; }}
.cell {{ display: flex; justify-content: space-between; gap: 2px; padding: 2px 5px;
         min-height: 19px; border-radius: 4px; font-size: 12px; }}
.cell span {{ font-size: 11px; }}
.missing {{ background: #404b56; }}
.zero {{ background: #555359; }}
.loss {{ background: #632e38; color: #ffc4c0; }}
.upcoming {{ background: #232b32; color: var(--muted); }}
.low {{ background: #544520; color: #ffe5a3; }}
.near {{ background: #435126; color: #e0f1b8; }}
.on {{ background: var(--accent); color: #1e2c12; }}
.high {{ background: #216044; color: #d0f7df; }}
.calendar-legend {{ display: grid; grid-template-columns: repeat(4, 1fr); row-gap: 3px;
                    margin-top: 8px; font-size: 11px; }}
.calendar-legend i {{ display: inline-block; width: 11px; height: 11px; border-radius: 3px;
                      vertical-align: middle; margin-right: 4px; }}
.highlights {{ grid-template-columns: repeat(3, 1fr); }}
.leader {{ display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }}
.leader span {{ overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }}
.leader, .quota-result {{ display: flex; font-size: 18px; margin-top: 5px; }}
</style>
<main id="card">
  <header>
    <div>
      <h1>{escape(club_name)}</h1>
      <div class="subtitle">{date_label}</div>
      <div class="quota">{active} {member_label} · Goal {format_fans(daily_goal)}/day</div>
    </div>
    <div class="standing">{rank_badge(rank_data.get("club_rank"))}{rank_text}</div>
  </header>
  <div class="metrics">
    {_stat("Club fans", format_fans(race.totals.get(day)))}
    {_stat("Daily gain", format_fans(gain, signed=True), color=_change_color(gain))}
    {_stat("7-day pace", format_fans(mean(pace) if pace else None), f"{len(pace)}/7 days observed")}
    {_stat("Rank movement", movement_text, last_month, _change_color(movement))}
  </div>
  <section class="month">
    <div class="month-header">
      <div><h3>The month so far</h3><div class="caption">{goal_note}</div></div>
      <div class="chart-legend"><span><i></i>Recorded total</span><span><i class="goal-key"></i>Goal</span></div>
    </div>
    {_line_chart(day, race.totals, race.targets)}
  </section>
  <div class="panels">
    <section><h3>Daily club rhythm</h3><div class="caption">Daily gain / daily club goal</div>
      {_rhythm_calendar(report_date, history.month_end, race)}
    </section>
    <section><h3>Rank trajectory</h3><div class="caption">Higher on the chart = better rank</div>
      {_line_chart(day, race.ranks)}
    </section>
  </div>
  <div class="highlights">
    {_leader("Daily front-runner", race.daily_leader)}
    {_leader("Monthly front-runner", race.monthly_leader)}
    <section><h2>Quota check</h2><strong class="quota-result">{quota_check}</strong>
      <small>{missing} missing quota data</small>
    </section>
  </div>
</main></html>"""


def _change_color(value):
    if value:
        return "positive" if value > 0 else "negative"
    return ""
