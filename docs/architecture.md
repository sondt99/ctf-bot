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
│  auth.py        │               └────────────────────┘
│  scoreboard_cog.py │
│  stats.py          │
│  audit.py       │
└──────┬──────────┘
       │ calls
┌──────▼──────────────────────────────────────────────┐
│                   bot/services/                      │
│  platform.py   — PlatformAdapter ABC + CTFd/rCTF     │
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
| `bot/cogs/ctf.py` | `/ctf *` slash commands (upcoming, running, archive, join, list, progress, export, hidden, remove) |
| `bot/cogs/challenge.py` | `/challenge`, `/challenge-fetch`, `/done`, `/undone`, `/remove-challenge`, `/challenges`, `/solvers`, `/ping`, plus CTFd new-challenge auto-polling |
| `bot/cogs/auth.py` | `/auth token`, `/auth login`, `/auth logout`, `/auth status` |
| `bot/cogs/scoreboard_cog.py` | `/scoreboard`, `/scoreboard_list`, `/scoreboard_remove`, plus scoreboard polling background task |
| `bot/cogs/stats.py` | `/stats` commands (leaderboard, user, sync) |
| `bot/cogs/audit.py` | Private BOT category + `/backup` |
| `bot/services/platform.py` | `PlatformAdapter` ABC, `CTFdAdapter`, `RCTFAdapter` — flag submit, team info, challenges, solvers, hints |
| `bot/services/ctftime.py` | CTFtime API calls with exponential-backoff retry |
| `bot/services/platform.py` | CTFd/rCTF adapters, platform fingerprinting |
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

## Data Flow: CTFd Challenge Auto-Polling

```
challenge.ChallengeCog (discord.ext.tasks.loop, every N minutes when enabled)
  → repo.list_scoreboard_configs()        # only CTFd configs are eligible
  → fetch_ctfd_challenges(config.url, config.auth_token)
  → compare fetched CTFd IDs against tracked challenges
  → create threads for newly released challenges in the channel matching their category
  → notify the event's general channel
```

## Concurrency Model

The bot runs in a single asyncio event loop. All I/O is `await`-based:
- Discord gateway — `discord.py` internal websocket
- HTTP — `aiohttp.ClientSession` (one per call, not shared)
- SQLite — `aiosqlite` with WAL mode + `PRAGMA synchronous=NORMAL`

Background tasks (`discord.ext.tasks`) run on the same loop and do not block command handling.

## Platform Adapter Layer

`bot/services/platform.py` defines a `PlatformAdapter` abstract base class with concrete implementations for CTFd and rCTF.

```
PlatformAdapter (ABC)
├── CTFdAdapter     — REST v1 API, Token/Bearer dual auth
└── RCTFAdapter     — REST v2/v1 auto-negotiation, Bearer auth
```

### Shared HTTP primitive

Both adapters use `_request_json()`, which returns an `_HttpResult` dataclass without raising on 4xx responses. This is critical for rCTF, where wrong-flag submissions return 4xx and the adapter must read the `kind` field to produce a user-friendly message.

### rCTF API version negotiation

`RCTFAdapter._detect_api_version()` probes `GET /api/v2/integrations/client/config` and caches the result per base URL (900s TTL in `_rctf_version_cache`). v2 adds tags, instancer metadata, scoring kind, and file sizes. The adapter falls back transparently to v1 for older rCTF deployments.

### Capability flags

Instead of requiring stubs on every adapter, capability flags indicate optional features:

| Flag | CTFd | rCTF | Meaning |
|---|---|---|---|
| `supports_hints` | Yes | No | Challenge hints (display only) |
| `supports_notifications` | Yes | No | Platform notification API |
| `supports_challenge_solvers` | Yes | Yes | Per-challenge solver list |
| `supports_team_members` | Yes | Yes | Team member enumeration |
| `supports_instancer` | No | (v2) | Instance lifetime/actions |

### Synthetic notifications

rCTF has no notification API. `diff_challenge_notifications()` compares the current challenge list against a previously saved set and generates notifications for new/removed challenges.

### Cleanup on event removal

`/ctf remove` now atomically deletes `platform_config` and `user_tokens` rows alongside the event, preventing orphaned encrypted tokens.
