"""
bot/middlewares.py — Shared guard helpers used at the start of handlers.

check_banned() returns True and sends a message if the user is banned.
check_rate_limit() returns True and replies if the user is rate-limited.
"""
import logging

from telebot.types import Message, CallbackQuery

from bot.instance import bot
from bot.rate_limiter import rate_limiter
from db.models import get_user_by_id

logger = logging.getLogger(__name__)


def check_banned(user_id: int, chat_id: int) -> bool:
    """
    Send a banned message if the user is banned.

    Args:
        user_id: Telegram user ID.
        chat_id: Chat to send the reply to.

    Returns:
        True if the user is banned (caller should return early), False otherwise.
    """
    user = get_user_by_id(user_id)
    if user and user.get("is_banned"):
        try:
            bot.send_message(chat_id, "🚫 <b>You have been banned from using this bot.</b>")
        except Exception as exc:
            logger.warning("Could not send ban message to %s: %s", user_id, exc)
        return True
    return False


def check_rate_limit(user_id: int, chat_id: int) -> bool:
    """
    Send a rate-limit message if the user has hit the limit.

    Args:
        user_id: Telegram user ID.
        chat_id: Chat to send the reply to.

    Returns:
        True if rate-limited (caller should return early), False otherwise.
    """
    if rate_limiter.is_limited(user_id):
        try:
            bot.send_message(chat_id, "⏳ <b>Too many requests — slow down!</b>")
        except Exception as exc:
            logger.warning("Could not send rate-limit message to %s: %s", user_id, exc)
        return True
    return False


def guard_message(message: Message) -> bool:
    """
    Run ban and rate-limit checks for a message handler.

    Args:
        message: Incoming Telegram message.

    Returns:
        True if the handler should abort, False if it may proceed.
    """
    uid = message.from_user.id
    cid = message.chat.id
    return check_banned(uid, cid) or check_rate_limit(uid, cid)


def guard_callback(call: CallbackQuery) -> bool:
    """
    Run ban and rate-limit checks for a callback handler.

    Args:
        call: Incoming callback query.

    Returns:
        True if the handler should abort, False if it may proceed.
    """
    uid = call.from_user.id
    cid = call.message.chat.id
    if check_banned(uid, cid):
        try:
            bot.answer_callback_query(call.id, "🚫 You are banned.", show_alert=True)
        except Exception:
            pass
        return True
    if check_rate_limit(uid, cid):
        try:
            bot.answer_callback_query(call.id, "⏳ Slow down!", show_alert=True)
        except Exception:
            pass
        return True
    return False
