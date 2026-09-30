"""
Prediction Store — persists daily overtake predictions to JSON files
so that the next day's report can verify them.
"""
import json
import logging
import os
from datetime import date
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

PREDICTIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "predictions")


def _ensure_dir():
    os.makedirs(PREDICTIONS_DIR, exist_ok=True)


def _prediction_path(club_name: str, d: date) -> str:
    return os.path.join(PREDICTIONS_DIR, f"{club_name}_{d.isoformat()}.json")


def save_predictions(club_name: str, report_date: date, predictions: List[Dict[str, Any]]) -> None:
    """
    Save the day's overtake predictions to a JSON file.

    Args:
        club_name: Club name (used for filename)
        report_date: The date of the report (when predictions were made)
        predictions: List of dicts with keys: challenger, target, target_rank,
                    gap_fans, daily_diff, eta_days
    """
    _ensure_dir()
    path = _prediction_path(club_name, report_date)
    data = {
        "date": report_date.isoformat(),
        "club_name": club_name,
        "predictions": predictions,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"Saved {len(predictions)} predictions for {club_name} on {report_date}")
    except Exception as e:
        logger.error(f"Failed to save predictions for {club_name} on {report_date}: {e}")


def load_predictions(club_name: str, d: date) -> Optional[List[Dict[str, Any]]]:
    """
    Load overtake predictions for a given club + date.

    Returns:
        List of prediction dicts, or None if no saved predictions exist.
    """
    path = _prediction_path(club_name, d)
    if not os.path.exists(path):
        logger.debug(f"No saved predictions found for {club_name} on {d}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        predictions = data.get("predictions", [])
        logger.debug(f"Loaded {len(predictions)} predictions for {club_name} on {d}")
        return predictions
    except Exception as e:
        logger.error(f"Failed to load predictions for {club_name} on {d}: {e}")
        return None