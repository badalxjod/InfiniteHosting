"""
handlers/user.py — User-facing command and callback handlers.

Covers: /start, /newproject, /myproject, /help, /cancel,
project panel callbacks (view, start, stop, restart, delete, logs, deps, editcmd),
file upload handling, and cancel_setup.
"""
import html
import io
import logging
import shutil
import threading
import zipfile
from pathlib import Path

from telebot.types import CallbackQuery, Message

from bot.instance import bot
from bot.middlewares import guard_callback, guard_message
from config import BACKUP_CHANNEL_ID, FOOTER
from core.analytics import get_storage_usage, usage_tracker
from core.health_monitor import health_monitor
from core.process_manager import (
    detect_entry_file,
    install_requirements,
    restart_project,
    safe_extract,
    start_project,
    stop_project,
)
from db.models import (
    count_active_projects,
    create_alert,
    delete_project_from_db,
    get_or_create_user,
    get_project_by_id,
    get_user_projects,
    increment_restart_count,
    update_project,
    update_user_activity,
)
from db.state_manager import clear_state, get_state, set_state
from utils.helpers import safe_html, schedule_delete
from utils.keyboards import (
    cancel_setup_keyboard,
    delete_confirmation_keyboard,
    my_projects_list_keyboard,
    project_panel_keyboard,
)

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB


# ── /start ────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message: Message) -> None:
    """Welcome the user and show basic options."""
    if guard_message(message):
        return
    user = get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )
    name = safe_html(user.get("first_name") or "there")
    text = (
        f"👋 <b>Welcome, {name}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "I can host your Python / Node.js projects 24/7.\n\n"
        "📌 <b>Commands:</b>\n"
        "• /newproject — Create a new project\n"
        "• /myproject — Manage your projects\n"
        "• /help — Show this help\n"
        f"{FOOTER}"
    )
    bot.reply_to(message, text, disable_web_page_preview=True)


# ── /help ─────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["help"])
def cmd_help(message: Message) -> None:
    """Show help text."""
    if guard_message(message):
        return
    text = (
        "📖 <b>HostingBot Help</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "/newproject — Create a new hosted project\n"
        "/myproject — List and manage your projects\n"
        "/cancel — Cancel current operation\n\n"
        "💡 Upload a <b>.zip</b> or a single <b>.py</b> file to get started.\n"
        f"{FOOTER}"
    )
    bot.reply_to(message, text, disable_web_page_preview=True)


# ── /cancel ───────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["cancel"])
def cmd_cancel(message: Message) -> None:
    """Cancel the current user operation."""
    state = get_state(message.from_user.id)
    if state.get("mode"):
        clear_state(message.from_user.id)
        bot.reply_to(message, "✅ Operation cancelled.")
    else:
        bot.reply_to(message, "ℹ️ No active operation to cancel.")


# ── /newproject ───────────────────────────────────────────────────────────────

@bot.message_handler(commands=["newproject"])
def cmd_new_project(message: Message) -> None:
    """Start the project creation flow."""
    if guard_message(message):
        return

    user = get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )
    uid = message.from_user.id

    if not user.get("is_admin"):
        active_count = count_active_projects(uid)
        total_slots = user.get("free_slots", 1) + user.get("premium_slots", 0)
        if active_count >= total_slots:
            bot.reply_to(
                message,
                f"⚠️ <b>Slot limit reached</b>: {active_count}/{total_slots} projects.\n\n"
                "Contact an admin for more slots.",
            )
            return

    set_state(uid, {"mode": "await_project_name"})
    bot.reply_to(
        message,
        "📝 <b>New Project</b>\n\nPlease send me a name for your project.\n\n"
        "💡 /cancel to abort.",
    )


# ── /myproject ────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["myproject"])
def cmd_my_project(message: Message) -> None:
    """List all projects for the user."""
    if guard_message(message):
        return
    get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )
    uid = message.from_user.id
    _send_project_list(uid, message.chat.id)


