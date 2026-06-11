# Deployment

## Docker (Recommended)

The bot ships with a production-ready `Dockerfile` and `docker-compose.yml`.

### First Deploy

```bash
# 1. Clone the repo on your server
git clone git@github.com:sondt99/ctf-bot.git
cd ctf-bot

# 2. Configure
cp .env.example .env
# Edit .env — required: DISCORD_TOKEN
# Recommended: FERNET_KEY (encrypt auth tokens at rest)

# 3. Start
docker compose up -d

# 4. Check logs
docker compose logs -f bot
```

### Upgrading

```bash
git pull
docker compose build --pull && docker compose up -d
```

The SQLite database is stored in a named Docker volume (`bot_data`) and is **not** removed during upgrades. Migrations run automatically on startup.

### Generating a Fernet Key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the output as `FERNET_KEY` in `.env` **before** the first run. If you add `FERNET_KEY` to an existing deployment, existing plaintext tokens remain readable (the bot falls back to plaintext on decryption failure).

---

## Bare Metal / Systemd

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create a systemd service
sudo tee /etc/systemd/system/ctf-bot.service <<'EOF'
[Unit]
Description=CTF Discord Bot
After=network.target

[Service]
Type=simple
User=ctfbot
WorkingDirectory=/opt/ctf-bot
EnvironmentFile=/opt/ctf-bot/.env
ExecStart=/opt/ctf-bot/.venv/bin/python -m bot.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ctf-bot
```

---

## Environment Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Discord bot token |
| `DISCORD_GUILD_ID` | No | — | Guild ID for faster slash command sync |
| `DATABASE_PATH` | No | `ctf_bot.db` | Path to SQLite database file |
| `FERNET_KEY` | No | — | Fernet key for auth token encryption at rest |
| `TIMEZONE` | No | `UTC+7` | IANA name (`Asia/Ho_Chi_Minh`) or offset (`UTC+7`) |
| `SCOREBOARD_POLL_SECONDS` | No | `30` | Scoreboard polling interval (seconds) |
| `SCOREBOARD_TOP_N` | No | `10` | Teams shown in scoreboard updates |
| `SCOREBOARD_TEAM_NAME` | No | — | Your team name for scoreboard highlighting |
| `CTF_REMOVE_PASSWORD` | No | — | Password required by `/ctf remove` (leave empty to disable) |
| `CTFD_POLL_INTERVAL_MINUTES` | No | `0` | Auto-poll CTFd for new challenges (0 = disabled) |
| `AUTO_BACKUP_INTERVAL_HOURS` | No | `0` | Auto-post DB backup to `#backup` channel (0 = disabled) |

---

## Discord Bot Setup

1. Go to [Discord Developer Portal](https://discord.com/developers/applications) → New Application
2. Bot → Reset Token → copy `DISCORD_TOKEN`
3. Bot → Privileged Gateway Intents → enable **Server Members Intent** and **Message Content Intent**
4. OAuth2 → URL Generator → scopes: `bot`, `applications.commands`
5. Bot Permissions: `Manage Channels`, `Manage Roles`, `Create Public Threads`, `Send Messages`, `Embed Links`, `Read Message History`
6. Copy the generated URL and invite the bot to your server

---

## Backups

Run `/backup` in any admin channel to post the current database as a file attachment in the `#backup` channel (created automatically in the private `BOT` category).

Set `AUTO_BACKUP_INTERVAL_HOURS` to post backups on a schedule (e.g. `24` for daily).
