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
from datetime import date, datetime, timezone
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
            description=f"Records spanning all available history for **{club_name}**.",
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
        else:
            club_rank_value = "_No rank data available._"
        
        embed.add_field(
            name="👑 Club Rank:",
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
            name="📊 Monthly Club Rank:",
            value=monthly_rank_value,
            inline=False,
        )

        # --- Longest 1st Place Streak ---
        if longest_streak:
            start_str = longest_streak["start_date"].strftime("%B %d, %Y")
            end_str = longest_streak["end_date"].strftime("%B %d, %Y")
            streak_value = (
                f"**{longest_streak['name']}** — {longest_streak['streak']} consecutive days at #1\n"
                f"({start_str} → {end_str})"
            )
        else:
            streak_value = "_No streak data available._"
        embed.add_field(
            name="👑 Longest 1st Place Streak",
            value=streak_value,
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

        while (year > EARLIEST_YEAR) or (year == EARLIEST_YEAR and month >= EARLIEST_MONTH):
            logger.info(f"Fetching API data for {year}-{month:02d}...")
            rows, monthly_rank = await cls._fetch_and_parse_api_month(circle_id, year, month)
            all_ranks[(year, month)] = monthly_rank
            if rows:
                all_rows.extend(rows)
            else:
                # No data for this month — club didn't exist yet, stop
                logger.info(f"No data for {year}-{month:02d}, club likely didn't exist yet.")
                break
            year, month = cls._prev_month(year, month)

        return all_rows, all_ranks

    @classmethod
    async def _fetch_and_parse_api_month(
        cls, circle_id: str, year: int, month: int
    ) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """
        Fetch a single month from Uma.moe API.
        Returns (rows, monthly_rank):
          - rows: [{date, lifetime_fans, trainer_name}, ...]
          - monthly_rank: top-level monthly_rank from API, or None if unavailable
        """
        base_url = "https://uma.moe/api/v4/circles"
        api_url = f"{base_url}?circle_id={circle_id}&year={year}&month={month}"

        headers = {
            "accept": "application/json",
            "X-API-Key": UMAMOE_API_KEY,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, headers=headers, timeout=30) as response:
                    if response.status != 200:
                        logger.warning(
                            f"API returned status {response.status} for {year}-{month:02d}"
                        )
                        return [], None

                    data = await response.json()
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError) as e:
            logger.warning(f"API request failed for {year}-{month:02d}: {e}")
            return [], None
        except Exception as e:
            logger.warning(f"Unexpected API error for {year}-{month:02d}: {e}")
            return [], None

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
        """
        Compute the best monthly fan total per member using lifetime values.
        For each member+month pair, uses the difference between the last
        day's lifetime value (preferring next_month_start if available)
        and the previous month's last value.
        """
        # Group by trainer_name, then by (year, month)
        member_data: Dict[str, List[Tuple[date, int]]] = defaultdict(list)
        for row in rows:
            member_data[row["trainer_name"]].append(
                (row["date"], row["lifetime_fans"])
            )

        best: Optional[Dict[str, Any]] = None

        for name, entries in member_data.items():
            # Sort by (date, lifetime_fans) so that for the same date,
            # the synthetic end-of-month entry (higher fans) comes after
            # the regular daily entry. This ensures monthly_last picks
            # next_month_start when available.
            entries.sort(key=lambda x: (x[0], x[1]))

            # For each month, find the first and last entry's lifetime value.
            # This allows computing the actual gain within the month.
            monthly_first: Dict[Tuple[int, int], int] = {}
            monthly_last: Dict[Tuple[int, int], int] = {}
            for d, fans in entries:
                key = (d.year, d.month)
                if key not in monthly_first:
                    monthly_first[key] = fans  # first day's value
                monthly_last[key] = fans       # last entry's value (keeps updating)

            # Compute monthly total = last entry of month - first entry of month
            # (or last entry of current month - last entry of previous month for
            #  subsequent months, which is equivalent via transitive subtraction)
            sorted_keys = sorted(monthly_last.keys())
            for idx, (year, month) in enumerate(sorted_keys):
                curr_lifetime = monthly_last[(year, month)]

                if idx == 0:
                    # First month — subtract the first day's value to strip
                    # any pre-existing lifetime before this month
                    month_start = monthly_first[(year, month)]
                    monthly_total = curr_lifetime - month_start
                else:
                    prev_key = sorted_keys[idx - 1]
                    prev_lifetime = monthly_last[prev_key]
                    monthly_total = curr_lifetime - prev_lifetime

                if monthly_total > 0 and (best is None or monthly_total > best["total"]):
                    month_name = calendar.month_name[month]
                    best = {
                        "name": name,
                        "total": monthly_total,
                        "month": f"{month_name} {year}",
                    }

        return best

    @classmethod
    def _compute_longest_first_place_streak(
        cls, rows: list
    ) -> Optional[Dict[str, Any]]:
        """
        Compute the longest consecutive streak of holding 1st place (highest
        lifetime_fans) across all dates in the data.

        Returns a dict with keys:
          - name: the member who held #1
          - streak: number of consecutive days
          - start_date: first day of the streak
          - end_date: last day of the streak
        or None if insufficient data.
        """
        # Group rows by date, keeping the highest lifetime_fans per member per date
        date_entries: Dict[date, List[Tuple[str, int]]] = defaultdict(list)
        for row in rows:
            date_entries[row["date"]].append(
                (row["trainer_name"], row["lifetime_fans"])
            )

        sorted_dates = sorted(date_entries.keys())
        if len(sorted_dates) < 2:
            return None

        # For each date, find the member(s) with the highest lifetime_fans.
        # If there's a tie, we don't count a streak (no single clear #1).
        daily_leader: Dict[date, Optional[str]] = {}
        for d in sorted_dates:
            entries = date_entries[d]
            # Find max fans
            max_fans = max(f for _, f in entries)
            # Find all members at that max
            leaders = [name for name, fans in entries if fans == max_fans]
            if len(leaders) == 1:
                daily_leader[d] = leaders[0]
            else:
                daily_leader[d] = None  # tie — no clear leader

        # Walk through dates tracking streaks
        best_streak = 0
        best_name: Optional[str] = None
        best_start: Optional[date] = None
        best_end: Optional[date] = None

        current_name: Optional[str] = None
        current_streak = 0
        current_start: Optional[date] = None

        for d in sorted_dates:
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
                # end_date implicitly updated on each iteration
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
