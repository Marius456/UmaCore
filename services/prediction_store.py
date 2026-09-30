"""Persist daily leaderboard predictions with stable, path-safe keys."""

import json
import logging
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

PREDICTIONS_DIR = str(Path(__file__).resolve().parent.parent / "data" / "predictions")
FORMAT_VERSION = 2


def _prediction_path(club_id: UUID, report_date: date) -> Path:
    """Return a path built only from validated, fixed-format values."""
    return Path(PREDICTIONS_DIR) / f"v2_{UUID(str(club_id))}_{report_date.isoformat()}.json"


def save_prediction_snapshot(
    club_id: UUID,
    club_name: str,
    report_date: date,
    predictions: list[dict[str, Any]],
) -> None:
    """Atomically save a prediction snapshot for a club and date."""
    directory = Path(PREDICTIONS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = _prediction_path(club_id, report_date)
    payload = {
        "version": FORMAT_VERSION,
        "date": report_date.isoformat(),
        "club_id": str(club_id),
        "club_name": club_name,
        "predictions": predictions,
    }

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".prediction-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        logger.debug(
            "Saved %d predictions for %s on %s",
            len(predictions),
            club_name,
            report_date,
        )
    except (OSError, TypeError, ValueError):
        logger.exception("Failed to save predictions for %s on %s", club_name, report_date)
    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary prediction file %s", temp_path)


def load_prediction_snapshot(
    club_id: UUID, report_date: date
) -> list[dict[str, Any]] | None:
    """Load and validate a version-2 prediction snapshot."""
    path = _prediction_path(club_id, report_date)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load predictions for club %s on %s", club_id, report_date)
        return None

    if (
        not isinstance(payload, dict)
        or payload.get("version") != FORMAT_VERSION
        or payload.get("club_id") != str(club_id)
        or payload.get("date") != report_date.isoformat()
        or not isinstance(payload.get("predictions"), list)
    ):
        logger.warning("Ignoring invalid prediction snapshot: %s", path)
        return None
    return payload["predictions"]
