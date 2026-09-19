"""Headline selection and copy must agree with the dated leaderboard evidence."""
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from services.leaderboard_report_service import (
    HeadlineCandidate,
    HeadlineType as Kind,
    LeaderboardReportService as Report,
)


CLUB = UUID(int=17)
START = date(2026, 8, 20)


def rankings(*days, start=START):
    """Build real ranked snapshots from (name, cumulative fans) observations."""
    rows = [
        {"date": start + timedelta(days=offset), "trainer_name": name,
         "cumulative_fans": fans, "deficit_surplus": 0}
        for offset, members in enumerate(days) if members is not None
        for name, fans in members.items()
    ]
    return Report._build_daily_rankings(rows)


def candidates(history, day=None):
    return Report._headline_candidates(day or max(history), history)


def of_kind(history, kind, day=None):
    return [c for c in candidates(history, day) if c.kind == kind]


def selected(history, rotation=0):
    return Report._select_headline_candidate(candidates(history), rotation)


def test_new_leader_beats_club_record_and_rotating_stories():
    history = rankings(
        {"Alpha": 10, "Bravo": 9, "Charlie": 1},
        {"Alpha": 12, "Bravo": 10, "Charlie": 2},
        {"Bravo": 30, "Alpha": 14, "Charlie": 8},
    )
    assert of_kind(history, Kind.CLUB_RECORD)
    assert of_kind(history, Kind.PERSONAL_BEST)
    for rotation in range(6):
        story = selected(history, rotation)
        assert story.kind == Kind.LEADER_CHANGE
        assert story.names == ("Bravo", "Alpha")
        assert story.facts["gap"] == 16


def test_club_record_beats_rotating_stories_and_uses_largest_gain():
    history = rankings(
        {"Leader": 100, "Bravo": 20, "Charlie": 10},
        {"Leader": 110, "Bravo": 25, "Charlie": 15},
        {"Leader": 120, "Bravo": 40, "Charlie": 35},
    )
    for rotation in range(6):
        story = selected(history, rotation)
        assert story.kind == Kind.CLUB_RECORD
        assert story.names == ("Charlie",)
        assert story.facts == {"gain": 20, "previous": 10, "shared": False}


def test_records_include_month_day_one_and_exclude_matched_bests():
    history = rankings({"Alpha": 10}, {"Alpha": 20}, start=date(2026, 8, 1))
    assert not of_kind(history, Kind.CLUB_RECORD)
    assert not of_kind(history, Kind.PERSONAL_BEST)
    assert selected(history).kind == Kind.DAILY_GAIN


def test_simultaneous_new_club_records_are_described_as_joint():
    history = rankings({"A": 10, "B": 9}, {"A": 11, "B": 10}, {"A": 15, "B": 14})
    story = selected(history)
    assert story.names == ("A",)
    assert story.facts["shared"]
    assert "jointly sets" in Report._render_headline(story, 0)


def test_climb_uses_actual_previous_calendar_rank_not_cached_prev_rank():
    history = rankings(
        {"A": 100, "B": 80, "C": 60, "D": 50},
        {"A": 100, "B": 80, "C": 60, "D": 90},
    )
    for entry in history[max(history)]:
        entry["prev_rank"] = 99
    story = of_kind(history, Kind.CLIMB)[0]
    assert story.names == ("D",)
    assert story.facts == {"old_rank": 4, "rank": 2, "climb": 2}
    one_place = rankings({"A": 10, "B": 9}, {"A": 10, "B": 11})
    assert not of_kind(one_place, Kind.CLIMB)


def test_personal_best_selects_largest_daily_gain_not_percentage_improvement():
    history = rankings(
        {"Leader": 100, "A": 20, "B": 10},
        {"Leader": 120, "A": 25, "B": 11},
        {"Leader": 140, "A": 35, "B": 20},
    )
    stories = of_kind(history, Kind.PERSONAL_BEST)
    assert {c.names[0] for c in stories} == {"A", "B"}
    assert Report._select_headline_candidate(stories, 0).names == ("A",)


