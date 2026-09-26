"""
Highscore Service — generates an embed showing recorded club highscores:
best single-day fan gain, best monthly total, best club rank achieved.

Uses Uma.moe lifetime snapshots with conservative uncertainty bounds and
bot-recorded club rank history. Missing data is not evidence of departure.
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
        for (year, month), rank in sorted(monthly_ranks.items()):
            if (year, month) == current_key:
                continue  # skip current incomplete month
            if type(rank) is int and rank > 0:
                if best_monthly_rank is None or rank < best_monthly_rank[0]:
                    # Use the 1st of the month as the achievement date
                    best_monthly_rank = (rank, date(year, month, 1))

        # 3b. Compute club-level rank highscores from DB
        best_rank = await ClubRankHistory.get_best_rank(club_id)

        # 4. Assemble Embed
        embed = discord.Embed(
            title=f"🏆 Recorded Club Highscores — {club_name}",
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
            if best_monthly.get("incomplete"):
                monthly_value += "\nIncomplete coverage — not a verified full-month total."
            if best_monthly.get("provisional"):
                monthly_value += "\nProvisional: current month is still in progress."
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
            total_streak_value = "_Insufficient reliable coverage to establish a reign._"
        embed.add_field(
            name="👑 Longest #1 Reign",
            value=total_streak_value,
            inline=False,
        )

        verified_reign = cls._compute_longest_first_place_streak_by_total(api_rows, verified_only=True)
        if verified_reign:
            embed.add_field(
                name="👑 Longest Verified #1 Reign",
                value=(f"**{verified_reign['name']}** — {verified_reign['streak']} days, no inferred days\n"
                       f"({verified_reign['start_date']:%B %d, %Y} → {verified_reign['end_date']:%B %d, %Y})"),
                inline=False,
            )
        regular_dates = [r["date"] for r in api_rows if not r.get("is_end_of_month")]
        if regular_dates:
            embed.add_field(
                name="Data coverage",
                value=(f"Member snapshots: {min(regular_dates):%B %d, %Y} → "
                       f"{max(regular_dates):%B %d, %Y}. Gaps may exist.\n"
                       "Verified means supported by available API snapshots, not independent game verification. "
                       "Missing membership or contradictory values can prevent a record from being verified."),
                inline=False,
            )
        embed.set_footer(text=f"{club_name} · Recorded history")
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
                year, month = cls._prev_month(year, month)

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
        monthly_rank = (data.get("circle") or {}).get("monthly_rank")
        if type(monthly_rank) is not int or monthly_rank <= 0:
            monthly_rank = None

        members = data.get("members", [])
        if not members:
            return [], monthly_rank

        rows: List[Dict[str, Any]] = []
        _, last_day = calendar.monthrange(year, month)

        for member in members:
            trainer_name = member.get("trainer_name")
            trainer_id = member.get("viewer_id")
            daily_fans = member.get("daily_fans", [])
            next_month_start = member.get("next_month_start")

            if not trainer_id or not trainer_name or not daily_fans:
                continue

            # daily_fans is lifetime cumulative — store as-is
            for day_idx, lifetime_total in enumerate(daily_fans):
                day_num = day_idx + 1
                if day_num > last_day:
                    break
                if not isinstance(lifetime_total, int) or lifetime_total <= 0:
                    continue

                row_date = date(year, month, day_num)
                if row_date > datetime.now(timezone.utc).date():
                    continue
                rows.append({
                    "date": row_date,
                    "lifetime_fans": lifetime_total,
                    "trainer_name": trainer_name,
                    "trainer_id": str(trainer_id),
                })

            # If next_month_start is available, append a synthetic end-of-month
            # entry so _compute_best_monthly_total can use the true final value.
            if (isinstance(next_month_start, int) and next_month_start > 0
                    and (year, month) < (datetime.now(timezone.utc).year,
                                         datetime.now(timezone.utc).month)):
                end_of_month = date(year, month, last_day)
                rows.append({
                    "date": end_of_month,
                    "lifetime_fans": next_month_start,
                    "trainer_name": trainer_name,
                    "trainer_id": str(trainer_id),
                    "is_end_of_month": True,
                })

        return rows, monthly_rank

    # ── Computation Logic ───────────────────────────────────────────────

    @classmethod
    def _history(cls, rows):
        """Index by stable ID and quarantine contradictory lifetime snapshots.

        Name fallback supports legacy callers; API rows always require an ID.
        A drop is unknown until the last trusted lifetime total is recovered.
        The first recovery observation cannot create a one-day rebound record.
        """
        raw = defaultdict(lambda: defaultdict(list))
        endpoints = {}
        names = {}
        today = datetime.now(timezone.utc).date()
        for row in sorted(rows, key=lambda r: (r['date'], bool(r.get('is_end_of_month')))):
            key = str(row.get('trainer_id') or row['trainer_name'])
            d, fans = row['date'], row['lifetime_fans']
            if d > today or type(fans) is not int or fans <= 0:
                continue
            names[key] = row['trainer_name']
            if row.get('is_end_of_month'):
                if (d.year, d.month) < (today.year, today.month):
                    endpoints[key, d.year, d.month] = fans
            else:
                raw[key][d].append(fans)
        members = {}
        for key, dates in raw.items():
            high = 0
            members[key] = {}
            for d, values in sorted(dates.items()):
                if len(set(values)) != 1 or values[0] < high:
                    continue
                high = values[0]
                members[key][d] = high
        return members, endpoints, names

    @classmethod
    def _daily_gains(cls, rows):
        members, _, names = cls._history(rows)
        gains = defaultdict(dict)
        for key, dates in members.items():
            for d, fans in dates.items():
                previous = dates.get(d - timedelta(days=1))
                if previous is not None:
                    gains[d][key] = fans - previous
        return members, names, gains

    @classmethod
    def _compute_best_daily_gain(cls, rows):
        _, names, gains = cls._daily_gains(rows)
        best = None
        for d, values in sorted(gains.items()):
            for key, value in sorted(values.items()):
                if value > 0 and (best is None or value > best['delta']):
                    best = dict(name=names[key], delta=value, date=d)
        return best

    @classmethod
    def _monthly_history(cls, rows):
        """Lower bounds count observed membership intervals only.

        Upper bounds include unobserved gains and are used only to avoid false
        leaders. They are never credited as club contributions. A preceding
        month's endpoint can restore a missing start-of-month baseline.
        """
        members, endpoints, names = cls._history(rows)
        months = defaultdict(dict)
        for key, dates in members.items():
            groups = defaultdict(list)
            for d, fans in sorted(dates.items()):
                groups[d.year, d.month].append((d, fans))
            for (year, month), entries in groups.items():
                first, first_fans = entries[0]
                previous_key = cls._prev_month(year, month)
                endpoint = endpoints.get((key, *previous_key))
                recovered = first.day > 1 and first.day <= 3 and endpoint is not None
                baseline = endpoint if recovered else first_fans
                if baseline > first_fans:
                    baseline = first_fans
                    recovered = False
                partial = first.day > 1
                total = first_fans - baseline
                points = {}
                previous_date, previous_fans = first, first_fans
                for d, fans in entries:
                    if d != first:
                        if d == previous_date + timedelta(days=1):
                            total += fans - previous_fans
                        else:
                            partial = True
                    points[d] = (total, fans - baseline if first.day == 1 or recovered else float('inf'))
                    previous_date, previous_fans = d, fans
                final = endpoints.get((key, year, month))
                last_day = calendar.monthrange(year, month)[1]
                if final is not None and final >= previous_fans and previous_date.day == last_day:
                    total += final - previous_fans
                else:
                    partial = True
                months[year, month][key] = dict(
                    points=points, baseline=baseline, total=total, partial=partial,
                    recovered=recovered, first=first,
                )
        return members, names, months

    @classmethod
    def _compute_best_monthly_total(cls, rows):
        _, names, months = cls._monthly_history(rows)
        best = None
        today = datetime.now(timezone.utc).date()
        for (year, month), entries in sorted(months.items()):
            for key, entry in sorted(entries.items()):
                if entry['total'] > 0 and (best is None or entry['total'] > best['total']):
                    best = dict(name=names[key], total=entry['total'],
                                month=f'{calendar.month_name[month]} {year}',
                                incomplete=entry['partial'],
                                provisional=(year, month) == (today.year, today.month))
        return best

    @classmethod
    def _walk_leaders(cls, states, names, max_gap):
        """Unknown dates are pending; a known defeat always breaks the reign."""
        best = None
        current = None
        start = last = None
        inferred = 0
        for d, (leader, defeated) in sorted(states.items()):
            if current in defeated:
                current = None
                last = None
            if leader is None:
                continue
            gap = (d - last).days - 1 if last else 0
            if leader == current and gap <= max_gap:
                inferred += gap
            else:
                current, start, inferred = leader, d, 0
            last = d
            length = (d - start).days + 1
            if length >= 2 and (best is None or length > best['streak']):
                best = dict(name=names[leader], streak=length, start_date=start,
                            end_date=d, inferred_days=inferred)
        return best

    @classmethod
    def _compute_longest_first_place_streak(cls, rows):
        members, names, gains = cls._daily_gains(rows)
        states = {}
        for d, values in sorted(gains.items()):
            # A member already seen this month may still be active during a
            # trailing outage. Do not silently remove them from competition.
            expected = {key for key, dates in members.items()
                        if any((p.year, p.month) == (d.year, d.month) and p <= d
                               for p in dates)}
            highest = max(values.values(), default=0)
            winners = [key for key, gain in values.items() if gain == highest]
            leader = winners[0] if highest > 0 and len(winners) == 1 and expected <= values.keys() else None
            states[d] = (leader, set(values) if leader is None else set(values) - {leader})
        return cls._walk_leaders(states, names, 0)

    @classmethod
    def _compute_longest_first_place_streak_by_total(cls, rows, *, verified_only=False):
        members, names, months = cls._monthly_history(rows)
        dates = [d for values in members.values() for d in values]
        if not dates:
            return None
        states = {}
        d, end = min(dates), max(dates)
        while d <= end:
            bounds = {}
            for key, entry in months.get((d.year, d.month), {}).items():
                points = entry['points']
                if d < entry['first']:
                    # Without membership evidence this may be a missing early
                    # snapshot rather than a later join.
                    bounds[key] = (0, float('inf'))
                    continue
                if d in points:
                    bounds[key] = points[d]
                else:
                    future = [p for p in points if p > d]
                    upper = points[min(future)][1] if future else float('inf')
                    bounds[key] = (0, upper)
            # Carry prior-month members as unknown at a reset until their first
            # observation. Absence alone is not evidence of departure.
            previous = months.get(cls._prev_month(d.year, d.month), {})
            for key, prior in previous.items():
                prior_last = max(prior['points'])
                if prior_last.day != calendar.monthrange(prior_last.year, prior_last.month)[1]:
                    continue
                if key not in bounds and (key not in months.get((d.year, d.month), {})
                        or d < months[d.year, d.month][key]['first']):
                    bounds[key] = (0, float('inf'))
            highest = max((v[0] for v in bounds.values()), default=0)
            winners = [key for key, (low, _) in bounds.items()
                       if low > 0 and all(low > upper for other, (_, upper) in bounds.items()
                                          if other != key)]
            leader = winners[0] if len(winners) == 1 else None
            # Even if a third member is unknown, a definite overtake or tie
            # prevents bridging the previous leader across this date.
            defeated = {key for key, (_, upper) in bounds.items()
                        if highest > 0 and any(other != key and low >= upper
                                               for other, (low, _) in bounds.items())}
            states[d] = (leader, defeated)
            d += timedelta(days=1)
        return cls._walk_leaders(states, names, 0 if verified_only else cls.MAX_REIGN_GAP_DAYS)


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
