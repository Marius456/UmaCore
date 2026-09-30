# Leaderboard News Report — How It Works

## Overview

The Leaderboard News Report is a dynamically generated Discord embed that analyzes a full month of daily QuotaHistory data for a single club. It examines position changes, fan gains, deficit/surplus trends, and rank rivalries to produce a "sports broadcast" style news report with 5 themed sections.

**Entry point:** `LeaderboardReportService.generate_leaderboard_report(club_id, club_name, year, month)`  
**File:** `services/leaderboard_report_service.py`  
**Returns:** `discord.Embed`

---

## 1. Input Data

### 1.1 Parameters

| Parameter | Type | Description |
|---|---|---|
| `club_id` | UUID | The club's unique identifier |
| `club_name` | str | Display name (e.g. "BonBon") |
| `year` | int | Calendar year (e.g. 2026) |
| `month` | int | Month number (1–12) |

### 1.2 Raw Database Fields (`quota_history` table)

The report works exclusively with data from the `quota_history` table, joined with `members` for trainer names.

**SQL Query** (`QuotaHistory.get_current_month_for_club`):
```sql
SELECT qh.date,
       qh.cumulative_fans,
       qh.deficit_surplus,
       m.trainer_name
FROM quota_history qh
JOIN members m ON m.member_id = qh.member_id
WHERE qh.club_id = $1
  AND date_part('year', qh.date) = $2
  AND date_part('month', qh.date) = $3
  AND m.is_active = TRUE
ORDER BY qh.date ASC
```

| Column | Type | Description |
|---|---|---|
| `date` | date | The date this record was captured |
| `cumulative_fans` | int | **Total** fans the member has accumulated so far this month |
| `deficit_surplus` | int | **Surplus** — difference between cumulative_fans and their expected_fans target. Negative = behind quota |
| `trainer_name` | str | The member's display name (from `members` table) |

### 1.3 Derived / Computed Fields

These are not in the DB but are computed from the raw data:

| Field | How It's Computed | Used For |
|---|---|---|
| **Daily** | `current_cumulative_fans - previous_day_cumulative_fans` | Efficiency King, Overtakes, Consistency |
| **Avg** | Sum of all daily deltas ÷ number of days recorded | Efficiency King, Consistency |
| **Rank** | Position within a day's entries sorted by cumulative_fans desc | Movers, Rivalries, Brick Wall |
| **Prev Rank** | The member's rank on the previous available date | Movers, Leader Change |

### 1.4 Configuration (`config/settings.py`)

| Setting | Value | Purpose |
|---|---|---|
| `COLOR_INFO` | `0x3498db` | Discord embed accent color (blue) |

### 1.5 Milestones (hardcoded)

```python
[1_000_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000, 100_000_000]
```

---

## 2. Data Processing Pipeline (Step-by-Step)

### Step 2.1 — Fetch Raw Data
```python
rows = await QuotaHistory.get_current_month_for_club(club_id, year, month)
```
Returns a list of asyncpg records, each with `date`, `cumulative_fans`, `deficit_surplus`, `trainer_name`.

**Guard clause:** If rows is empty, raises `ValueError`.

### Step 2.2 — Build Daily Rankings
```python
daily_rankings = cls._build_daily_rankings(rows)
```

**Input:** Raw rows from DB  
**Output:** `Dict[date, List[Dict]]` — each date maps to a list of member entries sorted by cumulative_fans descending.

**Per entry dict:**
```python
{
    "name": str,          # trainer_name
    "fans": int,           # cumulative_fans
    "rank": int,           # 1-based position (ties share rank)
    "prev_rank": int|None, # rank from previous available date
    "surplus": int,        # deficit_surplus
    "daily": int,          # fans gained since previous day (0 for first day)
}
```

