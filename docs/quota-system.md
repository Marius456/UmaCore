# Quota System

## How Quotas Work

Each club has a quota — a fan-earning goal that members need to meet. The bot checks progress daily and compares each member's actual fans against the expected cumulative total.

### Deficit & Surplus

```
deficit_surplus = cumulative_fans - expected_fans
```

- **Positive** → member is ahead of quota
- **Negative** → member is behind quota
- Surplus from previous days can cover future deficits

### Quota Periods

| Period | Description |
|---|---|
| `daily` | Quota applies per day (e.g. 1M/day = 30M/month) |
| `weekly` | Quota applies per 7 days (e.g. 5M/week) |
| `biweekly` | Quota applies per 14 days (e.g. 10M/2 weeks) |

Set or change the period with `/edit_club`.

### Mid-Month Quota Changes

You can change the quota at any time with `/quota`. The new quota applies from that day forward — historical data is unaffected and expected fans are recalculated automatically. The monthly info board also updates.

---

## Consecutive Days Behind

Every quota-history row records `days_behind`. A negative deficit extends the
streak only when the immediately preceding calendar day also has a negative
row. Reaching quota resets the value to zero, and a missing day starts the next
negative streak at one. The daily report and member status commands display
this value; there is no separate warning-countdown subsystem in the current
release.

---

## Monthly Reset

Quota calculations are month-bounded, so the new month's expected totals and
days-behind streaks start fresh while prior history and manual-deactivation
choices remain stored. No destructive reset job is required.

---

## Edge Cases

### Member Lifecycle

#### New Trainer Joins Mid-Month

When a trainer joins mid-month, the bot only knows their current total fan count — it has no data on how many fans they earned before joining. Because of this, **their first daily gain is recorded as `+0`** while the scraper establishes a baseline. Their cumulative quota expectation begins on the recorded join date.

From the next day onward, the bot can calculate their actual daily gain and quota tracking begins normally.

This appears in the daily report embed as:

```
TrainerName    +0    [total fans]
```

#### Trainer Leaves the Club

To protect against truncated upstream responses, a trainer is automatically
deactivated only after being absent from three validated, sufficiently complete
scrapes. Their quota history is preserved, but inactive trainers are removed
from daily reports.

If they rejoin later in the same month, the bot reactivates them and treats it as a fresh join — the same first-day `+0` behaviour applies, and their quota expectations are calculated from the return date, not the original join date.

#### Manually Deactivated vs Auto-Deactivated

There are two kinds of deactivation:

- **Auto-deactivated** — triggered when a trainer disappears from scrape data. They will be reactivated automatically if they reappear.
- **Manually deactivated** — triggered via `/deactivate_member`. The bot will **not** reactivate them automatically, even if they show up in future scrapes. Use `/activate_member` to restore them.

---

### Consecutive Days Reset Each Month

Consecutive days behind are counted **within the current month only**. If a
member ends the previous month behind quota, that streak does not carry over.

A missing scrape day also breaks the streak. Two behind-quota records separated
by a calendar gap are not treated as consecutive days.

---

### Quota Period Edge Cases

#### Weekly / Biweekly Periods

When a club uses `weekly` or `biweekly` quota periods, the daily report shows progress within the current period rather than a running monthly total:

```
TrainerName    500K / 700K this week  (-200K overall)
```

The "overall" figure is the full month deficit/surplus. The per-period figure resets at the start of each new period.

#### Multiple Quota Changes on the Same Day

If `/quota` is run multiple times in a single day, the most recently set value takes effect. The monthly info board reflects the final value for that day.

---

### First Ever Scrape

On the first scrape for a new club, every observed trainer is added as a new
member and month-relative history begins from the available Uma.moe baseline.

---

### Notifications

#### DMs Disabled or User Not Linked

If a member has not linked their Discord account with `/link_trainer`, has not
enabled deficit alerts, or has Discord DMs disabled, a deficit DM is not
delivered. DM failures do not prevent the daily report from being posted.

Successful deficit DMs are recorded per member and calendar day, so restarting
the bot or running multiple instances does not normally resend the same alert.
