"""
core/analytics.py — Resource usage tracking for running projects.

UsageTracker keeps in-memory stats and can snapshot them to MongoDB.
All psutil calls use interval=None (non-blocking cached values) after
the first priming call to avoid blocking the monitoring thread.
"""
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import psutil

logger = logging.getLogger(__name__)


class UsageTracker:
    """Track CPU and memory usage for multiple running projects."""

    def __init__(self) -> None:
        self.tracking_data: Dict[str, dict] = {}

    def start_tracking(self, project_id: str, pid: int) -> None:
        """
        Begin tracking a project.

        Args:
            project_id: Project identifier.
            pid: OS process ID.
        """
        # Prime the cpu_percent counter so subsequent interval=None calls work.
        try:
            psutil.Process(pid).cpu_percent(interval=0.05)
        except Exception:
            pass

        self.tracking_data[project_id] = {
            "pid": pid,
            "start_time": datetime.now(timezone.utc),
            "cpu_seconds": 0.0,
            "last_cpu_check": time.monotonic(),
            "memory_mb_seconds": 0.0,
            "last_memory_mb": 0.0,
            "samples": 0,
        }

    def stop_tracking(self, project_id: str) -> Optional[dict]:
        """
        Stop tracking a project and return final stats.

        Args:
            project_id: Project to stop tracking.

        Returns:
            Stats dict or None if not tracked.
        """
        if project_id not in self.tracking_data:
            return None
        self.update_usage(project_id)
        data = self.tracking_data.pop(project_id)
        uptime = (datetime.now(timezone.utc) - data["start_time"]).total_seconds()
        return {
            "uptime_seconds": int(uptime),
            "cpu_hours": data["cpu_seconds"] / 3600,
            "avg_memory_mb": data["memory_mb_seconds"] / uptime if uptime > 0 else 0,
            "samples": data["samples"],
        }

    def update_usage(self, project_id: str) -> None:
        """
        Refresh usage stats for one project.

        Args:
            project_id: Project to update.
        """
        if project_id not in self.tracking_data:
            return
        data = self.tracking_data[project_id]
        try:
            proc = psutil.Process(data["pid"])
            cpu = proc.cpu_percent(interval=None)  # non-blocking
            now = time.monotonic()
            delta = now - data["last_cpu_check"]
            data["cpu_seconds"] += (cpu / 100) * delta
            data["last_cpu_check"] = now

            mem_mb = proc.memory_info().rss / (1024 * 1024)
            data["memory_mb_seconds"] += mem_mb * delta
            data["last_memory_mb"] = mem_mb
            data["samples"] += 1
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:
            logger.debug("Usage tracking error for %s: %s", project_id, exc)

    def get_current_stats(self, project_id: str) -> Optional[dict]:
        """
        Return current usage snapshot for a project.

        Args:
            project_id: Project to inspect.

        Returns:
            Dict with uptime_seconds, cpu_hours, current_memory_mb, avg_memory_mb,
            or None if not tracked.
        """
        if project_id not in self.tracking_data:
            return None
        self.update_usage(project_id)
        data = self.tracking_data[project_id]
        uptime = (datetime.now(timezone.utc) - data["start_time"]).total_seconds()
        return {
            "uptime_seconds": int(uptime),
            "cpu_hours": data["cpu_seconds"] / 3600,
            "current_memory_mb": data["last_memory_mb"],
            "avg_memory_mb": data["memory_mb_seconds"] / uptime if uptime > 0 else 0,
        }

    def update_all(self) -> None:
        """Refresh stats for all tracked projects. Skips if none are tracked."""
        if not self.tracking_data:
            return
        for project_id in list(self.tracking_data.keys()):
            self.update_usage(project_id)


def get_storage_usage(project_dir: Path) -> dict:
    """
    Calculate disk usage for a project directory.

    Excludes venv/, __pycache__/, and .git/.

    Args:
        project_dir: Root of the project.

    Returns:
        Dict with total_bytes, total_mb, file_count, size_str.
    """
    _EXCLUDE = {"venv", "__pycache__", ".git", "node_modules"}
    total_bytes = 0
    file_count = 0
    try:
        for fp in project_dir.rglob("*"):
            if any(ex in fp.parts for ex in _EXCLUDE):
                continue
            if fp.is_file():
                total_bytes += fp.stat().st_size
                file_count += 1
    except Exception as exc:
        logger.warning("Storage calc error for %s: %s", project_dir, exc)

    size_mb = total_bytes / (1024 * 1024)
    size_str = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"
    return {"total_bytes": total_bytes, "total_mb": size_mb, "file_count": file_count, "size_str": size_str}


# Global singleton
usage_tracker = UsageTracker()
