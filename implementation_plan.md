# Implementation Plan

Upgrade the Leaderboard Report generator in `services/leaderboard_report_service.py` to compute new narrative-driven analytics (Efficiency King, The Brick Wall, Projected Overtakes, Milestone Watch, Consistency Check) and restructure the Discord embed output into a "sports-broadcast" news format with sections for Headline News, Momentum Shift, Projected Battles, and Milestone Tracker, while condensing the existing Risers/Fallers section.

The existing `LeaderboardReportService` class pulls QuotaHistory rows for a given club/month and builds daily rankings, daily deltas, movers, leader changes, rivalries, and records. The upgrade adds 5 new computation methods (efficiency king, surplus tank, projected overtakes, milestone watch, consistency check) and reformats the embed into 5 themed fields: Headline News, Momentum Shift, Projected Battles, Milestone Tracker, and a condensed Top Movers section. The underlying data model (`QuotaHistory` with `cumulative_fans`, `expected_fans`, `deficit_surplus`) remains unchanged; all new values (Daily, Avg) are derived from existing columns. The DB query `get_current_month_for_club` already returns `trainer_name`, `date`, `cumulative_fans` — no schema changes are needed. The effort is localized to a single file (`leaderboard_report_service.py`) plus optional test additions.

[Types]

No new DB models, enums, or data classes are being created. The existing `QuotaHistory` dataclass and row structure suffice. Internally, the service uses `Dict[str, Any]` (row dicts), `List[Dict]` (daily rankings), and `Dict[str, List[Dict]]` (daily deltas). The new computations return the following internal types:

- **Efficiency King**: `Dict{name: str, daily_gain: int, avg_daily: float, pct_above_avg: float}`
- **Brick Wall**: `Dict{name: str, surplus: int, gap_to_next: int, rank: int}`
- **Projected Overtakes**: `List[Dict{challenger: str, target: str, gap_fans: int, daily_diff: int, eta_days: float, target_rank: int}]`
- **Milestone Watch**: `List[Dict{name: str, total: int, milestone: int, amount_away: int, pct_to_milestone: float}]`
- **Consistency Check**: `Dict{overperformers: List[Dict], underperformers: List[Dict]}` (each with `name`, `daily`, `avg`, `pct_diff`)
- **Condensed Movers**: `List[Dict{name: str, old_rank: int, new_rank: int, abs_delta: int, direction: str}]` (top 3 by absolute delta)

[Files]

Only one file is being modified, and no new files are being created.

**Modified file**: `services/leaderboard_report_service.py`
- Add new computation helper methods (listed in Functions section below)
- Refactor `generate_leaderboard_report` to restructure the embed fields into the new 5-section format
- Replace `_format_today_movers` with a condensed format showing top 3 absolute movers
- Keep `_format_leader_change`, `_format_rivalries` (condensed), and `_format_today_records` but integrate them into the new sections
- Keep all existing public and private methods; only add new ones and modify format/embed logic

**Unchanged files**:
- `models/quota_history.py` — no schema changes
- `bot/commands/leaderboard.py` — no interface changes needed (it calls `generate_leaderboard_report` which returns a `discord.Embed`)
- `config/settings.py` — no config changes needed

[Functions]

**New functions** (all `@classmethod` on `LeaderboardReportService`):

1. `_compute_efficiency_king(cls, daily_deltas: Dict[str, List[Dict]], latest_date: date) -> Optional[Dict]`
   - For each member with data on the latest date, compute their `daily_gain` (delta on latest_date) and `avg_daily` (mean of all their deltas in the month). Find the member whose `daily_gain` is highest percentage above their `avg_daily`.
   - Returns `None` if no member qualifies (e.g., only 1 day of data).

2. `_compute_brick_wall(cls, daily_rankings: Dict[date, List[Dict]], latest_date: date) -> Optional[Dict]`
   - For each member in today's ranking, compute `surplus = cumulative_fans - expected_fans` (already in row data, but we need to re-fetch or compute from available data). Since `deficit_surplus` is in the original rows but not in the daily_rankings dict (which only stores `name`, `fans`, `rank`, `prev_rank`), we need to either: (a) pass the original rows through, or (b) add `surplus` to the daily_rankings entries. **Option (b)** — modify `_build_daily_rankings` to include the `deficit_surplus` field from the row (accessible as `row["deficit_surplus"]` in the original loop). Then in `_compute_brick_wall`, find the member with the highest `surplus` and compute `gap_to_next = surplus_of_member - surplus_of_member_ranked_below`.
   - Returns `None` if fewer than 2 members.

