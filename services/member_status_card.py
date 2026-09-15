"""Deterministic HTML/SVG status card rendered by the shared report browser."""
from datetime import timedelta
from html import escape

import discord

from services.member_status_service import MemberStatus


def compact(value, signed=False):
    if value is None:
        return "—"
    absolute = abs(value)
    divisor, suffix = next(((scale, suffix) for scale, suffix in
                            ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
                           if absolute >= scale), (1, ""))
    formatted = f"{absolute / divisor:.1f}".rstrip("0").rstrip(".") if suffix else f"{absolute:.0f}"
    return ("−" if value < 0 else "+" if signed else "") + formatted + suffix


def rank(value):
    return f"#{value:,}" if value is not None else "—"


def chart_svg(status):
    width, height = 940, 126
    points = status.points
    if not points:
        return '<svg viewBox="0 0 940 126"><text x="470" y="70" text-anchor="middle" fill="#8c849f" font-size="18">No fan history available</text></svg>'
    start = status.data_date.replace(day=1)
    span = max(1, (status.data_date - start).days)
    maximum = max(1, *(fans for _, fans in points))
    groups = []
    previous = None
    for day, fans in points:
        point = (4 + (day - start).days / span * (width - 8),
                 height - 2 - max(0, fans) / maximum * (height - 6))
        if not groups or day - previous != timedelta(days=1):
            groups.append([])
        groups[-1].append(point)
        previous = day
    paths = ['<path d="M0 4 H940" stroke="#22212a"/>']
    for group in groups:
        line = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in group)
        if len(group) > 1:
            paths.append(f'<path d="{line} L{group[-1][0]:.2f},126 L{group[0][0]:.2f},126 Z" fill="var(--accent)" opacity=".20"/>')
            paths.append(f'<path d="{line}" fill="none" stroke="var(--accent)" stroke-width="2"/>')
        else:
            x, y = group[0]
            paths.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="var(--accent)"/>')
    x, y = groups[-1][-1]
    paths.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="var(--accent)"/>')
    return '<svg viewBox="0 0 940 126" aria-label="Monthly fan progression">' + "".join(paths) + '</svg>'


