# Data Source

UmaCore fetches member fan counts and club rank data from the Uma.moe API.
Each club needs the numeric `circle_id` from its Uma.moe URL.

## Uma.moe API

The bot first makes a direct authenticated HTTP request. If that request fails,
it retries the same API through a shared Playwright browser, which can satisfy
upstream browser or Cloudflare checks. This is a transport fallback, not a
different data source.

The API provides full-month lifetime cumulative fan values. UmaCore converts
those values to month-relative totals, detects each trainer's first active day,
and stores daily snapshots in PostgreSQL.

### Finding a Circle ID

1. Go to [uma.moe/circles](https://uma.moe/circles/).
2. Find the club.
3. Copy the numeric suffix from its URL. For example,
   `https://uma.moe/circles/860280110` has circle ID `860280110`.
4. Supply it to `/add_club` or `/edit_club`.

### Publication Timing

Uma.moe generally publishes a new daily snapshot around 15:10 UTC. Scheduled
checks make three fast attempts. When the response specifically indicates that
current-day data is not available, they retry every ten minutes for at most six
hours and stop if the club's local date changes.

On the first day of a month, the bot reads the previous month's final snapshot
and attempts an endpoint correction from the new month. The report is dated to
the previous month's final day. During the end-of-month JST rollover, unreliable
rank fields are omitted rather than displayed as current data.

### Invalid or Missing Configuration

New clubs require a numeric `circle_id`. Legacy clubs without one are skipped
with an explanatory error until an administrator supplies it through
`/edit_club`.

## Failure and Concurrency Behavior

If all retries fail, the report channel receives an error instead of a quota
report. No deficit DMs are generated from a failed scrape.

Scrapes, manual checks, dashboard synchronization, and quota recalculations use
a per-club database lock. A competing operation is rejected or skipped. Active
operations refresh their ownership token; abandoned locks become reclaimable
after 30 minutes.
