# Club Management

These commands are restricted to server admins.

---

## /add_club

Register a new club to track.

| Parameter | Required | Description |
|---|---|---|
| `club_name` | Yes | Name of the club |
| `circle_id` | Yes | Numeric ID from Uma.moe |
| `scrape_url` | No | Legacy metadata URL; omitted values use the Uma.moe club URL |
| `daily_quota` | No | Fan goal per period (default: 1,000,000) |
| `quota_period` | No | `daily`, `weekly`, or `biweekly` (default: daily) |
| `timezone` | No | IANA timezone (default: Europe/Amsterdam) |
| `scrape_time` | No | Time to run daily check in HH:MM (default: 16:00) |

**Example:**
```
/add_club club_name:MyClub scrape_url:https://... circle_id:860280110 daily_quota:1000000
```

---

## /remove_club

Permanently delete a club and all its data. Asks for confirmation before proceeding.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Club to remove |

---

## /activate_club

Reactivate a previously deactivated club.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Club to reactivate |

---

## /list_clubs

View all clubs registered in this server, including their status and basic settings.

No parameters.

---

## /edit_club

Modify settings for an existing club.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Club to edit |
| `circle_id` | No | Update uma.moe circle ID |
| `daily_quota` | No | Update quota amount |
| `quota_period` | No | Update quota period (`daily`, `weekly`, `biweekly`) |
| `scrape_time` | No | Update daily check time (HH:MM) |
| `timezone` | No | Update timezone |
Only include the parameters you want to change.
