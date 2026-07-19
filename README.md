# Discord CTF Bot

A Discord bot for organizing Capture The Flag competitions. It integrates with CTFtime to fetch events, creates per-event categories and channels, tracks challenges with threads, and polls live scoreboards for CTFd and rCTF platforms.

## Documentation

- [Architecture](docs/architecture.md) — component overview and data flow
- [Database](docs/database.md) — schema, tables, migrations, repository API
- [Development](docs/development.md) — local setup, testing, type checking
- [Deployment](docs/deployment.md) — Docker, systemd, environment reference

## Features

- **CTFtime integration** — browse and join upcoming CTF events with pagination
- **Challenge management** — create threads per challenge, bulk-import CTFd challenges, track solved/open status
- **Live scoreboard** — periodic polling with change notifications for CTFd and rCTF
- **Message statistics** — per-user leaderboard, activity breakdown, and historical backfill
- **Multi-event support** — run multiple CTFs simultaneously, each with its own category
- **Role-based access** — `@ctf` role members can mark challenges as solved; admin commands remain admin-only
- **Audit logging** — automatic private `BOT` category with command logs and manual database backups

## Requirements

- Python 3.11+
- Discord bot token with the [required intents](#permissions)

## Setup

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env and set DISCORD_TOKEN

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the bot
python -m bot.main
```

## Docker Deployment

```bash
# 1. Copy and edit the env file
cp .env.example .env
# Edit .env: set DISCORD_TOKEN, optionally FERNET_KEY, etc.

# 2. Start the bot
docker compose up -d

# 3. View logs
docker compose logs -f bot

# 4. Upgrade (after git pull)
docker compose build --pull && docker compose up -d
```

The SQLite database is stored in a named Docker volume (`bot_data`) so it persists across restarts and container upgrades.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Discord bot token |
| `DATABASE_PATH` | No | `ctf_bot.db` | Path to SQLite database file |
| `SCOREBOARD_POLL_SECONDS` | No | `30` | Scoreboard polling interval (seconds) |
| `SCOREBOARD_TOP_N` | No | `10` | Number of teams shown in scoreboard updates |
| `SCOREBOARD_TEAM_NAME` | No | — | Your team name (for scoreboard tracking) |
| `TIMEZONE` | No | `UTC+7` | Timezone for event display — IANA name (`Asia/Ho_Chi_Minh`) or offset (`UTC+7`) |
| `CTF_REMOVE_PASSWORD` | No | — | Password required by `/ctf remove` |
| `DISCORD_GUILD_ID` | No | — | Guild ID for faster slash command sync |
| `FERNET_KEY` | No | — | Fernet key to encrypt auth tokens at rest (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) |

## Commands

### CTF Events

| Command | Description | Permission |
|---|---|---|
| `/ctf upcoming [limit]` | Browse upcoming CTFs from CTFtime | Everyone |
| `/ctf running [limit]` | List currently active CTFs from CTFtime | Everyone |
| `/ctf archive [limit] [days]` | List recently ended CTFs (default: last 30 days) | Everyone |
| `/ctf join <event_id>` | Create category and channels for an event (auto-creates `@ctf` role) | Admin |
| `/ctf list` | List joined CTFs and their event IDs | Everyone |
| `/ctf progress [event_id]` | Show challenge progress with per-category breakdown | Everyone |
| `/ctf export [event_id] [format]` | Export challenge data as JSON or CSV | Everyone |
| `/ctf hidden [event_id]` | Hide a CTF category from non-admins | Admin |
| `/ctf remove [event_id] password` | Delete a CTF category and all associated data | Admin |

### Challenges

| Command | Description | Permission |
|---|---|---|
| `/challenge <name>` | Create a thread for a challenge (must be in a topic channel) | Everyone |
| `/challenge-fetch <event_id> <url> [auth_token]` | Fetch CTFd challenges, map CTFd categories to topic channels, create missing threads, and refresh CTFd embeds only when description/files change | Admin |
| `/done <solver> [solver2] ...` | Mark a challenge as solved and rename the thread | Admin / `@ctf` role |
| `/undone` | Reopen a mistakenly solved challenge and remove the `[DONE]` thread prefix | Admin / `@ctf` role |
| `/challenges [event_id]` | List all challenges with status and thread links | Everyone |
| `/remove-challenge` | Untrack the current challenge (keeps the thread) | Admin |

### Scoreboard

| Command | Description | Permission |
|---|---|---|
| `/scoreboard <type> <url> [auth_token] [team] [event_id]` | Configure scoreboard polling (`CTFd` or `rCTF`) | Admin |
| `/scoreboard_list` | Show active scoreboard configs | Everyone |
| `/scoreboard_remove <event_id>` | Remove scoreboard config | Admin |

### Statistics

| Command | Description | Permission |
|---|---|---|
| `/stats leaderboard [limit] [channel]` | Top users by message count | Everyone |
| `/stats user <member>` | Per-user message stats, rank, and active channels | Everyone |
| `/stats sync [limit] [channel]` | Backfill message history into stats | Admin |

## Workflow

```
1. /ctf join <event_id>          → Bot creates category with topic channels
2. /challenge-fetch <event_id> <ctfd_url> [token]
                                  → Bot asks how to map CTFd categories, then imports into topic threads
3. Or go to a topic channel and run /challenge <name> for manual threads
4. Work on the challenge in the thread
5. /done @solver                 → Mark solved, thread renamed to [DONE]
   /undone                       → Reopen it if /done was clicked by mistake
6. /challenges                   → Overview with clickable thread links
```

## Channels Created on Join

Each CTF event gets a Discord category named after the event, containing:

```
account       — read-only info channel
general       — general discussion
rev           — reverse engineering
pwn           — binary exploitation
web           — web challenges
crypto        — cryptography
for           — forensics
misc          — miscellaneous
scoreboard    — live scoreboard updates
```

## BOT Admin Category

On startup, the bot creates a private `BOT` category visible only to admins:

- **#log** — command usage logs
- **#backup** — database uploads created by `/backup`

## Permissions

The bot requires these Discord permissions:

| Permission | Reason |
|---|---|
| Manage Channels | Create categories, channels, and threads |
| Create Public Threads | Challenge threads |
| Read Message History | `/stats sync` backfill |
| Send Messages | Thread creation and responses |
| Embed Links | All bot responses use embeds |
| Manage Roles | (Optional) if you want the bot to manage the `@ctf` role |

**Required intents** (enable in the Discord Developer Portal):

- Server Members
- Message Content (if using prefix commands in the future)

## Notes

- The `@ctf` role must be created manually in your server for `/done` access to work.
- `/challenge-fetch` asks for category mapping every run, so new CTFd categories added mid-event can be routed before import.
- Existing CTFd challenge threads are updated only when the description or file links change; point/solve-count changes alone are skipped. Threads marked with `/done` are not updated.
- `/challenge-fetch` accepts either a full CTFd URL (`http://localhost:8000`) or a host-only URL (`localhost:8000`).
- Message statistics only track messages sent after the bot is deployed, unless you run `/stats sync`.
- Scoreboard polling for rCTF uses the public API directly — no browser dependency required.
- The bot uses SQLite. For production use, ensure the database file is on a persistent volume.
