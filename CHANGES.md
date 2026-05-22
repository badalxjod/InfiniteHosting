# HostingBot — Refactor Changelog

This document maps every issue from the audit report to its fix in the refactored codebase.

---

## 🔴 Critical Fixes

### 1. Hardcoded credentials removed (`config.py`)
**Before:** `BOT_TOKEN`, `MONGO_URI`, admin IDs were hardcoded strings in source code.  
**After:** All secrets loaded exclusively from environment variables via `python-dotenv`. Bot crashes immediately with a descriptive error if required vars are missing. `.env.example` provided as template.

### 2. Shell injection eliminated (`core/process_manager.py`)
**Before:** `subprocess.Popen(run_cmd, shell=True)` — user-provided `run_cmd` was passed directly to shell.  
**After:** `shell=False` (default). Commands are split with `shlex.split()`. An allowlist of permitted prefixes (`python3`, `python`, `node`, `npm start`, `npm run`) is validated before execution.

### 3. ZIP path traversal blocked (`core/process_manager.py`)
**Before:** `zipfile.ZipFile.extractall()` used without validation — ZIP Slip attack possible.  
**After:** `safe_extract()` resolves every member path and aborts with `ValueError` if any entry escapes the target directory.

### 4. MongoDB TTL index for state (`db/state_manager.py`)
**Before:** User states stored as a plain in-memory `dict` — lost on restart, leaked forever if never cleared.  
**After:** States stored in MongoDB `user_states` collection with a TTL index (`expires_at`). States auto-expire after 30 minutes. Survives restarts.

---

## 🟠 High-Priority Fixes

### 5. N+1 query eliminated (`db/models.py`, `handlers/admin.py`)
**Before:** Admin user list loop called `get_user_projects()` once per user → N+1 queries.  
**After:** `get_user_project_counts()` uses a single `$group` aggregation pipeline to fetch all counts in one query.

### 6. Race condition on restart_count fixed (`db/models.py`)
**Before:** `restart_count += 1` read-modify-write pattern with two separate DB calls.  
**After:** `increment_restart_count()` uses MongoDB `$inc` operator — atomic, no stale-read race.

### 7. File size validation added (`handlers/user.py`, `utils/env_manager.py`)
**Before:** No size checks on uploaded files.  
**After:** 15 MB hard limit on project files; 50 KB limit on `.env` uploads; 100-variable cap enforced in `set_env_var()`.

### 8. Secrets never shown in plaintext (`handlers/features.py`, `utils/env_manager.py`)
**Before:** Environment variables listed inline in chat messages.  
**After:** `env_list` sends masked preview (auto-deleted after 30 seconds) plus a downloadable `.env.txt` file. Raw values never appear in chat.

### 9. Individual health check errors isolated (`core/health_monitor.py`)
**Before:** Exception in one project's health check could kill the monitor thread for all projects.  
**After:** Each project's check runs inside its own `try/except`. Outer loop also protected. Thread never dies from a single project's error.

---

## 🟡 Medium-Priority Fixes

### 10. Rate limiting added (`bot/rate_limiter.py`, `bot/middlewares.py`)
**Before:** No rate limiting — users could spam the bot freely.  
**After:** `RateLimiter` class enforces 5 calls per 10 seconds per user. Applied via `guard_message()` / `guard_callback()` at the start of every handler.

### 11. MongoDB connection pooling (`db/connection.py`)
**Before:** New `MongoClient` created on every operation (up to hundreds of connections).  
**After:** Single `MongoClient` singleton with `maxPoolSize=10`, retry writes, and startup ping to fail fast.

### 12. Non-blocking CPU measurement (`core/analytics.py`)
**Before:** `cpu_percent(interval=1)` — blocked for 1 second per call, freezing the monitoring thread.  
**After:** `cpu_percent(interval=None)` (non-blocking cached value) after a one-time priming call with `interval=0.05`.

### 13. Structured project IDs (`db/models.py`)
**Before:** `project_id` used raw timestamp without sanitisation.  
**After:** ID constructed as `{user_id}_{timestamp}_{slug}` where slug is alphanumeric-only, max 20 chars.

### 14. Startup MongoDB ping (`main.py`)
**Before:** Connection errors only surfaced during handler execution.  
**After:** `ping()` is called in `main()` before polling starts. Clear error message + `sys.exit()` on failure.

### 15. Database indexes created once on startup (`db/models.py`)
**Before:** No indexes — every query did a full collection scan.  
**After:** `_ensure_indexes()` creates unique indexes on `user_id`, `project_id`, and TTL index on `expires_at`.

### 16. Long-running operations run in daemon threads (`handlers/user.py`, `handlers/admin.py`)
**Before:** start/stop/restart/install ran on the polling thread, blocking all other updates.  
**After:** All long-running operations use `threading.Thread(daemon=True)` and update the message on completion.

### 17. Auto-delete for sensitive messages (`utils/helpers.py`)
**Before:** No cleanup of sensitive messages in chat.  
**After:** `schedule_delete()` helper schedules message deletion via a daemon thread. Used for env var listings.

---

## 🔵 Low-Priority / Architecture

### 18. Package structure reorganised
```
HostingBot/
├── bot/           # TeleBot instance, rate limiter, middlewares
├── core/          # Process manager, health monitor, analytics
├── db/            # Connection, models, state manager
├── handlers/      # user, admin, features
├── utils/         # keyboards, env_manager, helpers
├── scripts/       # run.sh, systemd unit
├── config.py      # Env-only config loader
└── main.py        # Entry point
```

### 19. Comprehensive docstrings
Every function has a Google-style docstring with Args and Returns.

### 20. `.gitignore` and `.env.example` added
Prevents accidental secret commits.

### 21. Systemd unit file (`scripts/hostingbot.service`)
Production-ready service definition with restart policy and journald logging.