@pytest.mark.parametrize("old_rank, new_rank, qualifies", [
    (5, 3, True), (4, 3, False), (6, 4, False),
])
def test_climbs_require_two_places_and_a_podium_finish(old_rank, new_rank, qualifies):
    yesterday = {f"Runner{i}": (10 - i) * 100 for i in range(1, 7)}
    name = f"Runner{old_rank}"
    today = {**yesterday, name: yesterday[f"Runner{new_rank}"] + 1}
    stories = of_kind(rankings(yesterday, today), Kind.CLIMB)
    assert bool(stories) == qualifies
    if qualifies:
        assert stories[0].facts == {"old_rank": old_rank, "rank": new_rank,
                                  "climb": old_rank - new_rank}


def test_chases_only_include_podium_targets_and_select_shortest_eta():
    history = rankings(
        {"A": 100, "B": 80, "C": 60, "D": 40, "E": 20, "F": 0},
        {"A": 100, "B": 90, "C": 80, "D": 75, "E": 65, "F": 64},
    )
    stories = of_kind(history, Kind.CHASE)
    assert {c.facts["rank"] for c in stories} == {1, 2, 3}
    story = Report._select_headline_candidate(stories, 0)
    assert story.names == ("D", "C")
    assert story.facts["rank"] == 3
    assert story.facts["eta"] == pytest.approx(5 / 15)


@pytest.mark.parametrize("target_rank", range(1, 30))
def test_chase_rank_boundary_even_for_an_imminent_pass(target_rank):
    yesterday = {f"Runner{i}": (40 - i) * 1000 for i in range(1, 31)}
    today = {**yesterday,
             f"Runner{target_rank + 1}": yesterday[f"Runner{target_rank}"] - 1}
    stories = of_kind(rankings(yesterday, today), Kind.CHASE)
    assert bool(stories) == (target_rank <= 3)


def test_endcore_rank_nineteen_chase_falls_back_to_daily_gain():
    leaders = {f"Runner{i}": (40 - i) * 1_000_000 for i in range(1, 19)}
    history = rankings(
        {**leaders, "Minato": 10_000_000, "zoro": 9_000_000},
        {**leaders, "Minato": 10_000_000, "zoro": 9_702_700},
    )
    assert not of_kind(history, Kind.CHASE)
    for rotation in range(10):
        assert selected(history, rotation).kind == Kind.DAILY_GAIN


def test_lower_rank_club_record_remains_headline_worthy():
    leaders = {f"Runner{i}": (40 - i) * 1_000_000 for i in range(1, 19)}
    history = rankings(
        {**leaders, "zoro": 1_000_000},
        {**leaders, "zoro": 2_000_000},
        {**leaders, "zoro": 4_000_000},
    )
    story = selected(history)
    assert story.kind == Kind.CLUB_RECORD
    assert story.rank == 19


@pytest.mark.parametrize("gap, qualifies", [(10, True), (20, True), (21, False)])
def test_chase_two_day_boundary_uses_unrounded_eta(gap, qualifies):
    history = rankings({"A": 100, "B": 90 - gap}, {"A": 100, "B": 100 - gap})
    assert bool(of_kind(history, Kind.CHASE)) == qualifies


@pytest.mark.parametrize("surplus, gains, qualifies", [
    (0, [1_000_000, 1_000_000, 2_000_000], True),
    (-1, [1_000_000, 1_000_000, 2_000_000], False),
    (0, [1_000_000, 1_000_000, 1_999_999], False),
    (0, [100_000, 100_000, 900_000], False),
    (0, [100_000, 2_000_000], False),
])
def test_breakout_respects_sample_volume_surplus_and_fifty_percent(surplus, gains, qualifies):
    days, total = [{"A": 10_000_000}], 10_000_000
    for gain in gains:
        total += gain
        days.append({"A": total})
    history = rankings(*days)
    history[max(history)][0]["surplus"] = surplus
    stories = of_kind(history, Kind.BREAKOUT)
    assert bool(stories) == qualifies
    if qualifies:
        assert stories[0].facts["pct"] == pytest.approx(50)


