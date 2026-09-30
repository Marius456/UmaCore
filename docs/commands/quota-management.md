# Quota Management

These commands are restricted to server admins.

---

## /quota

Set the daily quota requirement for a club. Takes effect from today onwards.

| Parameter | Required | Description |
|---|---|---|
| `amount` | Yes | Fan goal (integer) |
| `club` | Yes | Target club |

**Example:**
```
/quota amount:1500000 club:MyClub
```

---

## /quota_history

View all quota changes made this month for a club.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Target club |

---

## /delete_quota

Remove a specific quota entry from history. Useful for correcting mistakes.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Target club |
| `date` | Yes | Date of the entry (YYYY-MM-DD) |
| `amount` | Yes | Quota amount to remove |

---

## /force_check

Manually trigger a daily check immediately. Scrapes current data and posts a report to the report channel.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Club to check |

---

## /recalculate

Recalculate consecutive days-behind counts from existing current-month history, without clearing anything.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Club to recalculate |