**Logic:**
1. Group rows by `date`
2. For each date, sort entries by `cumulative_fans` descending
3. Assign ranks (ties get same rank — e.g. two members with equal fans both get rank 3, next is rank 5)
4. Track `prev_rank` by comparing to previous date's rank for the same member
5. Track `prev_day_fans` to compute `daily` delta (current fans - previous day's fans)

### Step 2.3 — Compute Daily Deltas
```python
daily_deltas = cls._compute_daily_deltas(rows)
```

**Input:** Raw rows from DB  
**Output:** `Dict[str, List[Dict]]` — each member name maps to a list of their daily deltas, sorted descending.

**Per delta dict:**
```python
{
    "date": date,
    "delta": int,       # single-day fan gain
    "fans_total": int,  # cumulative_fans after this gain
}
```

**Logic:**
1. Group rows by `trainer_name`
2. For each member, sort their entries by date ascending
3. For each consecutive pair, compute `delta = current_fans - previous_fans`
4. Store each delta with its date and resulting total
5. Sort each member's deltas by delta descending (largest gain first)

### Step 2.4 — Compute Analysis Segments

#### A) Today's Movers
**Method:** `_compute_today_movers(daily_rankings, latest_date)`

Finds members whose rank changed between yesterday and today.

**Output:**
```python
{
    "climbers": [  # top 3 by delta ascending
        {"name": str, "old_rank": int, "new_rank": int, "delta": int}
    ],
    "fallers": [   # top 3 by delta descending
        {"name": str, "old_rank": int, "new_rank": int, "delta": int}
    ]
}
```

**Logic:**
1. Get today's entries for `latest_date`
2. For each entry with a `prev_rank`, compute `delta = prev_rank - current_rank`
3. Positive delta = climber, negative = faller
4. Sort climbers descending by delta, fallers descending by absolute delta
5. Truncate each list to 3

#### B) Leader Change
**Method:** `_compute_leader_change(daily_rankings, latest_date, yesterday_date)`

Checks if the #1 position changed hands today.

**Output:**
```python
{
    "changed": bool,
    "old_leader": str|None,
    "new_leader": str|None,
    "old_streak": int  # consecutive days old leader held #1
}
```

**Logic:**
1. Compare the #1 name on latest_date vs yesterday_date
2. If different, count how many consecutive days the old leader held #1 by walking backwards through sorted dates (skipping today)

#### C) Rivalries
**Method:** `_compute_rivalries(daily_rankings)`

Finds the pairs of members who swapped positions most frequently over the entire month.

**Output:** `List[Dict]` — top 3, each:
```python
{
    "name_a": str,
    "name_b": str,
    "swap_count": int,
    "rank_range": Tuple[int, int],  # (min_rank, max_rank) they occupy
    "who_leads": str|None,           # name of the higher-ranked member today
    "fan_gap": int,                  # fan difference between them today
}
```

**Format in embed:**
```
⚔️ NameA vs NameB (X swaps) — WhoLeads leads by FanGap (#MinRank vs #MaxRank)
```

**Example:**
```
⚔️ Nishikyou vs WuBoy (3 swaps) — Nishikyou leads by 1.2M (#2 vs #3)
```

**Logic:**
1. Build a `day_rankings` dict mapping each date to `{name: rank}`
2. For each consecutive pair of dates, iterate all combinations of members present on both dates
3. If `prev_order != curr_order` (they swapped positions), increment a swap counter for that pair
4. **Current Proximity Gate:** Pairs whose current rank difference exceeds 3 ranks are excluded — they are no longer actively rivaling.
5. For each rivalry, compute current ranks, fan gap, and who leads on the latest date
6. Sort by swap count descending, return top 3

#### D) Today's Personal Records
**Method:** `_compute_today_records(daily_deltas, latest_date)`

Members whose personal best single-day fan gain occurred today.

**Output:**
```python
{
    "members": [
        {"name": str, "delta": int, "prev_best_delta": int|None}
    ],
    "count": int
}
```

**Logic:**
1. For each member, check if their best delta (index 0, since sorted descending) occurred on `latest_date`
2. Skip if they have no previous best to beat (`prev_best is None or <= 0`)
3. Sort by delta descending

#### E) Club Record
**Method:** `_compute_club_record(daily_deltas)`

The single highest daily fan gain across ALL members this month.

**Output:** `{name: str, delta: int, date: date}` or `None`

**Logic:**
1. Scan all deltas for all members
2. Track the highest `delta` value seen

#### F) Efficiency King (The Sprinter)
**Method:** `_compute_efficiency_king(daily_rankings, daily_deltas, latest_date)`

The member whose today's Daily is the highest percentage above their Avg.

**Output:** `{name, daily_gain, avg_daily, pct_above_avg}` or `None`

**Fields:**
| Field | Type | Description |
|---|---|---|
| `name` | str | Member name |
| `daily_gain` | int | Today's fan gain (delta on latest_date) |
| `avg_daily` | float | Mean daily gain across all their recorded days |
| `pct_above_avg` | float | `((daily_gain - avg_daily) / avg_daily) * 100` |

**Logic:**
1. For each member with data on the latest date:
   - Compute `avg_daily = total_delta / len(deltas)`
   - Skip if `avg_daily <= 0` or `today_delta <= 0`
   - Compute `pct_above`
2. Return the member with the highest `pct_above_avg` (minimum threshold: > 0)

#### G) Brick Wall (The Tank)
**Method:** `_compute_brick_wall(daily_rankings, latest_date)`

The member with the highest surplus (deficit_surplus), measured against the player ranked directly below them.

**Output:** `{name, surplus, gap_to_next, rank}` or `None`

**Fields:**
| Field | Type | Description |
|---|---|---|
| `name` | str | Member name |
| `surplus` | int | Their deficit_surplus value |
| `gap_to_next` | int | `their_surplus - surplus_of_member_one_rank_below` |
| `rank` | int | Their current rank |

**Logic:**
1. Requires at least 2 members in today's entries
2. Find the member with the highest `surplus` value
3. Get the surplus of the member ranked directly below them
4. If last place, `gap_to_next = their own surplus`

#### H) Projected Overtakes
**Method:** `_compute_projected_overtakes(daily_rankings, latest_date)`

Adjacent rank pairs where the lower-ranked player is gaining faster.

**Output:** `List[Dict]` — top 3, each:
```python
{
    "challenger": str,     # lower-ranked player
    "target": str,         # higher-ranked player
    "gap_fans": int,       # fan difference to close
    "daily_diff": int,     # challenger's daily - target's daily
    "eta_days": float,     # gap_fans / daily_diff
    "target_rank": int,    # the rank being contested (target's current rank)
}
```

**Logic:**
1. Iterate adjacent pairs in today's ranking (rank N vs rank N+1)
2. If lower player's `daily` > higher player's `daily`:
   - `eta = (fans_above - fans_below) / (daily_below - daily_above)`
3. Only include if `0 < eta <= 14` days
4. Sort by eta ascending, return top 3

#### I) Milestone Watch
**Method:** `_compute_milestone_watch(daily_rankings, latest_date)`

Members whose cumulative fans is within 5% of a major milestone.

**Output:** `List[Dict]` — top 3, each:
```python
{
    "name": str,
    "total": int,              # current cumulative_fans
    "milestone": int,          # the next milestone (1M, 5M, 10M, etc.)
    "amount_away": int,        # milestone - total
    "pct_to_milestone": float, # (total / milestone) * 100
}
```

**Logic:**
1. For each member, iterate milestones in ascending order
2. Find the first milestone greater than their `total`
3. If `total >= milestone * 0.95` (within 5%), add to watch list
4. Break after finding the next milestone (only one per member)
5. Sort by `amount_away` ascending, return top 3

#### J) Consistency Check
**Method:** `_compute_consistency(daily_rankings, daily_deltas, latest_date)`

Compares today's Daily to the monthly Avg for every member.

**Output:**
```python
{
    "top_overperformer": {name, daily, avg, pct_diff}|None,
    "top_cooler": {name, daily, avg, pct_diff}|None,
    "overperformer_count": int,
    "cooler_count": int,
}
```

**Fields:**
| Field | Type | Description |
|---|---|---|
| `name` | str | Member name |
| `daily` | int | Today's fan gain |
| `avg` | float | Monthly average daily gain |
| `pct_diff` | float | `((daily - avg) / avg) * 100` |

**Logic:**
1. For each member with data on the latest date:
   - Skip if `daily <= 0` or `pct_diff == -100.0` (idle/no-play filter)
   - Overperformer if `pct_diff > 0`
   - Cooler if `pct_diff >= -90` (meaningful underperformance, not idle)
2. Sort overperformers descending by pct_diff, coolers ascending (most negative first)
3. Return the top of each category

#### K) Best Week (Rolling 7-Day Average)
**Method:** `_compute_best_week(daily_deltas, latest_date)`

The member with the highest average daily fan gain over the last 7 days of available data (ending on the latest date).

**Output:** `BestWeek{name, avg_daily, days}` or `None`

**Fields:**
| Field | Type | Description |
|---|---|---|
| `name` | str | Member name |
| `avg_daily` | float | Mean daily gain over the 7-day window |
| `days` | int | Number of days with data in the window |

**Logic:**
1. Compute `window_start = latest_date - 6 days` to define a 7-day window (inclusive)
2. For each member, filter their daily deltas to those within the window
3. Skip if fewer than 2 data points (to avoid skewed averages)
4. Compute `avg = sum(deltas) / len(deltas)`
5. Return the member with the highest avg (or None if no eligible members)

**Format in embed:**
```
🏅 Best Week — **MemberName** (avg +5.0M/day over the last 7 days)
```

#### L) Condensed Movers
**Method:** `_compute_condensed_movers(movers)`

Combines climbers and fallers from step A into a single top-3 list.

**Output:** `List[Dict]` — top 3, each:
```python
{
    "name": str,
    "old_rank": int,
    "new_rank": int,
    "abs_delta": int,      # absolute rank change
    "direction": "up"|"down"
}
```

**Logic:**
1. Combine climbers and fallers into one list
2. Sort by `abs_delta` descending
3. Return top 3

---

## 3. Embed Assembly

The final embed has 5 fields, plus a title, description, and footer.

### Embed Header
| Property | Format | Example |
|---|---|---|
| `title` | `📰 Leaderboard News — {club_name}` | `📰 Leaderboard News — BonBon` |
| `description` | `**{Month Year}** · {N} members\n_{Date} update_` | `**June 2026** · 15 members\n_Jun 10 update_` |
| `color` | `COLOR_INFO` (blue `0x3498db`) | |
| `timestamp` | `discord.utils.utcnow()` | |

### Section 1: 🔥 HEADLINE NEWS
**Priority order:**
1. If a leader change happened: `🏆 [NewLeader] has overtaken [OldLeader] for #1!` (with streak context)
2. If both Efficiency King and Brick Wall exist: `[KingName] is today's breakout star, while the battle for #[Rank] intensifies!`
3. If only Efficiency King exists: The full efficiency king narrative sentence
4. Fallback: `👑 [TopPlayer] holds the #1 spot with [Fans] fans.`

### Section 2: 📈 THE MOMENTUM SHIFT
Sub-sections (each with a bold label):
- **🏃 The Sprinter** — Efficiency King narrative (if exists)
- **🛡️ The Tank** — Brick Wall narrative (if exists)
- **🔥 Overperforming** — Top overperformer from consistency (if not the same as Sprinter)
- **❄️ Cooling Down** — Top cooler from consistency (if pass filter)
- **🏅 Records** — Today's personal bests + month's best club record

### Section 3: ⚔️ THE BATTLE ZONE
Sub-sections (sorted by urgency):
- **🚨 Urgent Overtakes** — Overtakes with ETA < 2 days (within 48h)
- **⏳ On the Horizon** — Overtakes with ETA >= 2 days
- **⚔️ Monthly Rivalries** — Top 3 rivalry pairs by swap count

**Urgent overtake line format:**
```
• [Challenger] is projected to overtake [Target] for #[Rank] — TODAY (closing [Gap] gap at +[Rate]/day)
```

**Horizon overtake line format:**
```
• [Challenger] is projected to overtake [Target] for #[Rank] ~[X] days (closing [Gap] gap at +[Rate]/day)
```

ETA is natural language:
- `< 1 day` → `"TODAY"`
- `1 <= eta < 2` → `"TOMORROW"`
- Otherwise → `"~[X] days"` in the horizon section

**Example:**
```
🚨 Urgent Overtakes
• Nishikyou is projected to overtake Pendta550 for #6 — TODAY (closing 120.0K gap at +25.0K/day)
```

### Section 4: 🎯 MILESTONE TRACKER
Each milestone line format:
```
🎯 [Name] — [▰▰▰▰▰▰▰▱▱▱] [Pct]% to [Milestone]
```

The progress bar is 10 blocks (▰ filled, ▱ empty), computed as:
```python
filled = int((pct_to_milestone / 100) * 10)
```

### Section 5: ⬆️⬇️ TOP MOVERS
Compact single line, top 3 absolute rank changes:
```
📈 PlayerA #5→#2 (+3) · 📉 PlayerB #1→#4 (-3) · 📈 PlayerC #10→#8 (+2)
```

### Footer
```
{club_name} · Data from QuotaHistory
```

---

## 4. Formatting Helpers

### `_fmt_fans(n: int) -> str`
Converts a raw integer to a compact human-readable string:

| Input | Output |
|---|---|
| `4_900_000` | `4.9M` |
| `850_000` | `850.0K` |
| `42_000` | `42.0K` |
| `999` | `999` |
| `-1_500_000` | `-1.5M` |

### `_fmt_date(d: date) -> str`
Formats a date as `"Jun 10"` using `d.strftime("%b %d")`.

---

## 5. Edge Cases & Empty State Handling

| Scenario | Behavior |
|---|---|
| No data for the club/month | Raises `ValueError` |
| Only 1 day of data | No daily deltas → no efficiency king, no overtakes, no consistency |
| Only 1 member | No brick wall, no rivalries, no overtakes |
| No rank changes today | Movers section shows `_No rank changes today._` |
| No efficiency king candidate | Falls back to leader-based headline |
| No overtakes within 14 days | Shows `_No imminent overtakes detected._` |
| No milestones within 5% | Shows `_No members approaching major milestones._` |
| No rivalries (no swaps) | Shows `_No notable rivalries this month — the leaderboard has been stable._` |
| Player has no gain today | Excluded from consistency check (idle filter) |
| Player is at -100% of avg | Excluded from cooler list (idle filter) |
| Cooling down < -90% | Excluded from cooler list (idle filter) |

---

## 6. File Reference

| File | Purpose |
|---|---|
| `services/leaderboard_report_service.py` | All computation and formatting logic (983 lines) |
| `models/quota_history.py` | Data model and DB query for fetching raw data |
| `bot/commands/leaderboard.py` | Discord command that triggers the report |
| `config/settings.py` | Embed color and configuration constants |