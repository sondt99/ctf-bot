import re
from typing import Any

import aiosqlite

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str) -> str:
    if not _SAFE_IDENTIFIER.match(name):
        raise ValueError(f"Invalid identifier: {name}")
    return name


SCHEMA = """
CREATE TABLE IF NOT EXISTS ctf_events (
  guild_id INTEGER NOT NULL,
  ctftime_event_id INTEGER NOT NULL,
  event_title TEXT NOT NULL,
  category_id INTEGER NOT NULL,
  channels_json TEXT NOT NULL,
  start_time TEXT,
  finish_time TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (guild_id, ctftime_event_id)
);

CREATE TABLE IF NOT EXISTS scoreboard_config (
  guild_id INTEGER NOT NULL,
  ctftime_event_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  url TEXT NOT NULL,
  auth_token TEXT,
  team_name TEXT,
  scoreboard_channel_id INTEGER NOT NULL,
  PRIMARY KEY (guild_id, ctftime_event_id)
);

CREATE TABLE IF NOT EXISTS scoreboard_state (
  guild_id INTEGER NOT NULL,
  ctftime_event_id INTEGER NOT NULL,
  last_hash TEXT,
  last_payload TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (guild_id, ctftime_event_id)
);

CREATE TABLE IF NOT EXISTS challenges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  ctftime_event_id INTEGER NOT NULL,
  challenge_name TEXT NOT NULL,
  category TEXT NOT NULL,
  thread_id INTEGER NOT NULL UNIQUE,
  channel_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  solved_by TEXT,
  created_at TEXT NOT NULL,
  solved_at TEXT,
  ctfd_challenge_id INTEGER,
  ctfd_description TEXT,
  ctfd_files_json TEXT,
  ctfd_message_id INTEGER,
  FOREIGN KEY (guild_id, ctftime_event_id) REFERENCES ctf_events(guild_id, ctftime_event_id)
);

CREATE TABLE IF NOT EXISTS message_events (
  message_id INTEGER PRIMARY KEY,
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_events_guild_user
  ON message_events(guild_id, user_id);

CREATE INDEX IF NOT EXISTS idx_message_events_guild_channel_user
  ON message_events(guild_id, channel_id, user_id);

CREATE TABLE IF NOT EXISTS platform_config (
  guild_id INTEGER NOT NULL,
  ctftime_event_id INTEGER NOT NULL,
  platform_type TEXT NOT NULL,
  platform_url TEXT NOT NULL,
  team_token TEXT,
  team_name TEXT,
  category_mapping_json TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (guild_id, ctftime_event_id)
);

CREATE TABLE IF NOT EXISTS user_tokens (
  guild_id INTEGER NOT NULL,
  ctftime_event_id INTEGER NOT NULL,
  discord_user_id INTEGER NOT NULL,
  auth_token TEXT NOT NULL,
  platform_username TEXT,
  validated_at TEXT NOT NULL,
  PRIMARY KEY (guild_id, ctftime_event_id, discord_user_id)
);
"""


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def _table_info(db: aiosqlite.Connection, name: str) -> list[tuple[Any, ...]]:
    _validate_identifier(name)
    cursor = await db.execute(f"PRAGMA table_info({name})")
    rows = await cursor.fetchall()
    await cursor.close()
    return [tuple(row) for row in rows]


def _is_legacy_single_pk(rows: list[tuple]) -> bool:
    pk_cols = [row[1] for row in rows if row[5] > 0]
    return pk_cols == ["guild_id"]


