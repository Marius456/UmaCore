"""
Quota calculation service with multi-club support
"""
from datetime import date, timedelta
from typing import Dict, Optional, Tuple, Set
from uuid import UUID
import logging
import calendar
import math

from models import Member, QuotaHistory, QuotaRequirement, Club
from config.database import db

logger = logging.getLogger(__name__)


class QuotaCalculator:
    """Handles all quota calculations and tracking per club"""
    
    @staticmethod
    async def calculate_expected_fans(club_id: UUID, member_join_date: date,
                                     current_date: date, quota_period: str = 'daily') -> int:
        """
        Calculate expected cumulative fans based on days active in current month.

        For weekly/biweekly periods the stored quota is the period amount (e.g. 700 000 for
        weekly), so we divide by the period length to get the per-day contribution.

        Args:
            club_id: Club UUID
            member_join_date: When the member joined the club
            current_date: The date the data belongs to (not necessarily today)
            quota_period: 'daily', 'weekly', or 'biweekly'

        Returns:
            Expected cumulative fan count for this month only
        """
        quota_schedule = await db.fetch(
            """
            SELECT effective_date, daily_quota
            FROM quota_requirements
            WHERE club_id = $1
              AND effective_date >= date_trunc('month', $2::date)::date
              AND effective_date <= $2
            ORDER BY effective_date ASC
            """,
            club_id,
            current_date,
        )
        club = await Club.get_by_id(club_id)
        default_quota = club.daily_quota if club else 1_000_000
        return QuotaCalculator.calculate_expected_fans_from_schedule(
            member_join_date,
            current_date,
            quota_period,
            default_quota,
            quota_schedule,
        )

    @staticmethod
    def calculate_expected_fans_from_schedule(
        member_join_date: date,
        current_date: date,
        quota_period: str,
        default_quota: int,
        quota_schedule,
    ) -> int:
        """Calculate expected fans from an already-fetched quota schedule."""
        if member_join_date.year == current_date.year and member_join_date.month == current_date.month:
            start_date = member_join_date
        else:
            start_date = date(current_date.year, current_date.month, 1)

        period_days = {'daily': 1, 'weekly': 7, 'biweekly': 14}.get(quota_period, 1)

        total_expected = 0.0
        schedule = [
            (row['effective_date'], row['daily_quota'])
            for row in quota_schedule
        ]
        schedule_index = 0
        effective_quota = default_quota

        while schedule_index < len(schedule) and schedule[schedule_index][0] < start_date:
            effective_quota = schedule[schedule_index][1]
            schedule_index += 1

        day_count = (current_date - start_date).days + 1
        current_day = start_date
        for _ in range(day_count):
            while (
                schedule_index < len(schedule)
                and schedule[schedule_index][0] <= current_day
            ):
                effective_quota = schedule[schedule_index][1]
                schedule_index += 1
            total_expected += effective_quota / period_days
            current_day += timedelta(days=1)

        result = round(total_expected)
        logger.debug(f"Expected fans calculation: {start_date} to {current_date} = {day_count} days "
                     f"(period={quota_period}) = {result:,}")
        return result
    
    @staticmethod
    def calculate_days_active_in_month(member_join_date: date, current_date: date) -> int:
        """Calculate how many days a member has been active this month"""
        # Determine the effective start date for this month
        if member_join_date.year == current_date.year and member_join_date.month == current_date.month:
            # Joined this month
            start_date = member_join_date
        else:
            # Joined in previous month(s) - active since first day of current month
            start_date = date(current_date.year, current_date.month, 1)
        
        return (current_date - start_date).days + 1
    
    @staticmethod
    def calculate_deficit_surplus(actual_fans: int, expected_fans: int) -> int:
        """Calculate deficit or surplus (positive = surplus, negative = deficit)"""
        return actual_fans - expected_fans
    
    async def _get_previous_cumulative_totals(self, club_id: UUID) -> Dict[str, int]:
        """
        Get the latest cumulative fan counts from database for monthly reset detection
        
        Args:
            club_id: Club UUID
        
        Returns:
            Dict mapping trainer_id/name -> cumulative_fans
        """
        query = """
            SELECT m.trainer_id, m.trainer_name, qh.cumulative_fans
            FROM members m
            JOIN quota_history qh ON m.member_id = qh.member_id
            WHERE m.club_id = $1 AND qh.date = (
                SELECT MAX(date) FROM quota_history WHERE club_id = $1
            )
        """
        rows = await db.fetch(query, club_id)
        
        result = {}
        for row in rows:
            key = row['trainer_id'] if row['trainer_id'] else row['trainer_name']
            result[key] = row['cumulative_fans']
        
        return result
    
    def _detect_monthly_reset_from_scraped(self, scraped_data: Dict[str, Dict], 
                                           previous_totals: Dict[str, int]) -> bool:
        """
        Detect if a monthly reset has occurred by comparing scraped data to previous totals
        """
        if not previous_totals:
            logger.info("No previous data found, skipping reset detection")
            return False
        
        if not scraped_data:
            logger.warning("No scraped data, cannot detect reset")
            return False
        
        # Check if any member has significantly lower fans than before
        for key, member_data in scraped_data.items():
            current_fans = member_data["fans"][-1] if member_data["fans"] else 0
            
            if key in previous_totals:
                previous_fans = previous_totals[key]
                
                # If current count is less than 50% of previous, it's a reset
                if current_fans > 0 and current_fans < previous_fans * 0.5:
                    logger.warning(
                        f"Monthly reset detected: {member_data['name']} went from "
                        f"{previous_fans:,} to {current_fans:,} fans"
                    )
                    return True
        
        return False
    
    async def _auto_deactivate_missing_members(
        self,
        club_id: UUID,
        scraped_trainer_ids: Set[str],
        active_members=None,
    ):
        """Safely deactivate members absent from several complete scrapes."""
        if active_members is None:
            active_members = await Member.get_all_active(club_id)

        if not active_members:
            return

        # A partial response must never be treated as a roster update. Allowing
        # a small amount of roster churn still lets legitimate departures be
        # detected, while protecting against truncated API responses.
        minimum_expected = max(1, math.ceil(len(active_members) * 0.8))
        if len(scraped_trainer_ids) < minimum_expected:
            logger.warning(
                "Skipping auto-deactivation for club %s: scrape returned %d members, "
                "but at least %d of %d active members were expected",
                club_id, len(scraped_trainer_ids), minimum_expected, len(active_members),
            )
            return
        
        deactivated_count = 0
        for member in active_members:
            member_key = member.trainer_id if member.trainer_id else member.trainer_name
            
            if member_key not in scraped_trainer_ids:
                missing_count = await member.record_missing_scrape()
                if missing_count < 3:
                    logger.info(
                        "Member %s missing from validated scrape (%d/3); retaining active status",
                        member.trainer_name, missing_count,
                    )
                    continue

                await member.deactivate(manual=False)
                deactivated_count += 1
                logger.info(f"Auto-deactivated member (missing from 3 validated scrapes): {member.trainer_name}")
        
        if deactivated_count > 0:
            logger.info(f"Auto-deactivated {deactivated_count} member(s) who left the club")
    
    async def process_scraped_data(self, club_id: UUID, scraped_data: Dict[str, Dict],
                                   current_date: date, current_day: int,
                                   quota_period: str = 'daily') -> Tuple[int, int]:
        """
        Process scraped data and update database for a specific club.
        
        Args:
            club_id: Club UUID
            scraped_data: Dict of trainer_id -> {name, trainer_id, fans[], join_day}
            current_date: Date the data represents (calculated by scraper/tasks)
            current_day: Day number in the array (used for array indexing)
        
        Returns:
            Tuple of (new_members_count, updated_members_count)
        """
        # Use the date already calculated by the scraper and tasks.py
        data_date = current_date
        logger.info(f"Processing scraped data for club {club_id}: data_date = {data_date}, current_day = {current_day}")
        
        # Check for monthly reset
        logger.info(f"Checking for monthly reset for club {club_id}...")
        previous_totals = await self._get_previous_cumulative_totals(club_id)
        
        if self._detect_monthly_reset_from_scraped(scraped_data, previous_totals):
            logger.info(
                "Monthly reset detected for club %s; preserving historical data",
                club_id,
            )
            # Clear manual deactivation flags for this club
            await db.execute(
                "UPDATE members SET manually_deactivated = FALSE WHERE club_id = $1 AND manually_deactivated = TRUE",
                club_id
            )
            logger.info(f"Monthly member-state reset complete for club {club_id}")
        
        # Load the roster once. This replaces one member lookup per scraped row
        # and is also reused by missing-member reconciliation.
        all_members = await Member.get_all_for_club(club_id)
        active_members = [member for member in all_members if member.is_active]
        members_by_trainer_id = {
            member.trainer_id: member
            for member in all_members
            if member.trainer_id
        }
        members_by_name = {member.trainer_name: member for member in all_members}

        # Auto-deactivate members who are no longer in the scraped data
        scraped_trainer_ids = set(scraped_data.keys())
        await self._auto_deactivate_missing_members(
            club_id, scraped_trainer_ids, active_members
        )
        
        # Process each member
        new_members = 0
        updated_members = 0

        quota_schedule = await db.fetch(
            """
            SELECT effective_date, daily_quota
            FROM quota_requirements
            WHERE club_id = $1
              AND effective_date >= date_trunc('month', $2::date)::date
              AND effective_date <= $2
            ORDER BY effective_date ASC
            """,
            club_id,
            data_date,
        )
        club = await Club.get_by_id(club_id)
        default_quota = club.daily_quota if club else 1_000_000

        previous_rows = await db.fetch(
            """
            SELECT member_id, deficit_surplus, days_behind
            FROM quota_history
            WHERE club_id = $1 AND date = $2
            """,
            club_id,
            data_date - timedelta(days=1),
        )
        previous_by_member = {row['member_id']: row for row in previous_rows}
        seen_member_ids = []
        history_records = []
        
        for key, member_data in scraped_data.items():
            trainer_id = member_data.get("trainer_id")
            trainer_name = member_data["name"]
            daily_fans = member_data["fans"]
            detected_join_day = member_data["join_day"]
            
            if not daily_fans:
                logger.warning(f"No fan data for {trainer_name}")
                continue
            
            # Use the last value in the fans array
            cumulative_fans = daily_fans[-1]
            
            # Look up member by trainer_id first, then by name in the preloaded roster.
            member = (
                members_by_trainer_id.get(trainer_id)
                if trainer_id
                else members_by_name.get(trainer_name)
            )
            
            if not member:
                # New member - resolve their join day into a full date
                # detected_join_day is the day number in the scraped month (data_date.month)
                # Check if it's a valid day in that month
                last_day_of_month = calendar.monthrange(data_date.year, data_date.month)[1]
                
                if 1 <= detected_join_day <= last_day_of_month:
                    # Join day is within the current month being processed
                    join_date = date(data_date.year, data_date.month, detected_join_day)
                else:
                    # Join day exceeds current month, must be from previous month
                    if data_date.month == 1:
                        join_date = date(data_date.year - 1, 12, detected_join_day)
                    else:
                        join_date = date(data_date.year, data_date.month - 1, detected_join_day)
                
                member = await Member.create(club_id, trainer_name, join_date, trainer_id)
                new_members += 1
                logger.info(f"New member added: {trainer_name} (ID: {trainer_id}, joined {join_date.strftime('%Y-%m-%d')})")
            else:
                # Existing member
                if member.trainer_name != trainer_name:
                    await member.update_name(trainer_name)
                
                # Reactivate if previously auto-deactivated
                if not member.is_active:
                    if member.manually_deactivated:
                        logger.info(f"Skipping reactivation of manually deactivated member: {trainer_name}")
                        continue
                    else:
                        await member.activate()
                        await member.update_join_date(data_date)
                        logger.info(f"Reactivated returning member: {trainer_name} (join_date reset to {data_date})")
            
            seen_member_ids.append(member.member_id)
            
            # All quota calculations use data_date
            days_active = self.calculate_days_active_in_month(member.join_date, data_date)
            
            expected_fans = self.calculate_expected_fans_from_schedule(
                member.join_date,
                data_date,
                quota_period,
                default_quota,
                quota_schedule,
            )
            
            deficit_surplus = self.calculate_deficit_surplus(cumulative_fans, expected_fans)
            
            previous = previous_by_member.get(member.member_id)
            days_behind = 0
            if deficit_surplus < 0:
                days_behind = (
                    previous['days_behind'] + 1
                    if previous and previous['deficit_surplus'] < 0
                    else 1
                )
            
            # Store history keyed to data_date
            history_records.append((
                member.member_id,
                club_id,
                data_date,
                cumulative_fans,
                expected_fans,
                deficit_surplus,
                days_behind,
            ))
            
            updated_members += 1
            
            logger.debug(f"{trainer_name}: {cumulative_fans:,} fans "
                        f"(expected: {expected_fans:,}, {deficit_surplus:+,}, days active: {days_active})")
        
        if history_records:
            async with db.transaction() as conn:
                await conn.execute(
                    """
                    UPDATE members
                    SET last_seen = $1, missing_scrapes = 0, updated_at = NOW()
                    WHERE member_id = ANY($2::uuid[])
                    """,
                    current_date,
                    seen_member_ids,
                )
                await conn.executemany(
                    """
                    INSERT INTO quota_history
                        (member_id, club_id, date, cumulative_fans,
                         expected_fans, deficit_surplus, days_behind)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (member_id, date)
                    DO UPDATE SET
                        cumulative_fans = EXCLUDED.cumulative_fans,
                        expected_fans = EXCLUDED.expected_fans,
                        deficit_surplus = EXCLUDED.deficit_surplus,
                        days_behind = EXCLUDED.days_behind
                    """,
                    history_records,
                )

        logger.info(f"Processed {updated_members} members ({new_members} new) for club {club_id}")
        return new_members, updated_members
    
    async def _calculate_days_behind(self, member_id: UUID, current_deficit_surplus: int,
                                    data_date: date) -> int:
        """Calculate how many consecutive days a member has been behind"""
        if current_deficit_surplus >= 0:
            return 0

        recent_history = await QuotaHistory.get_last_n_days(member_id, 10)

        if not recent_history:
            return 1

        # Exclude records from data_date or later, and from a different month
        recent_history = [
            h for h in recent_history
            if h.date < data_date
            and h.date.year == data_date.year
            and h.date.month == data_date.month
        ]

        # Count consecutive days with negative deficit before data_date
        consecutive_days = 1  # Count the current day
        expected_date = data_date - timedelta(days=1)

        for history in recent_history:
            if history.date != expected_date or history.deficit_surplus >= 0:
                break
            consecutive_days += 1
            expected_date -= timedelta(days=1)

        logger.debug(f"Member {member_id}: {consecutive_days} consecutive days behind")
        return consecutive_days
    
    @staticmethod
    def get_period_info(quota_period: str, current_date: date) -> Optional[Dict]:
        """
        Return period metadata for the current date under weekly/biweekly quota.
        Returns None for daily quota.
        """
        if quota_period == 'daily':
            return None

        period_days = {'weekly': 7, 'biweekly': 14}[quota_period]
        days_in_month = calendar.monthrange(current_date.year, current_date.month)[1]

        day_of_month = current_date.day  # 1-indexed
        period_number = (day_of_month - 1) // period_days + 1

        period_start_day = (period_number - 1) * period_days + 1
        period_end_day = min(period_start_day + period_days - 1, days_in_month)

        period_start = date(current_date.year, current_date.month, period_start_day)
        period_end = date(current_date.year, current_date.month, period_end_day)

        total_periods = math.ceil(days_in_month / period_days)
        quota_label = 'week' if quota_period == 'weekly' else 'biweek'

        return {
            'period_number': period_number,
            'total_periods': total_periods,
            'period_start': period_start,
            'period_end': period_end,
            'period_days': period_days,
            'quota_label': quota_label,
        }

    async def get_member_status_summary(self, club_id: UUID, current_date: date,
                                        quota_period: str = 'daily') -> Dict:
        """
        Get summary of all members' status for a club.

        For weekly/biweekly quotas, each member_status entry will additionally
        contain 'period_start_fans' and 'period_info'.

        Returns:
            Dict with categorized member data
        """
        period_info = self.get_period_info(quota_period, current_date)

        # Pre-compute period_quota for the current period when not daily
        if period_info:
            actual_period_length = (period_info['period_end'] - period_info['period_start']).days + 1
            stored_quota = await QuotaRequirement.get_quota_for_date(club_id, period_info['period_start'])
            period_quota = round(stored_quota / period_info['period_days'] * actual_period_length)
            period_info['period_quota'] = period_quota

        period_start = period_info['period_start'] if period_info else None
        rows = await db.fetch(
            """
            SELECT
                m.member_id, m.club_id, m.trainer_id, m.trainer_name,
                m.join_date, m.is_active, m.manually_deactivated,
                m.last_seen, m.missing_scrapes,
                latest.id AS history_id,
                latest.date AS history_date,
                latest.cumulative_fans,
                latest.expected_fans,
                latest.deficit_surplus,
                latest.days_behind,
                previous.cumulative_fans AS previous_cumulative_fans,
                period_start.cumulative_fans AS period_start_fans
            FROM members m
            LEFT JOIN LATERAL (
                SELECT id, date, cumulative_fans, expected_fans,
                       deficit_surplus, days_behind
                FROM quota_history
                WHERE member_id = m.member_id
                ORDER BY date DESC
                LIMIT 1
            ) latest ON TRUE
            LEFT JOIN LATERAL (
                SELECT cumulative_fans
                FROM quota_history
                WHERE member_id = m.member_id AND date < $2
                ORDER BY date DESC
                LIMIT 1
            ) previous ON TRUE
            LEFT JOIN LATERAL (
                SELECT cumulative_fans
                FROM quota_history
                WHERE member_id = m.member_id AND date = $3::date - 1
                LIMIT 1
            ) period_start ON $3::date IS NOT NULL
            WHERE m.club_id = $1 AND m.is_active = TRUE
            ORDER BY m.trainer_name
            """,
            club_id,
            current_date,
            period_start,
        )

        on_track = []
        behind = []

        for row in rows:
            if row['history_id'] is None:
                continue

            member = Member(
                member_id=row['member_id'],
                club_id=row['club_id'],
                trainer_id=row['trainer_id'],
                trainer_name=row['trainer_name'],
                join_date=row['join_date'],
                is_active=row['is_active'],
                manually_deactivated=row['manually_deactivated'],
                last_seen=row['last_seen'],
                missing_scrapes=row['missing_scrapes'],
            )
            latest_history = QuotaHistory(
                id=row['history_id'],
                member_id=row['member_id'],
                club_id=row['club_id'],
                date=row['history_date'],
                cumulative_fans=row['cumulative_fans'],
                expected_fans=row['expected_fans'],
                deficit_surplus=row['deficit_surplus'],
                days_behind=row['days_behind'],
            )

            member_status = {
                'member': member,
                'history': latest_history
            }

            # Get the most recent cumulative_fans before today for daily progress calculation
            # Using the latest record strictly before current_date (not necessarily yesterday)
            # to properly compute today's delta even if a day was skipped
            member_status['yesterday_cumulative_fans'] = (
                row['previous_cumulative_fans'] or 0
            )

            if period_info:
                # Fans earned before this period started
                if period_info['period_start'].day == 1:
                    period_start_fans = 0
                else:
                    period_start_fans = row['period_start_fans'] or 0

                member_status['period_start_fans'] = period_start_fans
                member_status['period_info'] = period_info

            if latest_history.deficit_surplus >= 0:
                on_track.append(member_status)
            else:
                behind.append(member_status)

        # Sort on_track by surplus (descending)
        on_track.sort(key=lambda x: x['history'].deficit_surplus, reverse=True)

        # Sort behind by deficit (most behind first)
        behind.sort(key=lambda x: x['history'].deficit_surplus)

        return {
            'on_track': on_track,
            'behind': behind,
            'total_members': len(rows),
            'period_info': period_info,
        }
