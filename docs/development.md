# Development and Testing

## Local setup

Use Python 3.10 or newer. Install runtime and development dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

The test suite does not launch a browser or connect to Discord/PostgreSQL.
Run the checks used by CI with:

```bash
ruff check .
pytest -q
```

## Test layout

Regression tests live in `tests/` and use the `*_regression.py` suffix. New
bug fixes should include a focused regression test where practical. Database,
Discord, and scraper boundaries should be mocked in unit tests; reserve live
network checks for explicit integration scripts.

## Security notes

- Never commit `.env`, browser storage state, logs, or debug captures.
- The internal web API binds to `127.0.0.1`; keep it behind a trusted local
  reverse proxy if the dashboard needs remote access.
- Clubs are isolated by Discord guild. Records with no `guild_id` are not
  exposed to commands; startup attempts to associate legacy records using
  their configured report channel.
- Docker builds use `.dockerignore` so local credentials and browser cookies
  are not copied into image layers.

## Database changes

Schema initialization currently includes idempotent migrations in
`config/database.py`. Any schema change must be safe to run repeatedly against
an existing database and should have a rollback plan before deployment.

## Concurrency and delivery guarantees

- Scrapes, manual force checks, dashboard syncs, and dashboard recalculations
  share a per-club database lock. Lock ownership is token-based and refreshed
  while work is active; callers must not bypass `ScrapeContext` when writing
  member or quota-history state.
- Delayed daily-data retries are bounded to six hours and stop at the club's
  local date boundary. Shutdown cancels and awaits those jobs before database
  and browser resources are closed.
- Prediction snapshots are committed only after every primary report embed is
  delivered. Deficit DMs use a database delivery claim to prevent duplicate
  sends across restarts or multiple bot instances.
- Playwright browser creation and replacement are serialized. Every page must
  be closed in a `finally` block, including retry paths.