def _send_project_list(user_id: int, chat_id: int) -> None:
    """Helper: fetch and display the project list."""
    user = get_or_create_user(user_id)
    projects = get_user_projects(user_id)
    total_slots = user.get("free_slots", 1) + user.get("premium_slots", 0)
    active_count = sum(1 for p in projects if p["status"] not in ("deleted",))

    if not projects:
        bot.send_message(
            chat_id,
            "📂 <b>My Projects</b>\n\nYou have no projects yet.\nUse /newproject to create one.",
            reply_markup=my_projects_list_keyboard([]),
        )
        return

    text = (
        f"📂 <b>My Projects</b>\n"
        f"Total: {active_count}/{total_slots} slots used\n\n"
        "Tap a project to manage it:"
    )
    bot.send_message(chat_id, text, reply_markup=my_projects_list_keyboard(projects))


# ── refresh_projects callback ─────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "refresh_projects")
def callback_refresh_projects(call: CallbackQuery) -> None:
    """Refresh the project list."""
    if guard_callback(call):
        return
    bot.answer_callback_query(call.id, "🔄 Refreshed")
    user = get_or_create_user(call.from_user.id)
    projects = get_user_projects(call.from_user.id)
    total_slots = user.get("free_slots", 1) + user.get("premium_slots", 0)
    active_count = len(projects)
    text = (
        f"📂 <b>My Projects</b>\n"
        f"Total: {active_count}/{total_slots} slots used\n\n"
        "Tap a project to manage it:"
    )
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.id,
            reply_markup=my_projects_list_keyboard(projects),
            parse_mode="HTML",
        )
    except Exception:
        pass


# ── new_project inline button ─────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "new_project")
def callback_new_project(call: CallbackQuery) -> None:
    """Start new project flow from inline button."""
    if guard_callback(call):
        return
    bot.answer_callback_query(call.id)
    # Delegate to message handler logic
    user = get_or_create_user(call.from_user.id)
    uid = call.from_user.id
    if not user.get("is_admin"):
        active_count = count_active_projects(uid)
        total_slots = user.get("free_slots", 1) + user.get("premium_slots", 0)
        if active_count >= total_slots:
            bot.send_message(
                call.message.chat.id,
                f"⚠️ Slot limit reached: {active_count}/{total_slots}.\nContact an admin.",
            )
            return
    set_state(uid, {"mode": "await_project_name"})
    bot.send_message(
        call.message.chat.id,
        "📝 <b>New Project</b>\n\nSend me a name for your project.\n\n💡 /cancel to abort.",
    )


# ── view_project callback ─────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("view_project|"))
def callback_view_project(call: CallbackQuery) -> None:
    """Show the project control panel."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _show_project_panel(call.message.chat.id, call.message.id, project, edit=True)


def _show_project_panel(chat_id: int, message_id: int | None, project: dict, edit: bool = False) -> None:
    """Render the project panel (edit existing message or send new)."""
    _STATUS = {"new": "🆕 New", "ready": "✅ Ready", "running": "🟢 Running", "stopped": "⏸ Stopped"}
    status_text = _STATUS.get(project["status"], f"❓ {project['status']}")
    auto_restart_text = "✅ Enabled" if project.get("auto_restart") else "❌ Disabled"

    text = (
        f"<b>{safe_html(project['name'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Status: {status_text}\n"
        f"🆔 Project ID:\n<code>{safe_html(project['project_id'])}</code>\n"
        f"📄 Entry: {safe_html(project.get('entry') or 'Not set')}\n"
        f"🔄 Auto-Restart: {auto_restart_text}\n"
        f"♻️ Restarts: {project.get('restart_count', 0)}\n"
    )
    if project.get("pid"):
        text += f"⚡ PID: {project['pid']}\n"
    if project.get("run_cmd"):
        text += f"▶️ Run cmd: <code>{safe_html(project['run_cmd'])}</code>\n"

    markup = project_panel_keyboard(project["project_id"])
    if edit and message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup)


# ── back_to_project callback ──────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("back_to_project|"))
def callback_back_to_project(call: CallbackQuery) -> None:
    """Navigate back to the project panel."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _show_project_panel(call.message.chat.id, call.message.id, project, edit=True)


