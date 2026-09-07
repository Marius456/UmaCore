"""
Internal HTTP API server for web UI integration.
Binds to 127.0.0.1 only — never exposed publicly.
"""
import json
import logging
from uuid import UUID
from datetime import datetime, date, timedelta

import pytz
from aiohttp import web

from config.database import db
from models import Club
from scrapers import UmaMoeAPIScraper
from services import QuotaCalculator, ScrapeContext, ScrapeLockUnavailableError

logger = logging.getLogger(__name__)


async def _send_json(request: web.Request, data: dict, status: int = 200) -> web.StreamResponse:
    """Send a JSON response, explicitly writing and flushing the body."""
    payload = json.dumps(data).encode('utf-8')
    resp = web.StreamResponse(status=status)
    resp.content_type = 'application/json'
    resp.content_length = len(payload)
    await resp.prepare(request)
    await resp.write(payload)
    await resp.write_eof()
    return resp


async def _backfill_month(club: Club, scraped_data: dict, fetched_year: int, fetched_month: int) -> int:
    """
    Insert quota_history rows for every day in the scraped fans array
    that doesn't already have a record.

    Uma.moe returns daily_fans as a lifetime-converted monthly array where
    fans[i] represents competition results for date(year, month, i).
    join_day is the first index that has data (1-based), so we iterate
    range(join_day, len(fans)) to cover all competition days up to current.
    """
    period_days = {'daily': 1, 'weekly': 7, 'biweekly': 14}.get(club.quota_period, 1)
    default_quota = club.daily_quota
    month_start = date(fetched_year, fetched_month, 1)

    quota_reqs = await db.fetch(
        "SELECT effective_date, daily_quota FROM quota_requirements "
        "WHERE club_id = $1 AND effective_date >= $2 "
        "ORDER BY effective_date ASC",
        club.club_id, month_start
    )

    days_in_month = (
        date(fetched_year + (fetched_month == 12), fetched_month % 12 + 1, 1)
        - month_start
    ).days
    quota_prefix = [0.0] * (days_in_month + 1)
    quota_index = 0
    effective_quota = default_quota
    for day in range(1, days_in_month + 1):
        current = date(fetched_year, fetched_month, day)
        while (
            quota_index < len(quota_reqs)
            and quota_reqs[quota_index]['effective_date'] <= current
        ):
            effective_quota = quota_reqs[quota_index]['daily_quota']
            quota_index += 1
        quota_prefix[day] = quota_prefix[day - 1] + effective_quota / period_days

    def calc_expected(join_date: date, data_date: date) -> int:
        start = join_date if join_date >= month_start else month_start
        if start > data_date:
            return 0
        return round(quota_prefix[data_date.day] - quota_prefix[start.day - 1])

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
    members_by_trainer_id = {row['trainer_id']: row for row in member_rows}
    existing_rows = await db.fetch(
        """
        SELECT member_id, date, deficit_surplus
        FROM quota_history
        WHERE club_id = $1 AND date >= $2 AND date < $3
        """,
        club.club_id,
        month_start,
        month_start + timedelta(days=days_in_month),
    )
    existing_by_member = {}
    for row in existing_rows:
        existing_by_member.setdefault(row['member_id'], {})[row['date']] = row['deficit_surplus']

    records = []
    for trainer_id, member_data in scraped_data.items():
        member_row = members_by_trainer_id.get(trainer_id)
        if member_row is None:
            continue

        member_id = member_row['member_id']
        join_date_val: date = member_row['join_date']
        join_day: int = member_data['join_day']
        fans: list = member_data['fans']
        existing = existing_by_member.get(member_id, {})

        consecutive_behind = 0

        for i in range(join_day, len(fans)):
            comp_fans = fans[i]
            if comp_fans == 0:
                consecutive_behind = 0
                continue

            comp_date = date(fetched_year, fetched_month, i)

            if comp_date in existing:
                consecutive_behind = (
                    consecutive_behind + 1 if existing[comp_date] < 0 else 0
                )
                continue

            expected = calc_expected(join_date_val, comp_date)
            deficit_surplus = comp_fans - expected
            consecutive_behind = consecutive_behind + 1 if deficit_surplus < 0 else 0

            records.append((
                member_id,
                club.club_id,
                comp_date,
                comp_fans,
                expected,
                deficit_surplus,
                consecutive_behind,
            ))

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


