"""Build one dated status snapshot for both member commands and text fallback."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from models import Club, Member, QuotaHistory, QuotaRequirement
from models.club_rank_history import ClubRankHistory
from services.trainer_profile_service import TrainerProfile, profile_client


def number(value, *, positive=False):
    if type(value) is int and value >= (1 if positive else 0):
        return value
    return None


def mapping(value):
    return value if isinstance(value, dict) else {}


def entries(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


@dataclass
class MemberStatus:
    name: str
    trainer_id: str | None
    club_name: str
    joined: date
    active: bool
    manually_deactivated: bool
    data_date: date
    fans: int
    expected: int
    surplus: int
    days_behind: int
    quota: int | None
    quota_label: str
    average: float
    best_day: int | None
    streak: int
    days_active: int
    points: list[tuple[date, int]]
    team_rating: int | None = None
    followers: int | None = None
    rank_score: int | None = None
    monthly_rank: int | None = None
    gain_30d: int | None = None
    alltime_rank: int | None = None
    circle_rank: int | None = None
    profile_fetched_at: datetime | None = None
    portrait: str | None = None

    @property
    def percent(self):
        return int(self.fans / self.expected * 100) if self.expected > 0 else None

    @property
    def badge(self):
        if not self.active:
            return "INACTIVE"
        if self.expected <= 0:
            return "NO QUOTA"
        return "QUOTA MET" if self.surplus >= 0 else "BEHIND QUOTA"


def build_status(member, club, latest, records, quota, profile=None, circle_rank=None):
    records = sorted(
        (r for r in records if member.join_date <= r.date <= latest.date),
        key=lambda r: r.date,
    )
    month_start = latest.date.replace(day=1)
    month_records = [r for r in records if r.date >= month_start]
    days = max(1, (latest.date - max(member.join_date, month_start)).days + 1)
    gains = [
        current.cumulative_fans - previous.cumulative_fans
        for previous, current in zip(records, records[1:])
        if current.date - previous.date == timedelta(days=1)
        and current.date.replace(day=1) == previous.date.replace(day=1)
        and current.cumulative_fans >= previous.cumulative_fans
    ]
    streak = 0
    expected_date = latest.date
    for record in reversed(records):
        if record.date != expected_date or record.deficit_surplus < 0:
            break
        streak += 1
        expected_date -= timedelta(days=1)

    status = MemberStatus(
        name=member.trainer_name, trainer_id=member.trainer_id,
        club_name=club.club_name if club else "Unknown club", joined=member.join_date,
        active=member.is_active, manually_deactivated=member.manually_deactivated,
        data_date=latest.date, fans=latest.cumulative_fans, expected=latest.expected_fans,
        surplus=latest.deficit_surplus, days_behind=latest.days_behind,
        quota=quota, quota_label={"weekly": "Weekly Quota", "biweekly": "Biweekly Quota"}.get(
            club.quota_period if club else "daily", "Daily Quota"),
        average=latest.cumulative_fans / days, best_day=max(gains) if gains else None,
        streak=streak, days_active=len(records),
        points=[(r.date, r.cumulative_fans) for r in month_records],
        circle_rank=number(circle_rank, positive=True),
    )
    if isinstance(profile, TrainerProfile):
        trainer = mapping(profile.data.get("trainer"))
        fans = mapping(profile.data.get("fan_history"))
        status.team_rating = number(trainer.get("team_evaluation_point"))
        status.followers = number(trainer.get("follower_num"))
        status.rank_score = number(trainer.get("rank_score"))
        status.gain_30d = number(mapping(fans.get("rolling")).get("gain_30d"))
        status.alltime_rank = number(mapping(fans.get("alltime")).get("rank"), positive=True)
        monthly = next((r for r in entries(fans.get("monthly"))
                        if (r.get("year"), r.get("month")) ==
                        (latest.date.year, latest.date.month)), {})
        status.monthly_rank = number(monthly.get("rank"), positive=True)
        if status.circle_rank is None and club and club.circle_id:
            circle = next((r for r in entries(profile.data.get("circle_history"))
                           if str(r.get("circle_id")) == str(club.circle_id)
                           and (r.get("year"), r.get("month")) ==
                           (latest.date.year, latest.date.month)), {})
            status.circle_rank = number(circle.get("circle_rank"), positive=True)
        status.profile_fetched_at = profile.fetched_at
        status.portrait = profile.portrait
    return status


async def load_member_status(member: Member) -> MemberStatus | None:
    latest = await QuotaHistory.get_latest_for_member(member.member_id)
    if latest is None or latest.date < member.join_date:
        return None
    club = await Club.get_by_id(member.club_id)
    quota = await QuotaRequirement.get_quota_for_date(club.club_id, latest.date) if club else None
    records = await QuotaHistory.get_for_member_range(member.member_id, member.join_date, latest.date)
    rank = await ClubRankHistory.get_previous(member.club_id, latest.date + timedelta(days=1))
    circle_rank = rank.monthly_rank if rank and rank.date.replace(day=1) == latest.date.replace(day=1) else None
    profile = await profile_client.fetch(member.trainer_id)
    return build_status(member, club, latest, records, quota, profile, circle_rank)
