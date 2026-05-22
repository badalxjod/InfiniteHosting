"""
utils/helpers.py — Miscellaneous helper functions.

Includes HTML escaping, uptime formatting, and auto-delete scheduling.
"""
import html
import logging
import threading
import time

logger = logging.getLogger(__name__)


def safe_html(text: str) -> str:
    """
    Escape a string for safe insertion into an HTML Telegram message.

    Args:
        text: Raw user-provided or dynamic string.

    Returns:
        HTML-escaped string safe for use in bot messages.
    """
    return html.escape(str(text), quote=False)


def format_uptime(seconds: int) -> str:
    """
    Convert a duration in seconds to a human-readable uptime string.

    Args:
        seconds: Total uptime in seconds.

    Returns:
        String like "2d 3h", "45m 10s", or "30s".
    """
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m {seconds % 60}s"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h {minutes % 60}m"
    days = hours // 24
    return f"{days}d {hours % 24}h"


def schedule_delete(bot, chat_id: int, message_id: int, delay_seconds: int = 30) -> None:
    """
    Schedule deletion of a Telegram message after a delay.

    Runs in a daemon thread so it never blocks the event loop.

    Args:
        bot: TeleBot instance.
        chat_id: Chat containing the message.
        message_id: Message to delete.
        delay_seconds: Seconds to wait before deletion.
    """
    def _delete():
        time.sleep(delay_seconds)
        try:
            bot.delete_message(chat_id, message_id)
        except Exception as exc:
            logger.debug("Auto-delete failed (message may already be gone): %s", exc)

    threading.Thread(target=_delete, daemon=True).start()


def format_analytics_report(stats: dict, storage: dict) -> str:
    """
    Build a formatted HTML analytics report string.

    Args:
        stats: Dict with uptime_seconds, restart_count, uptime_percentage,
               cpu_hours, current_cpu, current_memory_mb, avg_memory_mb.
        storage: Dict with size_str, file_count, total_bytes.

    Returns:
        HTML-formatted report string.
    """
    return (
        "📊 <b>Analytics Report</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏱ <b>Uptime:</b> {format_uptime(stats.get('uptime_seconds', 0))}\n"
        f"🔄 <b>Restarts:</b> {stats.get('restart_count', 0)}\n"
        f"📈 <b>Uptime %:</b> {stats.get('uptime_percentage', 100):.1f}%\n\n"
        "💻 <b>CPU Usage:</b>\n"
        f"├─ Total: {stats.get('cpu_hours', 0):.2f} hours\n"
        f"└─ Current: {stats.get('current_cpu', 0):.1f}%\n\n"
        "🧠 <b>Memory Usage:</b>\n"
        f"├─ Current: {stats.get('current_memory_mb', 0):.1f} MB\n"
        f"└─ Average: {stats.get('avg_memory_mb', 0):.1f} MB\n\n"
        "💾 <b>Storage:</b>\n"
        f"├─ Used: {storage.get('size_str', '0 MB')}\n"
        f"├─ Files: {storage.get('file_count', 0)}\n"
        f"└─ Bytes: {storage.get('total_bytes', 0):,}"
    )