async def handle_sync(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except Exception:
        return await _send_json(request, {'error': 'Invalid JSON body'}, status=400)

    club_id_str = body.get('club_id')
    if not club_id_str:
        return await _send_json(request, {'error': 'club_id required'}, status=400)

    try:
        club_id = UUID(club_id_str)
    except ValueError:
        return await _send_json(request, {'error': 'Invalid club_id'}, status=400)

    club = await Club.get_by_id(club_id)
    if not club:
        return await _send_json(request, {'error': 'Club not found'}, status=404)
    if not club.is_active:
        return await _send_json(request, {'error': 'Club is not active'}, status=400)

    if not club.circle_id:
        return await _send_json(request, {'error': 'Club has no circle_id configured'}, status=400)
    if not club.is_circle_id_valid():
        return await _send_json(request, {'error': 'Invalid circle_id (must be numeric)'}, status=400)
    scraper = UmaMoeAPIScraper(club.circle_id)

    result: dict | None = None
    error: str | None = None

    try:
        async with ScrapeContext(club.club_id, f"web_sync_{club.club_name}"):
            club_tz = pytz.timezone(club.timezone)
            current_date = datetime.now(club_tz).date()

            scraped_data = await scraper.scrape()
            current_day = scraper.get_current_day()

            data_date = scraper.get_data_date()
            if data_date:
                current_date = data_date

            if not scraped_data:
                error = 'Scraper returned no data'
            else:
                quota_calculator = QuotaCalculator()
                new_members, updated_members = await quota_calculator.process_scraped_data(
                    club.club_id, scraped_data, current_date, current_day,
                    quota_period=club.quota_period
                )

                fetched_year = getattr(scraper, '_fetched_year', None) or current_date.year
                fetched_month = getattr(scraper, '_fetched_month', None) or current_date.month
                backfilled = await _backfill_month(club, scraped_data, fetched_year, fetched_month)

                if backfilled:
                    logger.info(f"Backfilled {backfilled} missing quota_history rows for {club.club_name}")

                result = {
                    'success': True,
                    'club_name': club.club_name,
                    'date': str(current_date),
                    'new_members': new_members,
                    'updated_members': updated_members,
                    'backfilled': backfilled,
                }

    except ScrapeLockUnavailableError as e:
        logger.warning("Web sync lock unavailable for club %s: %s", club.club_id, e)
        return await _send_json(
            request,
            {'error': 'Another sync or recalculation is already running'},
            status=409,
        )
    except Exception as e:
        logger.error(f"Web sync failed for {club.club_name}: {e}", exc_info=True)
        error = "Sync failed. Please try again later."

    if error:
        return await _send_json(request, {'error': error}, status=500)
    return await _send_json(request, result)


async def handle_recalculate(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except Exception:
        return await _send_json(request, {'error': 'Invalid JSON body'}, status=400)

    club_id_str = body.get('club_id')
    if not club_id_str:
        return await _send_json(request, {'error': 'club_id required'}, status=400)

    try:
        club_id = UUID(club_id_str)
    except ValueError:
        return await _send_json(request, {'error': 'Invalid club_id'}, status=400)

    club = await Club.get_by_id(club_id)
    if not club:
        return await _send_json(request, {'error': 'Club not found'}, status=404)

    try:
        async with ScrapeContext(club.club_id, f"web_recalculate_{club.club_name}"):
            updated = await _recalculate_club(club)
    except ScrapeLockUnavailableError as e:
        logger.warning("Recalculation lock unavailable for club %s: %s", club_id, e)
        return await _send_json(
            request,
            {'error': 'Another sync or recalculation is already running'},
            status=409,
        )
    except Exception as e:
        logger.error("Recalculation failed for club %s: %s", club_id, e, exc_info=True)
        return await _send_json(request, {'error': 'Recalculation failed'}, status=500)

    logger.info(f"Recalculated {updated} quota_history rows for club {club_id}")
    return await _send_json(request, {'recalculated': updated})


async def _recalculate_club(club: Club) -> int:
    """Recalculate the current month while the caller holds the club lock."""
    period_days = {'daily': 1, 'weekly': 7, 'biweekly': 14}.get(club.quota_period, 1)
    default_quota = club.daily_quota

    today = datetime.now(pytz.timezone(club.timezone)).date()
    month_start = date(today.year, today.month, 1)

    quota_reqs = await db.fetch(
        "SELECT effective_date, daily_quota FROM quota_requirements "
        "WHERE club_id = $1 AND effective_date >= $2 "
        "ORDER BY effective_date ASC",
        club.club_id, month_start
    )

    days_in_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - month_start
    quota_prefix = [0.0] * (days_in_month.days + 1)
    quota_index = 0
    effective_quota = default_quota
    for day in range(1, days_in_month.days + 1):
        current = date(today.year, today.month, day)
        while (
            quota_index < len(quota_reqs)
            and quota_reqs[quota_index]['effective_date'] <= current
        ):
            effective_quota = quota_reqs[quota_index]['daily_quota']
            quota_index += 1
        quota_prefix[day] = quota_prefix[day - 1] + effective_quota / period_days

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
        start = row['join_date'] if row['join_date'] >= month_start else month_start
        expected = (
            round(quota_prefix[row['date'].day] - quota_prefix[start.day - 1])
            if start <= row['date']
            else 0
        )
        deficit_surplus = row['cumulative_fans'] - expected
        previous_date, consecutive_behind = streaks.get(row['member_id'], (None, 0))
        is_adjacent = (
            previous_date is not None
            and row['date'] == previous_date + timedelta(days=1)
        )
        consecutive_behind = (
            consecutive_behind + 1 if deficit_surplus < 0 and is_adjacent
            else 1 if deficit_surplus < 0
            else 0
        )
        streaks[row['member_id']] = (row['date'], consecutive_behind)
        updates.append((expected, deficit_surplus, consecutive_behind, row['id']))

    if updates:
        async with db.transaction() as conn:
            await conn.executemany(
                """
                UPDATE quota_history
                SET expected_fans = $1, deficit_surplus = $2, days_behind = $3
                WHERE id = $4
                """,
                updates,
            )
    return len(updates)


async def handle_health(request: web.Request) -> web.StreamResponse:
    return await _send_json(request, {'status': 'ok'})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post('/sync', handle_sync)
    app.router.add_post('/recalculate', handle_recalculate)
    app.router.add_get('/health', handle_health)
    return app


async def start_api_server(port: int) -> web.AppRunner:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', port)
    await site.start()
    logger.info(f"Internal API server listening on http://127.0.0.1:{port}")
    return runner