async def _migrate_ctf_events(db: aiosqlite.Connection) -> None:
    if not await _table_exists(db, "ctf_events"):
        return
    info = await _table_info(db, "ctf_events")
    if not _is_legacy_single_pk(info):
        return

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS ctf_events_new (
          guild_id INTEGER NOT NULL,
          ctftime_event_id INTEGER NOT NULL,
          event_title TEXT NOT NULL,
          category_id INTEGER NOT NULL,
          channels_json TEXT NOT NULL,
          start_time TEXT,
          finish_time TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY (guild_id, ctftime_event_id)
        )
        """
    )
    await db.execute(
        """
        INSERT INTO ctf_events_new
          (guild_id, ctftime_event_id, event_title, category_id, channels_json, start_time, finish_time, created_at)
        SELECT guild_id, ctftime_event_id, event_title, category_id, channels_json, start_time, finish_time, created_at
        FROM ctf_events
        """
    )
    await db.execute("DROP TABLE ctf_events")
    await db.execute("ALTER TABLE ctf_events_new RENAME TO ctf_events")


async def _ensure_column(
    db: aiosqlite.Connection, table: str, column: str, column_type: str
) -> None:
    _validate_identifier(table)
    _validate_identifier(column)
    _validate_identifier(column_type)
    info = await _table_info(db, table)
    if any(row[1] == column for row in info):
        return
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


async def _migrate_scoreboard_config(db: aiosqlite.Connection) -> None:
    if not await _table_exists(db, "scoreboard_config"):
        return
    info = await _table_info(db, "scoreboard_config")
    if not _is_legacy_single_pk(info):
        return

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS scoreboard_config_new (
          guild_id INTEGER NOT NULL,
          ctftime_event_id INTEGER NOT NULL,
          type TEXT NOT NULL,
          url TEXT NOT NULL,
          auth_token TEXT,
          scoreboard_channel_id INTEGER NOT NULL,
          PRIMARY KEY (guild_id, ctftime_event_id)
        )
        """
    )
    await db.execute(
        """
        INSERT INTO scoreboard_config_new
          (guild_id, ctftime_event_id, type, url, auth_token, scoreboard_channel_id)
        SELECT sc.guild_id, ce.ctftime_event_id, sc.type, sc.url, sc.auth_token, sc.scoreboard_channel_id
        FROM scoreboard_config sc
        LEFT JOIN ctf_events ce ON ce.guild_id = sc.guild_id
        WHERE ce.ctftime_event_id IS NOT NULL
        """
    )
    await db.execute("DROP TABLE scoreboard_config")
    await db.execute("ALTER TABLE scoreboard_config_new RENAME TO scoreboard_config")


async def _migrate_scoreboard_state(db: aiosqlite.Connection) -> None:
    if not await _table_exists(db, "scoreboard_state"):
        return
    info = await _table_info(db, "scoreboard_state")
    if not _is_legacy_single_pk(info):
        return

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS scoreboard_state_new (
          guild_id INTEGER NOT NULL,
          ctftime_event_id INTEGER NOT NULL,
          last_hash TEXT,
          last_payload TEXT,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (guild_id, ctftime_event_id)
        )
        """
    )
    await db.execute(
        """
        INSERT INTO scoreboard_state_new
          (guild_id, ctftime_event_id, last_hash, last_payload, updated_at)
        SELECT ss.guild_id, ce.ctftime_event_id, ss.last_hash, ss.last_payload, ss.updated_at
        FROM scoreboard_state ss
        LEFT JOIN ctf_events ce ON ce.guild_id = ss.guild_id
        WHERE ce.ctftime_event_id IS NOT NULL
        """
    )
    await db.execute("DROP TABLE scoreboard_state")
    await db.execute("ALTER TABLE scoreboard_state_new RENAME TO scoreboard_state")


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await _migrate_ctf_events(db)
        await _migrate_scoreboard_config(db)
        await _migrate_scoreboard_state(db)
        await db.executescript(SCHEMA)
        await _ensure_column(db, "scoreboard_config", "team_name", "TEXT")
        await _ensure_column(db, "challenges", "ctfd_challenge_id", "INTEGER")
        await _ensure_column(db, "challenges", "ctfd_description", "TEXT")
        await _ensure_column(db, "challenges", "ctfd_files_json", "TEXT")
        await _ensure_column(db, "challenges", "ctfd_message_id", "INTEGER")
        await _ensure_column(db, "challenges", "platform_challenge_id", "TEXT")
        await _ensure_column(db, "platform_config", "last_notification_id", "TEXT")
        await _ensure_column(db, "platform_config", "last_solve_ids_json", "TEXT")
        await db.commit()
