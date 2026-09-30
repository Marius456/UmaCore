# Implementation Plan

## Overview

Add a "Best Week" statistic to the Leaderboard News report that identifies which member has the highest average daily fan gain over the last 7 days of available data.

## Scope and Context

The existing leaderboard report (`services/leaderboard_report_service.py`) already computes daily deltas per member via `_compute_daily_deltas()` and tracks the "Sprinter" (most daily gain today) and "Efficiency King" (highest % above personal average today). However, there is no rolling window aggregation. The "Best Week" stat fills this gap by looking at a sliding 7-day window ending on the latest available date, computing each member's average daily gain, and returning the top performer.

The stat will appear in the **📈 THE MOMENTUM SHIFT** section as a new sub-section called `**🏅 Best Week**`.

## Edge Cases Handled

- If the month has fewer than 7 days of data, the window shrinks to whatever is available (minimum 1 day required per member).
- Members with only 1 day in the window are excluded to avoid skewed averages.
- Members with zero or negative average daily gain are excluded (shouldn't happen with real data, but defensive).
- If no eligible members exist (e.g., only 1 day of month data), the Best Week stat simply doesn't render.

## Types

Add one new NamedTuple and update the Momentum section assembly.

### New NamedTuple

```python
class BestWeek(NamedTuple):
    name: str
    avg_daily: float
    days: int                # number of days in the 7-day window with data
```

### Modified: Existing `ConsistencyResult` — unchanged.

## Files

Modifications are contained entirely within `services/leaderboard_report_service.py`. No new files are created.

### Modified File: `services/leaderboard_report_service.py`

| Change | Location | Description |
|--------|----------|-------------|
| 1. Add `BestWeek` NamedTuple | After `ConsistencyResult` (~line 65) | New type for the weekly stat |
| 2. Add `_compute_best_week()` method | After `_compute_consistency` (~line 469) | New computation method |
| 3. Call `_compute_best_week()` in `generate_leaderboard_report()` | Inside the analytics section (~line 108) | Compute alongside existing stats |
| 4. Pass `best_week` to `_assemble_momentum()` | At the `_assemble_momentum` call site (~line 129) | Wire the data into assembly |
| 5. Update `_assemble_momentum()` signature | Method definition (~line 509) | Accept new parameter |
| 6. Add Best Week rendering inside `_assemble_momentum()` | Inside the method, after Sprinter block (~line 525) | Render the new stat |

### Unchanged Files

- `tools/generate_test_report.py` — No changes needed; it calls through to the existing pipeline.
- `docs/leaderboard-news-report.md` — Should be updated to document the new feature.
- `docs/leaderboard-test-paragon.md` — Not code, but regenerating would reflect the change.

## Functions

### New Function

| Name | Signature | File | Purpose |
|------|-----------|------|---------|
| `_compute_best_week` | `(cls, daily_deltas: Dict[str, List[Dict]], latest_date: date) -> Optional[BestWeek]` | `services/leaderboard_report_service.py` | Computes the member with the highest average daily gain over the 7 days preceding (and including) `latest_date`. |

**Algorithm:**

1. Determine the 7-day window: `start_date = latest_date - 6 days` to `latest_date` (inclusive).
2. For each member in `daily_deltas`:
   a. Filter their daily deltas to those whose `date` falls within the window.
   b. If fewer than 2 data points in the window → skip.
   c. Compute `avg = sum(deltas) / len(deltas)`.
   d. If `avg <= 0` → skip.
   e. Track candidate with highest avg.
3. Return `BestWeek(name, round(avg, 1), len(filtered_deltas))`.

### Modified Function

| Name | Signature Change | Location | Change |
|------|-----------------|----------|--------|
| `generate_leaderboard_report` | No signature change | ~line 75 | Add `best_week = cls._compute_best_week(daily_deltas, latest_date)` after the consistency computation; pass `best_week` to `_assemble_momentum()` |
| `_assemble_momentum` | `(cls, king, tank, consistency, records, club_rec, latest_date, daily_leader, best_week=None)` | ~line 509 | Add `best_week: Optional[BestWeek] = None` parameter; render section |
| `_assemble_momentum` body | No signature change needed | ~line 525 | After the Sprinter block (`if daily_leader:`), add: `if best_week: parts.append(f"**🏅 Best Week** — **{best_week.name}** (avg +{cls._fmt_fans(round(best_week.avg_daily))}/day over the last {best_week.days} days)")` |

### Removed Functions

None.

## Classes

### Modified Class: `LeaderboardReportService`

| Change | Details |
|--------|---------|
| Add `_compute_best_week` static/class method | New analytical method |
| Update `_assemble_momentum` signature | Accept `best_week` parameter |
| Update `generate_leaderboard_report` | Wire the computation into the pipeline |

No classes are removed or added.

## Dependencies

No new Python packages. Only uses standard library (`datetime` already imported).

## Testing

### Test the new method directly

```python
# In a test file or manually via generate_test_report:
# 1. Verify _compute_best_week returns None when daily_deltas is empty
# 2. Verify it correctly averages exactly 7 days of data
# 3. Verify it correctly averages fewer than 7 days (e.g., early in month)
# 4. Verify a member with 1 data point is excluded
# 5. Verify the highest avg is selected
```

### Manual validation

Run `python tools/generate_test_report.py --club_name "Paragon" --year 2026 --month 6 --verbose` and inspect the generated markdown for the new "Best Week" line in the Momentum section.

## Implementation Order

1. Add the `BestWeek` NamedTuple after `ConsistencyResult` (~line 65).
2. Add the `_compute_best_week` method after `_compute_consistency` (~line 469).
3. In `generate_leaderboard_report`, add the call to `_compute_best_week` and pass it to `_assemble_momentum`.
4. Update `_assemble_momentum` signature to accept `best_week: Optional[BestWeek] = None`.
5. Add the Best Week rendering block inside `_assemble_momentum` after The Sprinter block.
6. Regenerate the test report for Paragon to verify the output looks correct.
7. Update `docs/leaderboard-news-report.md` to document the new feature.