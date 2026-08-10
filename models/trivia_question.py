"""
Trivia question data model
"""
import json
from dataclasses import dataclass
from typing import Optional, List
import logging

from config.database import db

logger = logging.getLogger(__name__)


@dataclass
class TriviaQuestion:
    """Represents a trivia question with multiple choice options"""
    id: int
    question_text: str
    options: List[str]
    correct_answer: str

    @classmethod
    def _parse_row(cls, row) -> dict:
        """Convert a database row to a dict, deserializing options if needed"""
        data = dict(row)
        if isinstance(data.get('options'), str):
            data['options'] = json.loads(data['options'])
        return data

    @classmethod
    async def get_random(cls) -> Optional['TriviaQuestion']:
        """Fetch a random question from the database"""
        query = """
            SELECT id, question_text, options, correct_answer
            FROM trivia_questions
            ORDER BY RANDOM()
            LIMIT 1
        """
        row = await db.fetchrow(query)
        if row:
            return cls(**cls._parse_row(row))
        return None

    @classmethod
    async def get_by_id(cls, question_id: int) -> Optional['TriviaQuestion']:
        """Fetch a specific question by ID"""
        query = """
            SELECT id, question_text, options, correct_answer
            FROM trivia_questions
            WHERE id = $1
        """
        row = await db.fetchrow(query, question_id)
        if row:
            return cls(**cls._parse_row(row))
        return None

    @classmethod
    async def get_all(cls) -> List['TriviaQuestion']:
        """Fetch all trivia questions ordered by ID."""
        query = """
            SELECT id, question_text, options, correct_answer
            FROM trivia_questions
            ORDER BY id
        """
        rows = await db.fetch(query)
        return [cls(**cls._parse_row(row)) for row in rows]

    @classmethod
    async def delete(cls, question_id: int) -> bool:
        """Delete a trivia question and return whether it existed."""
        query = """
            DELETE FROM trivia_questions
            WHERE id = $1
            RETURNING id
        """
        row = await db.fetchrow(query, question_id)
        if row:
            logger.info(f"Deleted trivia question #{question_id}")
            return True
        return False

    @classmethod
    async def create(cls, question_text: str, options: List[str], correct_answer: str) -> 'TriviaQuestion':
        """Insert a new question into the database"""
        query = """
            INSERT INTO trivia_questions (question_text, options, correct_answer)
            VALUES ($1, $2, $3)
            RETURNING id, question_text, options, correct_answer
        """
        row = await db.fetchrow(query, question_text, options, correct_answer)
        logger.info(f"Created trivia question #{row['id']}")
        return cls(**dict(row))

    @classmethod
    async def get_count(cls) -> int:
        """Return total number of questions in the database"""
        query = "SELECT COUNT(*) as count FROM trivia_questions"
        row = await db.fetchrow(query)
        return row['count'] if row else 0