@pytest.mark.parametrize("length, qualifies", [(4, False), (5, True), (6, False), (10, True),
                                                         (15, True), (20, True), (25, True), (30, True)])
def test_streak_counts_today_and_only_announces_milestones(length, qualifies):
    history = rankings(*[{"A": 100, "B": 10}] * length, start=date(2026, 8, 1))
    stories = of_kind(history, Kind.STREAK)
    assert bool(stories) == qualifies
    if qualifies:
        assert stories[0].facts["streak"] == length


def test_streak_breaks_on_missing_dates_and_shared_first_place():
    for interruption in (None, {"A": 100, "B": 100}):
        history = rankings(
            {"A": 100, "B": 10}, interruption,
            {"A": 100, "B": 10}, {"A": 100, "B": 10}, {"A": 100, "B": 10},
        )
        assert not of_kind(history, Kind.STREAK)


@pytest.mark.parametrize("history", [
    rankings({"A": 100, "B": 90}, None, {"B": 130, "A": 110}),
    rankings({"A": 100, "B": 90}, {"New": 130, "A": 100, "B": 90}),
    rankings({"A": 100, "B": 90}, {"B": 90}),
    rankings({"A": 100, "B": 100}, {"B": 110, "A": 100}),
    rankings({"A": 100, "B": 90}, {"A": 100, "B": 100}),
])
def test_new_leader_requires_two_unique_leaders_present_on_both_dates(history):
    assert not of_kind(history, Kind.LEADER_CHANGE)


def test_missing_intervals_and_midmonth_newcomers_are_not_daily_gains():
    history = rankings({"A": 10}, None, {"A": 100, "New": 80})
    assert {c.kind for c in candidates(history)} == {Kind.QUIET}
    history = rankings({"A": 100, "B": 10}, {"A": 101}, {"A": 102, "B": 90})
    assert all(c.names[0] != "B" for c in candidates(history))


def test_first_observation_does_not_claim_a_record():
    history = rankings({"A": 10}, start=date(2026, 8, 1))
    assert selected(history).kind == Kind.DAILY_GAIN
    assert not of_kind(history, Kind.PERSONAL_BEST)
    assert not of_kind(history, Kind.CLUB_RECORD)
    midmonth = rankings({"A": 10})
    assert selected(midmonth).kind == Kind.QUIET


def test_negative_corrections_and_unknown_intervals_do_not_inflate_record_baselines():
    history = rankings({"A": 100}, {"A": 90}, None, {"A": 1000}, {"A": 1010})
    assert selected(history).kind == Kind.DAILY_GAIN
    assert not of_kind(history, Kind.PERSONAL_BEST)
    assert not of_kind(history, Kind.CLUB_RECORD)
    correction = rankings({"A": 100}, {"A": 90})
    assert selected(correction).kind == Kind.QUIET


def test_month_boundaries_and_future_observations_do_not_supply_evidence():
    history = rankings({"A": 5}, {"A": 10}, {"A": 100}, start=date(2026, 7, 31))
    stories = candidates(history, date(2026, 8, 1))
    assert {c.kind for c in stories} == {Kind.DAILY_GAIN, Kind.QUIET}
    daily = next(c for c in stories if c.kind == Kind.DAILY_GAIN)
    assert daily.facts["gain"] == 10


def test_shared_lead_is_reported_without_naming_a_sole_leader():
    history = rankings({"C": 10, "B": 10, "A": 10})
    story = selected(history)
    assert story.kind == Kind.SHARED_LEAD
    text = Report._render_headline(story, 0)
    assert "**A**, **B** and 1 other share **#1**" in text
    assert not of_kind(history, Kind.CHASE)


def test_tied_ranks_do_not_generate_arbitrary_chase_pairs():
    history = rankings({"A": 100, "B": 90, "C": 80}, {"A": 100, "B": 100, "C": 99})
    assert not of_kind(history, Kind.CHASE)


