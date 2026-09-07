# User Commands

These commands are available to all members.

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

View your own current quota status, including fans today, cumulative progress, deficit/surplus, and consecutive days behind. Requires being linked via `/link_trainer`.

No parameters.

---

## /member_status

View the quota status of any member by name.

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
