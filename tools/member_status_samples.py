"""Render offline status fixtures for visual QA (no Discord or database required).

Run from the repository root: python -m tools.member_status_samples --output PATH
An optional --browser executable lets local QA use an installed Chromium browser.
Production rendering continues to use the bundled Playwright Chromium.
"""
import argparse
import asyncio
from dataclasses import replace
from pathlib import Path

from services.member_status_card import card_html, render_card
from services import report_generator as reports
from tests.member_status_regression import sample_status


async def main(output, browser=None, profile_id=None):
    output.mkdir(parents=True, exist_ok=True)
    if browser:
        from playwright.async_api import async_playwright
        reports._playwright = await async_playwright().start()
        reports._playwright_browser = await reports._playwright.chromium.launch(
            executable_path=browser, headless=True,
        )
        reports._playwright_context = await reports._playwright_browser.new_context()
    normal = sample_status()
    if profile_id:
        from services.trainer_profile_service import profile_client
        profile = await profile_client.fetch(profile_id)
        normal = replace(normal, portrait=profile.portrait if profile else None)
        print("Live portrait:", "available" if normal.portrait else "initials fallback")
    variants = {
        "normal": normal,
        "behind": replace(normal, fans=42_000_000, surplus=-62_000_000, days_behind=4,
                          streak=0, points=[(day, int(fans * 42 / 279.5)) for day, fans in normal.points]),
        "inactive": replace(normal, active=False, manually_deactivated=True),
        "long-name": replace(normal, name='日本語のトレーナー • A very long trainer name <&> "Uma"' * 2,
                             club_name="A very long club name & friends" * 3),
        "missing-profile": replace(normal, team_rating=None, followers=None, rank_score=None,
                                   monthly_rank=None, alltime_rank=None, gain_30d=None,
                                   circle_rank=None, profile_fetched_at=None, portrait=None),
        "sparse": replace(normal, points=[normal.points[0], normal.points[5], normal.points[-1]]),
        "zero-single": replace(normal, fans=0, expected=0, surplus=0, best_day=None,
                               quota=0, average=0, streak=1, days_active=1,
                               points=[(normal.data_date, 0)]),
    }
    try:
        for name, status in variants.items():
            (output / f"{name}.html").write_text(card_html(status), encoding="utf-8")
            (output / f"{name}.png").write_bytes(await render_card(status))
            print(f"Rendered {name}")
    finally:
        await reports._close_playwright_browser_async()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser")
    parser.add_argument("--profile-id", help="Use only this public profile's portrait; statistics remain fixtures")
    args = parser.parse_args()
    asyncio.run(main(args.output, args.browser, args.profile_id))
