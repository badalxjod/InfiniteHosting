"""
db/models.py — All database operations for HostingBot.

Collections: users, projects, activities, analytics, alerts.
Indexes are created once on first import via _ensure_indexes().
"""
import logging
from datetime import datetime, timezone, timedelta
from pymongo import ASCENDING

from config import OWNER_IDS, BASE_DIR
from db.connection import get_db

logger = logging.getLogger(__name__)

# ── Collection accessors (never cached at module level to avoid import crashes) ──

def _col(name: str):
    return get_db()[name]


def _ensure_indexes():
    """Create all required indexes. Called once at startup from main()."""
    db = get_db()
    db["users"].create_index([("user_id", ASCENDING)], unique=True)
    db["projects"].create_index([("project_id", ASCENDING)], unique=True)
    db["projects"].create_index([("user_id", ASCENDING)])
    db["activities"].create_index([("user_id", ASCENDING)])
    db["activities"].create_index(
        [("created_at", ASCENDING)],
        expireAfterSeconds=2592000,  # 30-day TTL — matches existing "activities_ttl" index
        name="activities_ttl",
    )
    db["analytics"].create_index([("project_id", ASCENDING)])
    db["analytics"].create_index([("timestamp", ASCENDING)])
    db["alerts"].create_index([("user_id", ASCENDING)])
    db["alerts"].create_index([("created_at", ASCENDING)])
    db["user_states"].create_index("expires_at", expireAfterSeconds=0)
    logger.info("MongoDB indexes ensured.")


# ── User operations ──────────────────────────────────────────────────────────────

def get_or_create_user(user_id: int, username: str | None = None, first_name: str | None = None) -> dict:
    """
    Fetch an existing user or create a new one.

    Args:
        user_id: Telegram user ID.
        username: Telegram @username (without @).
        first_name: Telegram first name.

    Returns:
        User document dict.
    """
    col = _col("users")
    now = datetime.now(timezone.utc).isoformat()
    user = col.find_one({"user_id": user_id})

    if not user:
        is_admin = user_id in OWNER_IDS
        user = {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "is_banned": False,
            "is_admin": is_admin,
            "free_slots": 1,
            "premium_slots": 0,
            "created_at": now,
            "last_active_at": now,
            "last_action": "JOINED",
        }
        col.insert_one(user)
        logger.info("New user created: %s", user_id)
    else:
        updates = {"last_active_at": now, "username": username, "first_name": first_name}
        if user_id in OWNER_IDS:
            updates["is_admin"] = True
        col.update_one({"user_id": user_id}, {"$set": updates})
        user.update(updates)

    return user


def get_user_by_id(user_id: int) -> dict | None:
    """Fetch user by Telegram ID. Returns None if not found."""
    return _col("users").find_one({"user_id": user_id})


def get_all_users(limit: int = 500) -> list[dict]:
    """
    Fetch all users up to `limit`.

    Args:
        limit: Maximum number of users to return.

    Returns:
        List of user documents.
    """
    return list(_col("users").find({}, limit=limit))


def ban_user(user_id: int) -> None:
    """Mark user as banned."""
    _col("users").update_one({"user_id": user_id}, {"$set": {"is_banned": True}})


def unban_user(user_id: int) -> None:
    """Remove ban from user."""
    _col("users").update_one({"user_id": user_id}, {"$set": {"is_banned": False}})


def update_user_slots(user_id: int, free_delta: int = 0, premium_delta: int = 0) -> bool:
    """
    Adjust slot counts for a user.

    Args:
        user_id: Target user.
        free_delta: Amount to add/subtract from free_slots.
        premium_delta: Amount to add/subtract from premium_slots.

    Returns:
        True if user found and updated, False otherwise.
    """
    user = get_user_by_id(user_id)
    if not user:
        return False
    new_free = max(0, user.get("free_slots", 1) + free_delta)
    new_premium = max(0, user.get("premium_slots", 0) + premium_delta)
    _col("users").update_one(
        {"user_id": user_id},
        {"$set": {"free_slots": new_free, "premium_slots": new_premium}},
    )
    return True


def update_user_activity(user_id: int, action: str, details: str | None = None, project_id: str | None = None) -> None:
    """
    Log user activity and update last_action.

    Args:
        user_id: Actor user ID.
        action: Short action string (e.g. "START").
        details: Optional detail string.
        project_id: Related project ID if any.
    """
    now = datetime.now(timezone.utc).isoformat()
    _col("users").update_one(
        {"user_id": user_id},
        {"$set": {"last_action": action, "last_active_at": now}},
    )
    _col("activities").insert_one(
        {"user_id": user_id, "project_id": project_id, "action": action, "details": details, "created_at": now}
    )


def get_user_project_counts() -> dict[int, int]:
    """
    Return a mapping of user_id → active project count using a single aggregation.

    Returns:
        Dict mapping user_id to count.
    """
    pipeline = [
        {"$match": {"status": {"$ne": "deleted"}}},
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
    ]
    return {doc["_id"]: doc["count"] for doc in _col("projects").aggregate(pipeline)}


# ── Project operations ───────────────────────────────────────────────────────────

