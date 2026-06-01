"""
Art balance service for managing fan-based currency for the /art command.

Balances are stored in a JSON file, isolated from the database report system.
They are updated by the daily scrape and reset monthly.
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

from config.settings import ART_BALANCES_PATH

logger = logging.getLogger(__name__)


class InsufficientBalanceError(Exception):
    """Raised when a user doesn't have enough fans to use /art."""


def _load() -> dict:
    """Load the balances JSON file. Creates default structure if missing."""
    if not os.path.exists(ART_BALANCES_PATH):
        return _default_data()

    try:
        with open(ART_BALANCES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure required keys exist
        if "month" not in data or "balances" not in data:
            return _default_data()
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load art balances from {ART_BALANCES_PATH}: {e}")
        return _default_data()


def _save(data: dict) -> None:
    """Atomically write the balances JSON file."""
    os.makedirs(os.path.dirname(ART_BALANCES_PATH) or ".", exist_ok=True)
    tmp_path = ART_BALANCES_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        # Atomic replace on most OSes
        os.replace(tmp_path, ART_BALANCES_PATH)
    except OSError as e:
        logger.error(f"Failed to save art balances to {ART_BALANCES_PATH}: {e}")
        # Clean up temp file if it exists
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _default_data() -> dict:
    """Return the default empty data structure with the current month."""
    return {
        "month": datetime.now().strftime("%Y-%m"),
        "balances": {},
    }


def reset_if_new_month() -> bool:
    """
    Check if the JSON's month matches the current month.
    If not, clear all balances and update the month field.
    Returns True if a reset was performed.
    """
    data = _load()
    current_month = datetime.now().strftime("%Y-%m")

    if data["month"] != current_month:
        logger.info(
            f"Art balance monthly reset: {data['month']} → {current_month} "
            f"(clearing {len(data['balances'])} entries)"
        )
        data = _default_data()
        _save(data)
        return True
    return False


def get_balance(discord_user_id: int) -> int:
    """
    Get a user's current spendable fan balance.
    Returns 0 if the user is not found.
    """
    data = _load()

    entry = data["balances"].get(str(discord_user_id))
    if not entry:
        return 0

    return entry.get("balance", 0)


def get_balance_info(discord_user_id: int) -> Optional[dict]:
    """
    Get full balance info for a user.
    Returns dict with balance, trainer_name, last_updated or None if not found.
    """
    data = _load()

    return data["balances"].get(str(discord_user_id))


def deduct_balance(discord_user_id: int, amount: int) -> int:
    """
    Deduct `amount` from a user's balance.
    Returns the new balance after deduction.
    Raises InsufficientBalanceError if balance < amount.
    """
    data = _load()

    entry = data["balances"].get(str(discord_user_id))
    if not entry:
        raise InsufficientBalanceError(
            "You don't have a fan balance yet. "
            "Your balance will be set after the next daily scrape."
        )

    current = entry.get("balance", 0)
    if current < amount:
        raise InsufficientBalanceError(
            f"Insufficient fans. You have **{current:,}** fans but need **{amount:,}**."
        )

    entry["balance"] = current - amount
    _save(data)

    logger.info(
        f"Art balance deducted: user {discord_user_id} "
        f"-{amount:,} (remaining: {entry['balance']:,})"
    )
    return entry["balance"]


def set_balance(
    discord_user_id: int,
    member_id: str,
    trainer_name: str,
    fans: int,
) -> None:
    """
    Set or update a user's balance. Called by the daily scrape.
    This replaces the user's balance with the provided fan count.
    """
    data = _load()
    current_month = datetime.now().strftime("%Y-%m")

    # If month changed, reset first
    if data["month"] != current_month:
        data = _default_data()

    data["balances"][str(discord_user_id)] = {
        "balance": fans,
        "member_id": member_id,
        "trainer_name": trainer_name,
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
    }

    _save(data)