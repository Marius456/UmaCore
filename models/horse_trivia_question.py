"""Curated horse photos, including tombstones for deleted starter questions."""
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from config.database import db

ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "horse_trivia"
MANIFEST_PATH = ASSET_DIR / "manifest.json"
GLOBAL_AVAILABILITY_PATH = ASSET_DIR / "global_availability.json"
ROSTER_PATH = ASSET_DIR / "gametora_support_roster.json"
HorseTriviaMode = Literal["global", "japanese"]
HORSE_TRIVIA_MODES = frozenset(("global", "japanese"))
# Only replace known retired starter URLs; preserve administrator overrides.
RETIRED_IMAGE_URLS = {
    "horse-photo-v1-005": (
        "https://upload.wikimedia.org/wikipedia/commons/f/f8/Gold_Ship_Arima_kinen_2015%28IMG1%29.jpg",
    ),
    "horse-photo-v4-1069-c163d0601c96": (
        "https://static.wikia.nocookie.net/umamusume/images/5/51/IRL_Sakura_Chiyono_O.jpg/revision/latest?cb=20240726235107",
        "https://vignette.wikia.nocookie.net/umamusume/images/5/51/IRL_Sakura_Chiyono_O.jpg/revision/latest?cb=20240726235107",
    ),
    "horse-photo-v4-1070-f4e5db4a7b7b": (
        "https://static.wikia.nocookie.net/umamusume/images/0/00/IRL_Sirius_Symboli.jpg/revision/latest?cb=20240727000208",
        "https://vignette.wikia.nocookie.net/umamusume/images/0/00/IRL_Sirius_Symboli.jpg/revision/latest?cb=20240727000208",
    ),
}


def validate_horse_mode(mode: str) -> HorseTriviaMode:
    if mode not in HORSE_TRIVIA_MODES:
        raise ValueError("Horse trivia mode must be 'global' or 'japanese'.")
    return mode


@lru_cache(maxsize=1)
def global_horse_names() -> frozenset[str]:
    snapshot = json.loads(GLOBAL_AVAILABILITY_PATH.read_text(encoding="utf-8"))
    names = snapshot.get("global_horses")
    if not isinstance(names, list) or not names or len(names) != len(set(names)):
        raise ValueError("Invalid Global horse availability snapshot")
    roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    roster_names = {horse["name"] for horse in roster["horses"]}
    if any(not isinstance(name, str) or name not in roster_names for name in names):
        raise ValueError("Global horse availability contains an unknown horse")
    return frozenset(names)


def validate_https_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https" and parsed.hostname and not parsed.username
            and not parsed.password and not any(c.isspace() for c in value)
            and not any(c in value for c in "<>\\") and len(value) <= 1500
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Use a valid, publicly accessible HTTPS URL without credentials.")
    return value


def validate_question(options: list[str], author: str, license: str) -> list[str]:
    options = [name.strip() for name in options]
    if len(options) != 4 or any(not name or len(name) > 80 for name in options):
        raise ValueError("Provide four horse names, each between 1 and 80 characters.")
    if len({name.casefold() for name in options}) != 4:
        raise ValueError("All four horse names must be distinct.")
    if not author.strip() or len(author) > 200 or not license.strip() or len(license) > 120:
        raise ValueError("Provide a photo author (up to 200 characters) and license (up to 120).")
    return options


