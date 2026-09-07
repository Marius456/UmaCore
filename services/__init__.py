"""Lazy compatibility exports for the services package.

Application code should import concrete service modules directly. Lazy exports
keep older integrations working without importing Discord, Plotly, and every
analytics module whenever one lightweight service is requested.
"""

from importlib import import_module

_EXPORTS = {
    "QuotaCalculator": ("quota_calculator", "QuotaCalculator"),
    "ReportGenerator": ("report_generator", "ReportGenerator"),
    "NotificationService": ("notification_service", "NotificationService"),
    "MonthlyInfoService": ("monthly_info_service", "MonthlyInfoService"),
    "ScrapeLockManager": ("scrape_lock_manager", "ScrapeLockManager"),
    "ScrapeContext": ("scrape_lock_manager", "ScrapeContext"),
    "ScrapeLockUnavailableError": (
        "scrape_lock_manager",
        "ScrapeLockUnavailableError",
    ),
    "LeaderboardReportService": (
        "leaderboard_report_service",
        "LeaderboardReportService",
    ),
    "QuotaSchedule": ("quota_schedule", "QuotaSchedule"),
    "advance_days_behind": ("quota_schedule", "advance_days_behind"),
    "QuotaMaintenanceService": (
        "quota_maintenance_service",
        "QuotaMaintenanceService",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    """Resolve legacy package-level service imports on first access."""
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(name) from error
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value
