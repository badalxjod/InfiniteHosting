"""
utils/keyboards.py — All InlineKeyboardMarkup factory functions.

Each function returns a ready-to-use markup object. No logic lives here;
keyboards are purely structural.
"""
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


# ── User keyboards ────────────────────────────────────────────────────────────

def my_projects_list_keyboard(projects: list) -> InlineKeyboardMarkup:
    """
    Build project list keyboard with Refresh and New Project buttons.

    Args:
        projects: List of project documents.

    Returns:
        InlineKeyboardMarkup with one button per project plus controls.
    """
    markup = InlineKeyboardMarkup(row_width=1)
    _STATUS_EMOJI = {"new": "🆕", "ready": "✅", "running": "🟢", "stopped": "⏸"}

    for proj in projects:
        emoji = _STATUS_EMOJI.get(proj["status"], "❓")
        markup.add(
            InlineKeyboardButton(
                f"{emoji} {proj['name']}",
                callback_data=f"view_project|{proj['project_id']}",
            )
        )

    markup.add(
        InlineKeyboardButton("🔄 Refresh", callback_data="refresh_projects"),
        InlineKeyboardButton("➕ New Project", callback_data="new_project"),
    )
    return markup


def project_panel_keyboard(project_id: str) -> InlineKeyboardMarkup:
    """
    Build the main control panel for a single project.

    Args:
        project_id: Project to build controls for.

    Returns:
        InlineKeyboardMarkup with start/stop/restart/etc.
    """
    markup = InlineKeyboardMarkup(row_width=2)
    pid = project_id
    markup.add(
        InlineKeyboardButton("▶️ Start", callback_data=f"start|{pid}"),
        InlineKeyboardButton("⏹ Stop", callback_data=f"stop|{pid}"),
    )
    markup.add(
        InlineKeyboardButton("🔄 Restart", callback_data=f"restart|{pid}"),
        InlineKeyboardButton("📊 Dashboard", callback_data=f"dashboard|{pid}"),
    )
    markup.add(
        InlineKeyboardButton("🔐 Env Vars", callback_data=f"envvars|{pid}"),
        InlineKeyboardButton("📈 Analytics", callback_data=f"analytics|{pid}"),
    )
    markup.add(
        InlineKeyboardButton("📄 Logs", callback_data=f"logs|{pid}"),
        InlineKeyboardButton("📦 Dependencies", callback_data=f"deps|{pid}"),
    )
    markup.add(InlineKeyboardButton("✏️ Edit Run Cmd", callback_data=f"editcmd|{pid}"))
    markup.add(InlineKeyboardButton("🗑 Delete Project", callback_data=f"delete|{pid}"))
    return markup


def delete_confirmation_keyboard(project_id: str) -> InlineKeyboardMarkup:
    """
    Confirmation keyboard for project deletion.

    Args:
        project_id: Project to confirm deletion for.

    Returns:
        Yes/Cancel InlineKeyboardMarkup.
    """
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirm_delete|{project_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_delete|{project_id}"),
    )
    return markup


def cancel_setup_keyboard(project_id: str) -> InlineKeyboardMarkup:
    """
    Cancel button shown while awaiting file uploads.

    Args:
        project_id: Incomplete project to cancel.

    Returns:
        InlineKeyboardMarkup with single cancel button.
    """
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup|{project_id}"))
    return markup


def back_to_project_keyboard(project_id: str) -> InlineKeyboardMarkup:
    """
    Single back button pointing to the project panel.

    Args:
        project_id: Destination project ID.

    Returns:
        InlineKeyboardMarkup with a Back button.
    """
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"back_to_project|{project_id}"))
    return markup


# ── Admin keyboards ───────────────────────────────────────────────────────────

def admin_main_panel_keyboard() -> InlineKeyboardMarkup:
    """Admin control panel main menu."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban"),
    )
    markup.add(
        InlineKeyboardButton("✅ Unban User", callback_data="admin_unban"),
        InlineKeyboardButton("⭐ Manage Slots", callback_data="admin_slots"),
    )
    markup.add(
        InlineKeyboardButton("👥 User List", callback_data="admin_users"),
        InlineKeyboardButton("📂 User Projects", callback_data="admin_user_projects"),
    )
    markup.add(InlineKeyboardButton("📈 Activity Log", callback_data="admin_activity"))
    return markup


def slots_management_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """
    Slot adjustment keyboard for a specific user.

    Args:
        user_id: Target user ID.

    Returns:
        InlineKeyboardMarkup with +/- free/premium slot buttons.
    """
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Free Slot", callback_data=f"slot_free_add|{user_id}"),
        InlineKeyboardButton("➖ Free Slot", callback_data=f"slot_free_sub|{user_id}"),
    )
    markup.add(
        InlineKeyboardButton("➕ Premium Slot", callback_data=f"slot_premium_add|{user_id}"),
        InlineKeyboardButton("➖ Premium Slot", callback_data=f"slot_premium_sub|{user_id}"),
    )
    markup.add(InlineKeyboardButton("🔙 Back", callback_data="admin_main"))
    return markup


# ── Feature keyboards ─────────────────────────────────────────────────────────

def env_vars_keyboard(project_id: str) -> InlineKeyboardMarkup:
    """Environment variables management keyboard."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Add Variable", callback_data=f"env_add|{project_id}"),
        InlineKeyboardButton("📋 List All", callback_data=f"env_list|{project_id}"),
    )
    markup.add(
        InlineKeyboardButton("🗑 Delete Variable", callback_data=f"env_delete|{project_id}"),
        InlineKeyboardButton("📤 Upload .env", callback_data=f"env_upload|{project_id}"),
    )
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"back_to_project|{project_id}"))
    return markup


def dashboard_keyboard(project_id: str) -> InlineKeyboardMarkup:
    """Dashboard refresh keyboard."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔄 Refresh", callback_data=f"dashboard|{project_id}"),
        InlineKeyboardButton("📊 Full Stats", callback_data=f"analytics|{project_id}"),
    )
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"back_to_project|{project_id}"))
    return markup


def analytics_keyboard(project_id: str) -> InlineKeyboardMarkup:
    """Analytics view keyboard."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔔 Alerts", callback_data=f"view_alerts|{project_id}"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"analytics|{project_id}"),
    )
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"back_to_project|{project_id}"))
    return markup
