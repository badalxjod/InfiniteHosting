"""
main.py — HostingBot entry point.

Startup sequence:
1. Configure logging
2. Ping MongoDB (fail fast with a clear error)
3. Ensure DB indexes
4. Register all handlers (import triggers @bot.* decorators)
5. Wire health monitor callbacks
6. Re-attach running projects (survive bot restarts)
7. Start health monitor thread
8. Start polling loop
"""
import logging
import sys
import threading
from pathlib import Path

# ── Logging must be configured before any other import ───────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── Core imports ──────────────────────────────────────────────────────────────
from bot.instance import bot
from config import BOT_TOKEN
from core.analytics import usage_tracker
from core.health_monitor import health_monitor
from core.process_manager import restart_project, start_project
from db.connection import ping
from db.models import (
    _ensure_indexes,
    create_alert,
    get_all_users,
    get_project_by_id,
    increment_restart_count,
    update_project,
)

# ── Handler imports (side-effects: register all @bot.* decorators) ────────────
import handlers.user      # noqa: F401
import handlers.admin     # noqa: F401
import handlers.features  # noqa: F401


# ── Health monitor callbacks ──────────────────────────────────────────────────

def _on_project_crash_restart(project_id: str) -> bool:
    """
    Called by the health monitor when a project crashes and needs an auto-restart.

    Args:
        project_id: ID of the crashed project.

    Returns:
        True if restart succeeded, False otherwise.
    """
    project = get_project_by_id(project_id)
    if not project:
        logger.warning("Auto-restart requested for unknown project: %s", project_id)
        return False

    ok, msg, new_pid = restart_project(project)
    if ok and new_pid:
        increment_restart_count(project_id)
        update_project(project_id, status="running", pid=new_pid)
        health_monitor.update_pid(project_id, new_pid)
        usage_tracker.start_tracking(project_id, new_pid)
        logger.info("Auto-restart succeeded: %s (new pid=%s)", project_id, new_pid)
        # Notify user
        try:
            bot.send_message(
                project["user_id"],
                f"🔄 <b>Auto-Restart</b>\n\n"
                f"Project <b>{project['name']}</b> crashed and was automatically restarted.\n"
                f"New PID: {new_pid}",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.debug("Could not notify user %s about restart: %s", project["user_id"], exc)
        return True
    else:
        update_project(project_id, status="stopped", pid=None)
        logger.warning("Auto-restart failed: %s — %s", project_id, msg)
        try:
            bot.send_message(
                project["user_id"],
                f"💥 <b>Project Crashed</b>\n\n"
                f"Project <b>{project['name']}</b> crashed and could not be restarted automatically.\n"
                f"Error: {msg}",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.debug("Could not notify user %s about crash: %s", project["user_id"], exc)
        return False


def _on_alert(user_id: int, project_id: str, alert_type: str, message: str) -> None:
    """
    Called by the health monitor to create and deliver an alert.

    Args:
        user_id: Recipient Telegram user ID.
        project_id: Project that triggered the alert.
        alert_type: e.g. "CRASH", "HIGH_CPU", "HIGH_MEMORY".
        message: Human-readable alert message.
    """
    create_alert(user_id, project_id, alert_type, message)
    try:
        bot.send_message(
            user_id,
            f"🔔 <b>Alert</b>\n\n{message}\n\n"
            f"Project: <code>{project_id}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.debug("Could not send alert to %s: %s", user_id, exc)


# ── Re-attach running projects after restart ──────────────────────────────────

def _reattach_running_projects() -> None:
    """
    Re-register any projects that were marked 'running' in the DB.

    This handles the case where the bot restarts while projects are still running.
    Projects whose processes are no longer alive are marked as stopped.
    """
    import psutil

    logger.info("Scanning for running projects to re-attach…")
    all_users = get_all_users(limit=1000)
    reattached = 0
    stopped = 0

    for user in all_users:
        from db.models import get_user_projects
        projects = get_user_projects(user["user_id"])
        for proj in projects:
            if proj.get("status") != "running" or not proj.get("pid"):
                continue
            pid = proj["pid"]
            try:
                proc = psutil.Process(pid)
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    health_monitor.add_project(
                        proj["project_id"],
                        pid,
                        proj["user_id"],
                        proj["name"],
                        auto_restart=proj.get("auto_restart", True),
                        max_restarts=proj.get("max_restarts", 5),
                    )
                    usage_tracker.start_tracking(proj["project_id"], pid)
                    reattached += 1
                    logger.info("Re-attached: %s (pid=%s)", proj["project_id"], pid)
                else:
                    update_project(proj["project_id"], status="stopped", pid=None)
                    stopped += 1
            except psutil.NoSuchProcess:
                update_project(proj["project_id"], status="stopped", pid=None)
                stopped += 1
            except Exception as exc:
                logger.warning("Re-attach error for %s: %s", proj["project_id"], exc)

    logger.info("Re-attach complete: %d running, %d stopped.", reattached, stopped)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    """Application entry point."""
    logger.info("=" * 60)
    logger.info("HostingBot starting up…")
    logger.info("=" * 60)

    # 1. MongoDB connectivity check
    logger.info("Connecting to MongoDB…")
    try:
        ping()
        logger.info("MongoDB: connected ✓")
    except Exception as exc:
        logger.critical("MongoDB connection failed: %s", exc)
        sys.exit(f"FATAL: Could not connect to MongoDB — {exc}")

    # 2. Ensure indexes
    logger.info("Ensuring database indexes…")
    _ensure_indexes()

    # 3. Wire health monitor
    health_monitor.set_restart_callback(_on_project_crash_restart)
    health_monitor.set_alert_callback(_on_alert)

    # 4. Re-attach any processes already running
    threading.Thread(target=_reattach_running_projects, daemon=True).start()

    # 5. Start health monitor
    health_monitor.start()

    # 6. Fetch bot identity for log
    try:
        me = bot.get_me()
        logger.info("Bot identity: @%s (id=%s)", me.username, me.id)
    except Exception as exc:
        logger.warning("Could not fetch bot identity: %s", exc)

    # 7. Start polling
    logger.info("Starting long-poll loop (none_stop=True)…")
    try:
        bot.infinity_polling(
            timeout=30,
            long_polling_timeout=20,
            logger_level=logging.DEBUG,
            skip_pending=True,
            restart_on_change=False,
        )
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt — shutting down.")
    except Exception as exc:
        logger.critical("Polling crashed: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        health_monitor.stop()
        logger.info("HostingBot shut down cleanly.")


if __name__ == "__main__":
    main()