@pytest.mark.parametrize("kind", [Kind.CLIMB, Kind.PERSONAL_BEST, Kind.CHASE,
                                  Kind.BREAKOUT, Kind.CLUB_RECORD, Kind.DAILY_GAIN])
def test_equal_strength_uses_current_rank_then_name(kind):
    stories = [HeadlineCandidate(kind, (name,), {}, 10, rank)
               for name, rank in (("Z", 3), ("B", 2), ("A", 2))]
    assert Report._select_headline_candidate(stories, 0).names == ("A",)
    assert Report._select_headline_candidate(list(reversed(stories)), 0).names == ("A",)


def test_rotates_between_types_rather_than_weighting_by_number_of_members():
    kinds = [Kind.CLIMB, Kind.PERSONAL_BEST, Kind.CHASE, Kind.BREAKOUT, Kind.STREAK]
    stories = [HeadlineCandidate(kind, ("A",), {}, 1, 1) for kind in kinds]
    stories += [HeadlineCandidate(Kind.CLIMB, ("B",), {}, 0, 2)]
    assert [Report._select_headline_candidate(stories, i).kind for i in range(10)] == kinds * 2


def test_report_date_and_club_offset_control_selection_reproducibly():
    history = rankings({"A": 100}, {"A": 100})
    stories = [HeadlineCandidate(Kind.CLIMB, ("A",), {}, 1, 1),
               HeadlineCandidate(Kind.PERSONAL_BEST, ("B",), {}, 1, 2)]
    with patch.object(Report, "_headline_candidates", return_value=stories), patch.object(
        Report, "_render_headline", side_effect=lambda candidate, variant: candidate.kind.value,
    ):
        first = Report._generate_headline(CLUB, START, history)
        assert Report._generate_headline(CLUB, START, history) == first
        assert Report._generate_headline(UUID(int=18), START, history) != first
        assert Report._generate_headline(CLUB, START + timedelta(days=1), history) != first
    assert Report._generate_headline(CLUB, max(history), history) == Report._generate_headline(
        CLUB, max(history), history,
    )


SAMPLE_STORIES = [
    HeadlineCandidate(Kind.LEADER_CHANGE, ("Ace", "Mike"), {"gap": 200_000}),
    HeadlineCandidate(Kind.CLUB_RECORD, ("Ace",), {"gain": 9_000_000, "previous": 8_000_000, "shared": False}),
    HeadlineCandidate(Kind.CLIMB, ("Ace",), {"climb": 3, "old_rank": 6, "rank": 3}),
    HeadlineCandidate(Kind.PERSONAL_BEST, ("Ace",), {"gain": 3_000_000, "previous": 2_000_000}),
    HeadlineCandidate(Kind.CHASE, ("sesbianlex", "Mike"), {"gap": 910_900, "eta": 0.4, "rank": 3}),
    HeadlineCandidate(Kind.BREAKOUT, ("Ace",), {"gain": 3_000_000, "pct": 63.9}),
    HeadlineCandidate(Kind.STREAK, ("Ace",), {"streak": 10}),
    HeadlineCandidate(Kind.DAILY_GAIN, ("Ace",), {"gain": 3_000_000}),
    HeadlineCandidate(Kind.QUIET, ("Ace",), {"fans": 40_000_000}),
    HeadlineCandidate(Kind.SHARED_LEAD, ("Ace", "Mike"), {"fans": 40_000_000, "count": 2}),
    HeadlineCandidate(Kind.EMPTY, (), {}),
]


@pytest.mark.parametrize("story", SAMPLE_STORIES, ids=lambda c: c.kind.value)
def test_each_story_has_three_complete_bounded_hook_variants(story):
    variants = [Report._render_headline(story, i) for i in range(3)]
    assert len(set(variants)) == 3
    assert len({text.split("\n")[1] for text in variants}) == 1
    for text in variants:
        hook, detail = text.split("\n")
        assert hook.startswith("**") and hook.endswith("**")
        assert detail.endswith(".")
        assert len(text + "\n\n───") <= Report.FIELD_MAX
        assert "Four positions" not in text
        assert "no one can" not in text
        assert not any(term in text.lower() for term in (
            "rear-view", "fast lane", "extra gear", "horsepower", "throne",
            "goalposts", "lease", "front-row seat", "shared custody",
        ))