def create_project(user_id: int, name: str) -> dict:
    """
    Create a new project record and its directory.

    Args:
        user_id: Owner user ID.
        name: Human-readable project name.

    Returns:
        New project document.
    """
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    slug = "".join(c if c.isalnum() else "_" for c in name.lower())[:20]
    project_id = f"{user_id}_{timestamp}_{slug}"
    project_dir = BASE_DIR / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    doc = {
        "project_id": project_id,
        "user_id": user_id,
        "name": name,
        "status": "new",
        "project_dir": str(project_dir),
        "entry": None,
        "requirements": None,
        "run_cmd": None,
        "pid": None,
        "auto_restart": True,
        "max_restarts": 5,
        "restart_count": 0,
        "first_start_time": None,
        "total_uptime_seconds": 0,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "logs": {},
    }
    _col("projects").insert_one(doc)
    logger.info("Project created: %s", project_id)
    return doc


def update_project(project_id: str, **fields) -> None:
    """
    Update arbitrary fields on a project.

    Args:
        project_id: Project to update.
        **fields: Field names and new values.
    """
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    _col("projects").update_one({"project_id": project_id}, {"$set": fields})


def increment_restart_count(project_id: str) -> None:
    """
    Atomically increment restart_count using $inc (avoids stale-read race).

    Args:
        project_id: Project to update.
    """
    _col("projects").update_one(
        {"project_id": project_id},
        {"$inc": {"restart_count": 1}, "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
    )


def get_project_by_id(project_id: str) -> dict | None:
    """Fetch project by its ID. Returns None if not found."""
    return _col("projects").find_one({"project_id": project_id})


def get_user_projects(user_id: int) -> list[dict]:
    """
    Fetch all non-deleted projects for a user, newest first.

    Args:
        user_id: Owner user ID.

    Returns:
        List of project documents.
    """
    return list(
        _col("projects").find(
            {"user_id": user_id, "status": {"$ne": "deleted"}},
            sort=[("created_at", -1)],
        )
    )


def count_active_projects(user_id: int) -> int:
    """
    Count non-deleted projects for a user.

    Args:
        user_id: User to count for.

    Returns:
        Integer count.
    """
    return _col("projects").count_documents({"user_id": user_id, "status": {"$ne": "deleted"}})


def delete_project_from_db(project_id: str) -> bool:
    """
    Permanently delete a project and all related records.

    Args:
        project_id: Project to delete.

    Returns:
        True always (errors are logged).
    """
    _col("projects").delete_one({"project_id": project_id})
    _col("analytics").delete_many({"project_id": project_id})
    _col("alerts").delete_many({"project_id": project_id})
    _col("activities").delete_many({"project_id": project_id})
    logger.info("Project deleted from DB: %s", project_id)
    return True


# ── Analytics ────────────────────────────────────────────────────────────────────

def save_analytics_snapshot(project_id: str, stats: dict) -> None:
    """
    Persist a resource-usage snapshot.

    Args:
        project_id: Project this snapshot belongs to.
        stats: Dict with keys cpu_hours, current_memory_mb, uptime_seconds.
    """
    _col("analytics").insert_one(
        {
            "project_id": project_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cpu_hours": stats.get("cpu_hours", 0),
            "memory_mb": stats.get("current_memory_mb", 0),
            "uptime_seconds": stats.get("uptime_seconds", 0),
        }
    )


def get_project_analytics(project_id: str, days: int = 7) -> list[dict]:
    """
    Fetch analytics snapshots for the last N days.

    Args:
        project_id: Project to query.
        days: Look-back window in days.

    Returns:
        List of snapshot documents sorted oldest-first.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return list(
        _col("analytics").find(
            {"project_id": project_id, "timestamp": {"$gte": cutoff}},
            sort=[("timestamp", ASCENDING)],
        )
    )


# ── Alerts ───────────────────────────────────────────────────────────────────────

def create_alert(user_id: int, project_id: str, alert_type: str, message: str) -> None:
    """
    Create a new alert for a user.

    Args:
        user_id: Recipient.
        project_id: Related project.
        alert_type: e.g. "CRASH", "HIGH_CPU".
        message: Human-readable alert text.
    """
    _col("alerts").insert_one(
        {
            "user_id": user_id,
            "project_id": project_id,
            "alert_type": alert_type,
            "message": message,
            "read": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def get_unread_alerts(user_id: int, limit: int = 10) -> list[dict]:
    """Return up to `limit` unread alerts for a user, newest first."""
    return list(
        _col("alerts").find(
            {"user_id": user_id, "read": False},
            sort=[("created_at", -1)],
            limit=limit,
        )
    )


def mark_alerts_read(user_id: int) -> None:
    """Mark all alerts for a user as read."""
    _col("alerts").update_many({"user_id": user_id, "read": False}, {"$set": {"read": True}})


def get_alert_count(user_id: int) -> int:
    """Count unread alerts for a user."""
    return _col("alerts").count_documents({"user_id": user_id, "read": False})


# ── Activity log ─────────────────────────────────────────────────────────────────

def get_recent_activities(limit: int = 20) -> list[dict]:
    """
    Fetch the most recent activity log entries.

    Args:
        limit: Maximum entries to return.

    Returns:
        List of activity documents, newest first.
    """
    return list(_col("activities").find({}, sort=[("created_at", -1)], limit=limit))
