"""
handlers/admin.py — Admin panel command and callback handlers.

All handlers check is_admin() before proceeding.
Broadcast runs in a background thread with rate limiting.
User list uses a single aggregation query (no N+1).
"""
import html
import logging
import threading
import time

from telebot.types import CallbackQuery, Message

from bot.instance import bot
from bot.middlewares import guard_callback, guard_message
from config import FOOTER, OWNER_IDS
from db.models import (
    ban_user,
    get_all_users,
    get_recent_activities,
    get_user_by_id,
    get_user_project_counts,
    get_user_projects,
    unban_user,
    update_user_slots,
)
from db.state_manager import clear_state, get_state, set_state
from utils.helpers import safe_html
from utils.keyboards import admin_main_panel_keyboard, project_panel_keyboard, slots_management_keyboard

logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    """
    Check whether a user has admin privileges.

    Args:
        user_id: Telegram user ID to check.

    Returns:
        True if user is an owner or has is_admin set in DB.
    """
    if user_id in OWNER_IDS:
        return True
    user = get_user_by_id(user_id)
    return bool(user and user.get("is_admin"))


# ── /admin ────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["admin"])
def cmd_admin(message: Message) -> None:
    """Show the admin control panel."""
    if guard_message(message):
        return
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ <b>Access Denied</b>\n\nYou don't have admin privileges.")
        return

    bot.reply_to(
        message,
        "🔐 <b>Admin Control Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Welcome, admin! Use the buttons below to manage the bot.\n"
        f"{FOOTER}",
        reply_markup=admin_main_panel_keyboard(),
        disable_web_page_preview=True,
    )


# ── Admin main panel callback ─────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "admin_main")
def callback_admin_main(call: CallbackQuery) -> None:
    """Return to the admin main panel."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Access denied", show_alert=True)
        return
    clear_state(call.from_user.id)
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            "🔐 <b>Admin Control Panel</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Welcome, admin!\n"
            f"{FOOTER}",
            call.message.chat.id,
            call.message.id,
            reply_markup=admin_main_panel_keyboard(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ── Broadcast ─────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "admin_broadcast")
def callback_admin_broadcast(call: CallbackQuery) -> None:
    """Enter broadcast mode."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Access denied", show_alert=True)
        return
    bot.answer_callback_query(call.id, "📢 Broadcast mode")
    set_state(call.from_user.id, {"mode": "broadcast_wait_content"})
    bot.send_message(
        call.message.chat.id,
        "📢 <b>Broadcast Message</b>\n\n"
        "Send the message to broadcast to all active users.\n\n"
        "Supported: text, photo, video, document.\n\n"
        "💡 /cancel to abort.",
        disable_web_page_preview=True,
    )


@bot.message_handler(
    content_types=["text", "photo", "video", "document"],
    func=lambda m: is_admin(m.from_user.id) and get_state(m.from_user.id).get("mode") == "broadcast_wait_content",
)
def handle_admin_broadcast(message: Message) -> None:
    """Broadcast a message to all non-banned users in a background thread."""
    if guard_message(message):
        return
    if get_state(message.from_user.id).get("mode") != "broadcast_wait_content":
        return
    clear_state(message.from_user.id)

    users = get_all_users()
    active_users = [u for u in users if not u.get("is_banned")]
    total = len(active_users)

    progress_msg = bot.reply_to(
        message,
        f"📢 <b>Broadcasting to {total} users…</b>",
        disable_web_page_preview=True,
    )

    def _broadcast_worker():
        success = 0
        fail = 0
        for i, user in enumerate(active_users):
            try:
                bot.copy_message(user["user_id"], message.chat.id, message.message_id)
                success += 1
            except Exception:
                fail += 1

            # Progress update every 50 messages
            if (i + 1) % 50 == 0:
                try:
                    bot.edit_message_text(
                        f"📢 <b>Broadcasting…</b>\nSent {i + 1}/{total}…",
                        progress_msg.chat.id,
                        progress_msg.message_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            # Rate limit: ~25 messages/second (sleep 1s every 25)
            if (i + 1) % 25 == 0:
                time.sleep(1)

        try:
            bot.edit_message_text(
                f"✅ <b>Broadcast Complete!</b>\n\n✅ Success: {success}\n❌ Failed: {fail}",
                progress_msg.chat.id,
                progress_msg.message_id,
                parse_mode="HTML",
            )
        except Exception:
            bot.send_message(
                message.chat.id,
                f"✅ Broadcast done. Success: {success}, Failed: {fail}",
            )

    threading.Thread(target=_broadcast_worker, daemon=True).start()


# ── Ban / Unban ───────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "admin_ban")
def callback_admin_ban(call: CallbackQuery) -> None:
    """Enter ban-user mode."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Access denied", show_alert=True)
        return
    bot.answer_callback_query(call.id, "🚫 Ban mode")
    set_state(call.from_user.id, {"mode": "await_ban_user_id"})
    bot.send_message(
        call.message.chat.id,
        "🚫 <b>Ban User</b>\n\nSend the user ID to ban.\n\n💡 /cancel to abort.",
    )


@bot.callback_query_handler(func=lambda c: c.data == "admin_unban")
def callback_admin_unban(call: CallbackQuery) -> None:
    """Enter unban-user mode."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Access denied", show_alert=True)
        return
    bot.answer_callback_query(call.id, "✅ Unban mode")
    set_state(call.from_user.id, {"mode": "await_unban_user_id"})
    bot.send_message(
        call.message.chat.id,
        "✅ <b>Unban User</b>\n\nSend the user ID to unban.\n\n💡 /cancel to abort.",
    )