@pytest.mark.parametrize("eta, horizon", [(0.01, "within a day"), (1, "within a day"),
                                         (1.01, "within two days"), (2, "within two days")])
def test_chase_copy_is_conditional(eta, horizon):
    story = HeadlineCandidate(Kind.CHASE, ("A", "B"), {"gap": 10, "eta": eta, "rank": 2})
    text = Report._render_headline(story, 0)
    assert f"at today's pace, that spot could change hands {horizon}." in text


@pytest.mark.parametrize("story", SAMPLE_STORIES, ids=lambda c: c.kind.value)
def test_long_markdown_names_preserve_the_complete_field(story):
    escaped = story._replace(names=tuple("*_[]`\\@everyone\n" * 100 for _ in story.names))
    text = Report._render_headline(escaped, 0)
    assert len(text + "\n\n───") <= Report.FIELD_MAX
    assert len(text.split("\n")) == 2
    assert text.endswith(".")
    assert "@everyone" not in text
    if story.names:
        assert "\\*\\_" in text
        assert "…" in text


def test_empty_history_has_an_honest_fallback():
    assert selected({START: []}).kind == Kind.EMPTY


@pytest.mark.parametrize("kind", [Kind.PERSONAL_BEST, Kind.CLUB_RECORD])
def test_small_record_improvement_keeps_distinguishable_numbers(kind):
    story = HeadlineCandidate(kind, ("A",),
                              {"gain": 2_000_001, "previous": 2_000_000, "shared": False})
    text = Report._render_headline(story, 0)
    assert "+2,000,001" in text
    assert "+2,000,000" in text


def test_hook_rotation_does_not_lock_three_story_types_to_one_variant():
    stories = SAMPLE_STORIES[2:5]  # Climb, personal best, chase.
    observed = {story.kind: set() for story in stories}
    with patch.object(Report, "_headline_candidates", return_value=stories):
        for offset in range(9):
            day = START + timedelta(days=offset)
            story = Report._select_headline_candidate(stories, day.toordinal() + CLUB.int)
            observed[story.kind].add(Report._generate_headline(CLUB, day, {}))
    assert all(len(variants) == 3 for variants in observed.values())


def test_full_report_keeps_sections_and_uses_the_new_headline():
    import asyncio

    rows = [
        {"date": day, "trainer_name": name, "cumulative_fans": fans, "deficit_surplus": 0}
        for day, observations in (
            (START, {"Alpha": 100_000_000, "Bravo": 90_000_000}),
            (START + timedelta(days=1), {"Bravo": 120_000_000, "Alpha": 101_000_000}),
        ) for name, fans in observations.items()
    ]
    with (
        patch("services.leaderboard_report_service.QuotaHistory.get_current_month_for_club",
              new=AsyncMock(return_value=rows)),
        patch("services.leaderboard_report_service.load_prediction_snapshot", return_value=None),
    ):
        embeds = asyncio.run(Report.generate_leaderboard_report(
            CLUB, "Test Club", 2026, 8, fans_to_next_tier=1000, fans_to_lower_tier=100,
        ))
    fields = [field for embed in embeds for field in embed.fields]
    assert [field.name for field in fields] == [
        "🔥 HEADLINE NEWS", "THE MOMENTUM SHIFT", "THE BATTLE ZONE",
        "CLUB ACTIVITY", "📊 MONTHLY RECORDS", "📈 CLUB GOAL",
    ]
    assert "**Bravo** takes **#1** from **Alpha**" in fields[0].value
    assert fields[0].value.endswith("\n\n───")
    assert len(fields[0].value) <= Report.FIELD_MAX
