"""Member-facing Discord leaderboards for imported Uma Hall of Fame archives."""

import asyncio
import io
import logging

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

from models import Club
from models.uma_archive import UmaArchive
from services.uma_archive_card import render_card
from .common import ClubAutocompleteMixin

logger = logging.getLogger(__name__)


def label(value, limit=100):
    escaped = discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))
    return escaped[:limit].rstrip("\\")


def score_line(row):
    return (
        f"**{row['score']:,}** · {label(row['rank'], 10)}\n"
        f"{label(row['uma_name'])} — {label(row['variant'])}"
    )


def add_score_fields(embed, rows, *, personal=False):
    for row in rows:
        if personal:
            name = f"{label(row['uma_name'])} — {label(row['variant'])}"
            value = (f"**{row['score']:,}** · {label(row['rank'], 10)}\n"
                     f"Outfit rank: **#{row['position']} / {row['participants']}** trainers")
        else:
            name = f"#{row['position']} · {label(row['trainer_name'])} · {label(row['club_name'])}"
            value = score_line(row)
        embed.add_field(name=name, value=value, inline=False)


def text_summary(embed):
    """Keep every entry and the page footer inside Discord's content limit."""
    def clipped(value, limit):
        value = str(value or '').replace('**', '')
        if len(value) <= limit:
            return value
        return value[:limit - 1].rstrip('\\') + '…'

    header = '\n'.join(filter(None, [clipped(embed.title, 150),
                                      clipped(embed.description, 500)]))
    footer = clipped(embed.footer.text, 250)
    fields = embed.fields
    # Reserve two newlines around each entry, plus the header/footer separator.
    budget = (2000 - len(header) - len(footer) - 2 - 2 * len(fields)) // max(1, len(fields))
    entries = []
    for field in fields:
        name = clipped(field.name, budget // 2)
        value = clipped(field.value, budget - len(name) - 1)
        entries.append(f'{name}\n{value}')
    return '\n\n'.join(filter(None, [header, *entries, footer]))


class ArchivePaginationView(discord.ui.View):
    """Browse the same message, retaining filters and preventing overlapping edits."""

    def __init__(self, cog, owner_id, page, pages, loader, *, personal=False):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.page = page
        self.pages = pages
        self.loader = loader
        self.personal = personal
        self.message = None
        self.lock = asyncio.Lock()
        self._update_buttons()

    def _update_buttons(self):
        self.previous.disabled = self.page <= 1
        self.next.disabled = self.page >= self.pages

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            'Run /uma status or /uma leaderboard to browse your own pages.', ephemeral=True,
        )
        return False

    async def change_page(self, interaction, delta):
        await interaction.response.defer()
        async with self.lock:
            if self.is_finished():
                return
            target = self.page + delta
            if not 1 <= target <= self.pages:
                return
            try:
                embed, rows, pages = await self.loader(target)
                file = await self.cog._render_card(embed, rows, personal=self.personal)
                old_page, old_pages = self.page, self.pages
                self.page, self.pages = target, pages
                self._update_buttons()
                try:
                    # Replace attachments even on fallback, so an old page image cannot linger.
                    await interaction.edit_original_response(
                        embed=None, content=None if file else text_summary(embed),
                        attachments=[file] if file else [], view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    self.page, self.pages = old_page, old_pages
                    self._update_buttons()
                    raise
                finally:
                    if file:
                        file.close()
            except Exception as error:
                await self.cog._error(interaction, error, private=True)

    @discord.ui.button(label='◀', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_page(interaction, -1)

    @discord.ui.button(label='▶', style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_page(interaction, 1)

    async def on_timeout(self):
        async with self.lock:
            for button in self.children:
                button.disabled = True
            if self.message:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass


class UmaArchiveCommands(ClubAutocompleteMixin, commands.Cog):
    uma = app_commands.Group(name="uma", description="Uma archive scores and rankings",
                             guild_only=True)
    club_autocomplete = ClubAutocompleteMixin.club_autocomplete

    def __init__(self, bot):
        self.bot = bot

    async def _render_card(self, embed, rows, *, personal=False):
        try:
            subtitle = embed.description.replace('**', '')
            image = await render_card(rows, embed.title, subtitle, embed.footer.text, personal=personal)
        except Exception:
            logger.warning('Uma archive card rendering failed; using text', exc_info=True)
            return None
        return discord.File(io.BytesIO(image), 'uma-archive.png')

    async def _send_card(self, interaction, embed, rows, view, *, personal=False):
        file = await self._render_card(embed, rows, personal=personal)
        kwargs = dict(view=view, ephemeral=False, wait=True,
                      allowed_mentions=discord.AllowedMentions.none())
        if file:
            kwargs['file'] = file
        else:
            kwargs['content'] = text_summary(embed)
        try:
            view.message = await interaction.followup.send(**kwargs)
        except Exception:
            view.stop()
            raise
        finally:
            if file:
                file.close()

    async def _club(self, interaction, club):
        if club is None:
            return None
        result = await Club.get_by_name(club, interaction.guild_id)
        if not result or not result.belongs_to_guild(interaction.guild_id):
            raise ValueError("Choose a club registered in this server.")
        return result.club_id

    @staticmethod
    def _validate_filter(uma, variant):
        if variant and not uma:
            raise ValueError("Choose an Uma before filtering by outfit.")

    async def _error(self, interaction, error, *, private=False):
        if isinstance(error, ValueError):
            message = str(error)
        elif isinstance(error, asyncpg.UndefinedTableError):
            message = "Archive scores are not set up yet. Ask the bot owner to import Hall of Fame scans."
        else:
            logger.error("Uma archive command failed", exc_info=(type(error), error, error.__traceback__))
            message = "Unable to load archive scores right now. Please try again later."
        await interaction.followup.send(message, ephemeral=private,
                                        allowed_mentions=discord.AllowedMentions.none())

    @uma.command(name="leaderboard", description="View overall or per-Uma archive score rankings")
    @app_commands.describe(uma="Uma name; omit for overall rankings",
                           variant="Outfit; omit to include all outfits",
                           club="Club in this server; omit to include all scanned clubs across servers",
                           page="Results page (10 scores per page)")
    async def leaderboard(self, interaction: discord.Interaction, uma: str | None = None,
                          variant: str | None = None, club: str | None = None,
                          page: app_commands.Range[int, 1, 10000] = 1):
        await interaction.response.defer()
        try:
            self._validate_filter(uma, variant)
            club_id = await self._club(interaction, club)
            rows = await UmaArchive.leaderboard(None, club_id, uma, variant, page)
            if not rows:
                await interaction.followup.send(
                    "No archive scores on this page for these filters. Try page 1 or another Uma/club."
                )
                return
            title = "Uma Archive — " + (label(uma, 70) if uma else "Overall")
            entry_unit = 'trainers' if uma else 'scores'
            description = (
                ("Each trainer's highest score for this Uma; equal scores share a rank.\n" if uma else
                 "All recorded Uma/outfit scores; trainers can appear more than once. Equal scores share a rank.\n")
                + (f"Club: {label(club)}" if club else "All scanned clubs across Discord servers")
                + (f" · Outfit: {label(variant)}" if variant else " · All outfits")
            )
            embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
            add_score_fields(embed, rows)
            pages = (rows[0]['participants'] + 9) // 10
            embed.set_footer(text=f"Page {page}/{pages} · {rows[0]['participants']} {entry_unit} · "
                             "/uma status for your ranks")
            async def load_page(target):
                new_rows = await UmaArchive.leaderboard(None, club_id, uma, variant, target)
                if not new_rows:
                    raise ValueError('Scores have changed. Run /uma leaderboard again to refresh.')
                new_pages = (new_rows[0]['participants'] + 9) // 10
                new_embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
                add_score_fields(new_embed, new_rows)
                new_embed.set_footer(text=f"Page {target}/{new_pages} · {new_rows[0]['participants']} {entry_unit} · "
                                    "/uma status for your ranks")
                return new_embed, new_rows, new_pages

            view = ArchivePaginationView(self, interaction.user.id, page, pages, load_page)
            await self._send_card(interaction, embed, rows, view)
        except Exception as error:
            await self._error(interaction, error)

    @uma.command(name="status", description="Show your archive scores and global rankings in this channel")
    @app_commands.describe(uma="Uma name; omit to view your whole archive",
                           variant="Outfit; omit to include all outfits",
                           page="Results page (10 scores per page)")
    async def status(self, interaction: discord.Interaction, uma: str | None = None,
                     variant: str | None = None, page: app_commands.Range[int, 1, 10000] = 1):
        await interaction.response.defer()
        try:
            self._validate_filter(uma, variant)
            member = await UmaArchive.linked_member(None, interaction.user.id)
            if not member:
                raise ValueError("Use /link_trainer in your club's server to link an active trainer first.")
            if member['observed_at'] is None:
                raise ValueError("Your archive has not been scanned yet. Ask the bot owner to import your scan.")
            if member['entry_count'] == 0:
                raise ValueError("Your latest complete archive scan contains no scores.")
            rows = await UmaArchive.personal_scores(
                None, member['member_id'], uma, variant, page,
            )
            if not rows:
                raise ValueError("No scores on this page for these filters. Try page 1 or another Uma.")
            standing = await UmaArchive.standing(None, member['member_id'], uma, variant)
            description = f"{label(member['trainer_name'])} · {label(member['club_name'])}\n"
            if standing:
                description += (
                    f"{'Filtered' if uma else 'Overall'} global rank: "
                    f"**#{standing['position']} / {standing['participants']}** {'trainers' if uma else 'scores'} "
                    f"· Best score: **{standing['score']:,}**\n"
                )
            embed = discord.Embed(title="Your Uma Archive", description=description,
                                  color=discord.Color.blurple())
            add_score_fields(embed, rows, personal=True)
            pages = (rows[0]['total_entries'] + 9) // 10
            embed.set_footer(text=f"Page {page}/{pages} · {rows[0]['total_entries']} scores · "
                             "Ranks across all scanned clubs; ties share a rank")
            async def load_page(target):
                # Revalidate the link, including active membership, before revealing another page.
                current_member = await UmaArchive.linked_member(None, interaction.user.id)
                if not current_member or current_member['member_id'] != member['member_id']:
                    raise ValueError('Your trainer link has changed. Run /uma status again.')
                new_rows = await UmaArchive.personal_scores(None, member['member_id'], uma, variant, target)
                if not new_rows:
                    raise ValueError('Scores have changed. Run /uma status again to refresh.')
                new_standing = await UmaArchive.standing(None, member['member_id'], uma, variant)
                new_description = f"{label(current_member['trainer_name'])} · {label(current_member['club_name'])}\n"
                if new_standing:
                    new_description += (
                        f"{'Filtered' if uma else 'Overall'} global rank: "
                        f"**#{new_standing['position']} / {new_standing['participants']}** {'trainers' if uma else 'scores'} "
                        f"· Best score: **{new_standing['score']:,}**\n"
                    )
                new_pages = (new_rows[0]['total_entries'] + 9) // 10
                new_embed = discord.Embed(title='Your Uma Archive', description=new_description,
                                          color=discord.Color.blurple())
                add_score_fields(new_embed, new_rows, personal=True)
                new_embed.set_footer(text=f"Page {target}/{new_pages} · {new_rows[0]['total_entries']} scores · "
                                    "Ranks across all scanned clubs; ties share a rank")
                return new_embed, new_rows, new_pages

            view = ArchivePaginationView(self, interaction.user.id, page, pages, load_page, personal=True)
            await self._send_card(interaction, embed, rows, view, personal=True)
        except Exception as error:
            await self._error(interaction, error, private=True)

    async def _choices(self, interaction, current, *, variants=False):
        try:
            uma = getattr(interaction.namespace, "uma", None) if variants else None
            if variants and not uma:
                return []
            async def fetch_choices():
                club_id = await self._club(interaction, getattr(interaction.namespace, "club", None))
                return await UmaArchive.choices(
                    None, club_id, uma, current, variants=variants,
                )

            rows = await asyncio.wait_for(fetch_choices(), timeout=1.5)
            # Discord string choices have a 100-character name and value limit.
            return [app_commands.Choice(name=row['name'], value=row['name'])
                    for row in rows if 0 < len(row['name']) <= 100]
        except Exception:
            logger.debug("Archive autocomplete unavailable", exc_info=True)
            return []

    async def uma_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._choices(interaction, current)

    async def variant_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._choices(interaction, current, variants=True)

    leaderboard.autocomplete("club")(club_autocomplete)
    leaderboard.autocomplete("uma")(uma_autocomplete)
    leaderboard.autocomplete("variant")(variant_autocomplete)
    status.autocomplete("uma")(uma_autocomplete)
    status.autocomplete("variant")(variant_autocomplete)
