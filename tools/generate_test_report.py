"""
CLI tool to generate a test leaderboard report for a specific club.
Connects to the real database, fetches QuotaHistory data, runs the
full report pipeline, and outputs the result as a readable markdown file.

Usage:
    python tools/generate_test_report.py --club_name "BonBon" --year 2026 --month 6
    python tools/generate_test_report.py --club_id <UUID> --year 2026 --month 6
    python tools/generate_test_report.py --club_name "Paragon" --year 2026 --month 6 --output docs/paragon-report.md
    python tools/generate_test_report.py --club_name "BonBon" --year 2026 --month 6 --verbose
"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import date

# Add project root to path so we can import project modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import db
from config.settings import DATABASE_URL
from models.club import Club
from models.quota_history import QuotaHistory
from services.leaderboard_report_service import LeaderboardReportService


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


def fmt_fans(n: int) -> str:
    """Compact fan number formatting (same as the service)."""
    abs_n = abs(n)
    if abs_n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs_n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


async def dump_rankings(club_id, year, month, days: int = 5) -> str:
    """Fetch and return raw daily rankings for the last N days as a markdown string."""
    rows = await QuotaHistory.get_current_month_for_club(club_id, year, month)

    # Group rows by date
    from collections import defaultdict
    by_date: dict = defaultdict(list)
    for row in rows:
        by_date[row["date"]].append(row)

    sorted_dates = sorted(by_date.keys())
    # Only show last N days
    recent_dates = sorted_dates[-days:]

    lines = []
    lines.append("")
    lines.append("## 📊 RAW DAILY RANKINGS")
    lines.append("")
    lines.append(f"_{len(recent_dates)} most recent days of data_")
    lines.append("")

    for d in recent_dates:
        day_rows = by_date[d]
        day_rows.sort(key=lambda r: r["cumulative_fans"], reverse=True)
        lines.append(f"### {d.strftime('%b %d')}")
        lines.append("")
        lines.append("| # | Member | Fans | Surplus |")
        lines.append("|---|---:|---:|---:|")
        for i, row in enumerate(day_rows, 1):
            lines.append(
                f"| {i} | {row['trainer_name']} | "
                f"{fmt_fans(row['cumulative_fans'])} | "
                f"{fmt_fans(row['deficit_surplus'])} |"
            )
        lines.append("")

    return "\n".join(lines)


async def generate_report(club_id, club_name, year, month) -> str:
    """Run the full report pipeline and return markdown string."""
    embed = await LeaderboardReportService.generate_leaderboard_report(
        club_id, club_name, year, month
    )

    lines = []
    lines.append(f"# 📰 Leaderboard News — {club_name}")
    lines.append("")
    lines.append(embed.description)
    lines.append("")
    lines.append("---")
    lines.append("")

    for field in embed.fields:
        lines.append(f"## {field.name}")
        lines.append("")
        lines.append(field.value)
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append(f"*Generated at: {embed.timestamp}*")
    lines.append("")
    lines.append(f"*Footer: {embed.footer.text}*")

    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(
        description="Generate a test leaderboard report markdown file."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--club_id", type=str, help="Club UUID")
    group.add_argument("--club_name", type=str, help="Club name (e.g. 'BonBon')")

    parser.add_argument("--year", type=int, required=True, help="Year (e.g. 2026)")
    parser.add_argument("--month", type=int, required=True, help="Month number (1-12)")
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output markdown file path (default: docs/leaderboard-test-{club_name}.md)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Dump raw daily rankings for the last 5 days before generating the report"
    )

    args = parser.parse_args()

    print(f"🔌 Connecting to database...")
    db.url = DATABASE_URL
    await db.connect()

    # Resolve club
    if args.club_name:
        club = await Club.get_by_name(args.club_name)
        if not club:
            print(f"❌ Club '{args.club_name}' not found in database.")
            await db.disconnect()
            sys.exit(1)
        club_id = club.club_id
        club_name = club.club_name
    else:
        from uuid import UUID
        club_id = UUID(args.club_id)
        club = await Club.get_by_id(club_id)
        club_name = club.club_name if club else str(club_id)

    try:
        print(f"📊 Generating report for {club_name} ({args.year}-{args.month:02d})...")
        markdown = await generate_report(club_id, club_name, args.year, args.month)

        if args.verbose:
            rankings_md = await dump_rankings(club_id, args.year, args.month, days=5)
            markdown += rankings_md

        output_path = args.output or f"docs/leaderboard-test-{club_name.lower()}.md"
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(markdown)

        print(f"✅ Report saved to: {output_path}")

    except ValueError as e:
        print(f"❌ {e}")
    except Exception as e:
        print(f"❌ Error: {e}")
        raise
    finally:
        await db.disconnect()
        print("🔌 Disconnected from database.")


if __name__ == "__main__":
    asyncio.run(main())