3. `_compute_projected_overtakes(cls, daily_rankings: Dict[date, List[Dict]], daily_deltas: Dict[str, List[Dict]], latest_date: date) -> List[Dict]`
   - Iterate through adjacent rank pairs (rank N vs rank N+1). If the lower-ranked player has a higher `daily` (delta on latest_date) than the higher-ranked player, compute:
     - `gap_fans = fans_above - fans_below`
     - `daily_diff = daily_below - daily_above`
     - `eta_days = gap_fans / daily_diff`
   - Include only if `eta_days > 0` and `eta_days <= 14`.
   - Return sorted by `eta_days` ascending, limit to top 3.

4. `_compute_milestone_watch(cls, daily_rankings: Dict[date, List[Dict]], latest_date: date) -> List[Dict]`
   - Check every member in latest_date's rankings. Define milestones = [1_000_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000, 100_000_000].
   - For each member, find the next milestone above their `cumulative_fans`. If `(fans / milestone) >= 0.95` (within 5%), add to watch list.
   - Return sorted by `amount_away` ascending, limit to top 3.

5. `_compute_consistency(cls, daily_deltas: Dict[str, List[Dict]], latest_date: date) -> Dict`
   - For each member with data on the latest date, compare their `daily_gain` (delta on latest_date) to `avg_daily`. If `daily > avg`, mark as "overperforming". If `daily < avg`, mark as "cooling down".
   - Return the most extreme overperformer and most extreme cooler by percentage difference.

6. `_compute_condensed_movers(cls, movers: Dict[str, List[Dict]]) -> List[Dict]`
   - Take `movers` dict (climbers + fallers), combine into one list, sort by absolute `delta` descending, return top 3. Add `direction` field ("up"/"down").

7. `calculate_eta(cls, player_above: Dict, player_below: Dict) -> Optional[float]`
   - Static helper: `gap = player_above["fans"] - player_below["fans"]`, `daily_diff = player_below["daily"] - player_above["daily"]`. If `daily_diff <= 0`, return `None`. Else return `gap / daily_diff`.

**Modified functions**:

1. `_build_daily_rankings` — Add `"surplus"` key to each entry dict using `row["deficit_surplus"]` (available in the original `rows` but not propagated). Also compute `"daily"` delta for the latest date (difference from previous day's `cumulative_fans`). Add `"daily"` key: compute this by tracking previous day's fans per member.

2. `generate_leaderboard_report` — Restructure the embed fields from 4 sections (Today's Movers, Leader Change, Rivalries, Today's Records) to 5 new sections with new ordering. Keep the same parameters and return type (`discord.Embed`). Add calls to the 6 new computation methods. Keep the leader change and rivalries sections as sub-components of the new layout.

3. `_format_today_movers` — Replace with `_format_condensed_movers(cls, condensed: List[Dict]) -> str` — compact single-line format like `📈 PlayerA (+3) · 📉 PlayerB (-2) · 📈 PlayerC (+1)`.

**Removed functions**:
- None. All existing visualization/format functions are kept and may be reused or integrated into sub-sections.
- `_format_today_records` — format logic will be repurposed into "Momentum Shift > Today's Records" sub-section.

[Classes]

No new classes. The single existing class `LeaderboardReportService` in `services/leaderboard_report_service.py` is modified by adding 7 methods and modifying 3 existing methods. No inheritance changes.

[Testing]

No existing test files exist for this service (tests/ directory is empty except `__init__.py`). Testing is considered out of scope for this plan, but the implementation should be manually testable by running the `leaderboard_report` command in Discord with a club that has QuotaHistory data for the current month.

If tests are desired in the future, they should mock `QuotaHistory.get_current_month_for_club` and verify the resulting embed fields contain expected text patterns.

[Implementation Order]

All changes are confined to a single file (`services/leaderboard_report_service.py`). The recommended implementation order is:

1. **Modify `_build_daily_rankings`** to include `"surplus"` (from `deficit_surplus`) and `"daily"` (computed delta from previous day) in each entry dict. This is foundational because subsequent methods depend on these fields.
2. **Create the 6 new computation methods** (Efficiency King, Brick Wall, Projected Overtakes, Milestone Watch, Consistency, Condensed Movers) — these depend on step 1 being complete.
3. **Create helper formatters**: Modify `_format_today_movers` -> `_format_condensed_movers`, create `_format_efficiency_king`, `_format_brick_wall`, `_format_projected_overtakes`, `_format_milestone_watch`.
4. **Refactor `generate_leaderboard_report`** to call all new methods and restructure the embed fields into the new 5-section layout.
5. **Remove or condense** any unused formatter calls and ensure the embed fields are ordered correctly.
6. **Final review** — verify the embed has exactly the right number of fields, no field exceeds Discord's 1024-character value limit, and all formatting is consistent.