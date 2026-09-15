# User Commands

These commands are available to all members.

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

View your own status as a public image card with quota progress, monthly fan history,
performance, and trainer rankings. Requires being linked via `/link_trainer`.

Quota progress compares stored monthly fans with the requirement through the displayed
data date, not the full month's future target. Weekly and biweekly requirements are
labelled accordingly. Average/day uses elapsed membership days in that month. Best day
uses consecutive observations within a month; missing days break the quota streak.
Days active counts recorded days in the current membership.

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
