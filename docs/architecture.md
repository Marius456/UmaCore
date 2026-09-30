# Architecture

## Dependency direction

UmaCore uses four practical layers:

1. `bot/` and `events/commands.py` are transport adapters. They validate Discord
   or HTTP input, invoke services, and format responses.
2. `services/` owns application workflows, presentation builders, and pure
   quota policy. Services may use models, scrapers, the database, and Discord
   value types, but must not import command modules or `aiohttp.web` requests.
3. `models/` owns persisted entities and focused queries.
4. `scrapers/` and `events/client.py` are external-data adapters.

Import concrete service modules in application code. `services/__init__.py`
contains lazy compatibility exports only; eager package-wide imports recreate
cycles and load Discord/Plotly analytics for unrelated domain operations.

## Quota boundaries

- `QuotaSchedule` is the single source for expected-fan calculations.
- `advance_days_behind` is the single source for calendar-day streak changes.
- `QuotaCalculator` reconciles a fresh scrape with member state.
- `QuotaMaintenanceService` owns bulk backfill and recalculation. HTTP handlers
  and Discord commands must not contain quota-history SQL.
- All state-changing quota workflows must hold `ScrapeContext` for the club.

## Discord command boundaries

Command cogs may depend on services and models for lookup, but pure policy and
maintenance services must not send Discord responses. Delivery-oriented
services such as `NotificationService` are explicit exceptions. Guild-scoped
autocomplete is provided by `ClubAutocompleteMixin` to keep filtering and error
behavior consistent.

## Remaining large modules

The following modules are intentionally not split without additional
characterization coverage:

- `services/leaderboard_report_service.py` combines many tightly related
  analytics and embed assembly helpers. A future split should first separate a
  typed analysis result from Discord rendering.
- `bot/tasks.py` still combines daily report scheduling and official-event
  scheduling. These should become separate lifecycle components when task-loop
  startup/shutdown integration tests are available.
- `scrapers/umamoe_api_scraper.py` combines direct HTTP, browser fallback, and
  response normalization. The next safe seam is an injected API transport with
  parser fixtures shared across both transports.
- `config/database.py` embeds schema bootstrap SQL. Future schema changes should
  move to ordered, versioned migrations rather than extending that bootstrap
  function indefinitely.
