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
