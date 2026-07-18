"""
Club Rank History data model
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Dict
from uuid import UUID
import logging

from config.database import db

logger = logging.getLogger(__name__)


@dataclass
class ClubRankHistory:
    """Represents a club's rank snapshot on a given date"""
    id: Optional[UUID]
    club_id: UUID
    date: date
    club_rank: Optional[int]
    monthly_rank: Optional[int]
    scraped_at: datetime

    @classmethod
    async def save(cls, club_id: UUID, record_date: date,
                   club_rank: Optional[int], monthly_rank: Optional[int]) -> 'ClubRankHistory':
        """Upsert a rank snapshot for the given date (one record per club per day)."""
        query = """
            INSERT INTO club_rank_history (club_id, date, club_rank, monthly_rank, scraped_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (club_id, date)
            DO UPDATE SET
                club_rank = $3,
                monthly_rank = $4,
                scraped_at = NOW()
            RETURNING id, club_id, date, club_rank, monthly_rank, scraped_at
        """
        row = await db.fetchrow(query, club_id, record_date, club_rank, monthly_rank)
        return cls(**dict(row))

    @classmethod
    async def get_previous(cls, club_id: UUID, before_date: date) -> Optional['ClubRankHistory']:
        """Return the most recent rank record strictly before before_date."""
        query = """
            SELECT id, club_id, date, club_rank, monthly_rank, scraped_at
            FROM club_rank_history
            WHERE club_id = $1 AND date < $2
            ORDER BY date DESC
            LIMIT 1
        """
        row = await db.fetchrow(query, club_id, before_date)
        if row:
            return cls(**dict(row))
        return None

    @classmethod
    async def get_best_rank(cls, club_id: UUID) -> Optional[Dict]:
        """Return the best (lowest) club_rank and monthly_rank ever recorded,
        along with the dates they were achieved.

        Returns a dict with keys:
          - best_club_rank: int
          - best_club_rank_date: date
          - best_monthly_rank: int
          - best_monthly_rank_date: date
        or None if no rank records exist.
        """
        query = """
            WITH best_ranks AS (
                SELECT
                    MIN(club_rank) FILTER (WHERE club_rank IS NOT NULL) AS best_club_rank,
                    MIN(monthly_rank) FILTER (WHERE monthly_rank IS NOT NULL) AS best_monthly_rank
                FROM club_rank_history
                WHERE club_id = $1
            )
            SELECT
                (SELECT best_club_rank FROM best_ranks) AS best_club_rank,
                (SELECT date FROM club_rank_history
                 WHERE club_id = $1 AND club_rank = (SELECT best_club_rank FROM best_ranks)
                 ORDER BY date ASC LIMIT 1) AS best_club_rank_date,
                (SELECT best_monthly_rank FROM best_ranks) AS best_monthly_rank,
                (SELECT date FROM club_rank_history
                 WHERE club_id = $1 AND monthly_rank = (SELECT best_monthly_rank FROM best_ranks)
                 ORDER BY date ASC LIMIT 1) AS best_monthly_rank_date
        """
        row = await db.fetchrow(query, club_id)
        if row and (row["best_club_rank"] is not None or row["best_monthly_rank"] is not None):
            return dict(row)
        return None