# ── START callback ────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("start|"))
def callback_start(call: CallbackQuery) -> None:
    """Start a project in a background thread."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    if project["status"] == "running":
        bot.answer_callback_query(call.id, "Already running!", show_alert=True)
        return
    if not project.get("entry"):
        bot.answer_callback_query(call.id, "❌ No entry file set. Upload files first.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "⏳ Starting…")
    try:
        bot.edit_message_text(
            "⏳ <b>Starting Project…</b>\n\nPlease wait.",
            call.message.chat.id,
            call.message.id,
            reply_markup=project_panel_keyboard(project_id),
            parse_mode="HTML",
        )
    except Exception:
        pass

    def _do_start():
        ok, msg, pid = start_project(project)
        if ok:
            update_project(project_id, status="running", pid=pid,
                           first_start_time=project.get("first_start_time") or _now_iso())
            health_monitor.add_project(
                project_id, pid, call.from_user.id, project["name"],
                auto_restart=project.get("auto_restart", True),
                max_restarts=project.get("max_restarts", 5),
            )
            usage_tracker.start_tracking(project_id, pid)
            update_user_activity(call.from_user.id, "START", project_id=project_id)
            _silent_backup(project)
        else:
            update_project(project_id, status="stopped", pid=None)

        proj = get_project_by_id(project_id)
        try:
            bot.edit_message_text(
                f"{'✅' if ok else '❌'} <b>{'Started!' if ok else 'Start Failed'}</b>\n\n{safe_html(msg)}",
                call.message.chat.id,
                call.message.id,
                reply_markup=project_panel_keyboard(project_id),
                parse_mode="HTML",
            )
        except Exception:
            bot.send_message(call.message.chat.id,
                             f"{'✅' if ok else '❌'} {safe_html(msg)}",
                             reply_markup=project_panel_keyboard(project_id))

    threading.Thread(target=_do_start, daemon=True).start()


# ── STOP callback ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("stop|"))
def callback_stop(call: CallbackQuery) -> None:
    """Stop a running project in a background thread."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "⏳ Stopping…")
    try:
        bot.edit_message_text(
            "⏳ <b>Stopping Project…</b>\n\nPlease wait.",
            call.message.chat.id,
            call.message.id,
            reply_markup=project_panel_keyboard(project_id),
            parse_mode="HTML",
        )
    except Exception:
        pass

    def _do_stop():
        ok, msg = stop_project(project)
        if ok:
            health_monitor.remove_project(project_id)
            usage_tracker.stop_tracking(project_id)
            update_project(project_id, status="stopped", pid=None)
            update_user_activity(call.from_user.id, "STOP", project_id=project_id)
        try:
            bot.edit_message_text(
                f"{'⏸' if ok else '❌'} <b>{'Project Stopped' if ok else 'Stop Failed'}</b>\n\n{safe_html(msg)}",
                call.message.chat.id,
                call.message.id,
                reply_markup=project_panel_keyboard(project_id),
                parse_mode="HTML",
            )
        except Exception:
            bot.send_message(call.message.chat.id, safe_html(msg),
                             reply_markup=project_panel_keyboard(project_id))

    threading.Thread(target=_do_stop, daemon=True).start()


