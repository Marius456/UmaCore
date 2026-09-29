"""
Trivia leaderboard data model
"""
from dataclasses import dataclass
from typing import ClassVar, Optional, List
from datetime import datetime
import logging

from config.database import db

logger = logging.getLogger(__name__)


@dataclass
class TriviaLeaderboardEntry:
    """Represents a user's trivia statistics"""
    user_id: int
    highest_streak: int
    total_correct: int
    last_played: datetime
    # Fixed class constants only; never accept SQL identifiers from user input.
    table: ClassVar[str] = "trivia_leaderboard"
    select_columns: ClassVar[str] = (
        "user_id, highest_streak, total_correct, last_played"
    )

    @classmethod
    async def get_by_user(cls, user_id: int) -> Optional['TriviaLeaderboardEntry']:
        """Fetch a user's leaderboard entry"""
        query = f"""
            SELECT {cls.select_columns}
            FROM {cls.table}
            WHERE user_id = $1
        """
        row = await db.fetchrow(query, user_id)
        if row:
            return cls(**dict(row))
        return None

    @classmethod
    async def upsert(cls, user_id: int, highest_streak: int, total_correct: int) -> 'TriviaLeaderboardEntry':
        """Insert or update a user's stats"""
        entry, _ = await cls.record_result(user_id, highest_streak, total_correct)
        return entry

    @classmethod
    async def record_result(
        cls, user_id: int, streak: int, total_correct: int
    ) -> tuple['TriviaLeaderboardEntry', bool]:
        """Atomically update stats and report whether the streak beat the old PB."""
        query = f"""
            INSERT INTO {cls.table}
                (user_id, highest_streak, total_correct, last_played)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET highest_streak = GREATEST({cls.table}.highest_streak, $2),
                total_correct = {cls.table}.total_correct + $3,
                last_played = NOW()
            RETURNING {cls.select_columns}
        """
        async with db.transaction() as conn:
            # SELECT FOR UPDATE cannot lock an absent row. The advisory lock
            # covers both first insert and subsequent updates for this user.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            previous = await conn.fetchval(
                f"SELECT highest_streak FROM {cls.table} WHERE user_id = $1",
                user_id,
            )
            row = await conn.fetchrow(query, user_id, streak, total_correct)
        data = dict(row)
        is_new_record = previous is None or streak > previous
        logger.info(
            f"Updated trivia stats for user {user_id}: "
            f"streak={streak}, total_correct={total_correct}"
        )
        return cls(**data), is_new_record

    @classmethod
    async def get_top(cls, n: int = 10) -> List['TriviaLeaderboardEntry']:
        """Fetch the top N users by highest_streak descending"""
        query = f"""
            SELECT {cls.select_columns}
            FROM {cls.table}
            ORDER BY highest_streak DESC, total_correct DESC
            LIMIT $1
        """
        rows = await db.fetch(query, n)
        return [cls(**dict(row)) for row in rows]


@dataclass
class HorseTriviaLeaderboardEntry(TriviaLeaderboardEntry):
    """Photo-game scores use the same atomic updates in their own table."""
    table = "horse_trivia_leaderboard"
    select_columns = (
        "user_id, highest_streak, total_correct, last_played, best_mode"
    )
    best_mode: str = "japanese"

    @classmethod
    async def record_result(
        cls, user_id: int, streak: int, total_correct: int,
        mode: str = "japanese",
    ) -> tuple['HorseTriviaLeaderboardEntry', bool]:
        """Update combined photo stats and attribute a strictly higher PB."""
        if mode not in {"global", "japanese"}:
            raise ValueError("Invalid horse trivia mode")
        query = f"""
            INSERT INTO {cls.table}
                (user_id, highest_streak, total_correct, last_played, best_mode)
            VALUES ($1, $2, $3, NOW(), $4)
            ON CONFLICT (user_id) DO UPDATE
            SET highest_streak = GREATEST({cls.table}.highest_streak, $2),
                total_correct = {cls.table}.total_correct + $3,
                best_mode = CASE
                    WHEN $2 > {cls.table}.highest_streak THEN $4
                    ELSE {cls.table}.best_mode
                END,
                last_played = NOW()
            RETURNING {cls.select_columns}
        """
        async with db.transaction() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            previous = await conn.fetchval(
                f"SELECT highest_streak FROM {cls.table} WHERE user_id = $1",
                user_id,
            )
            row = await conn.fetchrow(query, user_id, streak, total_correct, mode)
        is_new_record = previous is None or streak > previous
        logger.info(
            "Updated horse trivia stats for user %s: streak=%s, total_correct=%s, mode=%s",
            user_id, streak, total_correct, mode,
        )
        return cls(**dict(row)), is_new_record
