# Member Management

These commands are restricted to server admins.

---

## /add_member

Manually add a member to a club. The bot auto-detects members from scrape data, but this lets you add someone early or manually.

| Parameter | Required | Description |
|---|---|---|
| `trainer_name` | Yes | In-game trainer name |
| `join_date` | Yes | Date they joined (YYYY-MM-DD) |
| `club` | Yes | Target club |
| `trainer_id` | No | Numeric trainer ID (if known) |

---

## /deactivate_member

Deactivate a member. They won't be auto-reactivated even if they appear in future scrapes.

| Parameter | Required | Description |
|---|---|---|
| `trainer_name` | Yes | Trainer name to deactivate |
| `club` | Yes | Target club |

---

## /activate_member

Reactivate a previously deactivated member.

| Parameter | Required | Description |
|---|---|---|
| `trainer_name` | Yes | Trainer name to reactivate |
| `club` | Yes | Target club |
