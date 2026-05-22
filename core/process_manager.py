"""
core/process_manager.py — Safe process lifecycle management.

Key security guarantees:
- NEVER uses shell=True with user-provided data.
- run_cmd is validated against an allowlist of safe prefixes.
- Commands are split via shlex.split() before being passed to Popen.
- ZIP extraction uses safe_extract() to prevent path traversal (ZIP Slip).
"""
import logging
import shlex
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

# Only these command prefixes are permitted as run commands.
ALLOWED_PREFIXES = ("python3 ", "python ", "node ", "npm start", "npm run ")

MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB


# ── ZIP helpers ───────────────────────────────────────────────────────────────

def safe_extract(zip_ref: zipfile.ZipFile, target_dir: Path) -> None:
    """
    Extract a ZIP archive, blocking path traversal attempts.

    Args:
        zip_ref: Open ZipFile object.
        target_dir: Directory to extract into.

    Raises:
        ValueError: If any entry tries to escape target_dir.
    """
    resolved_target = target_dir.resolve()
    for member in zip_ref.namelist():
        member_path = (resolved_target / member).resolve()
        if not str(member_path).startswith(str(resolved_target)):
            raise ValueError(f"Path traversal attempt blocked: {member!r}")
        zip_ref.extract(member, resolved_target)


# ── Logging helpers ───────────────────────────────────────────────────────────

def _append_log(project_dir: Path, filename: str, text: str) -> None:
    """
    Append a timestamped entry to a log file.

    Args:
        project_dir: Project root directory.
        filename: Log file name (e.g. "project.log").
        text: Text to append.
    """
    log_file = project_dir / filename
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}]\n{text}\n\n")
    except Exception as exc:
        logger.warning("Could not write to %s: %s", log_file, exc)


# ── Venv helpers ──────────────────────────────────────────────────────────────

def _setup_venv(project_dir: Path) -> Path | None:
    """
    Create a virtual environment inside project_dir if absent.

    Args:
        project_dir: Project root.

    Returns:
        Path to venv directory, or None on failure.
    """
    venv_dir = project_dir / "venv"
    if venv_dir.exists():
        return venv_dir
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        return venv_dir
    except Exception as exc:
        _append_log(project_dir, "error.log", f"Failed to create venv: {exc}")
        return None


def _get_python_path(project_dir: Path) -> str:
    """
    Return the path to the Python executable inside the project venv if available.

    Args:
        project_dir: Project root.

    Returns:
        Absolute path string or "python3" fallback.
    """
    venv_python = project_dir / "venv" / "bin" / "python3"
    if venv_python.exists():
        return str(venv_python)
    return "python3"


# ── Process kill ─────────────────────────────────────────────────────────────

def kill_process_tree(pid: int) -> bool:
    """
    Kill a process and all its children.

    Args:
        pid: PID of the root process.

    Returns:
        True on success (including if already dead), False on unexpected error.
    """
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            parent.kill()
        except psutil.NoSuchProcess:
            pass
        gone, alive = psutil.wait_procs(children + [parent], timeout=3)
        for proc in alive:
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
        return True
    except psutil.NoSuchProcess:
        return True
    except Exception as exc:
        logger.error("Error killing process tree (pid=%s): %s", pid, exc)
        return False


# ── Entry file detection ──────────────────────────────────────────────────────

_SKIP_DIRS = {"venv", "__pycache__", ".git", "node_modules"}
_ENTRY_PATTERNS = ["main.py", "app.py", "bot.py", "run.py", "index.py", "index.js", "app.js"]


def detect_entry_file(project_dir: Path) -> str | None:
    """
    Find the most likely entry file in a project directory.

    Checks the root first, then subdirectories, skipping venv/__pycache__/.git.

    Args:
        project_dir: Project root.

    Returns:
        Relative path string (e.g. "main.py") or None if not found.
    """
    # Check root first
    for pattern in _ENTRY_PATTERNS:
        candidate = project_dir / pattern
        if candidate.is_file():
            return pattern

    # Recurse, skipping junk directories
    for pattern in _ENTRY_PATTERNS:
        for candidate in project_dir.rglob(pattern):
            # Skip paths that contain excluded directory names
            parts = set(candidate.relative_to(project_dir).parts[:-1])
            if parts & _SKIP_DIRS:
                continue
            return str(candidate.relative_to(project_dir))

    return None


# ── Public API ────────────────────────────────────────────────────────────────