def card_html(s: MemberStatus) -> str:
    def e(value):
        return escape(str(value), quote=True)
    accent = "#f4c638" if s.surplus >= 0 else "#f4a34d"
    delta_color = "#3bca7d" if s.surplus >= 0 else "#f47777"
    percent = f"{s.percent}%" if s.percent is not None else "—"
    fill = max(0, min(100, s.percent or 0))
    initials = "".join(part[0] for part in s.name.split()[:2]) or "?"
    portrait = (f'<img src="{e(s.portrait)}" alt="">' if s.portrait else "")
    tiles = "".join(f'<div class="tile"><label>{label}</label><strong>{compact(value)}</strong></div>'
                    for label, value in (("TEAM RATING", s.team_rating), ("FOLLOWERS", s.followers),
                                         ("RANK SCORE", s.rank_score)))

    def rows(items):
        return "".join(f'<div class="row"><span>{e(label)}</span><b style="color:{color}">{e(value)}</b></div>'
                       for label, value, color in items)

    performance = rows([
        (s.quota_label, compact(s.quota), "inherit"),
        ("Surplus" if s.surplus >= 0 else "Deficit", compact(s.surplus, True), delta_color),
        ("Days Behind", s.days_behind, "inherit"), ("Avg / Day", compact(s.average), "inherit"),
    ])
    standings = rows([
        ("Monthly Rank", rank(s.monthly_rank), "inherit"),
        ("30d Gain", compact(s.gain_30d), "inherit"),
        ("All-Time Rank", rank(s.alltime_rank), "inherit"),
        ("Circle Rank", rank(s.circle_rank), "inherit"),
    ])
    stats = "".join(f'<div><label>{label}</label><b>{e(value)}</b></div>' for label, value in (
        ("BEST DAY", compact(s.best_day, True)), ("STREAK", f"{s.streak}d"),
        ("DAYS ACTIVE", s.days_active), ("JOINED", s.joined.strftime("%b %d, %Y")),
    ))
    profile_date = ("Profile fetched " + s.profile_fetched_at.strftime("%b %d, %Y %H:%M UTC")
                    if s.profile_fetched_at else "Profile data unavailable")
    inactive = " · Manually deactivated" if s.manually_deactivated else ""
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
* {{box-sizing:border-box}} body {{margin:0;background:#0c0c0f;color:#e8e5f0;font-family:"DejaVu Sans","Segoe UI",Arial,sans-serif}}
#card {{width:1024px;height:1010px;--accent:{accent};padding:42px;background:linear-gradient(#131218 0 132px,#0c0c0f 132px)}}
header {{height:92px;border-bottom:3px solid #2c8bcc;display:flex;align-items:flex-start;gap:20px;position:relative}}
.avatar {{width:82px;height:82px;border:1px solid #2c8bcc;border-radius:50%;flex:none;overflow:hidden;background:#242334;position:relative;display:grid;place-items:center;font-size:30px;color:#aca4bd}}
.avatar img {{position:absolute;width:100%;height:100%;object-fit:cover;inset:0;transform:scale(1.6);transform-origin:50% 25%}}
.identity {{min-width:0;flex:1;padding-top:9px}}
h1 {{margin:0 205px 0 0;font-size:40px;line-height:48px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.subtitle {{font-size:18px;color:#8c849f;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:25px}}
.badge {{position:absolute;right:0;top:1px;border-radius:25px;padding:9px 19px;background:var(--accent);color:#121116;font-size:18px;font-weight:800;white-space:nowrap}}
.tiles {{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-top:24px}}
.tile {{height:105px;border-radius:15px;background:#141319;text-align:center;padding:21px 12px}}
label {{display:block;font-size:17px;font-weight:750;color:#8c849f;letter-spacing:.25px}}
.tile strong {{display:block;font-size:34px;line-height:44px;white-space:nowrap}}
.quota {{margin-top:23px}}
.section-line {{display:flex;justify-content:space-between;align-items:center}}
.percentage {{color:var(--accent);font-size:28px;font-weight:750}}
.track {{height:22px;background:#202027;border-radius:16px;overflow:hidden;margin-top:10px}}
.fill {{height:100%;width:{fill}%;background:var(--accent);border-radius:16px}}
.quota-values {{margin-top:9px;font-size:20px;font-weight:650}}
.chart {{margin-top:58px}}
h2 {{margin:0;font-size:18px;line-height:24px;letter-spacing:.25px;color:#2c8bcc}}
.chart-end {{font-size:17px;color:#8c849f;font-weight:650}}
svg {{display:block;width:940px;height:126px;margin-top:15px;overflow:visible}}
.columns {{display:grid;grid-template-columns:1fr 1fr;gap:29px;margin-top:36px}}
.columns h2 {{margin-bottom:11px}}
.row {{height:40px;display:flex;justify-content:space-between;align-items:center;font-size:19px;padding:0 8px;margin:0 -8px;gap:12px}}
.row:nth-child(odd) {{background:#121116}}
.row span {{color:#8c849f}}
.row b {{font-size:20px;white-space:nowrap}}
.stats {{display:grid;grid-template-columns:repeat(4,1fr);background:#1c1b23;border-radius:15px;margin-top:23px;height:55px;align-items:center;text-align:center}}
.stats>div+div {{border-left:2px solid #5d536e}}
.stats label {{font-size:14px;line-height:18px}}
.stats b {{display:block;font-size:20px;line-height:24px;white-space:nowrap}}
footer {{display:flex;justify-content:space-between;color:#625a73;font-size:15px;margin-top:26px;line-height:22px;gap:15px}}
footer .right {{text-align:right}}
</style></head><body><main id="card">
<header><div class="avatar">{e(initials)}{portrait}</div><div class="identity"><h1>{e(s.name)}</h1>
<div class="subtitle">ID {e(s.trainer_id or '—')} · {e(s.club_name)} · Circle {rank(s.circle_rank)}{inactive}</div></div><div class="badge">{s.badge}</div></header>
<section class="tiles">{tiles}</section>
<section class="quota"><div class="section-line"><label>MONTHLY QUOTA</label><span class="percentage">{percent}</span></div>
<div class="track"><div class="fill"></div></div><div class="section-line quota-values"><span>{compact(s.fans)} / {compact(s.expected)}</span><span style="color:{delta_color}">{compact(s.surplus, True)}</span></div></section>
<section class="chart"><div class="section-line"><h2>FAN PROGRESSION</h2><span class="chart-end">{compact(s.fans)} · {s.data_date.strftime('%b %d')}</span></div>{chart_svg(s)}</section>
<section class="columns"><div><h2>PERFORMANCE</h2>{performance}</div><div><h2>STANDINGS</h2>{standings}</div></section>
<section class="stats">{stats}</section><footer><div>Last updated {s.data_date.strftime('%b %d, %Y')}</div><div class="right">Data: uma.moe + UmaCore<br>{profile_date}</div></footer>
</main></body></html>'''


async def render_card(status: MemberStatus) -> bytes:
    from services import report_generator as reports

    async def render_once():
        context = await reports._get_browser_context_async()
        page = await context.new_page()
        try:
            await page.set_viewport_size({"width": 1024, "height": 1010})
            # The document is self-contained; no browser network requests are needed.
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(card_html(status), wait_until="load", timeout=15000)
            await page.evaluate("""async () => {
                await document.fonts.ready;
                for (const img of document.images) {
                    try { await img.decode(); } catch { img.remove(); }
                }
            }""")
            return await page.locator("#card").screenshot(type="png", timeout=15000)
        finally:
            await page.close()

    async with reports._playwright_lock:
        try:
            return await render_once()
        except Exception:
            await reports._close_playwright_browser_unlocked_async()
            return await render_once()


def fallback_embed(s: MemberStatus) -> discord.Embed:
    """Keep status readable if the browser cannot render the image."""
    embed = discord.Embed(title=f"{s.name[:200]} — {s.badge}", color=0x3498DB if s.surplus >= 0 else 0xF4A34D)
    embed.description = f"Trainer ID: {s.trainer_id or '—'}\nClub: {s.club_name}\nJoined: {s.joined:%b %d, %Y}"
    if s.manually_deactivated:
        embed.description += "\nManually deactivated"
    embed.add_field(name="Monthly quota", value=f"{compact(s.fans)} / {compact(s.expected)}"
                    + (f" ({s.percent}%)" if s.percent is not None else ""), inline=False)
    embed.add_field(name="Performance", value=f"{s.quota_label}: {compact(s.quota)}\n"
                    f"Surplus/Deficit: {compact(s.surplus, True)}\nDays Behind: {s.days_behind}\n"
                    f"Avg / Day: {compact(s.average)}")
    embed.add_field(name="Standings", value=f"Monthly Rank: {rank(s.monthly_rank)}\n"
                    f"30d Gain: {compact(s.gain_30d)}\nAll-Time Rank: {rank(s.alltime_rank)}\n"
                    f"Circle Rank: {rank(s.circle_rank)}")
    embed.add_field(name="Statistics", value=f"Best Day: {compact(s.best_day, True)}\n"
                    f"Streak: {s.streak}d\nDays Active: {s.days_active}")
    embed.add_field(name="Profile", value=f"Team Rating: {compact(s.team_rating)}\n"
                    f"Followers: {compact(s.followers)}\nRank Score: {compact(s.rank_score)}")
    footer = f"Last updated {s.data_date:%b %d, %Y}"
    if s.profile_fetched_at:
        footer += f" • Profile fetched {s.profile_fetched_at:%b %d, %Y %H:%M UTC}"
    embed.set_footer(text=footer)
    return embed
