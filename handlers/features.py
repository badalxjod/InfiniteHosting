"""
handlers/features.py — Dashboard, analytics, and environment variable handlers.

Env var list is sent as a file download to avoid exposing secrets in chat.
Auto-delete is used as an additional safeguard if shown inline.
"""
import html
import io
import logging
import threading
import time
from pathlib import Path

from telebot.types import CallbackQuery, Message

from bot.instance import bot
from bot.middlewares import guard_callback, guard_message
from core.analytics import get_storage_usage, usage_tracker
from core.health_monitor import health_monitor
from core.process_manager import get_usage
from db.models import (
    get_alert_count,
    get_project_by_id,
    get_unread_alerts,
    mark_alerts_read,
)
from db.state_manager import clear_state, get_state, set_state
from utils.env_manager import (
    delete_env_var,
    get_env_display,
    load_env_file,
    save_uploaded_env_file,
    set_env_var,
)
from utils.helpers import format_analytics_report, safe_html, schedule_delete
from utils.keyboards import (
    analytics_keyboard,
    back_to_project_keyboard,
    dashboard_keyboard,
    env_vars_keyboard,
    project_panel_keyboard,
)

logger = logging.getLogger(__name__)


# ── Dashboard callback ────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("dashboard|"))
def callback_dashboard(call: CallbackQuery) -> None:
    """Show live resource-usage dashboard for a project."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "📊 Refreshing…")

    status_emoji = {"new": "🆕", "ready": "✅", "running": "🟢", "stopped": "⏸"}.get(
        project["status"], "❓"
    )

    text = (
        f"📊 <b>Dashboard — {safe_html(project['name'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Status: {status_emoji} {project['status'].upper()}\n"
        f"🆔 ID: <code>{safe_html(project['project_id'])}</code>\n"
    )

    if project["status"] == "running" and project.get("pid"):
        pid = project["pid"]

        # Try live stats from health monitor first
        live = health_monitor.get_project_stats(project_id)
        if live and live.get("status") == "running":
            text += (
                f"\n💻 CPU: {live['cpu_percent']:.1f}%\n"
                f"🧠 RAM: {live['memory_mb']:.1f} MB\n"
                f"⏱ Uptime: {_fmt_seconds(live['uptime_seconds'])}\n"
                f"🔄 Restarts: {live['restart_count']}\n"
                f"⚡ PID: {pid}\n"
            )
        else:
            # Fall back to direct psutil
            usage = get_usage(pid)
            if usage:
                text += f"\n<pre>{html.escape(usage)}</pre>\n"

        # Storage
        proj_dir = Path(project["project_dir"])
        storage = get_storage_usage(proj_dir)
        text += f"\n💾 Storage: {storage['size_str']} ({storage['file_count']} files)\n"
    else:
        text += "\n<i>Start the project to see live stats.</i>\n"

    # Alert badge
    alert_count = get_alert_count(call.from_user.id)
    if alert_count:
        text += f"\n🔔 <b>{alert_count} unread alert(s)</b>"

    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.id,
            reply_markup=dashboard_keyboard(project_id),
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=dashboard_keyboard(project_id))


# ── Analytics callback ────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("analytics|"))
def callback_analytics(call: CallbackQuery) -> None:
    """Show full analytics report for a project."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "📈 Loading…")

    proj_dir = Path(project["project_dir"])
    storage = get_storage_usage(proj_dir)

    # Pull live usage stats
    stats = usage_tracker.get_current_stats(project_id) or {}
    live = health_monitor.get_project_stats(project_id) or {}

    combined = {
        "uptime_seconds": stats.get("uptime_seconds", live.get("uptime_seconds", 0)),
        "restart_count": project.get("restart_count", 0),
        "uptime_percentage": 100.0,
        "cpu_hours": stats.get("cpu_hours", 0),
        "current_cpu": live.get("cpu_percent", 0),
        "current_memory_mb": stats.get("current_memory_mb", live.get("memory_mb", 0)),
        "avg_memory_mb": stats.get("avg_memory_mb", 0),
    }

    report = format_analytics_report(combined, storage)

    try:
        bot.edit_message_text(
            report,
            call.message.chat.id,
            call.message.id,
            reply_markup=analytics_keyboard(project_id),
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(call.message.chat.id, report, reply_markup=analytics_keyboard(project_id))


# ── Alerts callback ───────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("view_alerts|"))
def callback_view_alerts(call: CallbackQuery) -> None:
    """Show unread alerts for a project."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)

    alerts = get_unread_alerts(call.from_user.id, limit=10)
    mark_alerts_read(call.from_user.id)

    if not alerts:
        text = "🔔 <b>Alerts</b>\n\nNo unread alerts. Your projects are healthy! ✅"
    else:
        lines = []
        for a in alerts:
            lines.append(
                f"{'💥' if a['alert_type'] == 'CRASH' else '⚠️'} <b>{a['alert_type']}</b>\n"
                f"{safe_html(a['message'])}\n"
                f"🕐 <i>{a['created_at'][:19]}</i>"
            )
        text = "🔔 <b>Alerts</b>\n━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(lines)

    bot.send_message(
        call.message.chat.id,
        text,
        reply_markup=back_to_project_keyboard(project_id),
    )


# ── Env vars panel ────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("envvars|"))
def callback_env_vars(call: CallbackQuery) -> None:
    """Show the environment variables management panel."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)

    proj_dir = Path(project["project_dir"])
    env_vars = load_env_file(proj_dir)
    count = len(env_vars)

    try:
        bot.edit_message_text(
            f"🔐 <b>Environment Variables</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 Variables set: {count}\n\n"
            "Use the buttons below to manage your .env:",
            call.message.chat.id,
            call.message.id,
            reply_markup=env_vars_keyboard(project_id),
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            f"🔐 <b>Environment Variables</b>\n\nVariables set: {count}",
            reply_markup=env_vars_keyboard(project_id),
        )


# ── env_list: send as file (secrets must not appear in chat) ──────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("env_list|"))
def callback_env_list(call: CallbackQuery) -> None:
    """
    Send environment variables as a downloadable .env.txt file.

    Masked values are also shown inline with a 30-second auto-delete warning.
    """
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)

    proj_dir = Path(project["project_dir"])
    env_vars = load_env_file(proj_dir)

    if not env_vars:
        bot.send_message(
            call.message.chat.id,
            "📝 No environment variables set.",
            reply_markup=env_vars_keyboard(project_id),
        )
        return

    # Send masked preview inline with auto-delete warning
    masked_text = get_env_display(proj_dir)
    sent = bot.send_message(
        call.message.chat.id,
        f"{masked_text}\n\n⚠️ <b>This message will auto-delete in 30 seconds.</b>",
        reply_markup=env_vars_keyboard(project_id),
    )
    schedule_delete(bot, call.message.chat.id, sent.message_id, delay_seconds=30)

    # Also send the full .env file as a download
    env_content = "\n".join(f"{k}={v}" for k, v in sorted(env_vars.items()))
    buf = io.BytesIO(env_content.encode("utf-8"))
    buf.name = "project.env.txt"
    bot.send_document(
        call.message.chat.id,
        buf,
        visible_file_name="project.env.txt",
        caption="📥 Full .env file — keep this secure!",
    )