def start_project(meta: dict) -> tuple[bool, str, int | None]:
    """
    Start a project process safely (no shell=True).

    Args:
        meta: Project document from DB (must have project_dir and entry).

    Returns:
        (success, message, pid) tuple.
    """
    project_dir = Path(meta["project_dir"])
    entry = meta.get("entry")

    if not entry or not (project_dir / entry).is_file():
        return False, "Entry file not found. Please upload your main file.", None

    _setup_venv(project_dir)
    python_cmd = _get_python_path(project_dir)

    run_cmd = meta.get("run_cmd") or f"python3 {entry}"

    # Security: validate run_cmd prefix
    if not any(run_cmd.startswith(p) for p in ALLOWED_PREFIXES):
        return False, (
            "❌ Invalid run command. Must start with: python3, python, node, npm start, or npm run."
        ), None

    # Replace generic "python3" with venv python if available
    if run_cmd.startswith("python3 ") and python_cmd != "python3":
        run_cmd = python_cmd + run_cmd[7:]

    try:
        cmd_parts = shlex.split(run_cmd)
    except ValueError as exc:
        return False, f"❌ Invalid command syntax: {exc}", None

    log_file = open(project_dir / "project.log", "a")
    err_file = open(project_dir / "error.log", "a")

    try:
        proc = subprocess.Popen(
            cmd_parts,
            cwd=str(project_dir),
            stdout=log_file,
            stderr=err_file,
            start_new_session=True,
        )
    except Exception as exc:
        _append_log(project_dir, "error.log", f"Exception during Popen: {exc}")
        return False, f"❌ Could not start process: {exc}", None
    finally:
        log_file.close()
        err_file.close()

    # Brief wait (in background — caller runs this in a thread)
    time.sleep(0.5)

    try:
        p = psutil.Process(proc.pid)
        if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
            _append_log(project_dir, "project.log", f"Started PID {proc.pid} | cmd: {run_cmd}")
            return True, f"✅ Started with PID {proc.pid}", proc.pid
        else:
            _append_log(project_dir, "error.log", f"Process {proc.pid} died immediately")
            return False, "Process started but died immediately. Check error.log.", None
    except psutil.NoSuchProcess:
        _append_log(project_dir, "error.log", f"Process {proc.pid} not found after start")
        return False, "Process died immediately. Check error.log.", None


def stop_project(meta: dict) -> tuple[bool, str]:
    """
    Stop a running project process.

    Args:
        meta: Project document from DB (must have pid and project_dir).

    Returns:
        (success, message) tuple.
    """
    project_dir = Path(meta["project_dir"])
    pid = meta.get("pid")

    if not pid:
        return False, "No PID stored. Project may not be running."

    try:
        psutil.Process(pid)
    except psutil.NoSuchProcess:
        _append_log(project_dir, "project.log", f"Process {pid} already stopped")
        return True, "Process already stopped."

    success = kill_process_tree(pid)
    if success:
        _append_log(project_dir, "project.log", f"Process {pid} stopped")
        return True, "✅ Process stopped successfully."
    _append_log(project_dir, "error.log", f"Failed to stop process {pid}")
    return False, "❌ Failed to stop process."


def restart_project(meta: dict) -> tuple[bool, str, int | None]:
    """
    Stop then start a project.

    Args:
        meta: Project document from DB.

    Returns:
        (success, message, new_pid) tuple.
    """
    project_dir = Path(meta["project_dir"])

    if meta.get("pid"):
        ok, msg = stop_project(meta)
        if not ok:
            _append_log(project_dir, "error.log", f"Restart failed at stop: {msg}")
            return False, f"Failed to stop: {msg}", None
        time.sleep(0.5)

    return start_project(meta)


def install_requirements(meta: dict) -> tuple[bool, str]:
    """
    Install Python dependencies from requirements.txt inside the project venv.

    Runs synchronously — callers MUST invoke this in a background thread.

    Args:
        meta: Project document from DB.

    Returns:
        (success, message) tuple.
    """
    project_dir = Path(meta["project_dir"])
    requirements = meta.get("requirements")

    if not requirements:
        return False, "No requirements.txt file specified."

    req_path = project_dir / requirements
    if not req_path.exists():
        return False, "requirements.txt not found."

    venv_dir = _setup_venv(project_dir)

    if venv_dir:
        pip_exec = str(venv_dir / "bin" / "pip")
        cmd = [pip_exec, "install", "-r", str(req_path)]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--break-system-packages", "-r", str(req_path)]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
            cwd=str(project_dir),
        )
        _append_log(project_dir, "deps.log", f"Command: {' '.join(cmd)}\n\nOutput:\n{result.stdout}")

        if result.returncode != 0:
            _append_log(project_dir, "error.log", "Dependency installation failed")
            return False, "❌ Installation failed. Check deps.log for details."

        return True, "✅ Dependencies installed. Check deps.log for details."

    except subprocess.TimeoutExpired:
        _append_log(project_dir, "error.log", "pip install timed out")
        return False, "❌ Installation timed out (>5 minutes)."
    except Exception as exc:
        _append_log(project_dir, "error.log", f"Exception during install: {exc}")
        return False, f"❌ Error: {exc}"


def get_usage(pid: int) -> str | None:
    """
    Fetch resource usage for a running process.

    Args:
        pid: PID to inspect.

    Returns:
        Formatted string with CPU/memory/uptime info, or None if unavailable.
    """
    if not pid:
        return None
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        cpu = proc.cpu_percent(interval=None)
        mem_mb = proc.memory_info().rss / (1024 * 1024)
        create_time = datetime.fromtimestamp(proc.create_time())
        uptime = datetime.now() - create_time
        days = uptime.days
        hours, rem = divmod(uptime.seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if days > 0:
            uptime_str = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            uptime_str = f"{hours}h {minutes}m {secs}s"
        else:
            uptime_str = f"{minutes}m {secs}s"

        return (
            f"PID: {pid}\n"
            f"Status: {proc.status()}\n"
            f"CPU: {cpu:.1f}%\n"
            f"Memory: {mem_mb:.1f} MB\n"
            f"Uptime: {uptime_str}"
        )
    except psutil.NoSuchProcess:
        return None
    except Exception as exc:
        return f"Error reading usage: {exc}"
