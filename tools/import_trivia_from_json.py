"""
CLI tool to import trivia questions from an LLM-generated JSON file into the
database. The input JSON follows the LLM output schema (each entry has
question_text, correct_answer, wrong_option_1, wrong_option_2, wrong_option_3)
as defined by tools/dump_trivia_for_llm.py.

The options are stored as a JSON array with the correct answer first, matching
the format used by the bot's /trivia add command:
    options = [correct_answer, wrong_option_1, wrong_option_2, wrong_option_3]

Usage:
    python tools/import_trivia_from_json.py
    python tools/import_trivia_from_json.py --input docs/qna.json
    python tools/import_trivia_from_json.py --dry-run
    python tools/import_trivia_from_json.py --verbose
"""
import argparse
import asyncio
import json
import logging
import os
import sys

# Add project root to path so we can import project modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import db
from config.settings import DATABASE_URL
from models.trivia_question import TriviaQuestion


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


async def existing_question_texts() -> set:
    """Fetch all question texts currently in the database."""
    rows = await db.fetch("SELECT question_text FROM trivia_questions")
    return {row["question_text"] for row in rows}


def build_options(entry: dict) -> list:
    """Build the options list for the DB row (correct answer first)."""
    return [
        entry["correct_answer"],
        entry["wrong_option_1"],
        entry["wrong_option_2"],
        entry["wrong_option_3"],
    ]


def validate_entry(entry: dict, index: int) -> str | None:
    """Validate a single question entry. Returns an error message or None."""
    required_keys = [
        "question_text",
        "correct_answer",
        "wrong_option_1",
        "wrong_option_2",
        "wrong_option_3",
    ]
    for key in required_keys:
        value = entry.get(key, "")
        if not isinstance(value, str) or not value.strip():
            return f"Entry #{index}: field '{key}' is missing or empty"

    options = build_options(entry)
    if len(set(options)) != 4:
        return f"Entry #{index}: all four options must be unique"

    return None


async def main():
    parser = argparse.ArgumentParser(
        description="Import trivia questions from an LLM-generated JSON file into "
                    "the database."
    )
    parser.add_argument(
        "--input", type=str, default="docs/qna.json",
        help="Input JSON file path (default: docs/qna.json)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate entries without inserting anything into the database"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print extra debug information"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    if not os.path.exists(args.input):
        print(f"❌ Input file not found: {args.input}")
        sys.exit(1)

    with open(args.input, "r", encoding="utf-8") as f:
        entries = json.load(f)

    print(f"📄 Loaded {len(entries)} question(s) from {args.input}")

    # Validate all entries first
    validation_errors = []
    for i, entry in enumerate(entries, 1):
        error = validate_entry(entry, i)
        if error:
            validation_errors.append(error)

    if validation_errors:
        print("❌ Validation failed for the following entries:")
        for error in validation_errors:
            print(f"   - {error}")
        sys.exit(1)

    print("✅ All entries passed validation")

    if args.dry_run:
        print("🔍 Dry run mode: no entries were inserted.")
        return

    print("🔌 Connecting to database...")
    db.url = DATABASE_URL
    await db.connect()

    try:
        existing = await existing_question_texts()
        print(f"📚 Found {len(existing)} existing question(s) in the database.")

        inserted = 0
        skipped_duplicates = 0

        for i, entry in enumerate(entries, 1):
            question_text = entry["question_text"]
            if question_text in existing:
                logger.info(f"Skipping duplicate question #{i}: {question_text[:60]}...")
                skipped_duplicates += 1
                continue

            options = build_options(entry)
            await TriviaQuestion.create(
                question_text=question_text,
                options=options,
                correct_answer=entry["correct_answer"],
            )
            existing.add(question_text)
            inserted += 1

        print(f"✅ Imported {inserted} new question(s).")
        print(f"   Skipped {skipped_duplicates} duplicate(s).")

        total = await TriviaQuestion.get_count()
        print(f"   Total questions now in database: {total}")

    except Exception as e:
        print(f"❌ Error: {e}")
        raise
    finally:
        await db.disconnect()
        print("🔌 Disconnected from database.")


if __name__ == "__main__":
    asyncio.run(main())