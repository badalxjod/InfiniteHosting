"""
db/connection.py — MongoDB connection management.

Uses lazy initialization so importing this module never crashes
even if MongoDB is temporarily unreachable at import time.
Call get_db() to obtain the database handle; startup ping is
done explicitly in main() to fail fast with a clear message.
"""
import logging
from pymongo import MongoClient
from pymongo.database import Database

from config import MONGO_URI, DB_NAME

logger = logging.getLogger(__name__)

_client: MongoClient | None = None
_db: Database | None = None


def get_client() -> MongoClient:
    """Return (and lazily create) the MongoClient singleton."""
    global _client
    if _client is None:
        _client = MongoClient(
            MONGO_URI,
            maxPoolSize=10,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            retryWrites=True,
        )
        logger.debug("MongoClient created.")
    return _client


def get_db() -> Database:
    """Return the application database handle."""
    global _db
    if _db is None:
        _db = get_client()[DB_NAME]
    return _db


def ping() -> bool:
    """Ping MongoDB. Returns True on success, raises on failure."""
    get_client().admin.command("ping")
    return True