# ── RESTART callback ──────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("restart|"))
def callback_restart(call: CallbackQuery) -> None:
    """Restart a project in a background thread."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "🔄 Restarting…")
    try:
        bot.edit_message_text(
            "🔄 <b>Restarting Project…</b>\n\nPlease wait.",
            call.message.chat.id,
            call.message.id,
            reply_markup=project_panel_keyboard(project_id),
            parse_mode="HTML",
        )
    except Exception:
        pass

    def _do_restart():
        health_monitor.remove_project(project_id)
        usage_tracker.stop_tracking(project_id)

        ok, msg, pid = restart_project(project)
        if ok:
            increment_restart_count(project_id)
            update_project(project_id, status="running", pid=pid)
            health_monitor.add_project(
                project_id, pid, call.from_user.id, project["name"],
                auto_restart=project.get("auto_restart", True),
                max_restarts=project.get("max_restarts", 5),
            )
            usage_tracker.start_tracking(project_id, pid)
            update_user_activity(call.from_user.id, "RESTART", project_id=project_id)
        else:
            update_project(project_id, status="stopped", pid=None)

        pid_line = f"\n🔢 PID: {pid}" if ok and pid else ""
        try:
            bot.edit_message_text(
                f"{'🔄' if ok else '❌'} <b>{'Restarted!' if ok else 'Restart Failed'}</b>"
                f"\n\n{safe_html(msg)}{pid_line}",
                call.message.chat.id,
                call.message.id,
                reply_markup=project_panel_keyboard(project_id),
                parse_mode="HTML",
            )
        except Exception:
            bot.send_message(call.message.chat.id, safe_html(msg),
                             reply_markup=project_panel_keyboard(project_id))

    threading.Thread(target=_do_restart, daemon=True).start()


# ── DELETE callbacks ──────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("delete|"))
def callback_delete(call: CallbackQuery) -> None:
    """Show delete confirmation prompt."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            f"🗑 <b>Delete Project?</b>\n\n"
            f"Project: <b>{safe_html(project['name'])}</b>\n\n"
            "⚠️ This action is permanent and cannot be undone.",
            call.message.chat.id,
            call.message.id,
            reply_markup=delete_confirmation_keyboard(project_id),
            parse_mode="HTML",
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_delete|"))
def callback_confirm_delete(call: CallbackQuery) -> None:
    """Perform project deletion."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "🗑 Deleting…")

    # Stop if running
    if project.get("pid"):
        stop_project(project)
    health_monitor.remove_project(project_id)
    usage_tracker.stop_tracking(project_id)

    # Remove files
    proj_dir = Path(project["project_dir"])
    try:
        if proj_dir.exists():
            shutil.rmtree(proj_dir)
    except Exception as exc:
        logger.warning("Could not remove project dir %s: %s", proj_dir, exc)

    delete_project_from_db(project_id)
    update_user_activity(call.from_user.id, "DELETE", project_id=project_id)

    try:
        bot.edit_message_text(
            "✅ <b>Project Deleted</b>\n\nThe project has been permanently removed.",
            call.message.chat.id,
            call.message.id,
            parse_mode="HTML",
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_delete|"))
def callback_cancel_delete(call: CallbackQuery) -> None:
    """Cancel deletion and return to project panel."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    bot.answer_callback_query(call.id, "Cancelled")
    if project:
        _show_project_panel(call.message.chat.id, call.message.id, project, edit=True)