# ── Slots management ──────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "admin_slots")
def callback_admin_slots(call: CallbackQuery) -> None:
    """Enter slot management mode."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Access denied", show_alert=True)
        return
    bot.answer_callback_query(call.id, "⭐ Slot management")
    set_state(call.from_user.id, {"mode": "await_slots_user_id"})
    bot.send_message(
        call.message.chat.id,
        "⭐ <b>Manage User Slots</b>\n\nSend the user ID.\n\n💡 /cancel to abort.",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("slot_"))
def callback_slot_management(call: CallbackQuery) -> None:
    """Handle slot +/- button presses."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Access denied", show_alert=True)
        return
    parts = call.data.split("|")
    action_parts = parts[0].split("_")  # ["slot", "free"/"premium", "add"/"sub"]
    slot_type = action_parts[1]
    operation = action_parts[2]
    target_uid = int(parts[1])
    delta = 1 if operation == "add" else -1

    if slot_type == "free":
        update_user_slots(target_uid, free_delta=delta)
    else:
        update_user_slots(target_uid, premium_delta=delta)

    bot.answer_callback_query(call.id, f"{'Added' if delta > 0 else 'Removed'} {slot_type} slot")
    user = get_user_by_id(target_uid)
    _show_slot_panel(call.message.chat.id, call.message.id, user, target_uid, edit=True)


def _show_slot_panel(chat_id: int, message_id: int | None, user: dict, target_uid: int, edit: bool = False) -> None:
    """Render the slot management panel."""
    text = (
        "⭐ <b>Manage User Slots</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 User: <b>{safe_html(user.get('first_name', 'Unknown'))}</b>\n"
        f"🆔 ID: <code>{target_uid}</code>\n\n"
        f"🆓 Free Slots: {user.get('free_slots', 1)}\n"
        f"⭐ Premium Slots: {user.get('premium_slots', 0)}\n"
        f"📊 Total: {user.get('free_slots', 1) + user.get('premium_slots', 0)}"
    )
    markup = slots_management_keyboard(target_uid)
    if edit and message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup)


# ── User list ─────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "admin_users")
def callback_admin_users(call: CallbackQuery) -> None:
    """Show user list using a single aggregation for project counts."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Access denied", show_alert=True)
        return
    bot.answer_callback_query(call.id, "📊 Loading…")

    users = get_all_users(limit=30)
    project_counts = get_user_project_counts()  # Single aggregation — no N+1

    active = sum(1 for u in users if not u.get("is_banned"))
    banned = len(users) - active
    admins = sum(1 for u in users if u.get("is_admin"))

    stats = (
        "👥 <b>User Statistics</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Total: {len(users)}\n✅ Active: {active}\n🚫 Banned: {banned}\n👑 Admins: {admins}\n\n"
    )

    lines = []
    for u in users:
        uname = f"@{u.get('username')}" if u.get("username") else "No username"
        status = "🚫 BANNED" if u.get("is_banned") else "✅ Active"
        badge = "👑 " if u.get("is_admin") else ""
        proj_count = project_counts.get(u["user_id"], 0)
        slots = u.get("free_slots", 1) + u.get("premium_slots", 0)
        lines.append(
            f"{badge}<b>{html.escape(u.get('first_name', 'Unknown'), quote=False)}</b> ({uname})\n"
            f"🆔 <code>{u['user_id']}</code> | {status}\n"
            f"📂 {proj_count}/{slots} projects"
        )

    bot.send_message(
        call.message.chat.id,
        stats + "\n\n".join(lines),
        disable_web_page_preview=True,
    )


# ── User projects inspection ──────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "admin_user_projects")
def callback_admin_user_projects(call: CallbackQuery) -> None:
    """Enter inspect-user-projects mode."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Access denied", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, {"mode": "await_inspect_user_id"})
    bot.send_message(
        call.message.chat.id,
        "📂 <b>Inspect User Projects</b>\n\nSend the user ID.\n\n💡 /cancel to abort.",
    )


