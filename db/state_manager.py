"""
db/state_manager.py — Persistent user-state storage backed by MongoDB.

States are stored with a TTL index on `expires_at` so they expire
automatically. This survives bot restarts and crashes, unlike a plain dict.
"""
import logging
from datetime import datetime, timezone, timedelta

from db.connection import get_db

logger = logging.getLogger(__name__)

_DEFAULT_TTL_MINUTES = 30


def _col():
    return get_db()["user_states"]


def get_state(user_id: int) -> dict:
    """
    Fetch the current state for a user.

    Args:
        user_id: Telegram user ID.

    Returns:
        State dict, or empty dict if no state is set.
    """
    try:
        doc = _col().find_one({"user_id": user_id})
        if doc:
            return doc.get("state", {})
    except Exception as exc:
        logger.warning("get_state failed for %s: %s", user_id, exc)
    return {}


def set_state(user_id: int, state: dict, ttl_minutes: int = _DEFAULT_TTL_MINUTES) -> None:
    """
    Persist state for a user, overwriting any existing entry.

    Args:
        user_id: Telegram user ID.
        state: Arbitrary dict to store.
        ttl_minutes: Minutes until the record auto-expires.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    try:
        _col().update_one(
            {"user_id": user_id},
            {"$set": {"state": state, "expires_at": expires_at}},
            upsert=True,
        )
    except Exception as exc:
        logger.warning("set_state failed for %s: %s", user_id, exc)


def clear_state(user_id: int) -> None:
    """
    Remove state for a user.

    Args:
        user_id: Telegram user ID.
    """
    try:
        _col().delete_one({"user_id": user_id})
    except Exception as exc:
        logger.warning("clear_state failed for %s: %s", user_id, exc)