# ── LOGS callback ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("logs|"))
def callback_logs(call: CallbackQuery) -> None:
    """Show last 20 log lines inline and offer full log download."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)

    proj_dir = Path(project["project_dir"])
    log_files = {"project.log": proj_dir / "project.log", "error.log": proj_dir / "error.log"}

    for log_name, log_path in log_files.items():
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            last_20 = "".join(lines[-20:]).strip() or "(empty)"
            bot.send_message(
                call.message.chat.id,
                f"📄 <b>{log_name} — last 20 lines:</b>\n<pre>{html.escape(last_20)}</pre>",
                reply_markup=project_panel_keyboard(project_id),
                parse_mode="HTML",
            )
            # Full file download
            with open(log_path, "rb") as fh:
                bot.send_document(call.message.chat.id, fh, visible_file_name=log_name)
        except Exception as exc:
            logger.warning("Log send error: %s", exc)


# ── DEPS callback ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("deps|"))
def callback_deps(call: CallbackQuery) -> None:
    """Install requirements in a background thread."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    if not project.get("requirements"):
        bot.answer_callback_query(call.id, "No requirements.txt file uploaded.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "📦 Installing… (this may take a few minutes)")
    bot.send_message(
        call.message.chat.id,
        "📦 <b>Installing dependencies…</b>\n\nThis may take several minutes. I'll notify you when done.",
        reply_markup=project_panel_keyboard(project_id),
    )

    def _do_install():
        ok, msg = install_requirements(project)
        update_user_activity(call.from_user.id, "DEPS_INSTALL", details=msg[:80], project_id=project_id)
        bot.send_message(
            call.message.chat.id,
            f"{'✅' if ok else '❌'} <b>{'Done!' if ok else 'Failed'}</b>\n\n{safe_html(msg)}",
            reply_markup=project_panel_keyboard(project_id),
        )
        # Offer deps.log download
        deps_log = Path(project["project_dir"]) / "deps.log"
        if deps_log.exists():
            try:
                with open(deps_log, "rb") as fh:
                    bot.send_document(call.message.chat.id, fh, visible_file_name="deps.log")
            except Exception as exc:
                logger.debug("Could not send deps.log: %s", exc)

    threading.Thread(target=_do_install, daemon=True).start()


# ── EDIT RUN CMD callback ─────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("editcmd|"))
def callback_edit_cmd(call: CallbackQuery) -> None:
    """Prompt user to enter a new run command."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if not project or project["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "❌ Project not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, {"mode": "await_run_cmd", "project_id": project_id})
    bot.send_message(
        call.message.chat.id,
        "✏️ <b>Edit Run Command</b>\n\n"
        "Send the new run command.\n\n"
        "✅ Allowed prefixes: <code>python3</code>, <code>python</code>, "
        "<code>node</code>, <code>npm start</code>, <code>npm run</code>\n\n"
        f"Current: <code>{safe_html(project.get('run_cmd') or 'auto-detect')}</code>\n\n"
        "💡 /cancel to abort.",
    )


# ── cancel_setup callback ─────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_setup|"))
def callback_cancel_setup(call: CallbackQuery) -> None:
    """Cancel incomplete project setup and remove the project."""
    if guard_callback(call):
        return
    project_id = call.data.split("|", 1)[1]
    project = get_project_by_id(project_id)
    if project and project["user_id"] == call.from_user.id:
        proj_dir = Path(project["project_dir"])
        delete_project_from_db(project_id)
        try:
            if proj_dir.exists():
                shutil.rmtree(proj_dir)
        except Exception:
            pass
    clear_state(call.from_user.id)
    bot.answer_callback_query(call.id, "❌ Setup cancelled.")
    try:
        bot.edit_message_text(
            "❌ <b>Setup cancelled.</b>\nProject removed. Use /newproject to start again.",
            call.message.chat.id,
            call.message.id,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(call.message.chat.id, "❌ Setup cancelled.")


# ── Text input handler (state machine) ────────────────────────────────────────

@bot.message_handler(
    content_types=["text"],
    func=lambda m: get_state(m.from_user.id).get("mode") in (
        "await_project_name", "await_run_cmd"
    ),
)
def handle_user_text_state(message: Message) -> None:
    """Handle text messages when user is in an interactive state."""
    if guard_message(message):
        return
    state = get_state(message.from_user.id)
    mode = state.get("mode")

    if mode == "await_project_name":
        name = message.text.strip()[:50]
        if not name:
            bot.reply_to(message, "❌ Name cannot be empty.")
            return
        get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        from db.models import create_project
        project = create_project(message.from_user.id, name)
        project_id = project["project_id"]
        set_state(message.from_user.id, {"mode": "await_files", "project_id": project_id})
        update_user_activity(message.from_user.id, "PROJECT_CREATED", details=name, project_id=project_id)
        bot.reply_to(
            message,
            f"✅ Project <b>{safe_html(name)}</b> created!\n\n"
            "📤 Now send me your project files:\n"
            "• A <b>.zip</b> archive containing your project\n"
            "• Or a single <b>.py / .js</b> file\n\n"
            "💡 /cancel to abort.",
            reply_markup=cancel_setup_keyboard(project_id),
        )

    elif mode == "await_run_cmd":
        project_id = state.get("project_id")
        if not project_id:
            clear_state(message.from_user.id)
            return
        project = get_project_by_id(project_id)
        if not project or project["user_id"] != message.from_user.id:
            clear_state(message.from_user.id)
            bot.reply_to(message, "❌ Project not found.")
            return
        from core.process_manager import ALLOWED_PREFIXES
        cmd = message.text.strip()
        if not any(cmd.startswith(p) for p in ALLOWED_PREFIXES):
            bot.reply_to(
                message,
                "❌ Command must start with: python3, python, node, npm start, or npm run.\nTry again or /cancel.",
            )
            return
        update_project(project_id, run_cmd=cmd)
        clear_state(message.from_user.id)
        bot.reply_to(
            message,
            f"✅ Run command updated to:\n<code>{safe_html(cmd)}</code>",
            reply_markup=project_panel_keyboard(project_id),
        )


# ── File upload handler ───────────────────────────────────────────────────────

@bot.message_handler(
    content_types=["document"],
    func=lambda m: get_state(m.from_user.id).get("mode") == "await_files",
)
def handle_document(message: Message) -> None:
    """Handle file uploads during project setup."""
    if guard_message(message):
        return

    state = get_state(message.from_user.id)
    project_id = state.get("project_id")
    if not project_id:
        return

    project = get_project_by_id(project_id)
    if not project or project["user_id"] != message.from_user.id:
        return

    doc = message.document

    # File size check
    if doc.file_size and doc.file_size > MAX_FILE_SIZE_BYTES:
        bot.reply_to(
            message,
            f"❌ File too large. Maximum allowed size is {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB.",
        )
        return

    proj_dir = Path(project["project_dir"])
    filename = doc.file_name or "upload"

    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
    except Exception as exc:
        bot.reply_to(message, f"❌ Download failed: {exc}")
        return

    # Handle ZIP
    if filename.endswith(".zip"):
        zip_path = proj_dir / filename
        zip_path.write_bytes(downloaded)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                safe_extract(zf, proj_dir)
            zip_path.unlink(missing_ok=True)
        except ValueError as exc:
            bot.reply_to(message, f"❌ Security error: {exc}")
            return
        except zipfile.BadZipFile:
            bot.reply_to(message, "❌ Invalid ZIP file.")
            return
        update_user_activity(message.from_user.id, "UPLOAD_ZIP", project_id=project_id)

    elif filename.endswith((".py", ".js")):
        (proj_dir / filename).write_bytes(downloaded)
        update_user_activity(message.from_user.id, "UPLOAD_PY", project_id=project_id)

    elif filename == "requirements.txt":
        (proj_dir / "requirements.txt").write_bytes(downloaded)
        update_project(project_id, requirements="requirements.txt")
        bot.reply_to(
            message,
            "📦 <b>requirements.txt saved!</b>\n\nUse the Dependencies button to install them.",
            reply_markup=cancel_setup_keyboard(project_id),
        )
        return

    else:
        # Save any other file as-is
        (proj_dir / filename).write_bytes(downloaded)

    # Detect entry file
    entry = detect_entry_file(proj_dir)
    if entry:
        update_project(project_id, entry=entry, status="ready")
        clear_state(message.from_user.id)
        update_user_activity(message.from_user.id, "PROJECT_READY", project_id=project_id)
        _silent_backup(project)
        bot.reply_to(
            message,
            f"✅ <b>Project ready!</b>\n\n"
            f"📄 Entry file: <code>{safe_html(entry)}</code>\n\n"
            "Use the panel below to start your project.",
            reply_markup=project_panel_keyboard(project_id),
        )
    else:
        bot.reply_to(
            message,
            "📁 File saved! No entry file detected yet.\n\n"
            "Send your main file (main.py / app.py / index.js) or /cancel.",
            reply_markup=cancel_setup_keyboard(project_id),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _silent_backup(project: dict) -> None:
    """Silently back up the project zip to the backup channel if configured."""
    if not BACKUP_CHANNEL_ID:
        return
    proj_dir = Path(project["project_dir"])

    def _do_backup():
        try:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for fp in proj_dir.rglob("*"):
                    if fp.is_file() and "venv" not in fp.parts and "__pycache__" not in fp.parts:
                        zf.write(fp, fp.relative_to(proj_dir))
            buf.seek(0)
            bot.send_document(
                BACKUP_CHANNEL_ID,
                buf,
                visible_file_name=f"{project['project_id']}.zip",
                caption=f"Backup: {project['name']} (uid={project['user_id']})",
            )
        except Exception as exc:
            logger.debug("Backup failed: %s", exc)

    threading.Thread(target=_do_backup, daemon=True).start()
