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
        latest_date = max(daily_rankings.keys())
        yesterday_date = cls._get_previous_day(daily_rankings, latest_date)

        movers = cls._compute_today_movers(daily_rankings, latest_date)
        leader_change = cls._compute_leader_change(daily_rankings, latest_date, yesterday_date)
        rivalries = cls._compute_rivalries(daily_rankings)
        today_records = cls._compute_today_records(daily_deltas, latest_date)
        club_record = cls._compute_club_record(daily_deltas)

        # ── Build embed ─────────────────────────────────────────────────
        embed = discord.Embed(
            title=f"📰 Leaderboard News — {club_name}",
            description=(
                f"**{month_label}** · {member_count} members\n"
                f"_{cls._fmt_date(latest_date)} update_"
            ),
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )

        # Section 1 — Today's Movers
        movers_text = cls._format_today_movers(movers)
        embed.add_field(
            name="⬆️⬇️ Today's Movers",
            value=movers_text,
            inline=False,
        )

        # Section 2 — Leader Change (only if something happened)
        leader_text = cls._format_leader_change(leader_change)
        if leader_text:
            embed.add_field(
                name="🏆 Leader Change",
                value=leader_text,
                inline=False,
            )

        # Section 3 — Rivalries
        rivalries_text = cls._format_rivalries(rivalries)
        embed.add_field(
            name="⚔️ Rivalries",
            value=rivalries_text,
            inline=False,
        )

        # Section 4 — Today's Records
        records_text = cls._format_today_records(today_records, club_record, latest_date)
        embed.add_field(
            name="🔥 Today's Records",
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
        by_date: Dict[date, list] = defaultdict(list)
        for row in rows:
            by_date[row["date"]].append(row)

        sorted_dates = sorted(by_date.keys())
        rankings: Dict[date, List[Dict]] = {}
        previous_lookup: Dict[str, int] = {}

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
                entries.append({
                    "name": name,
                    "fans": fans,
                    "rank": rank,
                    "prev_rank": prev_day_rank,
                })
                previous_lookup[name] = rank

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
    def _compute_rivalries(
        cls, daily_rankings: Dict[date, List[Dict]]
    ) -> List[Dict[str, Any]]:
        """
        Find pairs of members who swapped positions most frequently
        over the entire month.
        Returns [{name_a, name_b, swap_count, rank_range, who_leads, fan_gap}, ...]
        sorted desc.
        """
        day_rankings: Dict[date, Dict[str, int]] = {
            d: {e["name"]: e["rank"] for e in entries}
            for d, entries in daily_rankings.items()
        }

        # Also track name->fans for latest day (to compute gap)
        latest_date = max(daily_rankings.keys())
        latest_fans: Dict[str, int] = {
            e["name"]: e["fans"]
            for e in daily_rankings.get(latest_date, [])
        }

        sorted_dates = sorted(day_rankings.keys())
        swap_counter: Dict[Tuple[str, str], int] = defaultdict(int)

        for i in range(1, len(sorted_dates)):
            prev = day_rankings[sorted_dates[i - 1]]
            curr = day_rankings[sorted_dates[i]]

            common_names = set(prev.keys()) & set(curr.keys())
            for a, b in combinations(sorted(common_names), 2):
                prev_order = prev[a] < prev[b]
                curr_order = curr[a] < curr[b]
                if prev_order != curr_order:
                    swap_counter[(a, b)] += 1

        rivalries: List[Dict[str, Any]] = []
        for (a, b), count in swap_counter.items():
            # Current ranks on the latest day
            rank_a = day_rankings.get(latest_date, {}).get(a)
            rank_b = day_rankings.get(latest_date, {}).get(b)
            # rank_range = the two positions they currently occupy (sorted)
            if rank_a is not None and rank_b is not None:
                min_rank = min(rank_a, rank_b)
                max_rank = max(rank_a, rank_b)
            else:
                min_rank = 1
                max_rank = 1
            fans_a = latest_fans.get(a, 0)
            fans_b = latest_fans.get(b, 0)

            who_leads: Optional[str] = None
            fan_gap: int = 0
            if rank_a is not None and rank_b is not None:
                if rank_a < rank_b:
                    who_leads = a
                    fan_gap = fans_a - fans_b
                elif rank_b < rank_a:
                    who_leads = b
                    fan_gap = fans_b - fans_a
                else:
                    # Same rank — whoever has more fans leads
                    if fans_a >= fans_b:
                        who_leads = a
                        fan_gap = fans_a - fans_b
                    else:
                        who_leads = b
                        fan_gap = fans_b - fans_a

            rivalries.append({
                "name_a": a,
                "name_b": b,
                "swap_count": count,
                "rank_range": (min_rank, max_rank),
                "who_leads": who_leads,
                "fan_gap": fan_gap,
            })

        rivalries.sort(key=lambda r: r["swap_count"], reverse=True)
        return rivalries[:3]

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
    def _format_today_movers(cls, movers: Dict[str, List[Dict]]) -> str:
        parts: List[str] = []

        # Climbers
        if movers["climbers"]:
            parts.append("**⬆️ Risers**")
            for c in movers["climbers"]:
                parts.append(
                    f"• **{c['name']}** — #{c['old_rank']} → #{c['new_rank']} "
                    f"(+{c['delta']})"
                )
        else:
            parts.append("_No one climbed today._")

        # Fallers
        if movers["fallers"]:
            parts.append("\n**⬇️ Fallers**")
            for f in movers["fallers"]:
                parts.append(
                    f"• **{f['name']}** — #{f['old_rank']} → #{f['new_rank']} "
                    f"(-{f['delta']})"
                )
        else:
            parts.append("\n_No one dropped today._")

        return "\n".join(parts)

    @classmethod
    def _format_leader_change(cls, leader_change: Dict[str, Any]) -> Optional[str]:
        """Return formatted text if leader changed, or None to skip the field."""
        if not leader_change["changed"]:
            return None

        old = leader_change["old_leader"]
        new = leader_change["new_leader"]
        streak = leader_change["old_streak"]

        lines = [f"**{new}** has overtaken **{old}** for **#1!**"]

        if streak >= 3:
            lines.append(
                f"_{old} had held the top spot for {streak} consecutive "
                f"day{'s' if streak != 1 else ''} before being dethroned._"
            )
        else:
            lines.append("_The leaderboard sees a new champion today!_")

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
            rank_min, rank_max = r["rank_range"]
            rank_str = f"#{rank_min}" if rank_min == rank_max else f"#{rank_min}-#{rank_max}"

            gap_str = ""
            if r["who_leads"] and r["fan_gap"] >= 0:
                gap_str = f" 🔺 {r['who_leads']} leads by {cls._fmt_fans(r['fan_gap'])}"

            lines.append(
                f"⚔️ **{r['name_a']}** vs **{r['name_b']}** — "
                f"fighting for {rank_str} ({r['swap_count']} swap{'s' if r['swap_count'] != 1 else ''})"
                f"{gap_str}"
            )
        return "\n".join(lines)

    @classmethod
    def _format_today_records(
        cls,
        today_records: Dict[str, Any],
        club_record: Optional[Dict[str, Any]],
        latest_date: date,
    ) -> str:
        parts: List[str] = []

        # Today's personal bests — ALL of them, no truncation
        if today_records["members"]:
            parts.append("**🏅 Personal Bests Set Today**")
            for pr in today_records["members"]:
                line = f"• **{pr['name']}** — **+{cls._fmt_fans(pr['delta'])}**"
                if pr["prev_best_delta"] is not None:
                    line += f" breaking previous *{cls._fmt_fans(pr['prev_best_delta'])}*"
                parts.append(line)
        else:
            parts.append("_No personal bests set today._")

        # Club record
        if club_record:
            record_info = (
                f"\n**🌟 Month's Best** — **{club_record['name']}** "
                f"with **+{cls._fmt_fans(club_record['delta'])}** "
                f"on {cls._fmt_date(club_record['date'])}"
            )
            # If the club record was set today, highlight it
            if club_record["date"] == latest_date:
                record_info += " ⭐ _(set today!)_"
            parts.append(record_info)

        return "\n".join(parts)
