"""
bot/instance.py — Single TeleBot instance used across all handlers.

threaded=False keeps polling single-threaded so long-running operations
must be moved to daemon threads (see handlers for examples).
"""
import logging

from telebot import TeleBot

from config import BOT_TOKEN

logger = logging.getLogger(__name__)

bot = TeleBot(
    BOT_TOKEN,
    parse_mode="HTML",
    threaded=False,
    num_threads=1,
)
