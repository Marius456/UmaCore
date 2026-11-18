"""Read the latest complete archive snapshots written by the Hall of Fame scraper."""

from config.database import db


class UmaArchive:
    # None means all scanned clubs; an explicit guild still supports scoped reads.
    # Filter before ranking so ranks and totals describe the selected scope.
    SCOPE = """
        FROM uma_archive_scores s
        JOIN members m ON m.member_id = s.member_id
        JOIN clubs c ON c.club_id = m.club_id
        WHERE ($1::bigint IS NULL OR c.guild_id = $1) AND c.is_active AND m.is_active
          AND ($2::uuid IS NULL OR c.club_id = $2)
          AND ($3::text IS NULL OR s.uma_name = $3)
          AND ($4::text IS NULL OR s.variant = $4)
    """
    RANKED = """
        WITH candidates AS (
            SELECT s.*, m.trainer_name, c.club_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.member_id ORDER BY s.score DESC, s.uma_name, s.variant
                   ) AS member_choice
    """ + SCOPE + """
        ), best AS (
            -- Overall includes every archive entry. Per-Uma chooses each trainer's best outfit.
            SELECT * FROM candidates WHERE $3::text IS NULL OR member_choice = 1
        ), ranked AS (
            SELECT *, RANK() OVER (ORDER BY score DESC) AS position,
                   COUNT(*) OVER () AS participants
            FROM best
        )
    """

    @classmethod
    async def leaderboard(cls, guild_id, club_id=None, uma=None, variant=None, page=1):
        return await db.fetch(
            cls.RANKED + """
            SELECT * FROM ranked
            ORDER BY score DESC, trainer_name, member_id, uma_name, variant
            LIMIT 10 OFFSET $5
            """,
            guild_id, club_id, uma, variant, (page - 1) * 10,
        )

    @classmethod
    async def standing(cls, guild_id, member_id, uma=None, variant=None):
        # The member filter must come after the window functions, never before.
        return await db.fetchrow(
            cls.RANKED + """
            SELECT * FROM ranked WHERE member_id = $5
            ORDER BY score DESC, uma_name, variant LIMIT 1
            """,
            guild_id, None, uma, variant, member_id,
        )

    @classmethod
    async def linked_member(cls, guild_id, user_id):
        return await db.fetchrow("""
            SELECT m.member_id, m.trainer_name, c.club_name,
                   a.observed_at, a.entry_count
            FROM user_links u
            JOIN members m ON m.member_id = u.member_id
            JOIN clubs c ON c.club_id = m.club_id
            LEFT JOIN uma_archive_scans a ON a.member_id = m.member_id
            WHERE u.discord_user_id = $2 AND ($1::bigint IS NULL OR c.guild_id = $1)
              AND c.is_active AND m.is_active
        """, guild_id, user_id)

    @classmethod
    async def personal_scores(cls, guild_id, member_id, uma=None, variant=None, page=1):
        return await db.fetch("""
            WITH ranked AS (
                SELECT s.*,
                       RANK() OVER (
                           PARTITION BY s.uma_name, s.variant ORDER BY s.score DESC
                       ) AS position,
                       COUNT(*) OVER (
                           PARTITION BY s.uma_name, s.variant
                       ) AS participants
        """ + cls.SCOPE + """
            ), personal AS (
                SELECT *, COUNT(*) OVER () AS total_entries FROM ranked WHERE member_id = $5
            )
            SELECT * FROM personal
            ORDER BY score DESC, uma_name, variant
            LIMIT 10 OFFSET $6
        """, guild_id, None, uma, variant, member_id, (page - 1) * 10)

    @classmethod
    async def choices(cls, guild_id, club_id=None, uma=None, current="", *, variants=False):
        # Only fixed identifiers are interpolated; all Discord input is bound.
        column = "s.variant" if variants else "s.uma_name"
        return await db.fetch(
            f"SELECT DISTINCT {column} AS name " + cls.SCOPE + f"""
              AND STRPOS(LOWER({column}), LOWER($5)) > 0
            ORDER BY name LIMIT 25
            """,
            guild_id, club_id, uma, None, current,
        )