# ── env_add ───────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("env_add|"))
def callback_env_add(call: CallbackQuery) -> None:
    """Prompt user to enter a new KEY=VALUE pair."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, {"mode": "await_env_add", "project_id": project_id})
    bot.send_message(
        call.message.chat.id,
        "➕ <b>Add Environment Variable</b>\n\n"
        "Send in format: <code>KEY=value</code>\n\n"
        "Example: <code>API_TOKEN=abc123xyz</code>\n\n"
        "💡 /cancel to abort.",
    )


# ── env_delete ────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("env_delete|"))
def callback_env_delete_prompt(call: CallbackQuery) -> None:
    """Prompt user to enter the variable key to delete."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, {"mode": "await_env_delete", "project_id": project_id})
    bot.send_message(
        call.message.chat.id,
        "🗑 <b>Delete Environment Variable</b>\n\n"
        "Send the variable name (key) to delete.\n\n"
        "Example: <code>API_TOKEN</code>\n\n"
        "💡 /cancel to abort.",
    )


# ── env_upload ────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("env_upload|"))
def callback_env_upload(call: CallbackQuery) -> None:
    """Prompt user to upload a .env file."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, {"mode": "await_env_file", "project_id": project_id})
    bot.send_message(
        call.message.chat.id,
        "📤 <b>Upload .env File</b>\n\n"
        "Send your <code>.env</code> file as a document.\n\n"
        "💡 /cancel to abort.",
    )


# ── Text handler for env states ───────────────────────────────────────────────

@bot.message_handler(
    content_types=["text"],
    func=lambda m: get_state(m.from_user.id).get("mode") in ("await_env_add", "await_env_delete"),
)
def handle_env_text(message: Message) -> None:
    """Handle text input for adding or deleting environment variables."""
    if guard_message(message):
        return
    state = get_state(message.from_user.id)
    mode = state.get("mode")
    project_id = state.get("project_id")

    if not project_id:
        clear_state(message.from_user.id)
        return

    project = get_project_by_id(project_id)
    if not project or project["user_id"] != message.from_user.id:
        clear_state(message.from_user.id)
        bot.reply_to(message, "❌ Project not found.")
        return

    proj_dir = Path(project["project_dir"])

    if mode == "await_env_add":
        text = message.text.strip()
        if "=" not in text:
            bot.reply_to(message, "❌ Format must be <code>KEY=value</code>. Try again or /cancel.")
            return
        key, _, value = text.partition("=")
        ok, msg = set_env_var(proj_dir, key.strip(), value.strip())
        clear_state(message.from_user.id)
        bot.reply_to(message, msg, reply_markup=env_vars_keyboard(project_id))

    elif mode == "await_env_delete":
        key = message.text.strip()
        ok, msg = delete_env_var(proj_dir, key)
        clear_state(message.from_user.id)
        bot.reply_to(message, msg, reply_markup=env_vars_keyboard(project_id))


# ── Document handler for .env file upload ────────────────────────────────────

@bot.message_handler(
    content_types=["document"],
    func=lambda m: get_state(m.from_user.id).get("mode") == "await_env_file",
)
def handle_env_upload(message: Message) -> None:
    """Handle .env file upload."""
    if guard_message(message):
        return
    state = get_state(message.from_user.id)
    project_id = state.get("project_id")

    if not project_id:
        clear_state(message.from_user.id)
        return

    project = get_project_by_id(project_id)
    if not project or project["user_id"] != message.from_user.id:
        clear_state(message.from_user.id)
        bot.reply_to(message, "❌ Project not found.")
        return

    doc = message.document
    if doc.file_size and doc.file_size > 50 * 1024:
        bot.reply_to(message, "❌ .env file must be under 50 KB.")
        return

    try:
        file_info = bot.get_file(doc.file_id)
        content = bot.download_file(file_info.file_path)
    except Exception as exc:
        bot.reply_to(message, f"❌ Download failed: {exc}")
        return

    proj_dir = Path(project["project_dir"])
    ok, msg = save_uploaded_env_file(proj_dir, content)
    clear_state(message.from_user.id)
    bot.reply_to(message, msg, reply_markup=env_vars_keyboard(project_id))


# ── Helper ────────────────────────────────────────────────────────────────────

def _fmt_seconds(seconds: int) -> str:
    """Convert seconds to a short human-readable string."""
    if seconds < 60:
        return f"{seconds}s"
    m = seconds // 60
    if m < 60:
        return f"{m}m {seconds % 60}s"
    h = m // 60
    if h < 24:
        return f"{h}h {m % 60}m"
    return f"{h // 24}d {h % 24}h"
