# HostingBot

A Telegram bot for hosting Python and Node.js projects 24/7, with auto-restart, health monitoring, environment variable management, and an admin panel.

---

## Quick Start

### 1. Clone and configure

```bash
git clone <your-repo>
cd HostingBot
cp .env.example .env
# Edit .env and fill in BOT_TOKEN and MONGO_URI
```

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Run

```bash
python3 main.py
# or
bash scripts/run.sh
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram Bot API token |
| `MONGO_URI` | ✅ | MongoDB connection string |
| `OWNER_IDS` | recommended | Comma-separated admin user IDs |
| `BACKUP_CHANNEL_ID` | optional | Channel ID for project backups |
| `DEVELOPER_LINK` | optional | Your Telegram link (branding) |
| `CHANNEL_LINK` | optional | Updates channel link (branding) |

---

## Production Deployment (systemd)

```bash
# Copy and edit the service file
sudo cp scripts/hostingbot.service /etc/systemd/system/
sudo nano /etc/systemd/system/hostingbot.service  # update paths and user

sudo systemctl daemon-reload
sudo systemctl enable hostingbot
sudo systemctl start hostingbot
sudo journalctl -u hostingbot -f  # live logs
```

---

## Security Notes

- **Never commit `.env`** — it is in `.gitignore`.
- User-provided run commands are validated against an allowlist (`python3`, `python`, `node`, `npm start`, `npm run`).
- ZIP uploads are protected against path traversal (ZIP Slip).
- Environment variable values are never shown in plaintext in chat.
- Per-user rate limiting: 5 requests per 10 seconds.

---

## Project Structure

```
HostingBot/
├── bot/
│   ├── instance.py        # TeleBot singleton
│   ├── rate_limiter.py    # Per-user rate limiting
│   └── middlewares.py     # Ban + rate-limit guards
├── core/
│   ├── process_manager.py # Start/stop/restart/install (no shell injection)
│   ├── health_monitor.py  # Background crash detection + auto-restart
│   └── analytics.py       # CPU/memory/storage tracking
├── db/
│   ├── connection.py      # Lazy MongoDB client singleton
│   ├── models.py          # All DB operations + indexes
│   └── state_manager.py   # TTL-backed user state (survives restarts)
├── handlers/
│   ├── user.py            # User commands and project management
│   ├── admin.py           # Admin panel
│   └── features.py        # Dashboard, analytics, env vars
├── utils/
│   ├── keyboards.py       # All InlineKeyboardMarkup builders
│   ├── env_manager.py     # .env file read/write
│   └── helpers.py         # format_uptime, safe_html, schedule_delete
├── scripts/
│   ├── run.sh             # Launcher script
│   └── hostingbot.service # systemd unit
├── config.py              # Env-only config loader
├── main.py                # Entry point
├── requirements.txt
├── .env.example
└── CHANGES.md             # Audit fix log
```
