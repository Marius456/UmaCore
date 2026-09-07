"""Pure quota-policy calculations shared by ingestion and maintenance jobs."""

import calendar
from datetime import date, timedelta


def advance_days_behind(
    previous_date: date | None,
    previous_days: int,
    data_date: date,
    deficit_surplus: int,
) -> int:
    """Advance a behind-quota streak only across adjacent calendar days."""
    if deficit_surplus >= 0:
        return 0
    if previous_date == data_date - timedelta(days=1) and previous_days > 0:
        return previous_days + 1
    return 1


class QuotaSchedule:
    """Precomputed cumulative quota contributions for one calendar month."""

    PERIOD_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14}

    def __init__(
        self,
        reference_date: date,
        quota_period: str,
        default_quota: int,
        requirements,
    ):
        self.month_start = reference_date.replace(day=1)
        days_in_month = calendar.monthrange(reference_date.year, reference_date.month)[1]
        period_days = self.PERIOD_DAYS.get(quota_period, 1)
        ordered = sorted(requirements, key=lambda row: row["effective_date"])

        self._prefix = [0.0] * (days_in_month + 1)
        requirement_index = 0
        effective_quota = default_quota
        for day in range(1, days_in_month + 1):
            current = date(reference_date.year, reference_date.month, day)
            while (
                requirement_index < len(ordered)
                and ordered[requirement_index]["effective_date"] <= current
            ):
                effective_quota = ordered[requirement_index]["daily_quota"]
                requirement_index += 1
            self._prefix[day] = self._prefix[day - 1] + effective_quota / period_days

    def expected(self, member_join_date: date, data_date: date) -> int:
        """Return cumulative expected fans for a member through ``data_date``."""
        if (data_date.year, data_date.month) != (
            self.month_start.year,
            self.month_start.month,
        ):
            raise ValueError("data_date is outside this quota schedule's month")
        start = member_join_date if member_join_date >= self.month_start else self.month_start
        if start > data_date:
            return 0
        return round(self._prefix[data_date.day] - self._prefix[start.day - 1])
