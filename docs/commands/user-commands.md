# User Commands

These commands are available to all members.

## /uma leaderboard

View public Hall of Fame archive rankings for active trainers in all scanned active clubs
across Discord servers. Each trainer appears once, ranked by their highest single score.
Equal scores share a competition rank (1, 1, 3). These are latest complete imported scans,
not live scores or lifetime records; a new scan replaces the previous archive.

| Parameter | Required | Description |
|---|---|---|
| `uma` | No | Uma name from autocomplete; omit for overall rankings |
| `variant` | No | Outfit from autocomplete; requires `uma` |
| `club` | No | Limit rankings to one club in this server; omit for all scanned clubs |
| `page` | No | Page number, starting at 1; ten trainers per page |

Without `variant`, each trainer's best outfit for the selected Uma is used.
Every entry shows the trainer, club, Uma, outfit, grade icon, and score on a dark card
with circular Uma portraits. Portraits show the character's original outfit because
archive records contain outfit titles rather than image IDs. Scan times are hidden.
If image rendering is unavailable, the bot sends the scores as a text embed.

## /uma status

Privately view your linked trainer's highest score and overall global rank, plus each
Uma/outfit's score and rank against other trainers with that same Uma/outfit. Requires
`/link_trainer` in your club's server and an active trainer. You can then check status from
any Discord server with the bot. Optional `uma` and `variant` filters
also narrow the summary rank; `page` browses ten personal scores at a time.
Unscanned trainers and completed scans with no scores are reported separately.

The Hall of Fame scraper maintains `uma_archive_scans` and `uma_archive_scores` in the
same PostgreSQL database as UmaCore. Import scans using the scraper before using these
commands. UmaCore only reads these tables; running a command never triggers a new scan.

---

## /help

View a private guide to member commands, grouped into account linking and notifications,
clubs and progress, gacha and trivia, and privacy information. Includes required arguments
and guidance for linking your exact trainer name and club before checking personal status
or managing notifications.

No parameters. Available without linking an account or administrator permissions.
Only you can see the response.

---

## /link_trainer

Link your Discord account to your in-game trainer name. Required for DM notifications and `/my_status`.

| Parameter | Required | Description |
|---|---|---|
| `trainer_name` | Yes | Your in-game trainer name |
| `club` | Yes | Your club |

---

## /unlink

Remove your trainer link.

No parameters.

---

## /my_status

View your own status as a public image card with quota progress, daily fan gains,
performance, and trainer rankings. Requires being linked via `/link_trainer`.

Quota progress compares stored monthly fans with the requirement through the displayed
data date, not the full month's future target. Weekly and biweekly requirements are
labelled accordingly. Average/day uses elapsed membership days in that month. Best day
uses consecutive observations within a month; missing days break the quota streak.
Days active counts recorded days in the current membership.
When uma.moe still provides a missed daily snapshot, the next successful club scrape
automatically restores that absent history before generating cards and reports.

Team rating, followers, rank score, monthly/all-time rank, and 30-day gain come from
uma.moe. Circle rank is the club's overall monthly position. Profile data is cached
for five minutes and its retrieval time is shown separately from the quota date.
Missing or inaccessible profile values appear as `—`. Portraits use an explicitly
matched leader outfit, falling back to the shared character displayed on the uma.moe
profile, then initials if no image is available.
The existing `UMAMOE_API_KEY` setting enables profile access. If image rendering fails,
the bot sends a text status instead.

No parameters.

---

## /member_status

View the same public status card for any member by name, using their club's quota rules.

| Parameter | Required | Description |
|---|---|---|
| `trainer_name` | Yes | Trainer name to look up |
| `club` | Yes | Their club |

---

## /notification_settings

Manage which DM notifications you receive.

| Parameter | Required | Description |
|---|---|---|
| `deficit_alerts` | No | Receive DMs when you fall behind quota (`true`/`false`) |
