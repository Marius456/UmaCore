"""
Leaderboard Report Service — generates a "news segment" embed analyzing
today's leaderboard position changes (with month-long context for
rivalries and leader changes).
"""
import logging
import calendar
from collections import defaultdict
from datetime import date
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple, NamedTuple
from enum import Enum
from uuid import UUID

import discord

from config.settings import COLOR_INFO
from models import QuotaHistory

logger = logging.getLogger(__name__)


# --- Data Containers for Type Safety ---

class Overtake(NamedTuple):
    challenger: str
    target: str
    gap_fans: int
    daily_diff: int
    eta_days: float
    target_rank: int
    drama_score: float           # composite: smaller gap + faster rate = more dramatic

class EfficiencyKing(NamedTuple):
    name: str
    daily_gain: int
    avg_daily: float
    pct_above_avg: float

class TankAnalysis(NamedTuple):
    name: str
    name_2nd: str
    surplus: int
    gap_to_next: int
    daily_gain: int
    daily_gain_2nd: int
    streak: int
    is_dynasty: bool
    rank: int
    pressure_streak: int                   # days #2 has outgained #1 consecutively
    gap_trend: str                         # "shrinking" | "holding" | "expanding"
    eta_days: Optional[float]              # days until overtake, None if not closing
    leader_above_avg: bool                 # is #1's daily gain above their monthly avg?

class Milestone(NamedTuple):
    name: str
    total: int
    milestone: int
    amount_away: int
    pct_to_milestone: float

class ConsistencyResult(NamedTuple):
    top_overperformer: Optional[Dict[str, Any]]
    top_cooler: Optional[Dict[str, Any]]
    overperformer_count: int
    cooler_count: int

class BestWeek(NamedTuple):
    name: str
    avg_daily: float
    days: int

class StreakInfo(NamedTuple):
    name: str
    streak_type: str          # "daily_activity" | "over_2m" | "pb_streak" | "top_daily"
    current_streak: int
    description: str          # Human-readable: "6 consecutive days over 2M fans"

class Achievement(NamedTuple):
    name: str
    achievement_type: str     # "club_mvp" | "funny" | "rare"
    title: str                # "Club MVP", "Rocket Launch", etc.
    description: str          # Human-readable narrative


class HeadlineType(Enum):
    COMEBACK = "comeback"
    DOMINATION = "domination"
    UPSET = "upset"
    CLUTCH = "clutch"
    STREAK = "streak"
    CHAOS = "chaos"
    BREAKOUT = "breakout"
    QUIET = "quiet"


class BotMood(Enum):
    PEACEFUL = "peaceful"       # Few changes
    CHAOTIC = "chaotic"         # Many changes
    INTENSE = "intense"         # Leader under attack
    IMPRESSED = "impressed"     # Huge grinder
    NEUTRAL = "neutral"         # Default


