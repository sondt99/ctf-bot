# Database

## Engine

SQLite with WAL journal mode (`PRAGMA journal_mode=WAL`) and `PRAGMA synchronous=NORMAL`. The database file is created automatically on first run at the path set by `DATABASE_PATH` (default: `ctf_bot.db`, Docker: `/app/data/ctf_bot.db`).

## Schema

### `ctf_events`

Stores every CTF event that has been joined with `/ctf join`.

| Column | Type | Notes |
|---|---|---|
| `guild_id` | INTEGER | Discord server ID |
| `ctftime_event_id` | INTEGER | CTFtime event ID |
| `event_title` | TEXT | |
| `category_id` | INTEGER | Discord category channel ID |
| `channels_json` | TEXT | JSON map of channel names → channel IDs |
| `start_time` | TEXT | ISO-8601, nullable |
| `finish_time` | TEXT | ISO-8601, nullable |
| `created_at` | TEXT | ISO-8601 UTC |

**Primary key:** `(guild_id, ctftime_event_id)`

---

### `challenges`

One row per tracked challenge thread.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Auto-increment PK |
| `guild_id` | INTEGER | |
| `ctftime_event_id` | INTEGER | FK → `ctf_events` |
| `challenge_name` | TEXT | |
| `category` | TEXT | Topic/category label, e.g. `WEB`, `PWN`, `CRYPTO` |
| `thread_id` | INTEGER | UNIQUE — Discord thread ID |
| `channel_id` | INTEGER | Parent topic channel |
| `status` | TEXT | `open` or `done`; `/undone` changes solved challenges back to `open` |
| `solved_by` | TEXT | JSON array of Discord user IDs; reset to `[]` by `/undone` |
| `created_at` | TEXT | ISO-8601 UTC |
| `solved_at` | TEXT | ISO-8601 UTC, nullable; reset to `NULL` by `/undone` |
| `ctfd_challenge_id` | INTEGER | nullable — CTFd challenge ID |
| `ctfd_description` | TEXT | nullable — cached description |
| `ctfd_files_json` | TEXT | nullable — JSON array of file URLs |
| `ctfd_message_id` | INTEGER | nullable — Discord message with CTFd embed |

---

### `scoreboard_config`

One row per active scoreboard polling config (set via `/scoreboard`).

| Column | Type | Notes |
|---|---|---|
| `guild_id` | INTEGER | |
| `ctftime_event_id` | INTEGER | |
| `type` | TEXT | `CTFd` or `rCTF` |
| `url` | TEXT | Scoreboard base URL |
| `auth_token` | TEXT | Fernet-encrypted if `FERNET_KEY` is set |
| `team_name` | TEXT | nullable — override per config |
| `scoreboard_channel_id` | INTEGER | Where updates are posted |

**Primary key:** `(guild_id, ctftime_event_id)`

---

### `scoreboard_state`

Cache of the last scoreboard payload to detect changes.

| Column | Type | Notes |
|---|---|---|
| `guild_id` | INTEGER | |
| `ctftime_event_id` | INTEGER | |
| `last_hash` | TEXT | SHA-256 of last payload |
| `last_payload` | TEXT | Full JSON string |
| `updated_at` | TEXT | ISO-8601 UTC |

---

### `message_events`

Tracks individual messages for `/stats`. One row per message.

| Column | Type | Notes |
|---|---|---|
| `message_id` | INTEGER | Discord message ID (PK) |
| `guild_id` | INTEGER | |
| `channel_id` | INTEGER | |
| `user_id` | INTEGER | |
| `created_at` | TEXT | ISO-8601 UTC |

**Indexes:** `(guild_id, user_id)` and `(guild_id, channel_id, user_id)`

---

## Migrations

`database.py` runs additive migrations on every startup — no version table needed:

- `_migrate_ctf_events` — upgrades legacy single-PK schema to composite `(guild_id, ctftime_event_id)` PK
- `_migrate_scoreboard_config` / `_migrate_scoreboard_state` — same composite PK upgrade
- `_ensure_column` — adds `ctfd_*` columns to `challenges` if missing (safe to run on existing DBs)

All migrations are idempotent and run before the `CREATE TABLE IF NOT EXISTS` schema block.

## Repository API

All database access goes through `bot/db/repository.py`. Key methods:

```python
# CTF Events
repo.upsert_ctf_event(guild_id, ctftime_event_id, ...)
repo.get_ctf_event(guild_id, ctftime_event_id) -> CtfEvent | None
repo.list_ctf_events(guild_id) -> list[CtfEvent]
repo.delete_ctf_event(guild_id, ctftime_event_id)

# Challenges
repo.create_challenge(...) -> int | None          # returns new challenge id
repo.get_challenge_by_thread(thread_id) -> Challenge | None
repo.list_challenges(guild_id, ctftime_event_id) -> list[Challenge]
repo.mark_challenge_done(thread_id, solver_ids)
repo.mark_challenge_open(thread_id) -> bool
repo.update_challenge_ctfd_metadata(thread_id, ...)
repo.delete_challenge_by_thread(thread_id)
repo.delete_challenges_for_event(guild_id, ctftime_event_id)

# Scoreboard
repo.upsert_scoreboard_config(guild_id, ctftime_event_id, ...)
repo.get_scoreboard_config(guild_id, ctftime_event_id) -> ScoreboardConfig | None
repo.list_scoreboard_configs(guild_id=None) -> list[ScoreboardConfig]
repo.upsert_scoreboard_state(guild_id, ctftime_event_id, hash, payload)

# Stats
repo.record_message(guild_id, channel_id, user_id, message_id, created_at)
repo.record_messages(entries) -> int              # bulk insert
repo.get_message_leaderboard(guild_id, limit, channel_id=None)
repo.get_user_message_stats(guild_id, user_id) -> UserMessageStats | None
```
