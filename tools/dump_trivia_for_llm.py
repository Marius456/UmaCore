"""
CLI tool to dump all trivia questions from the database into an LLM-friendly
markdown file. The output is designed to be used as context for an LLM so it
can generate new questions that match the existing style, format, and tone.

Usage:
    python tools/dump_trivia_for_llm.py
    python tools/dump_trivia_for_llm.py --output docs/trivia-context.md
    python tools/dump_trivia_for_llm.py --limit 50
    python tools/dump_trivia_for_llm.py --shuffle-answers
    python tools/dump_trivia_for_llm.py --verbose
"""
import argparse
import asyncio
import logging
import os
import random
import sys

# Add project root to path so we can import project modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import db
from config.settings import DATABASE_URL


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


SCHEMA_BLOCK = """\
## 📋 OUTPUT SCHEMA (for the LLM)

Each new question MUST follow this exact JSON shape:

```json
{
  "question_text": "The question text",
  "correct_answer": "The correct answer",
  "wrong_option_1": "First wrong option",
  "wrong_option_2": "Second wrong option",
  "wrong_option_3": "Third wrong option"
}
```

Rules:
- Every question has exactly 4 options total (1 correct + 3 wrong).
- All 4 options MUST be unique (no duplicates).
- `question_text`, `correct_answer`, and all 3 wrong options are non-empty strings.
- Keep the same tone, topic area, and difficulty level as the examples above.
"""


async def fetch_all_questions(limit: int):
    """Fetch all (or up to limit) trivia questions ordered by id."""
    query = """
        SELECT id, question_text, options, correct_answer
        FROM trivia_questions
        ORDER BY id
    """
    rows = await db.fetch(query)
    if limit:
        rows = rows[:limit]
    return rows


def build_document(rows, shuffle_answers: bool) -> str:
    """Build the LLM-friendly markdown document from the fetched rows."""
    lines = []
    lines.append("# 🎓 Trivia Question Bank")
    lines.append("")
    lines.append(
        f"_This file contains **{len(rows)}** trivia question(s) exported from the "
        "database for use as LLM context._"
    )
    lines.append("")
    lines.append("Each question has a `question_text`, a list of `options` (multiple "
                 "choice), and the `correct_answer`. Use these as reference examples "
                 "when generating new questions.")
    lines.append("")
    lines.append("---")
    lines.append("")

    for index, row in enumerate(rows, 1):
        options = list(row["options"])
        if shuffle_answers:
            random.shuffle(options)

        lines.append(f"## Question {index} (id: {row['id']})")
        lines.append("")
        lines.append(f"**Question:** {row['question_text']}")
        lines.append("")
        lines.append("**Options:**")
        lines.append("")
        for i, option in enumerate(options, 1):
            lines.append(f"{i}. {option}")
        lines.append("")
        lines.append(f"**Correct answer:** {row['correct_answer']}")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append(SCHEMA_BLOCK)
    lines.append("")

    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(
        description="Dump all trivia questions from the database into an LLM-friendly "
                    "markdown file."
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output markdown file path (default: docs/trivia-for-llm.md)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only export the first N questions (default: all questions)"
    )
    parser.add_argument(
        "--shuffle-answers", action="store_true",
        help="Shuffle the option order for each question in the output"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print extra debug information"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    print("🔌 Connecting to database...")
    db.url = DATABASE_URL
    await db.connect()

    try:
        print("📚 Fetching trivia questions from database...")
        rows = await fetch_all_questions(args.limit)
        print(f"✅ Fetched {len(rows)} question(s).")

        if not rows:
            print("⚠️  No trivia questions found in the database. Exiting.")
            return

        markdown = build_document(rows, args.shuffle_answers)

        output_path = args.output or "docs/trivia-for-llm.md"
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(markdown)

        print(f"✅ LLM-friendly file saved to: {output_path}")
        print(f"   Total questions exported: {len(rows)}")

    except Exception as e:
        print(f"❌ Error: {e}")
        raise
    finally:
        await db.disconnect()
        print("🔌 Disconnected from database.")


if __name__ == "__main__":
    asyncio.run(main())