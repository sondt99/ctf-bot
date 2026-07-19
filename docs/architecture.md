# Architecture

## Overview

ctf-bot is a Discord bot built with `discord.py` (Cog-based) backed by an async SQLite database (`aiosqlite`). It follows a layered architecture: commands → services → repository → database.

```
┌─────────────────────────────────────────────────────┐
│                   Discord Gateway                    │
└──────────────────────┬──────────────────────────────┘
                       │ events / interactions
┌──────────────────────▼──────────────────────────────┐
│                    bot/main.py                       │
│  CtfBot(commands.Bot) — loads Cogs, syncs tree       │
└──────┬────────────────────────────────────┬──────────┘
       │ slash commands                     │ background tasks
┌──────▼──────────┐               ┌─────────▼──────────┐
│   bot/cogs/     │               │  tasks.loop()       │
│  ctf.py         │               │  scoreboard_poll    │
│  challenge.py   │               │  ctfd_poll          │
│  scoreboard.py  │               └────────────────────┘
│  stats.py       │
│  audit.py       │
└──────┬──────────┘
       │ calls
┌──────▼──────────────────────────────────────────────┐
│                   bot/services/                      │
│  ctftime.py    — CTFtime REST API (fetch/retry)      │
│  ctfd.py       — CTFd API (challenges + auth)        │
│  guild_setup.py — category/channel/role creation     │
│  scoreboard_fetcher.py — CTFd + rCTF poll            │
└──────┬──────────────────────────────────────────────┘
       │ reads/writes
┌──────▼──────────────────────────────────────────────┐
│                   bot/db/                            │
│  repository.py — typed dataclass ORM                 │
│  database.py   — schema, migrations, WAL init        │
└──────┬──────────────────────────────────────────────┘
       │ aiosqlite
┌──────▼──────────────────────────────────────────────┐
│               SQLite (WAL mode)                      │
│  ctf_bot.db  (or /app/data/ctf_bot.db in Docker)     │
└─────────────────────────────────────────────────────┘
```

## Key Modules

| Module | Responsibility |
|---|---|
| `bot/main.py` | Entry point, `CtfBot` class, extension loading |
| `bot/config.py` | Centralised env-var access (validated at import time) |
| `bot/crypto.py` | Fernet encrypt/decrypt for auth tokens at rest |
| `bot/cogs/ctf.py` | `/ctf *` slash commands (join, list, progress, export, archive, …) |
| `bot/cogs/challenge.py` | `/challenge`, `/done`, `/undone`, `/challenges`, `/challenge-fetch` |
| `bot/cogs/scoreboard_cog.py` | `/scoreboard*` commands + polling background task |
| `bot/cogs/stats.py` | `/stats` commands (leaderboard, user, sync) |
| `bot/cogs/audit.py` | Private BOT category + `/backup` |
| `bot/services/ctftime.py` | CTFtime API calls with exponential-backoff retry |
| `bot/services/ctfd.py` | CTFd challenge fetch, category mapping |
| `bot/services/guild_setup.py` | Discord guild structure (categories, channels, `@ctf` role) |
| `bot/services/scoreboard_fetcher.py` | Scoreboard polling for CTFd and rCTF |
| `bot/db/database.py` | Schema DDL, WAL init, additive migrations |
| `bot/db/repository.py` | Typed `Repository` class — all SQL in one place |
| `bot/utils/embeds.py` | Embed builders, timezone parsing (IANA + UTC±N) |
| `bot/views/ctf_pagination.py` | `discord.ui.View` paginator for event lists |

## Data Flow: `/ctf join`

```
User runs /ctf join 12345
  → CtfCog.join()
    → ctftime.fetch_event(12345)          # HTTP GET CTFtime API
    → guild_setup.create_ctf_category_and_channels()  # Discord API
    → guild_setup.ensure_ctf_role()       # create @ctf if missing
    → repo.upsert_ctf_event()             # persist to SQLite
    → send embed confirmation
```

## Data Flow: Scoreboard Polling

```
scoreboard_cog.ScoreboardCog (discord.ext.tasks.loop, every N seconds)
  → repo.list_scoreboard_configs()        # which events to poll
  → scoreboard_fetcher.fetch()            # CTFd or rCTF HTTP
  → hash new payload vs repo.get_scoreboard_state()
  → if changed: send embed to scoreboard channel
  → repo.upsert_scoreboard_state()
```

## Concurrency Model

The bot runs in a single asyncio event loop. All I/O is `await`-based:
- Discord gateway — `discord.py` internal websocket
- HTTP — `aiohttp.ClientSession` (one per call, not shared)
- SQLite — `aiosqlite` with WAL mode + `PRAGMA synchronous=NORMAL`

Background tasks (`discord.ext.tasks`) run on the same loop and do not block command handling.
