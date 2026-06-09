"""
Leaderboard Report Service — generates a "news segment" embed analyzing
daily leaderboard position changes, streaks, rivalries, and records
from the existing QuotaHistory data for a given month.
"""
import logging
import calendar
from collections import defaultdict
from datetime import date
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import discord

from config.settings import COLOR_INFO
from models import QuotaHistory

logger = logging.getLogger(__name__)


class LeaderboardReportService:
    """Generates a single news-style Discord embed with leaderboard analysis."""

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

        daily_rankings = cls._build_daily_rankings(rows)
        daily_deltas = cls._compute_daily_deltas(rows)

        member_count = cls._compute_member_count(rows)
        month_label = date(year, month, 1).strftime("%B %Y")

        # ── Analysis segments ───────────────────────────────────────────
        streak = cls._compute_first_place_streak(daily_rankings)
        climbers = cls._compute_biggest_climbers(daily_rankings)
        fallers = cls._compute_biggest_fallers(daily_rankings)
        rivalries = cls._compute_rivalries(daily_rankings)
        prs = cls._compute_personal_records(daily_deltas)
        club_record = cls._compute_club_record(daily_deltas)

        # ── Build embed ─────────────────────────────────────────────────
        embed = discord.Embed(
            title=f"📰 Leaderboard Report — {club_name}",
            description=(
                f"**{month_label}** · {member_count} members\n"
                f"_A look at the position battles and records from this month_"
            ),
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )

        # Section 1 — First Place Streak
        streak_text = cls._format_streak(streak)
        embed.add_field(
            name="🏆 First Place Streak",
            value=streak_text,
            inline=False,
        )

        # Section 2 — Biggest Risers
        risers_text = cls._format_climbers(climbers)
        embed.add_field(
            name="⬆️ Biggest Risers",
            value=risers_text,
            inline=False,
        )

        # Section 3 — Biggest Fallers
        fallers_text = cls._format_fallers(fallers)
        embed.add_field(
            name="⬇️ Biggest Fallers",
            value=fallers_text,
            inline=False,
        )

        # Section 4 — Rivalries
        rivalries_text = cls._format_rivalries(rivalries)
        embed.add_field(
            name="⚔️ Rivalries",
            value=rivalries_text,
            inline=False,
        )

        # Section 5 — Records
        records_text = cls._format_records(prs, club_record)
        embed.add_field(
            name="🔥 Records",
            value=records_text,
            inline=False,
        )

        embed.set_footer(text=f"{club_name} · Data from QuotaHistory")
        return embed

    # ── Daily ranking reconstruction ────────────────────────────────────

    @classmethod
    def _build_daily_rankings(
        cls, rows: list
    ) -> Dict[date, List[Dict[str, Any]]]:
        """
        Group QuotaHistory rows by date, sort each day by cumulative_fans
        descending, assign ranks (ties get the same rank), and attach
        prev_rank from the previous day.

        Returns {date: [{name, fans, rank, prev_rank}, ...]} sorted by date.
        """
        # Group rows by date
        by_date: Dict[date, list] = defaultdict(list)
        for row in rows:
            by_date[row["date"]].append(row)

        # Sort dates
        sorted_dates = sorted(by_date.keys())
        rankings: Dict[date, List[Dict]] = {}
        previous_lookup: Dict[str, int] = {}  # name -> rank of previous day

        for d in sorted_dates:
            day_rows = by_date[d]
            # Sort descending by cumulative_fans
            day_rows.sort(key=lambda r: r["cumulative_fans"], reverse=True)

            entries: List[Dict] = []
            prev_fans: Optional[int] = None
            prev_rank: int = 0

            for i, row in enumerate(day_rows):
                name: str = row["trainer_name"]
                fans: int = row["cumulative_fans"]

                # Assign rank (tied fans get same rank)
                if fans == prev_fans:
                    rank = prev_rank
                else:
                    rank = i + 1
                prev_fans = fans
                prev_rank = rank

                prev_day_rank = previous_lookup.get(name)
                entries.append({
                    "name": name,
                    "fans": fans,
                    "rank": rank,
                    "prev_rank": prev_day_rank,
                })
                previous_lookup[name] = rank

            rankings[d] = entries

        return rankings

    # ── Daily delta computation ─────────────────────────────────────────

    @classmethod
    def _compute_daily_deltas(
        cls, rows: list
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        For each member, compute the single-day fan gain (delta) between
        consecutive dates.

        Returns {member_name: [{date, delta}, ...]} with each member's
        list sorted by delta descending.
        """
        # Group by member name, collect date -> cumulative_fans
        member_data: Dict[str, List[Tuple[date, int]]] = defaultdict(list)
        for row in rows:
            member_data[row["trainer_name"]].append(
                (row["date"], row["cumulative_fans"])
            )

        result: Dict[str, List[Dict]] = {}
        for name, entries in member_data.items():
            entries.sort(key=lambda x: x[0])  # sort by date ascending
            deltas: List[Dict] = []
            for i in range(1, len(entries)):
                prev_date, prev_fans = entries[i - 1]
                curr_date, curr_fans = entries[i]
                delta = curr_fans - prev_fans
                deltas.append({
                    "date": curr_date,
                    "delta": delta,
                    "fans_total": curr_fans,
                })
            # Sort by delta descending
            deltas.sort(key=lambda d: d["delta"], reverse=True)
            if deltas:
                result[name] = deltas

        return result

    # ── Analysis helpers ────────────────────────────────────────────────

    @classmethod
    def _compute_first_place_streak(
        cls, daily_rankings: Dict[date, List[Dict]]
    ) -> Dict[str, Any]:
        """
        Find the longest consecutive run at rank 1 across all dates.
        Returns {name, streak_length, start_date, end_date}.
        If no streak found, streak_length is 0.
        """
        current_leader: Optional[str] = None
        current_streak = 0
        best: Dict[str, Any] = {
            "name": None,
            "streak_length": 0,
            "start_date": None,
            "end_date": None,
        }

        for d in sorted(daily_rankings.keys()):
            day_entries = daily_rankings[d]
            if not day_entries:
                continue
            top = day_entries[0]  # first entry = rank 1
            if top["name"] == current_leader:
                current_streak += 1
            else:
                # Check if previous streak was better
                if current_streak > best["streak_length"] and current_leader is not None:
                    best = {
                        "name": current_leader,
                        "streak_length": current_streak,
                        "start_date": None,  # set below
                        "end_date": None,
                    }
                current_leader = top["name"]
                current_streak = 1

        # Final check
        if current_streak > best["streak_length"] and current_leader is not None:
            best = {
                "name": current_leader,
                "streak_length": current_streak,
                "start_date": None,
                "end_date": None,
            }

        return best

    @classmethod
    def _compute_biggest_climbers(
        cls, daily_rankings: Dict[date, List[Dict]]
    ) -> List[Dict[str, Any]]:
        """
        Top 3 biggest single-day rank improvements (positive delta).
        Returns [{name, old_rank, new_rank, delta, date}, ...] sorted by delta desc.
        """
        jumps: List[Dict] = []
        for d in sorted(daily_rankings.keys()):
            entries = daily_rankings[d]
            for entry in entries:
                prev = entry.get("prev_rank")
                if prev is not None:
                    delta = prev - entry["rank"]  # positive = improvement
                    if delta > 0:
                        jumps.append({
                            "name": entry["name"],
                            "old_rank": prev,
                            "new_rank": entry["rank"],
                            "delta": delta,
                            "date": d,
                        })

        jumps.sort(key=lambda j: j["delta"], reverse=True)
        return jumps[:3]

    @classmethod
    def _compute_biggest_fallers(
        cls, daily_rankings: Dict[date, List[Dict]]
    ) -> List[Dict[str, Any]]:
        """
        Top 3 biggest single-day rank drops (negative delta, absolute).
        Returns [{name, old_rank, new_rank, delta, date}, ...] sorted by |delta| desc.
        """
        drops: List[Dict] = []
        for d in sorted(daily_rankings.keys()):
            entries = daily_rankings[d]
            for entry in entries:
                prev = entry.get("prev_rank")
                if prev is not None:
                    delta = prev - entry["rank"]  # negative = drop
                    if delta < 0:
                        drops.append({
                            "name": entry["name"],
                            "old_rank": prev,
                            "new_rank": entry["rank"],
                            "delta": abs(delta),
                            "date": d,
                        })

        drops.sort(key=lambda d: d["delta"], reverse=True)
        return drops[:3]

    @classmethod
    def _compute_rivalries(
        cls, daily_rankings: Dict[date, List[Dict]]
    ) -> List[Dict[str, Any]]:
        """
        Find pairs of members who swapped positions most frequently.
        Returns [{name_a, name_b, swap_count}, ...] sorted by swap_count desc.
        """
        # For each day, get mapping name->rank
        day_rankings: Dict[date, Dict[str, int]] = {
            d: {e["name"]: e["rank"] for e in entries}
            for d, entries in daily_rankings.items()
        }

        sorted_dates = sorted(day_rankings.keys())
        swap_counter: Dict[Tuple[str, str], int] = defaultdict(int)

        for i in range(1, len(sorted_dates)):
            prev = day_rankings[sorted_dates[i - 1]]
            curr = day_rankings[sorted_dates[i]]

            # Consider all member pairs present in both days
            common_names = set(prev.keys()) & set(curr.keys())
            for a, b in combinations(sorted(common_names), 2):
                prev_order = prev[a] < prev[b]
                curr_order = curr[a] < curr[b]
                if prev_order != curr_order:
                    swap_counter[(a, b)] += 1

        rivalries = [
            {"name_a": a, "name_b": b, "swap_count": count}
            for (a, b), count in swap_counter.items()
        ]
        rivalries.sort(key=lambda r: r["swap_count"], reverse=True)
        return rivalries[:3]

    @classmethod
    def _compute_personal_records(
        cls, daily_deltas: Dict[str, List[Dict]]
    ) -> List[Dict[str, Any]]:
        """
        Top 3 best single-day fan gains across all members.
        Returns [{name, delta, date, fans_total}, ...] sorted by delta desc.
        """
        all_bests: List[Dict] = []
        for name, deltas in daily_deltas.items():
            if deltas:
                best = deltas[0]  # already sorted desc
                all_bests.append({
                    "name": name,
                    "delta": best["delta"],
                    "date": best["date"],
                    "fans_total": best["fans_total"],
                })

        all_bests.sort(key=lambda r: r["delta"], reverse=True)
        return all_bests[:3]

    @classmethod
    def _compute_club_record(
        cls, daily_deltas: Dict[str, List[Dict]]
    ) -> Optional[Dict[str, Any]]:
        """
        The single highest single-day fan gain across ALL members.
        Returns {name, delta, date} or None.
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
    def _compute_member_count(cls, rows: list) -> int:
        """Count distinct member names in the dataset."""
        names = {row["trainer_name"] for row in rows}
        return len(names)

    # ── Formatting helpers ──────────────────────────────────────────────

    @classmethod
    def _fmt_fans(cls, n: int) -> str:
        """Format a fan count compactly (e.g., 1.5M, 890K)."""
        if abs(n) >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if abs(n) >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    @classmethod
    def _fmt_date(cls, d: date) -> str:
        """Format date as e.g. 'Jun 5'."""
        return d.strftime("%b %d")

    @classmethod
    def _format_streak(cls, streak: Dict) -> str:
        if not streak["name"] or streak["streak_length"] < 2:
            return "_No significant streak yet this month._"

        # Count how many total days have data
        length = streak["streak_length"]
        return (
            f"**{streak['name']}** has held **#1** for **{length} consecutive "
            f"day{'s' if length != 1 else ''}** so far this month.\n"
            f"_Dominating the leaderboard!_"
        )

    @classmethod
    def _format_climbers(cls, climbers: List[Dict]) -> str:
        if not climbers:
            return "_No significant rank jumps this month._"
        lines = []
        for i, c in enumerate(climbers, 1):
            lines.append(
                f"{i}. **{c['name']}** — #{c['old_rank']} → #{c['new_rank']} "
                f"(+{c['delta']}) on {cls._fmt_date(c['date'])}"
            )
        return "\n".join(lines)

    @classmethod
    def _format_fallers(cls, fallers: List[Dict]) -> str:
        if not fallers:
            return "_No significant rank drops this month._"
        lines = []
        for i, f in enumerate(fallers, 1):
            lines.append(
                f"{i}. **{f['name']}** — #{f['old_rank']} → #{f['new_rank']} "
                f"(-{f['delta']}) on {cls._fmt_date(f['date'])}"
            )
        return "\n".join(lines)

    @classmethod
    def _format_rivalries(cls, rivalries: List[Dict]) -> str:
        if not rivalries:
            return (
                "_No notable rivalries this month — "
                "the leaderboard has been stable._"
            )
        lines = []
        for r in rivalries:
            lines.append(
                f"⚔️ **{r['name_a']}** vs **{r['name_b']}** — "
                f"**{r['swap_count']}** position swap{'s' if r['swap_count'] != 1 else ''}"
            )
        return "\n".join(lines)

    @classmethod
    def _format_records(
        cls,
        prs: List[Dict[str, Any]],
        club_record: Optional[Dict[str, Any]],
    ) -> str:
        parts: List[str] = []

        # Personal records (top 3)
        if prs:
            parts.append("**🏅 Personal Best Single-Day Gain**")
            for i, pr in enumerate(prs, 1):
                parts.append(
                    f"{i}. **{pr['name']}** — **+{cls._fmt_fans(pr['delta'])}** "
                    f"on {cls._fmt_date(pr['date'])} "
                    f"(total: {cls._fmt_fans(pr['fans_total'])})"
                )
        else:
            parts.append("_No personal records available yet._")

        # Club record
        if club_record:
            parts.append(
                f"\n**🌟 Club Record (Month)** — **{club_record['name']}** "
                f"with **+{cls._fmt_fans(club_record['delta'])}** "
                f"on {cls._fmt_date(club_record['date'])}"
            )

        return "\n".join(parts)