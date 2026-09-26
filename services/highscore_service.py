"""
Highscore Service — generates an embed showing all-time club highscores:
best single-day fan gain, best monthly total, best club rank achieved.

Uses Uma.moe API for member-level data (lifetime cumulative fans, no monthly
reset issue) and the database for club rank history.
"""
import logging
import calendar
import json
import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import aiohttp
import discord

from config.settings import COLOR_INFO, UMAMOE_API_KEY
from models import ClubRankHistory

logger = logging.getLogger(__name__)

# Earliest possible game data (game launched June 26, 2025)
EARLIEST_YEAR = 2025
EARLIEST_MONTH = 6


class HighscoreService:
    """Computes and assembles all-time club highscore data into a Discord embed."""

    MAX_REIGN_GAP_DAYS = 2

    # ── Public entry point ──────────────────────────────────────────────

    @classmethod
    async def generate_highscore_embed(
        cls, club_id: UUID, club_name: str, circle_id: Optional[str] = None
    ) -> discord.Embed:
        """
        Fetch all months from the Uma.moe API (lifetime cumulative values),
        compute member-level highscores, and fetch club rank highscores from DB.
        Returns a single rich Embed.
        """
        if not circle_id:
            raise ValueError(
                f"Club {club_name} has no circle_id configured. Cannot fetch API data."
            )

        # 1. Fetch all months from API (current month backwards to launch)
        api_rows, monthly_ranks = await cls._fetch_all_months(circle_id)
        if not api_rows:
            raise ValueError(
                f"No data available for {club_name} from Uma.moe API."
            )

        # 2. Compute member-level highscores from lifetime cumulative values
        best_daily = cls._compute_best_daily_gain(api_rows)
        best_monthly = cls._compute_best_monthly_total(api_rows)

        # 2b. Compute longest streak as #1
        longest_streak = cls._compute_longest_first_place_streak(api_rows)

        # 3a. Compute best monthly rank from API (excluding current/incomplete month)
        now = datetime.now(timezone.utc)
        current_key = (now.year, now.month)
        best_monthly_rank: Optional[Tuple[int, date]] = None  # (rank, date)
        for (year, month), rank in monthly_ranks.items():
            if (year, month) == current_key:
                continue  # skip current incomplete month
            if rank is not None:
                if best_monthly_rank is None or rank < best_monthly_rank[0]:
                    # Use the 1st of the month as the achievement date
                    best_monthly_rank = (rank, date(year, month, 1))

        # 3b. Compute club-level rank highscores from DB
        best_rank = await ClubRankHistory.get_best_rank(club_id)

        # 4. Assemble Embed
        embed = discord.Embed(
            title=f"🏆 All-Time Club Highscores — {club_name}",
            description=(
                f"Records from available snapshots for **{club_name}**. "
                "Daily dates use the source snapshot dates; club rank uses bot-recorded history."
            ),
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )

        # --- Best Daily Gain ---
        if best_daily:
            daily_value = (
                f"**{best_daily['name']}** "
                f"+{cls._fmt_fans(best_daily['delta'])} fans "
                f"(achieved {best_daily['date'].strftime('%B %d, %Y')})"
            )
        else:
            daily_value = "_No data available._"
        embed.add_field(
            name="🔥 Best Single-Day Gain",
            value=daily_value,
            inline=False,
        )

        # --- Best Monthly Total ---
        if best_monthly:
            monthly_value = (
                f"**{best_monthly['name']}** "
                f"{cls._fmt_fans(best_monthly['total'])} fans "
                f"(achieved {best_monthly['month']})"
            )
        else:
            monthly_value = "_No data available._"
        embed.add_field(
            name="📅 Best Monthly Total",
            value=monthly_value,
            inline=False,
        )

        # --- Best Club Rank ---
        if best_rank and best_rank.get("best_club_rank") is not None:
            rank_date = best_rank["best_club_rank_date"]
            date_str = rank_date.strftime("%B %d, %Y") if rank_date else "Unknown"
            club_rank_value = f"**#{best_rank['best_club_rank']}** (achieved {date_str})"
            history_start = best_rank.get("club_rank_history_start")
            history_end = best_rank.get("club_rank_history_end")
            if history_start and history_end:
                club_rank_value += (
                    f"\nRecorded snapshots: {history_start:%B %d, %Y}"
                    f" → {history_end:%B %d, %Y}"
                )
        else:
            club_rank_value = "_No rank data available._"
        
        embed.add_field(
            name="👑 Best Recorded Club Rank",
            value=club_rank_value,
            inline=False,
        )
        
        if best_monthly_rank is not None:
            rank, rank_date = best_monthly_rank
            date_str = rank_date.strftime("%B %Y")
            monthly_rank_value = f"**#{rank}** (achieved {date_str})"
        else:
            monthly_rank_value = "_No rank data available._"
        
        embed.add_field(
            name="📊 Best Completed-Month Club Rank",
            value=monthly_rank_value,
            inline=False,
        )

        # --- Longest Top Daily Streak ---
        if longest_streak:
            start_str = longest_streak["start_date"].strftime("%B %d, %Y")
            end_str = longest_streak["end_date"].strftime("%B %d, %Y")
            streak_value = (
                f"**{longest_streak['name']}** — {longest_streak['streak']} consecutive days with the highest daily fan gain\n"
                f"({start_str} → {end_str})"
            )
        else:
            streak_value = "_No streak data available._"
        embed.add_field(
            name="👑 Longest Top Daily Streak",
            value=streak_value,
            inline=False,
        )

        # --- Longest #1 Reign ---
        longest_total_streak = cls._compute_longest_first_place_streak_by_total(api_rows)
        if longest_total_streak:
            start_str = longest_total_streak["start_date"].strftime("%B %d, %Y")
            end_str = longest_total_streak["end_date"].strftime("%B %d, %Y")
            total_streak_value = (
                f"**{longest_total_streak['name']}** — {longest_total_streak['streak']} consecutive days in 1st place\n"
                f"({start_str} → {end_str})"
            )
            inferred = longest_total_streak.get("inferred_days", 0)
            if inferred:
                total_streak_value += (
                    f"\nIncludes {inferred} inferred day(s) across gaps of at most "
                    f"{cls.MAX_REIGN_GAP_DAYS} days, with the same leader on both sides."
                )
        else:
            total_streak_value = "_No streak data available._"
        embed.add_field(
            name="👑 Longest #1 Reign",
            value=total_streak_value,
            inline=False,
        )

        embed.set_footer(text=f"{club_name} · All-Time Records")
        return embed

    # ── API Fetching ────────────────────────────────────────────────────

    @classmethod
    async def _fetch_all_months(
        cls, circle_id: str
    ) -> Tuple[List[Dict[str, Any]], Dict[Tuple[int, int], Optional[int]]]:
        """
        Fetch all months from the API, current month backwards to game launch.
        Returns:
          - flat list of {date, lifetime_fans, trainer_name} entries
          - dict of {(year, month): monthly_rank_or_None} for each month fetched
        """
        all_rows: List[Dict[str, Any]] = []
        all_ranks: Dict[Tuple[int, int], Optional[int]] = {}
        now = datetime.now(timezone.utc)

        year = now.year
        month = now.month
        first_month = True

        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"accept": "application/json", "X-API-Key": UMAMOE_API_KEY}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            while (year > EARLIEST_YEAR) or (year == EARLIEST_YEAR and month >= EARLIEST_MONTH):
                logger.info(f"Fetching API data for {year}-{month:02d}...")
                rows, monthly_rank = await cls._fetch_and_parse_api_month(
                    circle_id, year, month, session=session
                )
                all_ranks[(year, month)] = monthly_rank
                if rows:
                    all_rows.extend(rows)
                else:
                    # The current month can legitimately be empty before the
                    # upstream publishes its first snapshot. Older empty
                    # months still mark the beginning of the club's history.
                    if not first_month:
                        logger.info(
                            f"No data for {year}-{month:02d}, club likely didn't exist yet."
                        )
                        break
                year, month = cls._prev_month(year, month)
                first_month = False

        return all_rows, all_ranks

    @classmethod
    async def _fetch_and_parse_api_month(
        cls,
        circle_id: str,
        year: int,
        month: int,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """
        Fetch a single month from Uma.moe API.
        Returns (rows, monthly_rank):
          - rows: [{date, lifetime_fans, trainer_name}, ...]
          - monthly_rank: top-level monthly_rank from API, or None if unavailable
        """
        base_url = "https://uma.moe/api/v4/circles"
        api_url = f"{base_url}?circle_id={circle_id}&year={year}&month={month}"

        async def fetch(active_session: aiohttp.ClientSession):
            async with active_session.get(api_url) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Uma.moe API returned HTTP {response.status} "
                        f"for {year}-{month:02d}"
                    )
                return await response.json()

        try:
            if session is None:
                timeout = aiohttp.ClientTimeout(total=30)
                headers = {"accept": "application/json", "X-API-Key": UMAMOE_API_KEY}
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as owned_session:
                    data = await fetch(owned_session)
            else:
                data = await fetch(session)
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError) as e:
            raise RuntimeError(
                f"Uma.moe API request failed for {year}-{month:02d}"
            ) from e
        except Exception as e:
            raise RuntimeError(
                f"Unexpected Uma.moe API error for {year}-{month:02d}"
            ) from e

        # Extract top-level monthly rank
        monthly_rank: Optional[int] = data.get("circle", {}).get("monthly_rank")

        members = data.get("members", [])
        if not members:
            return [], monthly_rank

        rows: List[Dict[str, Any]] = []
        _, last_day = calendar.monthrange(year, month)

        for member in members:
            trainer_name = member.get("trainer_name")
            daily_fans = member.get("daily_fans", [])
            next_month_start = member.get("next_month_start")

            if not trainer_name or not daily_fans:
                continue

            # daily_fans is lifetime cumulative — store as-is
            for day_idx, lifetime_total in enumerate(daily_fans):
                day_num = day_idx + 1
                if day_num > last_day:
                    break
                if lifetime_total <= 0:
                    continue

                row_date = date(year, month, day_num)
                if row_date > datetime.now(timezone.utc).date():
                    continue
                rows.append({
                    "date": row_date,
                    "lifetime_fans": lifetime_total,
                    "trainer_name": trainer_name,
                })

            # If next_month_start is available, append a synthetic end-of-month
            # entry so _compute_best_monthly_total can use the true final value.
            if next_month_start is not None and next_month_start > 0:
                end_of_month = date(year, month, last_day)
                rows.append({
                    "date": end_of_month,
                    "lifetime_fans": next_month_start,
                    "trainer_name": trainer_name,
                    "is_end_of_month": True,
                })

        return rows, monthly_rank

    # ── Computation Logic ───────────────────────────────────────────────

    @classmethod
    def _compute_best_daily_gain(
        cls, rows: list
    ) -> Optional[Dict[str, Any]]:
        """
        Compute the best single-day fan gain from lifetime cumulative values.
        Groups by member, sorts by date, computes deltas between consecutive
        days. Only counts deltas where dates are exactly 1 day apart.
        Uses lifetime values (no monthly reset issues).
        """
        member_data: Dict[str, List[Tuple[date, int]]] = defaultdict(list)
        for row in rows:
            if row.get("is_end_of_month"):
                continue
            member_data[row["trainer_name"]].append(
                (row["date"], row["lifetime_fans"])
            )

        best: Optional[Dict[str, Any]] = None

        for name, entries in member_data.items():
            entries.sort(key=lambda x: x[0])
            for i in range(1, len(entries)):
                prev_date, prev_fans = entries[i - 1]
                curr_date, curr_fans = entries[i]

                # Must be consecutive calendar days
                days_diff = (curr_date - prev_date).days
                if days_diff != 1:
                    continue

                delta = curr_fans - prev_fans
                if delta > 0 and (best is None or delta > best["delta"]):
                    best = {
                        "name": name,
                        "delta": delta,
                        "date": curr_date,
                    }

        return best

    @classmethod
    def _compute_best_monthly_total(
        cls, rows: list
    ) -> Optional[Dict[str, Any]]:
        """Compare gains within each member's month, never across absences.

        The first observed snapshot is the baseline, including for mid-month
        joins. A synthetic endpoint supplies the final total, not the baseline.
        """
        monthly_rows = defaultdict(list)
        for row in rows:
            d = row["date"]
            monthly_rows[(row["trainer_name"], d.year, d.month)].append(row)

        best = None
        for (name, year, month), entries in sorted(monthly_rows.items()):
            regular = sorted(
                (row for row in entries if not row.get("is_end_of_month")),
                key=lambda row: row["date"],
            )
            if not regular:
                continue
            endpoints = [row for row in entries if row.get("is_end_of_month")]
            final = endpoints[-1] if endpoints else regular[-1]
            total = final["lifetime_fans"] - regular[0]["lifetime_fans"]
            if total > 0 and (best is None or total > best["total"]):
                best = {
                    "name": name,
                    "total": total,
                    "month": f"{calendar.month_name[month]} {year}",
                }
        return best

    @classmethod
    def _compute_longest_first_place_streak(
        cls, rows: list
    ) -> Optional[Dict[str, Any]]:
        """
        Compute the longest consecutive streak of holding 1st place (highest
        daily fan gain) across all dates in the data.

        Uses gains between consecutive daily snapshots to determine the leader.
        Dates refer to those snapshots, not verified in-game earning dates.

        Returns a dict with keys:
          - name: the member who held #1
          - streak: number of consecutive days
          - start_date: first day of the streak
          - end_date: last day of the streak
        or None if insufficient data.
        """
        # 1. Group rows by member, sorted by date
        member_data: Dict[str, List[Tuple[date, int]]] = defaultdict(list)
        for row in rows:
            if row.get("is_end_of_month"):
                continue
            member_data[row["trainer_name"]].append(
                (row["date"], row["lifetime_fans"])
            )

        # 2. Compute daily gain per member per date
        # daily_gain[date][member] = fans gained on that date
        daily_gain: Dict[date, Dict[str, int]] = defaultdict(dict)
        for name, entries in member_data.items():
            entries.sort(key=lambda x: x[0])
            for i in range(1, len(entries)):
                prev_date, prev_fans = entries[i - 1]
                curr_date, curr_fans = entries[i]

                # Must be consecutive calendar days
                days_diff = (curr_date - prev_date).days
                if days_diff != 1:
                    continue

                delta = curr_fans - prev_fans
                if delta > 0:
                    # Keep the largest observed delta for a member/date.
                    existing = daily_gain[curr_date].get(name, 0)
                    if delta > existing:
                        daily_gain[curr_date][name] = delta

        sorted_dates = sorted(daily_gain.keys())
        if len(sorted_dates) < 2:
            return None

        # 3. For each date, find the member with the highest daily gain.
        #    If there's a tie, no single clear #1 for that day.
        daily_leader: Dict[date, Optional[str]] = {}
        for d in sorted_dates:
            gains = daily_gain[d]
            if not gains:
                daily_leader[d] = None
                continue
            max_gain = max(gains.values())
            leaders = [name for name, g in gains.items() if g == max_gain]
            if len(leaders) == 1:
                daily_leader[d] = leaders[0]
            else:
                daily_leader[d] = None  # tie — no clear leader

        # 4. Walk through dates tracking streaks
        best_streak = 0
        best_name: Optional[str] = None
        best_start: Optional[date] = None
        best_end: Optional[date] = None

        current_name: Optional[str] = None
        current_streak = 0
        current_start: Optional[date] = None
        previous_streak_date: Optional[date] = None

        for d in sorted_dates:
            if previous_streak_date is None or d != previous_streak_date + timedelta(days=1):
                current_name = None
                current_streak = 0
                current_start = None
            previous_streak_date = d
            leader = daily_leader[d]
            if leader is None:
                # Tie or no data — reset
                current_name = None
                current_streak = 0
                current_start = None
                continue

            if leader == current_name:
                # Same leader — extend streak
                current_streak += 1
            else:
                # New leader — start new streak
                current_name = leader
                current_streak = 1
                current_start = d

            if current_streak > best_streak:
                best_streak = current_streak
                best_name = current_name
                best_start = current_start
                best_end = d

        if best_streak < 2:
            return None

        return {
            "name": best_name,
            "streak": best_streak,
            "start_date": best_start,
            "end_date": best_end,
        }

    @classmethod
    def _compute_longest_first_place_streak_by_total(
        cls, rows: list
    ) -> Optional[Dict[str, Any]]:
        """Bridge short unknown gaps only between matching confirmed leaders.

        Monthly resets and missing snapshots are unknown. Positive-gain ties
        break a sole-leader reign. Unresolved trailing gaps never extend it.
        """
        fans_by_date = defaultdict(dict)
        member_dates = defaultdict(list)
        baselines = {}
        today = datetime.now(timezone.utc).date()
        for row in sorted(rows, key=lambda row: row["date"]):
            if (row.get("is_end_of_month") or row["lifetime_fans"] <= 0
                    or row["date"] > today):
                continue
            d, name = row["date"], row["trainer_name"]
            fans = row["lifetime_fans"]
            baselines.setdefault((name, d.year, d.month), fans)
            fans_by_date[d][name] = fans
            member_dates[name].append(d)
        if not fans_by_date:
            return None

        # The parser omits zero lifetime values. A short internal hole in a
        # member's snapshots must not give a competitor a false confirmed win.
        incomplete_dates = set()
        for dates in member_dates.values():
            for before, after in zip(dates, dates[1:]):
                gap = (after - before).days - 1
                if 0 < gap <= cls.MAX_REIGN_GAP_DAYS:
                    incomplete_dates.update(before + timedelta(days=i)
                                            for i in range(1, gap + 1))

        best = None
        current_name = None
        current_start = None
        last_confirmed = None
        inferred_days = 0
        d = min(fans_by_date)
        end = max(fans_by_date)
        while d <= end:
            gains = {
                name: fans - baselines[(name, d.year, d.month)]
                for name, fans in fans_by_date.get(d, {}).items()
            }
            highest = max(gains.values(), default=0)
            leaders = [name for name, gain in gains.items() if gain == highest]
            unknown = d in incomplete_dates or not gains or highest <= 0
            if unknown:
                # Keep the last confirmed endpoint pending. Only a subsequent
                # matching leader can add these days to the record.
                pass
            elif len(leaders) != 1:
                current_name = None  # observed positive tie, not missing data
                last_confirmed = None
            else:
                leader = leaders[0]
                gap = (d - last_confirmed).days - 1 if last_confirmed else 0
                if leader == current_name and gap <= cls.MAX_REIGN_GAP_DAYS:
                    inferred_days += gap
                else:
                    current_name = leader
                    current_start = d
                    inferred_days = 0
                last_confirmed = d
                length = (d - current_start).days + 1
                if length >= 2 and (best is None or length > best["streak"]):
                    best = {
                        "name": leader,
                        "streak": length,
                        "start_date": current_start,
                        "end_date": d,
                        "inferred_days": inferred_days,
                    }
            d += timedelta(days=1)
        return best

    # ── Date Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _prev_month(year: int, month: int) -> Tuple[int, int]:
        if month == 1:
            return year - 1, 12
        return year, month - 1

    # ── Formatting Utilities ────────────────────────────────────────────

    @classmethod
    def _fmt_fans(cls, n: int) -> str:
        abs_n = abs(n)
        if abs_n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        elif abs_n >= 1_000:
            return f"{n / 1_000:.1f}K"
        else:
            return str(n)
