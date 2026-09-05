# Discord CTF Bot

Organize Capture The Flag competitions in Discord end-to-end: discover events on [CTFtime](https://ctftime.org), spin up per-event categories, track challenges in threads, connect **CTFd** / **rCTF**, submit flags from Discord, and keep a live scoreboard in sync.

Built with **Python 3.11+**, `discord.py`, async SQLite (`aiosqlite`), and optional Fernet encryption for platform tokens.

---

## Table of contents

- [Features](#features)
- [Quick start](#quick-start)
- [Docker](#docker)
- [Environment variables](#environment-variables)
- [Commands](#commands)
- [Typical workflow](#typical-workflow)
- [Guild layout](#guild-layout)
- [Permissions](#permissions)
- [Documentation](#documentation)
- [Notes](#notes)

---

## Features

| Area | What you get |
|---|---|
| **CTFtime** | Browse upcoming, running, and recently archived events with pagination |
| **Event setup** | One command creates a category, topic channels, and the `@ctf` role |
| **Challenges** | Threads per challenge; bulk import from CTFd; reopen mistaken solves |
| **Platforms** | Connect CTFd or rCTF — auth, flag submit, team info, solve sync, hints, solvers |
| **Deep integration** | rCTF v2/v1 auto-negotiation; division + rank display; email masking; instancer info |
| **Scoreboard** | Periodic polling with change notifications (CTFd + rCTF public API) |
| **Auto-poll** | Optionally detect newly released CTFd challenges and open threads |
| **Stats** | Per-user message leaderboard, activity breakdown, history backfill |
| **Multi-event** | Run several CTFs at once, each with its own category and config |
| **Access control** | `@ctf` for solve / reopen / ping; admins for destructive ops |
| **Ops** | Private `BOT` category for command logs and DB backups |

---

## Quick start

```bash
# 1. Clone and configure
git clone git@github.com:sondt99/ctf-bot.git
cd ctf-bot
cp .env.example .env
# Edit .env — set DISCORD_TOKEN (and DISCORD_GUILD_ID for fast slash sync)

# 2. Install (prefer a venv)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Run
python -m bot.main
```

**Requirements:** Python 3.11+ and a Discord bot token with the [required intents and permissions](#permissions).

For tests, linting, and project layout, see [docs/development.md](docs/development.md).

---

## Docker

```bash
cp .env.example .env
# Set DISCORD_TOKEN; recommended: FERNET_KEY

docker compose up -d
docker compose logs -f bot

# After git pull
docker compose build --pull && docker compose up -d
```

SQLite lives in the named volume `bot_data` (`DATABASE_PATH=/app/data/ctf_bot.db` inside the container), so data survives restarts and image rebuilds. Schema migrations run on startup.

---

## Environment variables

Copy [`.env.example`](.env.example) and adjust as needed.

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | **Yes** | — | Discord bot token |
| `DISCORD_GUILD_ID` | No | — | Guild ID for instant slash-command sync (recommended while developing) |
| `DATABASE_PATH` | No | `ctf_bot.db` | SQLite database path |
| `FERNET_KEY` | No | — | Encrypt auth tokens at rest. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `TIMEZONE` | No | `UTC+7` | Event display TZ — IANA (`Asia/Ho_Chi_Minh`) or offset (`UTC+7`) |
| `SCOREBOARD_POLL_SECONDS` | No | `30` | Scoreboard poll interval |
| `SCOREBOARD_TOP_N` | No | `10` | Teams shown in scoreboard embeds |
| `SCOREBOARD_TEAM_NAME` | No | — | Default team name to highlight / track |
| `CTF_REMOVE_PASSWORD` | No | — | Password required by `/ctf remove` (empty disables the check) |
| `CTFD_POLL_INTERVAL_MINUTES` | No | `0` | Auto-poll CTFd for new challenges (`0` = off) |
| `AUTO_BACKUP_INTERVAL_HOURS` | No | `0` | Auto-upload DB to `BOT/#backup` (`0` = off) |

---

## Commands

Permission column: **Everyone** · **`@ctf`** · **Admin**

### CTF events

| Command | Description | Who |
|---|---|---|
| `/ctf upcoming [limit]` | Browse upcoming CTFs from CTFtime | Everyone |
| `/ctf running [limit]` | List currently active CTFs | Everyone |
| `/ctf archive [limit] [days]` | Recently ended CTFs (default: last 30 days) | Everyone |
| `/ctf join <event_id>` | Create category + channels; auto-create `@ctf` if possible | Admin |
| `/ctf list` | Joined CTFs and event IDs | Everyone |
| `/ctf info [event_id]` | Event details, platform link, solve progress | Everyone |
| `/ctf connect <platform> <url> [event_id]` | Link CTFd or rCTF to a joined event | Admin |
| `/ctf progress [event_id]` | Challenge progress with per-category breakdown | Everyone |
| `/ctf export [event_id] [format]` | Export challenges as JSON or CSV | Everyone |
| `/ctf hidden [event_id]` | Hide the CTF category from non-admins | Admin |
| `/ctf remove [event_id] password` | Delete category and associated bot data | Admin |
| `/team [event_id]` | Live team score / rank / members from the connected platform | Everyone |

### Challenges

| Command | Description | Who |
|---|---|---|
| `/challenge <name>` | Create a challenge thread (run in a topic channel) | Everyone |
| `/challenge-fetch [event_id] [url] [platform] [auth_token]` | Import CTFd/rCTF challenges; map categories → topic channels | Admin |
| `/challenge-sync [event_id]` | Mark Discord challenges done from platform team solves | Admin |
| `/challenge-refresh [event_id]` | Re-fetch challenge metadata and refresh embeds | Admin |
| `/done <solver> [solver2] …` | Mark solved; rename thread with `[DONE]` | Admin / `@ctf` |
| `/undone` | Reopen a mistaken solve; drop the `[DONE]` prefix | Admin / `@ctf` |
| `/submit <flag>` | Submit a flag via the connected platform (in a challenge thread) | Everyone\* |
| `/challenges [event_id]` | List challenges with status, solvers, and thread links | Everyone |
| `/solvers` | Show who solved the current challenge thread (via platform API) | Everyone |
| `/remove-challenge` | Untrack the current challenge (keeps the thread) | Admin |
| `/ping [message] [event_id]` | Ping `@ctf` in every open challenge thread | Admin / `@ctf` |

\*`/submit` needs a saved user token (`/auth`) or a team token on the platform config.

### Auth (platform)

| Command | Description | Who |
|---|---|---|
| `/auth token <token> [event_id]` | Save and validate your CTFd / rCTF API token | Everyone |
| `/auth login <team_token> [event_id]` | Exchange an rCTF team token for an auth token | Everyone |
| `/auth status [event_id]` | Check whether your token is still valid | Everyone |
| `/auth logout [event_id]` | Remove your saved platform token | Everyone |

### Scoreboard

| Command | Description | Who |
|---|---|---|
| `/scoreboard <type> <url> [auth_token] [team] [event_id]` | Configure polling (`CTFd` or `rCTF`) | Admin |
| `/scoreboard_list` | Active scoreboard configs | Everyone |
| `/scoreboard_remove <event_id>` | Remove a scoreboard config | Admin |

### Statistics

| Command | Description | Who |
|---|---|---|
| `/stats leaderboard [limit] [channel]` | Top users by message count | Everyone |
| `/stats user <member>` | Per-user stats, rank, and active channels | Everyone |
| `/stats sync [limit_per_channel] [channel]` | Backfill message history into stats | Admin |

### Ops

| Command | Description | Who |
|---|---|---|
| `/backup` | Upload the SQLite DB to private `BOT/#backup` | Admin |

---

## Typical workflow

```text
1. /ctf upcoming                  → pick an event_id
2. /ctf join <event_id>           → category + topic channels + @ctf role
3. /ctf connect ctfd|rctf <url>   → link platform; guide posted to #account
4. Members: /auth token …         → or /auth login <team-token> on rCTF
5. /challenge-fetch …             → import platform challenges into topic threads
   — or — /challenge <name>       → manual thread in a topic channel
6. Work in the thread
7. /submit <flag>                 → live submit; auto-[DONE] on success
   — or — /done @solver           → mark solved without platform submit
8. /challenge-sync                → catch solves done outside Discord
9. /scoreboard ctfd|rctf <url>    → live standings in #scoreboard
10. /challenges · /ctf progress · /team · /ctf info
```

---

## Guild layout

### Per-event category

Created by `/ctf join`, named after the event:

| Channel | Purpose |
|---|---|
| `#account` | Read-only info (platform connect guide, tokens tips) |
| `#general` | Team discussion |
| `#rev` / `#pwn` / `#web` / `#crypto` / `#for` / `#misc` | Topic channels — challenge threads live here |
| `#scoreboard` | Live scoreboard embeds |

### Private `BOT` category

Created on startup; visible to admins (and the bot) only:

| Channel | Purpose |
|---|---|
| `#log` | Command usage audit |
| `#backup` | Manual (`/backup`) and scheduled DB uploads |

---

## Permissions

Invite the bot with scopes **`bot`** + **`applications.commands`**.

| Discord permission | Why |
|---|---|
| Manage Channels | Categories, channels, threads |
| Create Public Threads | Challenge threads |
| Send Messages / Embed Links | Replies and embeds |
| Read Message History | `/stats sync` backfill |
| Manage Roles | Optional — auto-create / manage `@ctf` |

**Privileged intents** (Discord Developer Portal → Bot):

- **Server Members Intent**
- **Message Content Intent** (stats and any future prefix use)

Full invite checklist: [docs/deployment.md](docs/deployment.md#discord-bot-setup).

---

## Documentation

| Doc | Contents |
|---|---|
| [Architecture](docs/architecture.md) | Components, data flow, background tasks |
| [Database](docs/database.md) | Schema, migrations, repository API |
| [Development](docs/development.md) | Local setup, tests, linting, structure |
| [Deployment](docs/deployment.md) | Docker, systemd, env reference, backups |

---

## Notes

- **`@ctf` role** — auto-created on `/ctf join` when the bot has Manage Roles; create it manually otherwise. Members can use `/done`, `/undone`, and `/ping`.
- **Token security** — set `FERNET_KEY` before storing platform tokens so they are encrypted at rest. Without it, tokens are stored in plaintext (a warning is logged at startup).
- **`/challenge-fetch`** — works on both CTFd and rCTF. The platform comes from `/ctf connect`; with an ad-hoc `url` it is fingerprinted from the host, and `platform:` overrides both. Prompts for category mapping each run so mid-event categories can be routed. Accepts full URLs (`https://ctf.example.com`) or host-only (`ctf.example.com`). Existing threads update only when description or files change; `[DONE]` threads stay frozen until `/undone`.
- **CTFd auto-poll** — `CTFD_POLL_INTERVAL_MINUTES` uses active CTFd scoreboard configs, default topic mapping, and no interactive prompts.
- **rCTF v2 negotiation** — the adapter probes for v2 API support and falls back to v1 transparently. Tags, instancer metadata, and scoring kind are only available on v2.
- **Hints** — CTFd challenge embeds show locked hints (title + cost) and unlocked hints (content). Unlocking hints from Discord is intentionally disabled — the bot never spends team points.
- **Instancer** — rCTF v2 challenge embeds show instance lifetime and extendable/stoppable flags. Starting/stopping instances from Discord is not supported.
- **Division display** — `/team` and `/ctf info` show the rCTF division name and division rank when available.
- **Email masking** — rCTF member emails are masked (e.g. `al***@domain.com`) in `/team` output.
- **rCTF scoreboard** — public API only; no browser automation.
- **Stats** — only messages after the bot is online, unless you run `/stats sync`.
- **Production** — keep SQLite on a persistent volume (Docker already uses `bot_data`). Prefer `/backup` or `AUTO_BACKUP_INTERVAL_HOURS` for recovery.
