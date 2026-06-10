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

        # ── NEW analysis segments ───────────────────────────────────────
        efficiency_king = cls._compute_efficiency_king(daily_rankings, daily_deltas, latest_date)
        brick_wall = cls._compute_brick_wall(daily_rankings, latest_date)
        projected_overtakes = cls._compute_projected_overtakes(daily_rankings, latest_date)
        milestone_watch = cls._compute_milestone_watch(daily_rankings, latest_date)
        consistency = cls._compute_consistency(daily_rankings, daily_deltas, latest_date)
        condensed_movers = cls._compute_condensed_movers(movers)

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

        # ── Section 1: 🔥 HEADLINE NEWS ─────────────────────────────────
        headline_parts: List[str] = []

        # Priority 1: Leader change — make THAT the headline if it happened
        leader_text = cls._format_leader_change(leader_change)
        if leader_text:
            headline_parts.append(f"🏆 {leader_text}")
        else:
            # Priority 2: Punchy summary about the breakout star
            if efficiency_king and brick_wall:
                headline_parts.append(
                    f"**{efficiency_king['name']}** is today's breakout star, "
                    f"while the battle for **#{brick_wall['rank']}** intensifies!"
                )
            elif efficiency_king:
                king_text = cls._format_efficiency_king(efficiency_king)
                if king_text:
                    headline_parts.append(king_text)
            else:
                # Fallback: talk about the current leader
                top_entry = daily_rankings.get(latest_date, [None])[0] if daily_rankings.get(latest_date) else None
                if top_entry:
                    headline_parts.append(
                        f"👑 **{top_entry['name']}** holds the #1 spot "
                        f"with **{cls._fmt_fans(top_entry['fans'])} fans**."
                    )

        embed.add_field(
            name="🔥 HEADLINE NEWS",
            value="\n".join(headline_parts) if headline_parts else "_No news to report._",
            inline=False,
        )

        # ── Section 2: 📈 THE MOMENTUM SHIFT ────────────────────────────
        momentum_parts: List[str] = []

        # The Sprinter (Efficiency King)
        if efficiency_king:
            momentum_parts.append(
                f"**🏃 The Sprinter** — **{efficiency_king['name']}** is on fire! "
                f"Today's gain (**+{cls._fmt_fans(efficiency_king['daily_gain'])}**) is "
                f"**{efficiency_king['pct_above_avg']}%** above their monthly average "
                f"({cls._fmt_fans(int(efficiency_king['avg_daily']))}/day)."
            )

        # The Tank (Brick Wall)
        if brick_wall:
            gap_str = cls._fmt_fans(brick_wall['gap_to_next'])
            momentum_parts.append(
                f"**🛡️ The Tank** — **{brick_wall['name']}** is the most secure at "
                f"**#{brick_wall['rank']}** with a surplus of "
                f"**{cls._fmt_fans(brick_wall['surplus'])}** — "
                f"**{gap_str}** ahead of the next player."
            )

        # Consistency note
        top_over = consistency.get("top_overperformer")
        top_cooler = consistency.get("top_cooler")
        if top_over and top_over["name"] != (efficiency_king or {}).get("name"):
            momentum_parts.append(
                f"**🔥 Overperforming**\n**{top_over['name']}** is running "
                f"**{top_over['pct_diff']}%** above their average today."
            )
        if top_cooler:
            momentum_parts.append(
                f"**❄️ Cooling Down**\n**{top_cooler['name']}** is at "
                f"**{top_cooler['pct_diff']}%** below their average today."
            )

        # Today's Records (compact)
        records_text = cls._format_today_records(today_records, club_record, latest_date)
        if records_text:
            momentum_parts.append(f"\n**🏅 Records**\n{records_text}")

        embed.add_field(
            name="📈 THE MOMENTUM SHIFT",
            value="\n".join(momentum_parts) if momentum_parts else "_No momentum data available._",
            inline=False,
        )

        # ── Section 3: ⚔️ THE BATTLE ZONE ───────────────────────────────
        battle_parts: List[str] = []

        # Sort overtakes: urgent (within 48h) first, then the rest
        urgent_overtakes = [o for o in projected_overtakes if o["eta_days"] < 2]
        later_overtakes = [o for o in projected_overtakes if o["eta_days"] >= 2]

        if urgent_overtakes:
            urgent_text = cls._format_projected_overtakes(urgent_overtakes)
            battle_parts.append(f"**🚨 Urgent Overtakes**\n{urgent_text}")

        if later_overtakes:
            later_text = cls._format_projected_overtakes(later_overtakes)
            battle_parts.append(f"**⏳ On the Horizon**\n{later_text}")

        # Rivalries (condensed) — append if we have them or if there were no overtakes
        rivalries_text = cls._format_rivalries(rivalries)
        if rivalries_text:
            # Only add a section break if we already added overtake content
            prefix = "\n" if battle_parts else ""
            battle_parts.append(f"{prefix}**⚔️ Monthly Rivalries**\n{rivalries_text}")

        embed.add_field(
            name="⚔️ THE BATTLE ZONE",
            value="\n".join(battle_parts) if battle_parts else "_No battles to report._",
            inline=False,
        )

        # ── Section 4: 🎯 MILESTONE TRACKER ─────────────────────────────
        milestone_text = cls._format_milestone_watch(milestone_watch)
        embed.add_field(
            name="🎯 MILESTONE TRACKER",
            value=milestone_text,
            inline=False,
        )

        # ── Section 5: ⬆️⬇️ TOP MOVERS (condensed) ────────────────────
        movers_text = cls._format_condensed_movers(condensed_movers)
        embed.add_field(
            name="⬆️⬇️ TOP MOVERS",
            value=movers_text,
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

    # ── NEW: Efficiency King (overperformer) ─────────────────────────────

    @classmethod
    def _compute_efficiency_king(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        daily_deltas: Dict[str, List[Dict]],
        latest_date: date,
    ) -> Optional[Dict[str, Any]]:
        """
        Identify the player whose Daily (delta on latest_date) is the highest
        percentage above their Avg (mean daily delta for the month).
        Returns {name, daily_gain, avg_daily, pct_above_avg} or None.
        """
        candidates: List[Dict] = []
        today_entries = {e["name"]: e for e in daily_rankings.get(latest_date, [])}

        for name, deltas in daily_deltas.items():
            if name not in today_entries:
                continue
            # Avg = mean of all their deltas in the month
            if not deltas:
                continue
            total_delta = sum(d["delta"] for d in deltas)
            avg_daily = total_delta / len(deltas)
            if avg_daily <= 0:
                continue
            # daily_gain = the delta on the latest date
            today_delta = today_entries[name]["daily"]
            if today_delta <= 0:
                continue
            pct_above = ((today_delta - avg_daily) / avg_daily) * 100
            if pct_above > 0:
                candidates.append({
                    "name": name,
                    "daily_gain": today_delta,
                    "avg_daily": round(avg_daily, 1),
                    "pct_above_avg": round(pct_above, 1),
                })

        if not candidates:
            return None
        candidates.sort(key=lambda c: c["pct_above_avg"], reverse=True)
        return candidates[0]

    # ── NEW: Brick Wall (largest surplus buffer) ────────────────────────

    @classmethod
    def _compute_brick_wall(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        latest_date: date,
    ) -> Optional[Dict[str, Any]]:
        """
        Find the player with the highest surplus (deficit_surplus from quota).
        Compute gap_to_next = their surplus - surplus of the player ranked
        directly below them.
        Returns {name, surplus, gap_to_next, rank} or None if <2 members.
        """
        today_entries = daily_rankings.get(latest_date, [])
        if len(today_entries) < 2:
            return None

        # Find member with highest surplus
        best = max(today_entries, key=lambda e: e["surplus"])
        best_idx = today_entries.index(best)

        # The rank below them (if any)
        if best_idx + 1 < len(today_entries):
            below = today_entries[best_idx + 1]
            gap_to_next = best["surplus"] - below["surplus"]
        else:
            gap_to_next = best["surplus"]  # last place, gap = their whole surplus

        return {
            "name": best["name"],
            "surplus": best["surplus"],
            "gap_to_next": gap_to_next,
            "rank": best["rank"],
        }

    # ── NEW: Projected Overtakes (The Chase) ────────────────────────────

    @classmethod
    def calculate_eta(
        cls, player_above: Dict, player_below: Dict
    ) -> Optional[float]:
        """
        Static helper: compute ETA in days for player_below to overtake
        player_above, given their current fans and daily rates.
        Returns None if the chase is not viable.
        """
        gap = player_above["fans"] - player_below["fans"]
        daily_diff = player_below["daily"] - player_above["daily"]
        if daily_diff <= 0:
            return None
        return gap / daily_diff

    @classmethod
    def _compute_projected_overtakes(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        latest_date: date,
    ) -> List[Dict[str, Any]]:
        """
        Iterate adjacent rank pairs. If the lower-ranked player has a higher
        Daily than the player above, compute ETA. Include only if 0 < ETA <= 14.
        Returns list sorted by ETA ascending, max 3.
        """
        today_entries = daily_rankings.get(latest_date, [])
        overtakes: List[Dict] = []

        for i in range(len(today_entries) - 1):
            above = today_entries[i]
            below = today_entries[i + 1]

            eta = cls.calculate_eta(above, below)
            if eta is None or eta <= 0 or eta > 14:
                continue

            overtakes.append({
                "challenger": below["name"],
                "target": above["name"],
                "gap_fans": above["fans"] - below["fans"],
                "daily_diff": below["daily"] - above["daily"],
                "eta_days": round(eta, 1),
                "target_rank": above["rank"],
            })

        overtakes.sort(key=lambda o: o["eta_days"])
        return overtakes[:3]

    # ── NEW: Milestone Watch ────────────────────────────────────────────

    @classmethod
    def _compute_milestone_watch(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        latest_date: date,
    ) -> List[Dict[str, Any]]:
        """
        Check every member on the latest_date. If their Total is within 5%
        of a major milestone, add to the watch list.
        Milestones: 1M, 5M, 10M, 25M, 50M, 100M.
        Returns sorted by amount_away ascending, max 3.
        """
        milestones = [1_000_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000, 100_000_000]
        today_entries = daily_rankings.get(latest_date, [])
        watch: List[Dict] = []

        for entry in today_entries:
            total = entry["fans"]
            for ms in milestones:
                if total >= ms:
                    continue
                # Check if within 5% of this milestone
                if total >= ms * 0.95:
                    watch.append({
                        "name": entry["name"],
                        "total": total,
                        "milestone": ms,
                        "amount_away": ms - total,
                        "pct_to_milestone": round((total / ms) * 100, 1),
                    })
                    break  # only the *next* milestone per player

        watch.sort(key=lambda w: w["amount_away"])
        return watch[:3]

    # ── NEW: Consistency Check ──────────────────────────────────────────

    @classmethod
    def _compute_consistency(
        cls,
        daily_rankings: Dict[date, List[Dict]],
        daily_deltas: Dict[str, List[Dict]],
        latest_date: date,
    ) -> Dict[str, Any]:
        """
        For each member with data on the latest date, compare Daily to Avg.
        Returns the most extreme overperformer and most extreme cooler
        by percentage difference, plus a boolean indicating overall status.
        """
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

        return {
            "top_overperformer": overperformers[0] if overperformers else None,
            "top_cooler": coolers[0] if coolers else None,
            "overperformer_count": len(overperformers),
            "cooler_count": len(coolers),
        }

    # ── NEW: Condensed Movers (top 3 by absolute delta) ─────────────────

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
    def _format_condensed_movers(cls, condensed: List[Dict]) -> str:
        """Top 3 movers in a compact single line, e.g. 📈 PlayerA (+3) · 📉 PlayerB (-2)."""
        if not condensed:
            return "_No rank changes today._"
        parts: List[str] = []
        for m in condensed:
            icon = "📈" if m["direction"] == "up" else "📉"
            old_r = m["old_rank"] if m["old_rank"] is not None else "?"
            parts.append(
                f"{icon} **{m['name']}** #{old_r}→#{m['new_rank']} ({'+' if m['direction'] == 'up' else '-'}{m['abs_delta']})"
            )
        return " · ".join(parts)

    @classmethod
    def _format_efficiency_king(cls, king: Optional[Dict]) -> Optional[str]:
        """Return narrative sentence about the top overperformer."""
        if not king:
            return None
        return (
            f"🏃 **{king['name']}** is on fire! "
            f"Today's gain (**+{cls._fmt_fans(king['daily_gain'])}**) is "
            f"**{king['pct_above_avg']}%** above their monthly average "
            f"({cls._fmt_fans(int(king['avg_daily']))}/day)."
        )

    @classmethod
    def _format_brick_wall(cls, wall: Optional[Dict]) -> Optional[str]:
        """Return narrative sentence about the hardest player to dethrone."""
        if not wall:
            return None
        gap_str = cls._fmt_fans(wall['gap_to_next'])
        return (
            f"🛡️ **{wall['name']}** is the most secure at **#{wall['rank']}** "
            f"with a surplus of **{cls._fmt_fans(wall['surplus'])}** "
            f"— **{gap_str}** ahead of the next player."
        )

    @classmethod
    def _format_projected_overtakes(cls, overtakes: List[Dict]) -> str:
        """Format projected overtakes list with natural language ETAs."""
        if not overtakes:
            return "_No imminent overtakes detected._"
        lines: List[str] = []
        for o in overtakes:
            eta = o['eta_days']
            if eta < 1:
                days_str = "Expected TODAY"
            elif eta < 2:
                days_str = "Expected TOMORROW"
            else:
                days_str = f"In {round(eta)} days"
            lines.append(
                f"• **{o['challenger']}** is projected to overtake **{o['target']}** "
                f"for **#{o['target_rank']}** — **{days_str}** "
                f"(closing {cls._fmt_fans(o['gap_fans'])} gap at "
                f"+{cls._fmt_fans(o['daily_diff'])}/day)"
            )
        return "\n".join(lines)

    @classmethod
    def _format_milestone_watch(cls, watch: List[Dict]) -> str:
        """Format milestone tracker items with a text-based progress bar."""
        if not watch:
            return "_No members approaching major milestones._"
        lines: List[str] = []
        for w in watch:
            ms_label = cls._fmt_fans(w['milestone'])
            pct = w['pct_to_milestone']
            # Build a 10-block progress bar
            filled = int((pct / 100) * 10)
            filled = max(0, min(10, filled))
            empty = 10 - filled
            bar = "▰" * filled + "▱" * empty
            lines.append(
                f"🎯 **{w['name']}** — [{bar}] {pct}% to **{ms_label}**"
            )
        return "\n".join(lines)

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
