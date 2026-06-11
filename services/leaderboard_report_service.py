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

class EfficiencyKing(NamedTuple):
    name: str
    daily_gain: int
    avg_daily: float
    pct_above_avg: float

class BrickWall(NamedTuple):
    name: str
    surplus: int
    gap_to_next: int
    rank: int

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
        wall = cls._compute_brick_wall(daily_rankings, latest_date)
        overtakes = cls._compute_projected_overtakes(daily_rankings, latest_date)
        milestones = cls._compute_milestone_watch(daily_rankings, latest_date)
        consistency = cls._compute_consistency(daily_rankings, daily_deltas, latest_date)

        # 3. Assemble Embed
        embed = discord.Embed(
            title=f"📰 Leaderboard News — {club_name}",
            description=(
                f"**{date(year, month, 1).strftime('%B %Y')}** · {member_count} members\n"
                f"_{cls._fmt_date(latest_date)} update_"
            ),
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )

        # --- Section 1: Headline ---
        headline = cls._assemble_headline(leader_change, king, wall, daily_rankings.get(latest_date))
        embed.add_field(name="🔥 HEADLINE NEWS", value=headline, inline=False)

        # --- Section 2: Momentum ---
        momentum = cls._assemble_momentum(king, wall, consistency, today_records, club_record, latest_date)
        embed.add_field(name="📈 THE MOMENTUM SHIFT", value=momentum or "_Stable activity today._", inline=False)

        # --- Section 3: Battle Zone ---
        battles = cls._assemble_battle_zone(overtakes, rivalries)
        embed.add_field(name="⚔️ THE BATTLE ZONE", value=battles or "_Peaceful on the ranks today._", inline=False)

        # --- Section 4: Milestones ---
        milestone_text = cls._format_milestone_watch(milestones)
        embed.add_field(name="🎯 MILESTONE TRACKER", value=milestone_text, inline=False)

        # --- Section 5: Movers ---
        condensed = cls._compute_condensed_movers(movers)
        embed.add_field(name="⬆️⬇️ TOP MOVERS", value=cls._format_condensed_movers(condensed), inline=False)

        embed.set_footer(text=f"{club_name} · Powering Through {calendar.month_name[month]}")
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

            avg = sum(d["delta"] for d in m_deltas) / len(m_deltas)
            today_gain = today_map[name]["daily"]

            if avg > 0 and today_gain > 0:
                pct = ((today_gain - avg) / avg) * 100
                if pct > 0:
                    candidates.append(EfficiencyKing(name, today_gain, avg, round(pct, 1)))

        if not candidates:
            return None
        return max(candidates, key=lambda x: x.pct_above_avg)

    @classmethod
    def _compute_brick_wall(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        latest_date: date,
    ) -> Optional[BrickWall]:
        entries = daily_rankings.get(latest_date, [])
        if len(entries) < 2:
            return None

        # Sort by surplus (most 'ahead' of their own quota)
        best = max(entries, key=lambda e: e["surplus"])
        idx = entries.index(best)

        # If they are last, compare to 0, otherwise compare to the person below them in rank
        if idx + 1 < len(entries):
            gap = best["surplus"] - entries[idx + 1]["surplus"]
        else:
            gap = best["surplus"]

        return BrickWall(best["name"], best["surplus"], gap, best["rank"])

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
                    results.append(
                        Overtake(below["name"], above["name"], gap, rate_diff, round(eta, 1), above["rank"])
                    )

        return sorted(results, key=lambda x: x.eta_days)[:3]

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

    # --- Assembly Helpers (The 'Polishing' Layer) ---

    @classmethod
    def _assemble_headline(
        cls,
        leader_change: Dict[str, Any],
        king: Optional[EfficiencyKing],
        wall: Optional[BrickWall],
        today_entries: Optional[List[Dict]],
    ) -> str:
        if leader_change["changed"]:
            streak = leader_change["old_streak"]
            ctx = f" ({streak} day streak broken!)" if streak >= 3 else ""
            return (
                f"🏆 **{leader_change['new_leader']}** has overtaken "
                f"**{leader_change['old_leader']}** for #1!{ctx}"
            )

        if king and wall and king.name == wall.name:
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
        wall: Optional[BrickWall],
        consistency: ConsistencyResult,
        records: Dict[str, Any],
        club_rec: Optional[Dict[str, Any]],
        latest_date: date,
    ) -> str:
        parts = []
        if king:
            parts.append(
                f"**🏃 The Sprinter** — **{king.name}** "
                f"(+{cls._fmt_fans(king.daily_gain)}) is "
                f"**{king.pct_above_avg}%** above average."
            )

        if wall:
            parts.append(
                f"**🛡️ The Tank** — **{wall.name}** is a brick wall "
                f"at **#{wall.rank}**, holding a "
                f"**{cls._fmt_fans(wall.gap_to_next)}** buffer."
            )

        top_over = consistency.top_overperformer
        if top_over and (not king or top_over["name"] != king.name):
            parts.append(
                f"**🔥 Overperforming** — **{top_over['name']}** "
                f"(+{top_over['pct_diff']}%)"
            )

        if records["members"]:
            best = records["members"][0]
            parts.append(
                f"**🏅 New PB** — **{best['name']}** just set a new "
                f"personal best: **+{cls._fmt_fans(best['delta'])}**!"
            )

        return "\n".join(parts)

    @classmethod
    def _assemble_battle_zone(
        cls,
        overtakes: List[Overtake],
        rivalries: List[Dict],
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
                parts.append("**🚨 Urgent Overtakes**\n" + "\n".join(lines))

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
                parts.append("**⏳ On the Horizon**\n" + "\n".join(lines))

        if rivalries:
            r_lines = []
            for r in rivalries:
                min_rank, max_rank = r["rank_range"]
                r_lines.append(
                    f"⚔️ **{r['name_a']}** vs **{r['name_b']}** "
                    f"({r['swap_count']} swaps) — **{r['who_leads']}** leads by "
                    f"{cls._fmt_fans(r['fan_gap'])} "
                    f"(#{min_rank} vs #{max_rank})"
                )
            parts.append("**⚔️ Monthly Rivalries**\n" + "\n".join(r_lines))

        return "\n\n".join(parts)

    # --- Formatting Utilities ---

    @classmethod
    def _fmt_fans(cls, n: int) -> str:
        abs_n = abs(n)
        if abs_n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if abs_n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    @classmethod
    def _fmt_date(cls, d: date) -> str:
        return d.strftime("%b %d")

    @classmethod
    def _format_milestone_watch(cls, watch: List[Milestone]) -> str:
        if not watch:
            return "_No one approaching a milestone._"
        lines = []
        for w in watch:
            filled = max(0, min(10, int((w.pct_to_milestone / 100) * 10)))
            bar = "▰" * filled + "▱" * (10 - filled)
            lines.append(
                f"🎯 **{w.name}** — [{bar}] {w.pct_to_milestone}% "
                f"to **{cls._fmt_fans(w.milestone)}**"
            )
        return "\n".join(lines)

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
                daily = fans - last_fans if last_fans is not None else 0

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

        Returns {changed: bool, old_leader, new_leader, old_streak}
        where old_streak is how many consecutive days the old leader
        held #1 before today.
        """
        result: Dict[str, Any] = {
            "changed": False,
            "old_leader": None,
            "new_leader": None,
            "old_streak": 0,
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

        return result

    @classmethod
    def _compute_today_records(
        cls,
        daily_deltas: Dict[str, List[Dict]],
        latest_date: date,
    ) -> Dict[str, Any]:
        """
        Members whose personal best single-day fan gain occurred TODAY.
        Returns {members: [{name, delta, prev_best_delta}], count}
        sorted by delta desc — all members are listed, no truncation.
        prev_best_delta is the member's previous best (second in sorted list)
        or None if this is their only recorded day.
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
                # Only include if they have a genuine previous best to beat
                if prev_best is None or prev_best <= 0:
                    continue
                today_bests.append({
                    "name": name,
                    "delta": best["delta"],
                    "prev_best_delta": prev_best,
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