@dataclass
class HorseTriviaQuestion:
    id: int
    options: list[str]
    correct_answer: str
    image_reference: str
    source_url: str
    author: str
    license: str
    license_url: str = ""
    modifications: str = "None"
    global_available: bool = False

    @property
    def question_text(self) -> str:
        return "Which horse is this?"

    @classmethod
    def _from_row(cls, row):
        data = dict(row)
        data.pop("seed_key", None)
        data.pop("deleted_at", None)
        if isinstance(data["options"], str):
            data["options"] = json.loads(data["options"])
        return cls(**data)

    @classmethod
    async def get_random(
        cls, excluded_ids=None, *, mode: HorseTriviaMode = "japanese",
    ):
        mode = validate_horse_mode(mode)
        row = await db.fetchrow(
            """SELECT * FROM horse_trivia_questions
               WHERE deleted_at IS NULL
                 AND ($2::boolean = FALSE OR global_available)
                 AND NOT (id = ANY($1::int[]))
               ORDER BY RANDOM() LIMIT 1""",
            sorted(excluded_ids or []),
            mode == "global",
        )
        return cls._from_row(row) if row else None

    @classmethod
    async def get_global_answer_names(cls) -> set[str]:
        rows = await db.fetch(
            """SELECT DISTINCT correct_answer FROM horse_trivia_questions
               WHERE deleted_at IS NULL AND global_available"""
        )
        return {row["correct_answer"] for row in rows}

    @classmethod
    async def get_all(cls):
        rows = await db.fetch(
            "SELECT * FROM horse_trivia_questions WHERE deleted_at IS NULL ORDER BY id"
        )
        return [cls._from_row(row) for row in rows]

    @classmethod
    async def create(
        cls, *, options, image_url, source_url, author, license,
        global_available: bool,
    ):
        options = validate_question(options, author, license)
        if not isinstance(global_available, bool):
            raise ValueError("Choose whether the photo is available in Global mode.")
        image_url = validate_https_url(image_url)
        source_url = validate_https_url(source_url)
        if len(source_url) > 300:
            raise ValueError("Use a source-page URL of at most 300 characters.")
        row = await db.fetchrow(
            """INSERT INTO horse_trivia_questions
               (options, correct_answer, image_reference, source_url, author, license,
                global_available)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
            options, options[0], image_url, source_url, author.strip(), license.strip(),
            global_available,
        )
        return cls._from_row(row)

    @classmethod
    async def delete(cls, question_id: int) -> bool:
        # Keep seed keys so a restart cannot restore a deliberately removed photo.
        row = await db.fetchrow(
            """UPDATE horse_trivia_questions SET deleted_at = NOW()
               WHERE id = $1 AND deleted_at IS NULL RETURNING id""", question_id,
        )
        return row is not None

    @classmethod
    async def seed(cls):
        entries = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        global_names = global_horse_names()
        keys = set()
        for entry in entries:
            validate_question(entry["options"], entry["author"], entry["license"])
            validate_https_url(entry["source_url"])
            if entry["correct_answer"] not in entry["options"] or entry["seed_key"] in keys:
                raise ValueError("Invalid horse starter manifest")
            keys.add(entry["seed_key"])
            validate_https_url(entry["image_reference"])
        async with db.transaction() as conn:
            for entry in entries:
                await conn.execute(
                    """INSERT INTO horse_trivia_questions
                       (seed_key, options, correct_answer, image_reference, source_url,
                        author, license, license_url, modifications, global_available)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                       ON CONFLICT (seed_key) DO NOTHING""",
                    entry["seed_key"], entry["options"], entry["correct_answer"],
                    entry["image_reference"], entry["source_url"], entry["author"],
                    entry["license"], entry["license_url"], entry["modifications"],
                    entry["correct_answer"] in global_names,
                )
                # Availability is maintained starter metadata. Updating it does
                # not recreate soft-deleted rows or affect administrator rows.
                await conn.execute(
                    """UPDATE horse_trivia_questions SET global_available = $2
                       WHERE seed_key = $1""",
                    entry["seed_key"], entry["correct_answer"] in global_names,
                )
                # Migrate known starter images only. Keep IDs, answers, custom
                # URLs, scores, and deletion tombstones intact.
                previous_references = (
                    entry["seed_key"].rsplit("-", 1)[-1] + ".jpg",
                    *RETIRED_IMAGE_URLS.get(entry["seed_key"], ()),
                )
                for previous_reference in previous_references:
                    await conn.execute(
                        """UPDATE horse_trivia_questions
                           SET image_reference = $2, source_url = $3, author = $4,
                               license = $5, license_url = $6, modifications = $7
                           WHERE seed_key = $1 AND image_reference = $8""",
                        entry["seed_key"], entry["image_reference"], entry["source_url"],
                        entry["author"], entry["license"], entry["license_url"],
                        entry["modifications"], previous_reference,
                    )