class LeaderboardReportService:
    """Generates a rich 'sports broadcast' style news report for club activity."""

    MILESTONES = [1_000_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000, 100_000_000]

    # ── Public entry point ──────────────────────────────────────────────

    @classmethod
    async def generate_leaderboard_report(
        cls, club_id: UUID, club_name: str, year: int, month: int
    ) -> discord.Embed:
        """
        Fetch the month's QuotaHistory, compute all analysis segments,
        and return a single rich Embed.
        """
        rows = await QuotaHistory.get_current_month_for_club(club_id, year, month)
        if not rows:
            raise ValueError(
                f"No QuotaHistory data available for {club_name} in "
                f"{calendar.month_name[month]} {year}."
            )

        # 1. Prepare Data
        daily_rankings = cls._build_daily_rankings(rows)
        daily_deltas = cls._compute_daily_deltas(rows)
        latest_date = max(daily_rankings.keys())
        yesterday_date = cls._get_previous_day(daily_rankings, latest_date)
        member_count = len({row["trainer_name"] for row in rows})

        # 2. Run Analytics
        # These are broken into small, testable methods
        movers = cls._compute_today_movers(daily_rankings, latest_date)
        leader_change = cls._compute_leader_change(daily_rankings, latest_date, yesterday_date)
        rivalries = cls._compute_rivalries(daily_rankings, latest_date)
        today_records = cls._compute_today_records(daily_deltas, latest_date)
        club_record = cls._compute_club_record(daily_deltas)

        king = cls._compute_efficiency_king(daily_rankings, daily_deltas, latest_date)
        tank = cls._compute_tank_analysis(daily_rankings, latest_date, daily_deltas)
        overtakes = cls._compute_projected_overtakes(daily_rankings, latest_date)
        milestones = cls._compute_milestone_watch(daily_rankings, latest_date)
        consistency = cls._compute_consistency(daily_rankings, daily_deltas, latest_date)
        best_week = cls._compute_best_week(daily_deltas, latest_date)

        # Sprinter = the member who gained the most fans today
        daily_leader = max(daily_rankings.get(latest_date, []), key=lambda e: e["daily"], default=None)

        # Phase 2: New analytics
        streaks = cls._compute_streaks(daily_rankings, daily_deltas, latest_date)
        club_activity = cls._compute_club_activity(daily_rankings, latest_date)
        mvp = cls._compute_club_mvp(king, tank, daily_leader, consistency, latest_date, today_records)
        funny_awards = cls._compute_funny_awards(daily_rankings, daily_deltas, latest_date)
        yesterday_results = cls._compute_yesterday_results(daily_rankings, overtakes, latest_date)
        teaser = cls._generate_teaser(overtakes, milestones, tank, leader_change, daily_deltas)
        month_name = calendar.month_name[month]
        # Add rivalry context to top rivalry
        if rivalries:
            rivalries[0]["is_top"] = True
            rivalries[0]["month_context"] = month_name

        # Compute milestone ETAs
        milestone_etas = {}
        for m in milestones:
            eta = cls._compute_milestone_eta(m.name, m.total, m.milestone, daily_deltas)
            if eta is not None:
                milestone_etas[m.name] = eta

        # Phase 3: Mood, rare achievements
        mood = cls._determine_mood(movers, leader_change, len(overtakes))
        rare_achievements = cls._compute_rare_achievements(daily_rankings, daily_deltas, club_record, latest_date)
        all_awards = (funny_awards or []) + (rare_achievements or [])
        total_movers_count = len(movers.get("climbers", [])) + len(movers.get("fallers", []))

        # 3. Assemble Embed
        embed = discord.Embed(
            title=f"🥕 Leaderboard News — {club_name}",
            description=(
                f"**{date(year, month, 1).strftime('%B %Y')}** · {member_count} members\n"
                f"_{cls._fmt_date(latest_date)} update_"
            ),
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )

        # --- Section 1: Headline ---
        headline = cls._generate_headline(leader_change, king, tank, daily_rankings.get(latest_date), mood, overtakes, total_movers_count)
        embed.add_field(name="🔥 HEADLINE NEWS", value=headline + "\n\n───", inline=False)

        # --- Section 2: Momentum ---
        momentum = cls._assemble_momentum(
            king, tank, consistency, today_records, club_record, latest_date,
            daily_leader, leader_change, best_week=best_week,
            streaks=streaks, club_activity=club_activity, mvp=mvp,
            funny_awards=funny_awards,
        )
        embed.add_field(name="THE MOMENTUM SHIFT", value=(momentum or "_Stable activity today._") + "\n\n───", inline=False)

        # --- Section 3: Battle Zone ---
        battles = cls._assemble_battle_zone(overtakes, rivalries, yesterday_results, month_name)
        if battles:
            embed.add_field(name="THE BATTLE ZONE", value=battles + "\n\n───", inline=False)

        # --- Section 4: Milestones ---
        milestone_text = cls._format_milestone_watch(milestones, milestone_etas)
        if milestones:
            embed.add_field(name="MILESTONE TRACKER", value=milestone_text + "\n\n───", inline=False)

        # --- Section 5: Movers ---
        condensed = cls._compute_condensed_movers(movers)
        if condensed:
            embed.add_field(name="TOP MOVERS", value=cls._format_condensed_movers(condensed), inline=False)

        # --- Section 6: Teaser ---
        if teaser:
            embed.add_field(name="LOOKING AHEAD", value=teaser, inline=False)

        embed.set_footer(text=f"{club_name} · Powering Through {month_name}")
        return embed

    # --- Computation Logic ---

    @classmethod
    def _compute_efficiency_king(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        daily_deltas: Dict[str, List[Dict]],
        latest_date: date,
    ) -> Optional[EfficiencyKing]:
        candidates = []
        today_map = {e["name"]: e for e in daily_rankings.get(latest_date, [])}

        for name, m_deltas in daily_deltas.items():
            if name not in today_map or not m_deltas:
                continue

            # Filter 1: Representative Sample — need at least 3 days of data
            if len(m_deltas) < 3:
                continue

            avg = sum(d["delta"] for d in m_deltas) / len(m_deltas)
            today_gain = today_map[name]["daily"]

            # Filter 2: Volume Floor — must have gained at least 1M fans today
            if today_gain < 1_000_000:
                continue

            # Filter 3: No Deficit — must be above quota (positive surplus)
            if today_map[name]["surplus"] < 0:
                continue

            if avg > 0 and today_gain > 0:
                pct = ((today_gain - avg) / avg) * 100
                if pct > 0:
                    candidates.append(EfficiencyKing(name, today_gain, avg, round(pct, 1)))

        if not candidates:
            return None
        return max(candidates, key=lambda x: x.pct_above_avg)

    @classmethod
    def _compute_tank_analysis(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        latest_date: date,
        daily_deltas: Dict[str, List[Dict]],
    ) -> Optional[TankAnalysis]:
        entries = daily_rankings.get(latest_date, [])
        if len(entries) < 2:
            return None

        # Get #1 and #2 by rank (entries are already sorted by fans descending)
        rank_1 = entries[0]
        rank_2 = entries[1]

        name = rank_1["name"]
        name_2nd = rank_2["name"]
        surplus = rank_1["surplus"]
        gap_to_next = rank_1["fans"] - rank_2["fans"]
        daily_gain = rank_1["daily"]
        daily_gain_2nd = rank_2["daily"]

        # Compute streak: consecutive days this member held #1
        sorted_dates = sorted(daily_rankings.keys())
        streak = 0
        for d in reversed(sorted_dates):
            if d == latest_date:
                continue  # skip today, we're counting previous days
            top = daily_rankings[d][0]["name"] if daily_rankings.get(d) else None
            if top == name:
                streak += 1
            else:
                break

        is_dynasty = streak >= 5

        # ── New analytics ────────────────────────────────────────────────

        # pressure_streak: consecutive days (including today) where #2 outgained #1
        pressure_streak = 0
        for d in reversed(sorted_dates):
            day_entries = daily_rankings.get(d, [])
            if len(day_entries) >= 2:
                top1 = day_entries[0]
                # Find the current #2 challenger at whatever rank they were on this previous day
                challenger = next((e for e in day_entries if e["name"] == name_2nd), None)
                if top1["name"] == name and challenger is not None and challenger["daily"] > top1["daily"]:
                    pressure_streak += 1
                else:
                    break
            else:
                break

        # gap_trend: compare today's gap to yesterday's gap
        yesterday_date = cls._get_previous_day(daily_rankings, latest_date)
        gap_trend = "holding"
        if yesterday_date is not None:
            y_entries = daily_rankings.get(yesterday_date, [])
            if len(y_entries) >= 2:
                y_top1 = y_entries[0]
                y_top2 = y_entries[1]
                # Find today's #1 and #2 in yesterday's data
                y_fans_1 = next((e["fans"] for e in y_entries if e["name"] == name), None)
                y_fans_2 = next((e["fans"] for e in y_entries if e["name"] == name_2nd), None)
                if y_fans_1 is not None and y_fans_2 is not None:
                    y_gap = y_fans_1 - y_fans_2
                    if gap_to_next < y_gap - 1000:  # buffer for rounding
                        gap_trend = "shrinking"
                    elif gap_to_next > y_gap + 1000:
                        gap_trend = "expanding"

        # eta_days: projected days until overtake at current net rate
        net_chase_rate = daily_gain_2nd - daily_gain
        eta_days: Optional[float] = None
        if net_chase_rate > 0 and gap_to_next > 0:
            eta_days = round(gap_to_next / net_chase_rate, 1)

        # leader_above_avg: is #1's daily gain above their monthly average?
        leader_above_avg = False
        leader_deltas = daily_deltas.get(name)
        if leader_deltas:
            avg = sum(d["delta"] for d in leader_deltas) / len(leader_deltas)
            leader_above_avg = daily_gain > avg

        return TankAnalysis(
            name=name,
            name_2nd=name_2nd,
            surplus=surplus,
            gap_to_next=gap_to_next,
            daily_gain=daily_gain,
            daily_gain_2nd=daily_gain_2nd,
            streak=streak,
            is_dynasty=is_dynasty,
            rank=1,
            pressure_streak=pressure_streak,
            gap_trend=gap_trend,
            eta_days=eta_days,
            leader_above_avg=leader_above_avg,
        )

    @classmethod
    def _compute_projected_overtakes(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        latest_date: date,
    ) -> List[Overtake]:
        entries = daily_rankings.get(latest_date, [])
        results = []
        for i in range(len(entries) - 1):
            above, below = entries[i], entries[i + 1]
            gap = above["fans"] - below["fans"]
            rate_diff = below["daily"] - above["daily"]

            if rate_diff > 0:
                eta = gap / rate_diff
                if 0 < eta <= 14:
                    # Drama score: smaller gap + faster rate = more dramatic
                    # Normalize: gap in millions, rate in millions
                    gap_m = gap / 1_000_000
                    rate_m = rate_diff / 1_000_000
                    # Higher score = more dramatic (inverse of gap, scaled by rate)
                    drama = max(0, (1.0 / (gap_m + 0.1)) * min(rate_m, 5.0))
                    results.append(
                        Overtake(below["name"], above["name"], gap, rate_diff, round(eta, 1), above["rank"], round(drama, 2))
                    )

        # Sort by drama score descending (most dramatic first)
        return sorted(results, key=lambda x: x.drama_score, reverse=True)[:3]

    @classmethod
    def _compute_rivalries(
        cls, daily_rankings: Dict[date, List[Dict]], latest_date: date
    ) -> List[Dict[str, Any]]:
        """
        Finds 'true' rivalries: pairs of members who are close in rank
        and have swapped positions multiple times this month.
        """
        # 1. Prepare lookups
        sorted_dates = sorted(daily_rankings.keys())
        # member_ranks[name][date] = rank
        member_ranks: Dict[str, Dict[date, int]] = defaultdict(dict)
        # latest_info[name] = {fans, rank}
        latest_info: Dict[str, Dict] = {}

        for d, entries in daily_rankings.items():
            for e in entries:
                member_ranks[e["name"]][d] = e["rank"]
                if d == latest_date:
                    latest_info[e["name"]] = e

        # 2. Analyze pairs
        all_members = list(member_ranks.keys())
        rivalry_stats = []

        # We only care about pairs that exist on the latest date
        active_pairs = list(combinations([m for m in all_members if m in latest_info], 2))

        for a, b in active_pairs:
            swaps = 0
            days_in_proximity = 0

            # Check every consecutive pair of days for a swap
            for i in range(1, len(sorted_dates)):
                d_prev = sorted_dates[i-1]
                d_curr = sorted_dates[i]

                # Both must have data on both days
                if not all(d in member_ranks[m] for d in (d_prev, d_curr) for m in (a, b)):
                    continue

                rank_a_prev, rank_b_prev = member_ranks[a][d_prev], member_ranks[b][d_prev]
                rank_a_curr, rank_b_curr = member_ranks[a][d_curr], member_ranks[b][d_curr]

                # Proximity Filter: They must be within 3 ranks of each other
                # to be considered "fighting" for that spot.
                if abs(rank_a_prev - rank_b_prev) <= 3:
                    days_in_proximity += 1
                    # Did they swap?
                    if (rank_a_prev < rank_b_prev) != (rank_a_curr < rank_b_curr):
                        swaps += 1

            # A rivalry is only valid if they swapped at least twice
            # and spent time near each other.
            if swaps >= 1:
                info_a = latest_info[a]
                info_b = latest_info[b]

                # Current Proximity Gate: They must still be close in rank today
                # to be considered an active rivalry.
                current_rank_diff = abs(info_a["rank"] - info_b["rank"])
                if current_rank_diff > 3:
                    continue

                # Determine who is currently winning
                if info_a["rank"] < info_b["rank"]:
                    who_leads, gap = a, info_a["fans"] - info_b["fans"]
                else:
                    who_leads, gap = b, info_b["fans"] - info_a["fans"]

                # Rivalry Score: Priority given to high swap counts
                # and small current rank gaps.
                current_rank_diff = abs(info_a["rank"] - info_b["rank"])
                score = (swaps * 100) + (days_in_proximity * 10) - (current_rank_diff * 5)

                rivalry_stats.append({
                    "name_a": a,
                    "name_b": b,
                    "swap_count": swaps,
                    "rank_range": (min(info_a["rank"], info_b["rank"]), max(info_a["rank"], info_b["rank"])),
                    "rank_a": info_a["rank"],
                    "rank_b": info_b["rank"],
                    "who_leads": who_leads,
                    "fan_gap": gap,
                    "score": score
                })

        # Sort by the computed score and return top 3
        rivalry_stats.sort(key=lambda x: x["score"], reverse=True)
        return rivalry_stats[:3]

    @classmethod
    def _compute_milestone_watch(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        latest_date: date,
    ) -> List[Milestone]:
        """Check every member on the latest_date. If their Total is within 5%
        of a major milestone, add to the watch list."""
        today_entries = daily_rankings.get(latest_date, [])
        watch: List[Milestone] = []

        for entry in today_entries:
            total = entry["fans"]
            for ms in cls.MILESTONES:
                if total >= ms:
                    continue
                # Check if within 5% of this milestone
                if total >= ms * 0.95:
                    watch.append(
                        Milestone(
                            name=entry["name"],
                            total=total,
                            milestone=ms,
                            amount_away=ms - total,
                            pct_to_milestone=round((total / ms) * 100, 1),
                        )
                    )
                    break  # only the *next* milestone per player

        watch.sort(key=lambda w: w.amount_away)
        return watch[:3]

    @classmethod
    def _compute_consistency(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        daily_deltas: Dict[str, List[Dict]],
        latest_date: date,
    ) -> ConsistencyResult:
        """For each member with data on the latest date, compare Daily to Avg.
        Returns the most extreme overperformer and most extreme cooler
        by percentage difference, plus counts."""
        today_entries = {e["name"]: e for e in daily_rankings.get(latest_date, [])}
        overperformers: List[Dict] = []
        coolers: List[Dict] = []

        for name, deltas in daily_deltas.items():
            if name not in today_entries:
                continue
            if not deltas:
                continue
            total_delta = sum(d["delta"] for d in deltas)
            avg_daily = total_delta / len(deltas)
            if avg_daily <= 0:
                continue
            today_daily = today_entries[name]["daily"]
            pct_diff = ((today_daily - avg_daily) / avg_daily) * 100

            # Skip inactive/no-play entries: no gain today or exactly -100%
            if today_daily <= 0 or pct_diff == -100.0:
                continue

            record = {
                "name": name,
                "daily": today_daily,
                "avg": round(avg_daily, 1),
                "pct_diff": round(pct_diff, 1),
            }

            if pct_diff > 0:
                overperformers.append(record)
            elif pct_diff >= -90:  # meaningful underperformance (not -100% idle)
                coolers.append(record)

        overperformers.sort(key=lambda x: x["pct_diff"], reverse=True)
        coolers.sort(key=lambda x: x["pct_diff"])  # most negative first

        return ConsistencyResult(
            top_overperformer=overperformers[0] if overperformers else None,
            top_cooler=coolers[0] if coolers else None,
            overperformer_count=len(overperformers),
            cooler_count=len(coolers),
        )

    @classmethod
    def _compute_best_week(
        cls,
        daily_deltas: Dict[str, List[Dict]],
        latest_date: date,
    ) -> Optional[BestWeek]:
        """
        Find the member with the highest average daily fan gain over the
        last 7 days (rolling window ending on latest_date).

        Members with fewer than 2 data points in the window are excluded
        to avoid skewed averages.
        """
        from datetime import timedelta
        window_start = latest_date - timedelta(days=6)

        best: Optional[BestWeek] = None

        for name, deltas in daily_deltas.items():
            # Filter deltas to the 7-day window
            window_deltas = [d for d in deltas if window_start <= d["date"] <= latest_date]
            if len(window_deltas) < 2:
                continue

            avg = sum(d["delta"] for d in window_deltas) / len(window_deltas)
            if avg <= 0:
                continue

            avg_rounded = round(avg, 1)
            if best is None or avg_rounded > best.avg_daily:
                best = BestWeek(name=name, avg_daily=avg_rounded, days=len(window_deltas))

        return best

    # ── Phase 2: New Computation Methods ──────────────────────────────────

    @classmethod
    def _compute_milestone_eta(
        cls,
        name: str,
        total: int,
        milestone: int,
        daily_deltas: Dict[str, List[Dict]],
    ) -> Optional[float]:
        """Project days until milestone at current pace (last 7 days avg)."""
        from datetime import timedelta
        deltas = daily_deltas.get(name, [])
        if not deltas:
            return None
        # Use recent deltas (last 7 days) for more relevant pace
        latest_date = max(d["date"] for d in deltas)
        window_start = latest_date - timedelta(days=6)
        recent = [d for d in deltas if window_start <= d["date"] <= latest_date and d["delta"] > 0]
        if len(recent) < 2:
            recent = [d for d in deltas if d["delta"] > 0]
        if not recent:
            return None
        avg_daily = sum(d["delta"] for d in recent) / len(recent)
        amount_away = milestone - total
        if avg_daily <= 0 or amount_away <= 0:
            return None
        return round(amount_away / avg_daily, 1)

    @classmethod
    def _compute_streaks(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        daily_deltas: Dict[str, List[Dict]],
        latest_date: date,
    ) -> List[StreakInfo]:
        """Detect active streaks: consecutive days active, over 2M, PBs, top daily."""
        sorted_dates = sorted(daily_rankings.keys())
        streaks: List[StreakInfo] = []

        if len(sorted_dates) < 2:
            return streaks

        today_map = {e["name"]: e for e in daily_rankings.get(latest_date, [])}

        for name, deltas in daily_deltas.items():
            if name not in today_map:
                continue
            # Sort deltas by date ascending for streak computation
            deltas_by_date = sorted(deltas, key=lambda d: d["date"])
            if len(deltas_by_date) < 2:
                continue

            # Streak: consecutive days over 2M
            over_2m_streak = 0
            for d in reversed(deltas_by_date):
                if d["delta"] >= 2_000_000:
                    over_2m_streak += 1
                else:
                    break
            if over_2m_streak >= 2:
                streaks.append(StreakInfo(
                    name=name,
                    streak_type="over_2m",
                    current_streak=over_2m_streak,
                    description=f"{over_2m_streak} consecutive days over 2M fans",
                ))

        # Top streak: most consecutive days with any activity (top 1)
        active_streaks = []
        for name, deltas in daily_deltas.items():
            deltas_by_date = sorted(deltas, key=lambda d: d["date"])
            streak = 0
            for d in reversed(deltas_by_date):
                if d["delta"] > 0:
                    streak += 1
                else:
                    break
            if streak >= 3:
                active_streaks.append((name, streak))
        active_streaks.sort(key=lambda x: x[1], reverse=True)
        if active_streaks:
            name, streak = active_streaks[0]
            streaks.append(StreakInfo(
                name=name,
                streak_type="daily_activity",
                current_streak=streak,
                description=f"{streak} consecutive active days",
            ))

        return streaks[:3]  # Show top 3 most interesting streaks

    @classmethod
    def _compute_club_activity(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        latest_date: date,
    ) -> Dict[str, Any]:
        """Total fans gained today, active member count, average gain."""
        today_entries = daily_rankings.get(latest_date, [])
        if not today_entries:
            return {"total_gain": 0, "active_count": 0, "avg_gain": 0}

        active = [e for e in today_entries if e["daily"] > 0]
        total_gain = sum(e["daily"] for e in active)
        active_count = len(active)
        avg_gain = round(total_gain / active_count) if active_count > 0 else 0

        return {
            "total_gain": total_gain,
            "active_count": active_count,
            "avg_gain": avg_gain,
        }

    @classmethod
    def _compute_club_mvp(
        cls,
        king: Optional[EfficiencyKing],
        tank: Optional[TankAnalysis],
        daily_leader: Optional[Dict[str, Any]],
        consistency: ConsistencyResult,
        latest_date: date,
        records: Dict[str, Any],
    ) -> Optional[Achievement]:
        """Pick the day's standout performer with narrative."""
        candidates = []

        # Candidate 1: Efficiency King (if exists)
        if king:
            candidates.append(("king", king.name, f"**{king.name}** — performed {king.pct_above_avg}% above their average, gaining +{king.daily_gain:,} fans today"))

        # Candidate 2: Daily Leader (top raw gain)
        if daily_leader:
            candidates.append(("leader", daily_leader["name"], f"**{daily_leader['name']}** — gained the most fans today with +{daily_leader['daily']:,}"))

        # Candidate 3: Top overperformer (if different from king)
        if consistency.top_overperformer:
            over = consistency.top_overperformer
            candidates.append(("overperformer", over["name"], f"**{over['name']}** — overperformed by +{over['pct_diff']}% today"))

        # Candidate 4: Best climber
        today_entries = []  # We don't have it here directly, skip for now

        if not candidates:
            return None

        # Score candidates: king > daily_leader > overperformer
        weights = {"king": 3, "leader": 2, "overperformer": 1}
        candidates.sort(key=lambda c: weights.get(c[0], 0), reverse=True)

        best = candidates[0]
        return Achievement(
            name=best[1],
            achievement_type="club_mvp",
            title="Club MVP",
            description=best[2],
        )

    @classmethod
    def _compute_funny_awards(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        daily_deltas: Dict[str, List[Dict]],
        latest_date: date,
    ) -> List[Achievement]:
        """Generate 1-3 humorous awards from templates."""
        awards: List[Achievement] = []
        today_entries = daily_rankings.get(latest_date, [])

        if not today_entries:
            return awards

        # Sleeping Giant: Top player inactive today
        top = today_entries[0]
        if top["daily"] == 0:
            awards.append(Achievement(
                name=top["name"],
                achievement_type="funny",
                title="Sleeping Giant",
                description=f"**{top['name']}** — #1 but didn't gain a single fan today. Resting on their laurels?",
            ))

        # Slow but Steady: Highest gain under 500K
        steady = [e for e in today_entries if 0 < e["daily"] < 500_000]
        if steady:
            steady.sort(key=lambda e: e["daily"], reverse=True)
            awards.append(Achievement(
                name=steady[0]["name"],
                achievement_type="funny",
                title="Slow but Steady",
                description=f"**{steady[0]['name']}** — highest gain under 500K, proving every fan counts.",
            ))

        # Rocket Launch: Biggest improver vs yesterday (largest daily delta increase)
        today_names = {e["name"]: e["daily"] for e in today_entries}
        improvers = []
        for name, deltas in daily_deltas.items():
            if name not in today_names or today_names[name] <= 0:
                continue
            sorted_d = sorted(deltas, key=lambda d: d["date"])
            if len(sorted_d) < 2:
                continue
            prev_daily = sorted_d[-2]["delta"]
            if prev_daily > 0:
                improvement = today_names[name] - prev_daily
                if improvement > 500_000:
                    improvers.append((name, improvement))
        if improvers:
            improvers.sort(key=lambda x: x[1], reverse=True)
            awards.append(Achievement(
                name=improvers[0][0],
                achievement_type="funny",
                title="Rocket Launch",
                description=f"**{improvers[0][0]}** — largest improvement vs yesterday (+{improvers[0][1]:,} fans)",
            ))

        return awards[:3]

    @classmethod
    def _compute_yesterday_results(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        overtakes: List[Overtake],
        latest_date: date,
    ) -> List[Dict[str, Any]]:
        """Check if yesterday's predicted overtakes happened (✅/❌)."""
        yesterday_date = cls._get_previous_day(daily_rankings, latest_date)
        if yesterday_date is None:
            return []

        yesterday_entries = daily_rankings.get(yesterday_date, [])
        today_entries = daily_rankings.get(latest_date, [])
        if not yesterday_entries or not today_entries:
            return []

        # Build rank lookups
        today_ranks = {e["name"]: e["rank"] for e in today_entries}
        yesterday_ranks = {e["name"]: e["rank"] for e in yesterday_entries}

        # We can't know which overtakes were predicted yesterday unless we re-compute
        # Instead, check adjacent pairs that were close yesterday
        results = []
        for i in range(len(yesterday_entries) - 1):
            above, below = yesterday_entries[i], yesterday_entries[i + 1]
            gap = above["fans"] - below["fans"]
            rate_diff = below["daily"] - above["daily"]
            if rate_diff > 0:
                eta = gap / rate_diff
                if 0 < eta <= 2:  # Was predicted within 2 days
                    # Did the overtake happen?
                    below_rank_today = today_ranks.get(below["name"])
                    above_rank_today = today_ranks.get(above["name"])
                    if below_rank_today is not None and above_rank_today is not None:
                        if below_rank_today < above_rank_today:
                            results.append({
                                "challenger": below["name"],
                                "target": above["name"],
                                "landed": True,
                            })
                        elif below_rank_today == above_rank_today:
                            # Still tied or overtake in progress
                            pass

        return results[:3]

    @classmethod
    def _generate_teaser(
        cls,
        overtakes: List[Overtake],
        milestones: List[Milestone],
        tank: Optional[TankAnalysis],
        leader_change: Dict[str, Any],
        daily_deltas: Dict[str, List[Dict]],
    ) -> Optional[str]:
        """Generate 'Watch tomorrow' section with 2-3 predictions."""
        items = []

        # Overtakes happening soon
        for o in overtakes[:2]:
            if o.eta_days <= 2:
                items.append(f"• **{o.challenger}** is expected to overtake **{o.target}** for #{o.target_rank}")

        # Milestones close
        for m in milestones[:2]:
            eta = cls._compute_milestone_eta(m.name, m.total, m.milestone, daily_deltas)
            if eta is not None and eta <= 3:
                items.append(f"• **{m.name}** is one good day away from **{cls._fmt_fans(m.milestone)}**")

        # Leader under pressure
        if tank and tank.pressure_streak >= 1 and tank.eta_days is not None and tank.eta_days <= 7:
            items.append(f"• Can **{tank.name}** hold off **{tank.name_2nd}**?")

        if not items:
            return None

        return "**👀 Watch tomorrow:**\n\n" + "\n".join(items[:3])

    @classmethod
    def _format_rivalry_with_context(
        cls,
        rivalry: Dict[str, Any],
        month_name: str,
    ) -> str:
        """Format rivalry with month context."""
        base = f"**{rivalry['name_a']}** (#{rivalry['rank_a']}) vs **{rivalry['name_b']}** (#{rivalry['rank_b']}) — **{rivalry['who_leads']}** leads by {cls._fmt_fans(rivalry['fan_gap'])}"
        if rivalry == "first" or True:  # For the top rivalry, add context
            base += f" — making this {month_name}'s fiercest rivalry with {rivalry['swap_count']} swaps"
        else:
            base += f" ({rivalry['swap_count']} swaps)"
        return base

    @classmethod
    def _format_overtake_dramatic(
        cls,
        overtake: Overtake,
    ) -> str:
        """Dramatic overtake language."""
        if overtake.eta_days < 1:
            return (
                f"🚨 **{overtake.challenger}** is only **{cls._fmt_fans(overtake.gap_fans)}** behind **{overtake.target}** "
                f"— the pass happens **before tomorrow's reset!**"
            )
        elif overtake.eta_days < 2:
            return (
                f"⚔️ **{overtake.challenger}** is only **{cls._fmt_fans(overtake.gap_fans)}** away from **{overtake.target}** "
                f"for #{overtake.target_rank}. Expected overtake: **Tomorrow**"
            )
        else:
            return (
                f"👀 **{overtake.challenger}** is closing in on **{overtake.target}** for #{overtake.target_rank} "
                f"~{round(overtake.eta_days)} days"
            )

    # ── Phase 2.16 + Phase 3: Headlines, Mood, Rare Achievements ──────────

    @classmethod
    def _determine_mood(
        cls,
        movers: Dict[str, List[Dict]],
        leader_change: Dict[str, Any],
        overtakes_count: int,
    ) -> BotMood:
        """Detect the day's character based on activity level."""
        total_movers = len(movers.get("climbers", [])) + len(movers.get("fallers", []))
        if leader_change["changed"] and overtakes_count >= 2:
            return BotMood.CHAOTIC
        if leader_change["changed"] and leader_change["old_streak"] >= 5:
            return BotMood.INTENSE
        if total_movers >= 4:
            return BotMood.CHAOTIC
        if overtakes_count >= 2:
            return BotMood.INTENSE
        if total_movers <= 1:
            return BotMood.PEACEFUL
        return BotMood.NEUTRAL

    @classmethod
    def _generate_headline(
        cls,
        leader_change: Dict[str, Any],
        king: Optional[EfficiencyKing],
        tank: Optional[TankAnalysis],
        today_entries: Optional[List[Dict]],
        mood: BotMood,
        overtakes: List[Overtake],
        movers_count: int,
    ) -> str:
        """Generate varied headline types based on the day's events."""
        # Priority 1: Leader change
        if leader_change["changed"]:
            streak = leader_change["old_streak"]
            old = leader_change["old_leader"]
            new_ld = leader_change["new_leader"]
            swaps = leader_change["swap_count_7d"]

            if swaps >= 3:
                return (
                    f"🌪️ Absolute chaos at the top! **{new_ld}** retakes #1 from **{old}** "
                    f"— the crown has changed hands {swaps} times this week!"
                )
            if streak >= 5:
                return (
                    f"👑 **{new_ld}** dethrones **{old}** after a dominant **{streak}-day reign**!"
                )
            if streak >= 3:
                return (
                    f"🏆 **{new_ld}** has overtaken **{old}** for #1! "
                    f"({streak} day streak broken!)"
                )
            return (
                f"🏆 **{new_ld}** has overtaken **{old}** for #1!"
            )

        # Priority 2: Domination (king is also #1)
        if king and tank and king.name == tank.name:
            return (
                f"👑 **{king.name}** is dominating the field, "
                f"leading in both momentum and defensive surplus!"
            )

        # Priority 3: Streak headline
        if tank and tank.is_dynasty and tank.daily_gain > tank.daily_gain_2nd:
            return (
                f"🔥 **{tank.name}** extends their reign to **{tank.streak} days** at #1 — "
                f"no one can keep up!"
            )

        # Priority 4: Clutch / milestone focus
        if king and king.pct_above_avg > 100:
            return (
                f"🎯 **{king.name}** is on fire — performing **{king.pct_above_avg}%** above their average!"
            )

        # Priority 5: Chaos (many overtakes)
        if len(overtakes) >= 2 and movers_count >= 3:
            return (
                f"🌪️ Four positions changed today — the most movement we've seen this week!"
            )

        # Priority 6: Comeback (leader losing ground)
        if tank and tank.pressure_streak >= 2:
            return (
                f"⚔️ **{tank.name_2nd}** is breathing down **{tank.name}**'s neck — "
                f"{tank.pressure_streak} days of mounting pressure!"
            )

        # Priority 7: Standard breakout star
        if king:
            return (
                f"🔥 **{king.name}** is today's breakout star, "
                f"performing {king.pct_above_avg}% above their usual pace!"
            )

        # Priority 8: Peaceful / quiet day
        if today_entries:
            top = today_entries[0]
            if mood == BotMood.PEACEFUL:
                return (
                    f"😌 A quiet day across the leaderboard. "
                    f"**{top['name']}** holds steady at #1 with {cls._fmt_fans(top['fans'])} fans."
                )
            return (
                f"👑 **{top['name']}** remains steady at #1 "
                f"with {cls._fmt_fans(top['fans'])} fans."
            )

        return "_Leaderboard remains stable._"

    @classmethod
    def _compute_rare_achievements(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        daily_deltas: Dict[str, List[Dict]],
        club_record: Optional[Dict[str, Any]],
        latest_date: date,
    ) -> List[Achievement]:
        """Detect rare achievements: first 5M+ day, 3 PBs in a week, first to milestone."""
        achievements: List[Achievement] = []
        today_entries = daily_rankings.get(latest_date, [])
        today_map = {e["name"]: e for e in today_entries}

        # First 5M+ day
        for name, deltas in daily_deltas.items():
            if name not in today_map:
                continue
            # Check if any delta >= 5M
            has_5m = any(d["delta"] >= 5_000_000 for d in deltas)
            if has_5m:
                # Check if this is their first 5M+ day
                five_m_days = [d for d in deltas if d["delta"] >= 5_000_000]
                if len(five_m_days) == 1 and five_m_days[0]["date"] == latest_date:
                    achievements.append(Achievement(
                        name=name,
                        achievement_type="rare",
                        title="First 5M+ Day",
                        description=f"🏅 **{name}** cracked **5 million fans** in a single day for the first time!",
                    ))

        # 3 PBs in one week (check if any member has 3+ PB entries in last 7 days)
        from datetime import timedelta
        week_ago = latest_date - timedelta(days=7)
        for name, deltas in daily_deltas.items():
            if name not in today_map:
                continue
            # Sort by date to find PBs
            by_date = sorted(deltas, key=lambda d: d["date"])
            pb_count = 0
            prev_best = 0
            for d in by_date:
                if d["date"] < week_ago:
                    continue
                if d["delta"] > prev_best:
                    pb_count += 1
                    prev_best = d["delta"]
            if pb_count >= 3:
                achievements.append(Achievement(
                    name=name,
                    achievement_type="rare",
                    title="PB Spree",
                    description=f"⚡ **{name}** set **{pb_count} personal bests** in the last week!",
                ))

        # Club record (highest single day gain)
        if club_record:
            achievements.append(Achievement(
                name=club_record["name"],
                achievement_type="rare",
                title="Club Record",
                description=f"📊 **{club_record['name']}** holds this month's club record with **+{club_record['delta']:,}** fans in a single day!",
            ))

        return achievements[:3]

    # --- Assembly Helpers (The 'Polishing' Layer) ---

    @classmethod
    def _assemble_headline(
        cls,
        leader_change: Dict[str, Any],
        king: Optional[EfficiencyKing],
        tank: Optional[TankAnalysis],
        today_entries: Optional[List[Dict]],
    ) -> str:
        if leader_change["changed"]:
            streak = leader_change["old_streak"]
            ctx = f" ({streak} day streak broken!)" if streak >= 3 else ""
            return (
                f"🏆 **{leader_change['new_leader']}** has overtaken "
                f"**{leader_change['old_leader']}** for #1!{ctx}"
            )

        if king and tank and king.name == tank.name:
            return (
                f"👑 **{king.name}** is dominating the field, "
                f"leading in both momentum and defensive surplus!"
            )

        if king:
            return (
                f"🔥 **{king.name}** is today's breakout star, "
                f"performing {king.pct_above_avg}% above their usual pace!"
            )

        if today_entries:
            top = today_entries[0]
            return (
                f"👑 **{top['name']}** remains steady at #1 "
                f"with {cls._fmt_fans(top['fans'])} fans."
            )

        return "_Leaderboard remains stable._"

    @classmethod
    def _assemble_momentum(
        cls,
        king: Optional[EfficiencyKing],
        tank: Optional[TankAnalysis],
        consistency: ConsistencyResult,
        records: Dict[str, Any],
        club_rec: Optional[Dict[str, Any]],
        latest_date: date,
        daily_leader: Optional[Dict[str, Any]] = None,
        leader_change: Optional[Dict[str, Any]] = None,
        best_week: Optional[BestWeek] = None,
        streaks: Optional[List[StreakInfo]] = None,
        club_activity: Optional[Dict[str, Any]] = None,
        mvp: Optional[Achievement] = None,
        funny_awards: Optional[List[Achievement]] = None,
    ) -> str:
        parts = []

        # --- Club MVP section ---
        if mvp:
            parts.append(
                f"**🏆 Club MVP**\n\n{mvp.description}"
            )

        if daily_leader:
            parts.append(
                f"**🥇 Top Trainer** — **{daily_leader['name']}** "
                f"(+{cls._fmt_fans(daily_leader['daily'])}) gained the most fans today!"
            )

        if best_week:
            parts.append(
                f"**🏅 Best Week** — **{best_week.name}** "
                f"(avg +{cls._fmt_fans(round(best_week.avg_daily))}/day "
                f"over the last {best_week.days} days)"
            )

        if tank:
            # Buffer size: small if 2nd place could overtake in ~3 days at current net rate
            net_chase_rate = tank.daily_gain_2nd - tank.daily_gain
            is_small_gap = net_chase_rate > 0 and tank.gap_to_next <= net_chase_rate * 3
            gap_fmt = cls._fmt_fans(tank.gap_to_next)

            # --- Fragile lead detection ---
            # Even if the leader is out-gaining #2, the gap may be tiny
            # relative to the raw firepower both sides are putting up.
            # A gap < ~1.5x the larger daily gain is dangerously thin.
            max_daily_gain = max(tank.daily_gain, tank.daily_gain_2nd)
            is_fragile_lead = (
                max_daily_gain > 0
                and tank.gap_to_next < max_daily_gain * 1.5
            )

            # --- Leader change: distinguish first-time conqueror from back-and-forth slugfest ---
            if leader_change and leader_change["changed"] and tank.name == leader_change["new_leader"]:
                old_leader = leader_change["old_leader"]
                streak = leader_change["old_streak"]
                swaps_7d = leader_change["swap_count_7d"]
                streak_ctx = f" ({streak}-day reign ended!)" if streak >= 3 else ""

                if swaps_7d >= 2:
                    # Back-and-forth slugfest — these two won't stay down
                    header = "**⚔️ THE SLUGFEST**"
                    if is_fragile_lead:
                        text = (
                            f"**{tank.name}** retakes **#1** from **{old_leader}**"
                            f"{streak_ctx} — but this war is far from over. "
                            f"The crown has swapped hands **{swaps_7d} times** "
                            f"in the last week alone. With a razor-thin **{gap_fmt}** "
                            f"lead and both sides trading blows at millions per day, "
                            f"expect another reversal soon."
                        )
                    else:
                        text = (
                            f"**{tank.name}** retakes **#1** from **{old_leader}**"
                            f"{streak_ctx}. These two have swapped the crown "
                            f"**{swaps_7d} times** in the last week — a true "
                            f"war of attrition. Who will blink first?"
                        )
                else:
                    # First overtake — triumphant conqueror
                    header = "**👑 THE CONQUEROR**"
                    if is_fragile_lead:
                        text = (
                            f"**{tank.name}** has done it. After a grueling battle, "
                            f"they've dethroned **{old_leader}**{streak_ctx} and "
                            f"claimed **#1**. But the war isn't over — the **{gap_fmt}** "
                            f"lead is razor-thin when both sides are pulling "
                            f"millions per day. One slip-up and the crown could "
                            f"change hands again."
                        )
                    else:
                        text = (
                            f"**{tank.name}** has done it. After a grueling battle, "
                            f"they've dethroned **{old_leader}**{streak_ctx} and "
                            f"claimed **#1** with a **{gap_fmt}** buffer. "
                            f"The reign of **{tank.name}** begins today."
                        )
            elif tank.is_dynasty:
                if tank.daily_gain > tank.daily_gain_2nd:
                    # Dynasty, winning
                    header = "**🏛️ THE DYNASTY**"
                    text = (
                        f"**{tank.name}**'s rule is absolute. After **{tank.streak} days**, "
                        f"they remain unmovable and continue to pull away—"
                        f"no one can challenge the throne."
                    )
                else:
                    # Dynasty, losing lead
                    header = "**🏰 THE SIEGE**"
                    text = (
                        f"The **{tank.streak}-day era** of **{tank.name}** is finally being "
                        f"challenged. **{tank.name_2nd}** is on a journey to defeat "
                        f"our long-standing leader—how long can they hold out?"
                    )
            else:
                if tank.daily_gain > tank.daily_gain_2nd:
                    if not is_small_gap and not is_fragile_lead:
                        # Standard, winning, big buffer
                        header = "**🛡️ THE TANK**"
                        text = (
                            f"**{tank.name}** is unmovable at **#1** with a "
                            f"**{gap_fmt}** buffer. No one can challenge them today."
                        )
                    else:
                        # Standard, winning, small or fragile buffer
                        if is_fragile_lead:
                            header = "**🏃 THE VANGUARD**"
                            text = (
                                f"**{tank.name}** holds a slim **{gap_fmt}** edge at **#1** "
                                f"— vulnerable territory when both sides are pulling "
                                f"**+{cls._fmt_fans(tank.daily_gain)}** and "
                                f"**+{cls._fmt_fans(tank.daily_gain_2nd)}** per day. "
                                f"**{tank.name_2nd}** is right on their heels."
                            )
                        else:
                            header = "**🏃 THE VANGUARD**"
                            text = (
                                f"**{tank.name}** is planting their feet. They remain unmovable "
                                f"despite **{tank.name_2nd}** breathing down their neck."
                            )
                else:
                    # Standard, losing lead — use new analytics for rich text
                    # Build narrative parts
                    narrative_parts = []

                    # Streak context
                    if tank.pressure_streak >= 1:
                        narrative_parts.append(
                            f"**{tank.name_2nd}** is turning up the heat, "
                            f"out-gaining **{tank.name}** for "
                            f"{'the first day in a row' if tank.pressure_streak == 1 else f'{tank.pressure_streak} days in a row'}"
                        )
                    else:
                        narrative_parts.append(
                            f"**{tank.name_2nd}** is gaining faster today"
                        )

                    # Gap trajectory
                    if tank.gap_trend == "shrinking" and tank.eta_days is not None:
                        net_rate = tank.daily_gain_2nd - tank.daily_gain
                        narrative_parts.append(
                            f"chipping away at the **{gap_fmt}** lead at "
                            f"**+{cls._fmt_fans(net_rate)}/day**"
                        )
                    elif tank.gap_trend == "shrinking":
                        narrative_parts.append(
                            f"the **{gap_fmt}** lead is shrinking"
                        )
                    elif tank.gap_trend == "expanding":
                        narrative_parts.append(
                            f"but **{tank.name}** is actually pulling away despite the pace"
                        )
                    else:
                        narrative_parts.append(
                            f"the **{gap_fmt}** gap is holding steady for now"
                        )

                    # Leader's response
                    if tank.leader_above_avg:
                        narrative_parts.append(
                            f"**{tank.name}** fought back today "
                            f"(+{cls._fmt_fans(tank.daily_gain)}, above their average)"
                        )
                    else:
                        narrative_parts.append(
                            f"**{tank.name}** had a below-average day "
                            f"(+{cls._fmt_fans(tank.daily_gain)})"
                        )

                    # Projection
                    if tank.eta_days is not None:
                        if tank.eta_days <= 3:
                            projection = (
                                f"At this rate, the crown changes hands in "
                                f"**under {int(tank.eta_days + 1)} days**!"
                            )
                        elif tank.eta_days <= 7:
                            projection = (
                                f"If this keeps up, we'll see a new **#1** "
                                f"in about **{int(tank.eta_days)} days**."
                            )
                        elif tank.eta_days <= 14:
                            projection = (
                                f"At this pace, a new **#1** would emerge "
                                f"in **~{int(tank.eta_days)} days**."
                            )
                        else:
                            projection = (
                                f"It's a slow burn — overtake projected "
                                f"**{int(tank.eta_days)}+ days** out."
                            )
                        narrative_parts.append(projection)

                    if not is_small_gap:
                        header = "**⚖️ THE MOMENTUM**"
                    else:
                        header = "**🚨 THE BRINK**"

                    text = " — ".join(narrative_parts)

            parts.append(f"{header}\n\n{text}")

        top_over = consistency.top_overperformer
        if top_over and (not king or top_over["name"] != king.name):
            parts.append(
                f"**🔥 Overperforming** — **{top_over['name']}** "
                f"(+{top_over['pct_diff']}%)"
            )

        if records["members"]:
            # Separate true new PBs from matched PBs
            true_pbs = [m for m in records["members"] if not m["is_tie"]]
            matched_pbs = [m for m in records["members"] if m["is_tie"]]

            if true_pbs:
                pb_lines = [f"**🏆 New PBs**"]
                for m in true_pbs:
                    prev = cls._fmt_fans(m["prev_best_delta"]) if m["prev_best_delta"] is not None else "N/A"
                    pb_lines.append(
                        f"**{m['name']}** — **+{cls._fmt_fans(m['delta'])}** "
                        f"(prev best +{prev})"
                    )
                parts.append("\n".join(pb_lines))

            if matched_pbs:
                matched_lines = [f"**⚖️ Matched PBs**"]
                for m in matched_pbs:
                    matched_lines.append(
                        f"**{m['name']}** — matched their PB of **+{cls._fmt_fans(m['delta'])}**"
                    )
                parts.append("\n".join(matched_lines))

        return "\n\n".join(parts)

    @classmethod
    def _assemble_battle_zone(
        cls,
        overtakes: List[Overtake],
        rivalries: List[Dict],
        yesterday_results: Optional[List[Dict]] = None,
        month_name: Optional[str] = None,
    ) -> str:
        parts = []

        if overtakes:
            urgent = [o for o in overtakes if o.eta_days < 2]
            if urgent:
                lines = [
                    f"• **{o.challenger}** is projected to overtake "
                    f"**{o.target}** for **#{o.target_rank}** — "
                    f"{'TODAY' if o.eta_days < 1 else 'TOMORROW'} "
                    f"(closing {cls._fmt_fans(o.gap_fans)} gap "
                    f"at +{cls._fmt_fans(o.daily_diff)}/day)"
                    for o in urgent
                ]
                parts.append("**🚨 Urgent Overtakes**\n\n" + "\n".join(lines))

            horizon = [o for o in overtakes if o.eta_days >= 2]
            if horizon:
                lines = [
                    f"• **{o.challenger}** is projected to overtake "
                    f"**{o.target}** for **#{o.target_rank}** "
                    f"~{round(o.eta_days)} days "
                    f"(closing {cls._fmt_fans(o.gap_fans)} gap "
                    f"at +{cls._fmt_fans(o.daily_diff)}/day)"
                    for o in horizon
                ]
                parts.append("**⏳ On the Horizon**\n\n" + "\n".join(lines))

        if rivalries:
            r_lines = []
            for r in rivalries:
                r_lines.append(
                    f"**{r['name_a']}** (#{r['rank_a']}) vs **{r['name_b']}** (#{r['rank_b']}) — "
                    f"**{r['who_leads']}** leads by {cls._fmt_fans(r['fan_gap'])} "
                    f"({r['swap_count']} swaps)"
                )
            parts.append("**Monthly Rivalries**\n\n" + "\n".join(r_lines))

        return "\n\n".join(parts)

    # --- Formatting Utilities ---

    @classmethod
    def _fmt_fans(cls, n: int, suffix: str = "") -> str:
        abs_n = abs(n)
        if abs_n >= 1_000_000:
            result = f"{n / 1_000_000:.1f}M"
        elif abs_n >= 1_000:
            result = f"{n / 1_000:.1f}K"
        else:
            result = str(n)
        if suffix:
            result += f" {suffix}"
        return result

    @classmethod
    def _fmt_date(cls, d: date) -> str:
        return d.strftime("%b %d")

    @classmethod
    def _format_milestone_watch(cls, watch: List[Milestone], milestone_etas: Optional[Dict[str, float]] = None) -> str:
        if not watch:
            return "_No one approaching a milestone._"
        if milestone_etas is None:
            milestone_etas = {}
        lines = []
        for w in watch:
            filled = max(0, min(10, int((w.pct_to_milestone / 100) * 10)))
            bar = "▰" * filled + "▱" * (10 - filled)
            eta_str = ""
            eta = milestone_etas.get(w.name)
            if eta is not None:
                if eta <= 1:
                    eta_str = " — expected **today**!"
                elif eta <= 2:
                    eta_str = " — expected **tomorrow**"
                else:
                    eta_str = f" — ~{int(eta)} days left"
            lines.append(
                f"🎯 **{w.name}** — [{bar}] {w.amount_away:,} remaining{eta_str}"
            )
        return "\n\n".join(lines)

    @classmethod
    def _format_condensed_movers(cls, condensed: List[Dict]) -> str:
        if not condensed:
            return "_No rank changes today._"
        return " · ".join([
            f"{'📈' if m['direction']=='up' else '📉'} **{m['name']}** "
            f"#{m['old_rank']}→#{m['new_rank']}"
            for m in condensed
        ])

    # ── Daily ranking reconstruction ────────────────────────────────────

    @classmethod
    def _build_daily_rankings(
        cls, rows: list
    ) -> Dict[date, List[Dict[str, Any]]]:
        """
        Group QuotaHistory rows by date, sort each day by cumulative_fans
        descending, assign ranks (ties get the same rank), and attach
        prev_rank, surplus (deficit_surplus), and daily delta from the
        previous day.

        Returns {date: [{name, fans, rank, prev_rank, surplus, daily}, ...]}
        sorted by date.
        """
        by_date: Dict[date, list] = defaultdict(list)
        for row in rows:
            by_date[row["date"]].append(row)

        sorted_dates = sorted(by_date.keys())
        rankings: Dict[date, List[Dict]] = {}
        previous_lookup: Dict[str, int] = {}
        # Track previous day's cumulative_fans per member to compute daily delta
        prev_day_fans: Dict[str, int] = {}

        for d in sorted_dates:
            day_rows = by_date[d]
            day_rows.sort(key=lambda r: r["cumulative_fans"], reverse=True)

            entries: List[Dict] = []
            prev_fans: Optional[int] = None
            prev_rank: int = 0

            for i, row in enumerate(day_rows):
                name: str = row["trainer_name"]
                fans: int = row["cumulative_fans"]

                if fans == prev_fans:
                    rank = prev_rank
                else:
                    rank = i + 1
                prev_fans = fans
                prev_rank = rank

                prev_day_rank = previous_lookup.get(name)
                # Compute daily delta: difference from previous available day
                last_fans = prev_day_fans.get(name)
                if last_fans is None:
                    # First day of data for this member (e.g., month reset).
                    # Treat cumulative_fans as today's gain.
                    daily = fans
                else:
                    daily = fans - last_fans

                entries.append({
                    "name": name,
                    "fans": fans,
                    "rank": rank,
                    "prev_rank": prev_day_rank,
                    "surplus": row["deficit_surplus"],
                    "daily": daily,
                })
                previous_lookup[name] = rank
                prev_day_fans[name] = fans

            rankings[d] = entries

        return rankings

    @classmethod
    def _get_previous_day(
        cls, daily_rankings: Dict[date, List[Dict]], current_date: date
    ) -> Optional[date]:
        """Get the previous available date in the rankings."""
        sorted_dates = sorted(daily_rankings.keys())
        idx = sorted_dates.index(current_date)
        if idx > 0:
            return sorted_dates[idx - 1]
        return None

    # ── Daily delta computation ─────────────────────────────────────────

    @classmethod
    def _compute_daily_deltas(
        cls, rows: list
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        For each member, compute the single-day fan gain (delta) between
        consecutive dates.

        Returns {member_name: [{date, delta, fans_total}, ...]} with each
        member's list sorted by delta descending.
        """
        member_data: Dict[str, List[Tuple[date, int]]] = defaultdict(list)
        for row in rows:
            member_data[row["trainer_name"]].append(
                (row["date"], row["cumulative_fans"])
            )

        result: Dict[str, List[Dict]] = {}
        for name, entries in member_data.items():
            entries.sort(key=lambda x: x[0])
            deltas: List[Dict] = []
            for i in range(1, len(entries)):
                _, prev_fans = entries[i - 1]
                curr_date, curr_fans = entries[i]
                delta = curr_fans - prev_fans
                deltas.append({
                    "date": curr_date,
                    "delta": delta,
                    "fans_total": curr_fans,
                })
            deltas.sort(key=lambda d: d["delta"], reverse=True)
            if deltas:
                result[name] = deltas

        return result

    # ── Analysis helpers ────────────────────────────────────────────────

    @classmethod
    def _compute_today_movers(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        latest_date: date,
    ) -> Dict[str, List[Dict]]:
        """
        Top climbers and fallers for today only (comparing latest_date
        to the day before).

        Returns {climbers: [{name, old_rank, new_rank, delta}],
                 fallers: [{name, old_rank, new_rank, delta}]}
        """
        today_entries = daily_rankings.get(latest_date, [])
        climbers: List[Dict] = []
        fallers: List[Dict] = []

        for entry in today_entries:
            prev = entry.get("prev_rank")
            if prev is None:
                continue
            delta = prev - entry["rank"]  # positive = climbed, negative = dropped
            if delta > 0:
                climbers.append({
                    "name": entry["name"],
                    "old_rank": prev,
                    "new_rank": entry["rank"],
                    "delta": delta,
                })
            elif delta < 0:
                fallers.append({
                    "name": entry["name"],
                    "old_rank": prev,
                    "new_rank": entry["rank"],
                    "delta": abs(delta),
                })

        climbers.sort(key=lambda c: c["delta"], reverse=True)
        fallers.sort(key=lambda f: f["delta"], reverse=True)

        return {
            "climbers": climbers[:3],
            "fallers": fallers[:3],
        }

    @classmethod
    def _compute_leader_change(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        latest_date: date,
        yesterday_date: Optional[date],
    ) -> Dict[str, Any]:
        """
        Check if the #1 position changed today vs yesterday.
        If so, provide context about the old leader's prior dominance.

        Returns {changed: bool, old_leader, new_leader, old_streak, swap_count_7d}
        where old_streak is how many consecutive days the old leader
        held #1 before today, and swap_count_7d is how many times #1
        changed hands in the last 7 days (including this one).
        """
        result: Dict[str, Any] = {
            "changed": False,
            "old_leader": None,
            "new_leader": None,
            "old_streak": 0,
            "swap_count_7d": 0,
        }

        if yesterday_date is None:
            return result

        today_top = daily_rankings[latest_date][0]["name"] if daily_rankings.get(latest_date) else None
        yesterday_top = daily_rankings[yesterday_date][0]["name"] if daily_rankings.get(yesterday_date) else None

        if today_top and yesterday_top and today_top != yesterday_top:
            result["changed"] = True
            result["new_leader"] = today_top
            result["old_leader"] = yesterday_top

            # Count how many consecutive days the old leader held #1
            sorted_dates = sorted(daily_rankings.keys())
            streak = 0
            for d in reversed(sorted_dates):
                if d == latest_date:
                    continue  # skip today, we know they lost
                top = daily_rankings[d][0]["name"] if daily_rankings.get(d) else None
                if top == yesterday_top:
                    streak += 1
                else:
                    break
            result["old_streak"] = streak

            # Count #1 swaps in the last 7 days (including this one)
            from datetime import timedelta
            window_start = latest_date - timedelta(days=7)
            swap_count = 0
            prev_leader = None
            for d in sorted_dates:
                if d < window_start:
                    continue
                top = daily_rankings[d][0]["name"] if daily_rankings.get(d) else None
                if prev_leader is not None and top is not None and top != prev_leader:
                    swap_count += 1
                prev_leader = top
            result["swap_count_7d"] = swap_count

        return result

    @classmethod
    def _compute_today_records(
        cls,
        daily_deltas: Dict[str, List[Dict]],
        latest_date: date,
    ) -> Dict[str, Any]:
        """
        Members whose personal best single-day fan gain occurred TODAY.
        Returns {members: [{name, delta, prev_best_delta, is_tie}], count}
        sorted by delta desc — all members are listed, no truncation.
        prev_best_delta is the member's previous best (second in sorted list)
        or None if this is their only recorded day.
        is_tie is True when today's gain equals the previous best (matched PB).
        """
        today_bests: List[Dict] = []
        for name, deltas in daily_deltas.items():
            if not deltas:
                continue
            # deltas are sorted desc, so index 0 is their best
            best = deltas[0]
            if best["date"] == latest_date:
                # prev_best is the next best (index 1) if it exists
                prev_best = deltas[1]["delta"] if len(deltas) > 1 else None
                # Skip if no genuine previous best to beat
                if prev_best is None or prev_best <= 0:
                    continue
                # Check if this is a tie (matched PB, not a new record)
                is_tie = (best["delta"] == prev_best)
                today_bests.append({
                    "name": name,
                    "delta": best["delta"],
                    "prev_best_delta": prev_best,
                    "is_tie": is_tie,
                })

        today_bests.sort(key=lambda r: r["delta"], reverse=True)
        return {
            "members": today_bests,
            "count": len(today_bests),
        }

    @classmethod
    def _compute_club_record(
        cls, daily_deltas: Dict[str, List[Dict]]
    ) -> Optional[Dict[str, Any]]:
        """
        The single highest single-day fan gain across ALL members
        this month. Returns {name, delta, date} or None.
        """
        best: Optional[Dict] = None
        for name, deltas in daily_deltas.items():
            for d in deltas:
                if best is None or d["delta"] > best["delta"]:
                    best = {
                        "name": name,
                        "delta": d["delta"],
                        "date": d["date"],
                    }
        return best

    @classmethod
    def _compute_condensed_movers(
        cls, movers: Dict[str, List[Dict]]
    ) -> List[Dict[str, Any]]:
        """
        Combine climbers + fallers from _compute_today_movers into a single
        list sorted by absolute rank delta descending. Return top 3 with
        direction indicator.
        """
        combined: List[Dict] = []
        for c in movers["climbers"]:
            combined.append({
                "name": c["name"],
                "old_rank": c["old_rank"],
                "new_rank": c["new_rank"],
                "abs_delta": c["delta"],
                "direction": "up",
            })
        for f in movers["fallers"]:
            combined.append({
                "name": f["name"],
                "old_rank": f["old_rank"],
                "new_rank": f["new_rank"],
                "abs_delta": f["delta"],
                "direction": "down",
            })
        combined.sort(key=lambda x: x["abs_delta"], reverse=True)
        return combined[:3]