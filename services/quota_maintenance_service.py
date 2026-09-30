"""Bulk quota-history maintenance independent of HTTP and Discord transports."""

from datetime import date, datetime

import pytz

from config.database import db
from models import Club
from .quota_schedule import QuotaSchedule, advance_days_behind


class QuotaMaintenanceService:
    """Set-based backfill and recalculation operations for quota history."""

    @staticmethod
    async def backfill_month(
        club: Club,
        scraped_data: dict,
        fetched_year: int,
        fetched_month: int,
    ) -> int:
        month_start = date(fetched_year, fetched_month, 1)
        next_month = (
            date(fetched_year + 1, 1, 1)
            if fetched_month == 12
            else date(fetched_year, fetched_month + 1, 1)
        )
        quota_requirements = await db.fetch(
            """
            SELECT effective_date, daily_quota
            FROM quota_requirements
            WHERE club_id = $1 AND effective_date >= $2 AND effective_date < $3
            ORDER BY effective_date ASC
            """,
            club.club_id,
            month_start,
            next_month,
        )
        schedule = QuotaSchedule(
            month_start,
            club.quota_period,
            club.daily_quota,
            quota_requirements,
        )

        member_rows = await db.fetch(
            """
            SELECT member_id, trainer_id, join_date
            FROM members
            WHERE club_id = $1 AND is_active = TRUE
              AND trainer_id = ANY($2::text[])
            """,
            club.club_id,
            list(scraped_data),
        )
        members_by_trainer_id = {row["trainer_id"]: row for row in member_rows}
        existing_rows = await db.fetch(
            """
            SELECT member_id, date, deficit_surplus
            FROM quota_history
            WHERE club_id = $1 AND date >= $2 AND date < $3
            """,
            club.club_id,
            month_start,
            next_month,
        )
        existing_by_member = {}
        for row in existing_rows:
            existing_by_member.setdefault(row["member_id"], {})[row["date"]] = row[
                "deficit_surplus"
            ]

        records = []
        for trainer_id, member_data in scraped_data.items():
            member_row = members_by_trainer_id.get(trainer_id)
            if member_row is None:
                continue
            member_id = member_row["member_id"]
            existing = existing_by_member.get(member_id, {})
            consecutive_behind = 0

            for day in range(member_data["join_day"], len(member_data["fans"])):
                cumulative_fans = member_data["fans"][day]
                if cumulative_fans == 0:
                    consecutive_behind = 0
                    continue
                data_date = date(fetched_year, fetched_month, day)
                if data_date in existing:
                    consecutive_behind = (
                        consecutive_behind + 1 if existing[data_date] < 0 else 0
                    )
                    continue

                expected = schedule.expected(member_row["join_date"], data_date)
                deficit_surplus = cumulative_fans - expected
                consecutive_behind = (
                    consecutive_behind + 1 if deficit_surplus < 0 else 0
                )
                records.append(
                    (
                        member_id,
                        club.club_id,
                        data_date,
                        cumulative_fans,
                        expected,
                        deficit_surplus,
                        consecutive_behind,
                    )
                )

        if not records:
            return 0
        inserted = await db.fetch(
            """
            INSERT INTO quota_history
                (member_id, club_id, date, cumulative_fans, expected_fans,
                 deficit_surplus, days_behind)
            SELECT *
            FROM UNNEST(
                $1::uuid[], $2::uuid[], $3::date[], $4::bigint[],
                $5::bigint[], $6::bigint[], $7::integer[]
            )
            ON CONFLICT (member_id, date) DO NOTHING
            RETURNING id
            """,
            *zip(*records),
        )
        return len(inserted)

    @staticmethod
    async def recalculate_current_month(club: Club, today: date | None = None) -> int:
        if today is None:
            today = datetime.now(pytz.timezone(club.timezone)).date()
        month_start = today.replace(day=1)
        quota_requirements = await db.fetch(
            """
            SELECT effective_date, daily_quota
            FROM quota_requirements
            WHERE club_id = $1 AND effective_date >= $2 AND effective_date <= $3
            ORDER BY effective_date ASC
            """,
            club.club_id,
            month_start,
            today,
        )
        schedule = QuotaSchedule(
            today,
            club.quota_period,
            club.daily_quota,
            quota_requirements,
        )
        history = await db.fetch(
            """
            SELECT qh.id, qh.member_id, qh.date, qh.cumulative_fans, m.join_date
            FROM quota_history qh
            JOIN members m ON m.member_id = qh.member_id
            WHERE m.club_id = $1 AND qh.date >= $2 AND qh.date <= $3
            ORDER BY qh.member_id, qh.date ASC
            """,
            club.club_id,
            month_start,
            today,
        )

        streaks = {}
        updates = []
        for row in history:
            expected = schedule.expected(row["join_date"], row["date"])
            deficit_surplus = row["cumulative_fans"] - expected
            previous_date, consecutive = streaks.get(row["member_id"], (None, 0))
            consecutive = advance_days_behind(
                previous_date,
                consecutive,
                row["date"],
                deficit_surplus,
            )
            streaks[row["member_id"]] = (row["date"], consecutive)
            updates.append((expected, deficit_surplus, consecutive, row["id"]))

        if updates:
            async with db.transaction() as connection:
                await connection.executemany(
                    """
                    UPDATE quota_history
                    SET expected_fans = $1, deficit_surplus = $2, days_behind = $3
                    WHERE id = $4
                    """,
                    updates,
                )
        return len(updates)

    @staticmethod
    async def recalculate_days_behind(club_id, current_date: date) -> int:
        rows = await db.fetch(
            """
            SELECT id, member_id, date, deficit_surplus
            FROM quota_history
            WHERE club_id = $1
              AND date >= date_trunc('month', $2::date)::date
              AND date <= $2
            ORDER BY member_id, date ASC
            """,
            club_id,
            current_date,
        )
        streaks = {}
        updates = []
        for row in rows:
            previous_date, consecutive = streaks.get(row["member_id"], (None, 0))
            consecutive = advance_days_behind(
                previous_date,
                consecutive,
                row["date"],
                row["deficit_surplus"],
            )
            streaks[row["member_id"]] = (row["date"], consecutive)
            updates.append((consecutive, row["id"]))

        if updates:
            async with db.transaction() as connection:
                await connection.executemany(
                    "UPDATE quota_history SET days_behind = $1 WHERE id = $2",
                    updates,
                )
        return len(updates)
