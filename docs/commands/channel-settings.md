# Channel Settings

These commands are restricted to server admins.

---

## /set_report_channel

Set the channel where daily quota reports are posted.

| Parameter | Required | Description |
|---|---|---|
| `channel` | Yes | Discord text channel |
| `club` | Yes | Target club |

---

## /set_alert_channel

Set the reserved operational-alert channel. Daily quota reports continue to use
the report channel; the current release does not emit automated kick alerts.

| Parameter | Required | Description |
|---|---|---|
| `channel` | Yes | Discord text channel |
| `club` | Yes | Target club |

---

## /channel_settings

View the current report and alert channel configuration for a club.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Target club |

---

## /post_monthly_info

Post the monthly info board to a channel. Changes made with `/quota` or
`/delete_quota` automatically refresh the saved board; use
`/update_monthly_info` after changing the base quota or quota period through
`/edit_club`.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Target club |
| `channel` | No | Channel to post in (defaults to the current channel) |

---

## /update_monthly_info

Manually refresh the monthly info board embed.

| Parameter | Required | Description |
|---|---|---|
| `club` | Yes | Target club |

---

## /set_leaderboard_channel

Set the channel for the daily leaderboard analysis generated after a successful
quota scrape.

| Parameter | Required | Description |
|---|---|---|
| `channel` | Yes | Discord text channel |
| `club` | Yes | Target club |

---

## /set_events_channel

Set the channel for official event starting and ending-soon notifications.

| Parameter | Required | Description |
|---|---|---|
| `channel` | Yes | Discord text channel |
| `club` | Yes | Target club |
