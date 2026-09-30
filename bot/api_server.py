"""
Internal HTTP API server for web UI integration.
Binds to 127.0.0.1 only — never exposed publicly.
"""
import json
import logging
from uuid import UUID
from datetime import datetime

import pytz
from aiohttp import web

from models import Club
from scrapers import UmaMoeAPIScraper
from services.quota_calculator import QuotaCalculator
from services.quota_maintenance_service import QuotaMaintenanceService
from services.scrape_lock_manager import ScrapeContext, ScrapeLockUnavailableError

logger = logging.getLogger(__name__)


def _parse_club_id(body) -> tuple[UUID | None, str | None]:
    """Validate the small JSON envelope shared by mutating API endpoints."""
    if not isinstance(body, dict):
        return None, "JSON body must be an object"
    value = body.get("club_id")
    if value is None or value == "":
        return None, "club_id required"
    if not isinstance(value, str):
        return None, "Invalid club_id"
    try:
        return UUID(value), None
    except (ValueError, AttributeError, TypeError):
        return None, "Invalid club_id"


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


async def handle_sync(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except Exception:
        return await _send_json(request, {'error': 'Invalid JSON body'}, status=400)

    club_id, validation_error = _parse_club_id(body)
    if validation_error:
        return await _send_json(request, {'error': validation_error}, status=400)

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
                backfilled = await QuotaMaintenanceService.backfill_month(
                    club, scraped_data, fetched_year, fetched_month
                )

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

    club_id, validation_error = _parse_club_id(body)
    if validation_error:
        return await _send_json(request, {'error': validation_error}, status=400)

    club = await Club.get_by_id(club_id)
    if not club:
        return await _send_json(request, {'error': 'Club not found'}, status=404)

    try:
        async with ScrapeContext(club.club_id, f"web_recalculate_{club.club_name}"):
            updated = await QuotaMaintenanceService.recalculate_current_month(club)
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
