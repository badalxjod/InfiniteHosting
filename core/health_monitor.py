"""
core/health_monitor.py — Process health monitor with auto-restart.

The monitor runs on a daemon thread. Each individual project check is
wrapped in its own try/except so one failing project never kills monitoring
for all others. The outer loop is also protected to prevent thread death.
"""
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

import psutil

logger = logging.getLogger(__name__)


class HealthMonitor:
    """
    Monitor running project processes and trigger auto-restarts on crash.

    Attributes:
        check_interval: Seconds between health-check sweeps.
    """

    def __init__(self, check_interval: int = 30) -> None:
        """
        Initialize the health monitor.

        Args:
            check_interval: Seconds between full sweep cycles.
        """
        self.check_interval = check_interval
        self._projects: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.running = False
        self._thread: threading.Thread | None = None
        self.restart_callback: Callable | None = None
        self.alert_callback: Callable | None = None

    # ── Callback setters ─────────────────────────────────────────────────────

    def set_restart_callback(self, cb: Callable) -> None:
        """
        Register the function called to restart a crashed project.

        Args:
            cb: Callable that accepts project_id and returns bool.
        """
        self.restart_callback = cb

    def set_alert_callback(self, cb: Callable) -> None:
        """
        Register the function called to create an alert.

        Args:
            cb: Callable(user_id, project_id, alert_type, message).
        """
        self.alert_callback = cb

    # ── Project registration ─────────────────────────────────────────────────

    def add_project(
        self,
        project_id: str,
        pid: int,
        user_id: int,
        project_name: str,
        auto_restart: bool = True,
        max_restarts: int = 5,
    ) -> None:
        """
        Register a project for monitoring.

        Args:
            project_id: Unique project identifier.
            pid: OS process ID.
            user_id: Owner Telegram user ID.
            project_name: Human-readable name for alerts.
            auto_restart: Whether to auto-restart on crash.
            max_restarts: Maximum auto-restart attempts.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            self._projects[project_id] = {
                "pid": pid,
                "user_id": user_id,
                "project_name": project_name,
                "auto_restart": auto_restart,
                "max_restarts": max_restarts,
                "restart_count": 0,
                "last_check": now,
                "start_time": now,
                "cpu_alerts": 0,
                "memory_alerts": 0,
            }
        logger.debug("Added project to monitor: %s (pid=%s)", project_id, pid)

    def remove_project(self, project_id: str) -> None:
        """
        Unregister a project from monitoring.

        Args:
            project_id: Project to remove.
        """
        with self._lock:
            self._projects.pop(project_id, None)

    def update_pid(self, project_id: str, new_pid: int) -> None:
        """
        Update the PID after a restart.

        Args:
            project_id: Project to update.
            new_pid: New process ID.
        """
        with self._lock:
            if project_id in self._projects:
                self._projects[project_id]["pid"] = new_pid
                self._projects[project_id]["restart_count"] += 1
                self._projects[project_id]["start_time"] = datetime.now(timezone.utc)

    # ── Health checking ──────────────────────────────────────────────────────

    def _check_single_project(self, project_id: str, data: dict) -> None:
        """
        Run health checks for one project and handle crashes.

        Args:
            project_id: Project identifier.
            data: Monitoring metadata dict (mutated in place).
        """
        pid = data["pid"]
        is_healthy = False

        try:
            proc = psutil.Process(pid)

            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                is_healthy = False
            else:
                is_healthy = True

                # CPU alert
                cpu = proc.cpu_percent(interval=None)
                if cpu > 90:
                    data["cpu_alerts"] += 1
                    if data["cpu_alerts"] >= 3 and self.alert_callback:
                        self.alert_callback(
                            data["user_id"], project_id, "HIGH_CPU",
                            f"⚠️ High CPU: {cpu:.1f}%",
                        )
                        data["cpu_alerts"] = 0
                else:
                    data["cpu_alerts"] = 0

                # Memory alert
                mem_pct = proc.memory_percent()
                mem_mb = proc.memory_info().rss / (1024 * 1024)
                if mem_pct > 80:
                    data["memory_alerts"] += 1
                    if data["memory_alerts"] >= 3 and self.alert_callback:
                        self.alert_callback(
                            data["user_id"], project_id, "HIGH_MEMORY",
                            f"⚠️ High memory: {mem_mb:.1f} MB ({mem_pct:.1f}%)",
                        )
                        data["memory_alerts"] = 0
                else:
                    data["memory_alerts"] = 0

                data["last_check"] = datetime.now(timezone.utc)

        except psutil.NoSuchProcess:
            is_healthy = False
        except Exception as exc:
            logger.error("Health check error for %s: %s", project_id, exc, exc_info=True)
            return  # Don't treat check errors as crashes

        if not is_healthy:
            logger.warning("Crash detected: %s (pid=%s)", project_id, pid)
            if self.alert_callback:
                self.alert_callback(data["user_id"], project_id, "CRASH", "💥 Process crashed!")

            if data["auto_restart"] and data["restart_count"] < data["max_restarts"]:
                logger.info("Auto-restarting %s (attempt %d/%d)",
                            project_id, data["restart_count"] + 1, data["max_restarts"])
                if self.restart_callback:
                    try:
                        success = self.restart_callback(project_id)
                        if success:
                            logger.info("Auto-restart succeeded: %s", project_id)
                        else:
                            logger.warning("Auto-restart failed: %s", project_id)
                            self.remove_project(project_id)
                    except Exception as exc:
                        logger.error("restart_callback raised: %s: %s", project_id, exc, exc_info=True)
                        self.remove_project(project_id)
            else:
                logger.warning("Max restarts reached or auto-restart off for %s", project_id)
                self.remove_project(project_id)

    def _monitor_loop(self) -> None:
        """Main monitoring loop. Runs on its own daemon thread."""
        logger.info("Health monitor thread started.")
        while self.running:
            try:
                with self._lock:
                    snapshot = list(self._projects.items())
                for project_id, data in snapshot:
                    try:
                        self._check_single_project(project_id, data)
                    except Exception as exc:
                        logger.error("Unhandled error checking %s: %s", project_id, exc, exc_info=True)
            except Exception as exc:
                logger.critical("Health monitor outer loop error: %s", exc, exc_info=True)
            time.sleep(self.check_interval)
        logger.info("Health monitor thread stopped.")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background monitoring thread."""
        if not self.running:
            self.running = True
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="HealthMonitor")
            self._thread.start()
            logger.info("Health monitor started.")

    def stop(self) -> None:
        """Stop the background monitoring thread."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Health monitor stopped.")

    # ── Stats ────────────────────────────────────────────────────────────────

    def get_project_stats(self, project_id: str) -> dict | None:
        """
        Return live stats for a monitored project.

        Args:
            project_id: Project to inspect.

        Returns:
            Dict with pid, cpu_percent, memory_mb, uptime_seconds, restart_count,
            and status; or None if not monitored.
        """
        with self._lock:
            data = self._projects.get(project_id)
        if not data:
            return None
        pid = data["pid"]
        try:
            proc = psutil.Process(pid)
            cpu = proc.cpu_percent(interval=None)
            mem_mb = proc.memory_info().rss / (1024 * 1024)
            uptime = int((datetime.now(timezone.utc) - data["start_time"]).total_seconds())
            return {
                "pid": pid,
                "cpu_percent": cpu,
                "memory_mb": mem_mb,
                "uptime_seconds": uptime,
                "restart_count": data["restart_count"],
                "status": "running",
            }
        except psutil.NoSuchProcess:
            return {"status": "dead"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}


# Global singleton
health_monitor = HealthMonitor(check_interval=30)
