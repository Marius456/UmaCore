# Charts & Stats

---

## /progress_chart

Generate a cumulative fan progression chart for all members in a club this month.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Target club |

The chart shows one line per active member with dates on the X-axis and cumulative fans on the Y-axis. Members who joined mid-month will only have data from their join date onward.

The chart uses the current month's stored `quota_history` rows. It reports that
no data is available until the first successful check of a new month.

---

## /previous_month

View last month's final fan totals for all members, ranked by performance.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Target club |

Shows each member's total fans earned last month. Members who joined mid-month are indicated.

---

## /stats

View bot-wide statistics including total clubs, members, servers, and uptime.

**Restricted to the bot author only.**

No parameters.
