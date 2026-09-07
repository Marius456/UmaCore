"""
Quota History data model
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, List
from uuid import UUID
import logging

from config.database import db

logger = logging.getLogger(__name__)


@dataclass
class QuotaHistory:
    """Represents a member's daily quota tracking"""
    id: Optional[UUID]
    member_id: UUID
    club_id: UUID
    date: date
    cumulative_fans: int
    expected_fans: int
    deficit_surplus: int
    days_behind: int
    
    @classmethod
    async def create(cls, member_id: UUID, club_id: UUID, date: date, cumulative_fans: int,
                     expected_fans: int, deficit_surplus: int, days_behind: int) -> 'QuotaHistory':
        """Create or update quota history for a date"""
        query = """
            INSERT INTO quota_history 
                (member_id, club_id, date, cumulative_fans, expected_fans, deficit_surplus, days_behind)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (member_id, date) 
            DO UPDATE SET 
                cumulative_fans = $4,
                expected_fans = $5,
                deficit_surplus = $6,
                days_behind = $7
            RETURNING id, member_id, club_id, date, cumulative_fans, expected_fans, deficit_surplus, days_behind
        """
        row = await db.fetchrow(query, member_id, club_id, date, cumulative_fans, 
                                expected_fans, deficit_surplus, days_behind)
        return cls(**dict(row))
    
    @classmethod
    async def get_latest_for_member(cls, member_id: UUID) -> Optional['QuotaHistory']:
        """Get the most recent quota history for a member"""
        query = """
            SELECT id, member_id, club_id, date, cumulative_fans, expected_fans, deficit_surplus, days_behind
            FROM quota_history
            WHERE member_id = $1
            ORDER BY date DESC
            LIMIT 1
        """
        row = await db.fetchrow(query, member_id)
        if row:
            return cls(**dict(row))
        return None
    
    @classmethod
    async def get_last_n_days(cls, member_id: UUID, n: int) -> List['QuotaHistory']:
        """Get last N days of history for a member"""
        query = """
            SELECT id, member_id, club_id, date, cumulative_fans, expected_fans, deficit_surplus, days_behind
            FROM quota_history
            WHERE member_id = $1
            ORDER BY date DESC
            LIMIT $2
        """
        rows = await db.fetch(query, member_id, n)
        return [cls(**dict(row)) for row in rows]
    
    @classmethod
    async def get_for_member_date(cls, member_id: UUID, target_date: date) -> Optional['QuotaHistory']:
        """Get a specific member's quota history for a given date"""
        query = """
            SELECT id, member_id, club_id, date, cumulative_fans, expected_fans, deficit_surplus, days_behind
            FROM quota_history
            WHERE member_id = $1 AND date = $2
        """
        row = await db.fetchrow(query, member_id, target_date)
        if row:
            return cls(**dict(row))
        return None

    @classmethod
    async def get_latest_for_member_before_date(cls, member_id: UUID, before_date: date) -> Optional['QuotaHistory']:
        """Get the most recent quota history for a member strictly before a given date"""
        query = """
            SELECT id, member_id, club_id, date, cumulative_fans, expected_fans, deficit_surplus, days_behind
            FROM quota_history
            WHERE member_id = $1 AND date < $2
            ORDER BY date DESC
            LIMIT 1
        """
        row = await db.fetchrow(query, member_id, before_date)
        if row:
            return cls(**dict(row))
        return None

    @classmethod
    async def get_for_date(cls, club_id: UUID, date: date) -> List['QuotaHistory']:
        """Get all quota histories for a specific date in a club"""
        query = """
            SELECT id, member_id, club_id, date, cumulative_fans, expected_fans, deficit_surplus, days_behind
            FROM quota_history
            WHERE club_id = $1 AND date = $2
        """
        rows = await db.fetch(query, club_id, date)
        return [cls(**dict(row)) for row in rows]
    
    @classmethod
    async def check_consecutive_behind_days(cls, member_id: UUID, check_days: int, current_date: date = None) -> int:
        """
        Check how many consecutive days a member has been behind quota.
        Only counts days within the same month as current_date to avoid
        February history carrying over into March.
        Returns: number of consecutive days behind (0 if currently on track)
        """
        if check_days <= 0:
            return 0
        if current_date is not None:
            query = """
                SELECT date, deficit_surplus
                FROM quota_history
                WHERE member_id = $1 AND date <= $3
                  AND date_part('year', date) = date_part('year', $3::date)
                  AND date_part('month', date) = date_part('month', $3::date)
                ORDER BY date DESC
                LIMIT $2
            """
            rows = await db.fetch(query, member_id, check_days, current_date)
        else:
            query = """
                SELECT date, deficit_surplus
                FROM quota_history
                WHERE member_id = $1
                ORDER BY date DESC
                LIMIT $2
            """
            rows = await db.fetch(query, member_id, check_days)

        if not rows:
            return 0
        expected_date = current_date or rows[0]['date']
        consecutive = 0
        for row in rows:
            if row['date'] != expected_date or row['deficit_surplus'] >= 0:
                break
            consecutive += 1
            expected_date -= timedelta(days=1)
        return consecutive
    
    @classmethod
    async def get_current_month_for_club(cls, club_id: UUID, year: int, month: int):
        """Get all quota history rows for a club in a given month, joined with trainer names.
        Returns raw asyncpg records with (date, cumulative_fans, trainer_name)."""
        month_start = date(year, month, 1)
        next_month = (
            date(year + 1, 1, 1)
            if month == 12
            else date(year, month + 1, 1)
        )
        query = """
            SELECT qh.date, qh.cumulative_fans, qh.deficit_surplus, m.trainer_name
            FROM quota_history qh
            JOIN members m ON m.member_id = qh.member_id
            WHERE qh.club_id = $1
              AND qh.date >= $2
              AND qh.date < $3
              AND m.is_active = TRUE
            ORDER BY qh.date ASC
        """
        return await db.fetch(query, club_id, month_start, next_month)

    @classmethod
    async def clear_all(cls, club_id: UUID):
        """Clear all quota history for a club (for monthly reset)"""
        query = "DELETE FROM quota_history WHERE club_id = $1"
        await db.execute(query, club_id)
        logger.info(f"Cleared all quota history for club {club_id} (monthly reset)")