# ── Activity log ──────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "admin_activity")
def callback_admin_activity(call: CallbackQuery) -> None:
    """Show the recent activity log."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Access denied", show_alert=True)
        return
    bot.answer_callback_query(call.id, "📈 Loading…")

    activities = get_recent_activities(limit=20)
    if not activities:
        bot.send_message(call.message.chat.id, "ℹ️ No activities logged yet.")
        return

    _ACTION_EMOJI = {
        "START": "▶️", "STOP": "⏹", "RESTART": "🔄", "DELETE": "🗑",
        "PROJECT_CREATED": "🆕", "PROJECT_READY": "✅",
        "UPLOAD_ZIP": "📦", "UPLOAD_PY": "📄", "DEPS_INSTALL": "📦",
    }
    lines = []
    for act in activities:
        user = get_user_by_id(act["user_id"])
        uname = f"@{user.get('username')}" if user and user.get("username") else f"ID:{act['user_id']}"
        emoji = _ACTION_EMOJI.get(act["action"], "📌")
        line = f"{emoji} <b>{act['action']}</b> by {uname}"
        if act.get("project_id"):
            line += f"\n📂 <code>{safe_html(act['project_id'])}</code>"
        if act.get("details"):
            line += f"\n💬 {safe_html(act['details'][:60])}"
        line += f"\n🕐 <i>{act['created_at'][:19]}</i>"
        lines.append(line)

    bot.send_message(
        call.message.chat.id,
        "📈 <b>Recent Activity Log</b>\n━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(lines),
        disable_web_page_preview=True,
    )


# ── Admin text input state machine ────────────────────────────────────────────

@bot.message_handler(
    content_types=["text"],
    func=lambda m: is_admin(m.from_user.id) and get_state(m.from_user.id).get("mode") in (
        "await_ban_user_id", "await_unban_user_id",
        "await_slots_user_id", "await_inspect_user_id",
    ),
)
def handle_admin_text(message: Message) -> None:
    """Handle admin text input for ban/unban/slots/inspect flows."""
    if guard_message(message):
        return
    state = get_state(message.from_user.id)
    mode = state.get("mode")

    try:
        target_uid = int(message.text.strip())
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID. Send a numeric ID.")
        return

    if mode == "await_ban_user_id":
        if target_uid in OWNER_IDS:
            bot.reply_to(message, "❌ Cannot ban an owner.")
            clear_state(message.from_user.id)
            return
        ban_user(target_uid)
        clear_state(message.from_user.id)
        bot.reply_to(message, f"✅ User <code>{target_uid}</code> banned.")

    elif mode == "await_unban_user_id":
        unban_user(target_uid)
        clear_state(message.from_user.id)
        bot.reply_to(message, f"✅ User <code>{target_uid}</code> unbanned.")

    elif mode == "await_slots_user_id":
        user = get_user_by_id(target_uid)
        if not user:
            bot.reply_to(message, f"❌ User <code>{target_uid}</code> not found.")
            clear_state(message.from_user.id)
            return
        clear_state(message.from_user.id)
        _show_slot_panel(message.chat.id, None, user, target_uid, edit=False)

    elif mode == "await_inspect_user_id":
        user = get_user_by_id(target_uid)
        if not user:
            bot.reply_to(message, f"❌ User <code>{target_uid}</code> not found.")
            clear_state(message.from_user.id)
            return
        projects = get_user_projects(target_uid)
        clear_state(message.from_user.id)

        if not projects:
            bot.reply_to(
                message,
                f"📂 <b>{safe_html(user.get('first_name', 'Unknown'))}</b> has no projects.",
            )
            return

        bot.reply_to(
            message,
            f"📂 <b>Projects for {safe_html(user.get('first_name', 'Unknown'))}</b>\n"
            f"Total: {len(projects)}",
        )
        for proj in projects:
            _STATUS = {"new": "🆕", "ready": "✅", "running": "🟢", "stopped": "⏸"}
            emoji = _STATUS.get(proj["status"], "❓")
            text = (
                f"{emoji} <b>{safe_html(proj['name'])}</b>\n"
                f"Status: {proj['status'].upper()}\n"
                f"ID: <code>{safe_html(proj['project_id'])}</code>\n"
            )
            if proj.get("pid"):
                text += f"PID: {proj['pid']}\n"
            if proj.get("entry"):
                text += f"Entry: {safe_html(proj['entry'])}\n"
            bot.send_message(
                message.chat.id,
                text,
                reply_markup=project_panel_keyboard(proj["project_id"]),
            )
