import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from bot.config import FERNET_KEY
from bot.crypto import decrypt_token, encrypt_token


@dataclass
class CtfEvent:
    guild_id: int
    ctftime_event_id: int
    event_title: str
    category_id: int
    channels: dict
    start_time: str | None
    finish_time: str | None
    created_at: str


@dataclass
class ScoreboardConfig:
    guild_id: int
    ctftime_event_id: int
    type: str
    url: str
    auth_token: str | None
    team_name: str | None
    scoreboard_channel_id: int


@dataclass
class ScoreboardState:
    guild_id: int
    ctftime_event_id: int
    last_hash: str | None
    last_payload: str | None
    updated_at: str


@dataclass
class Challenge:
    id: int
    guild_id: int
    ctftime_event_id: int
    challenge_name: str
    category: str
    thread_id: int
    channel_id: int
    status: str
    solved_by: list[int]
    created_at: str
    solved_at: str | None
    ctfd_challenge_id: int | None = None
    ctfd_description: str | None = None
    ctfd_files: list[str] | None = None
    ctfd_message_id: int | None = None
    platform_challenge_id: str | None = None


@dataclass
class PlatformConfig:
    guild_id: int
    ctftime_event_id: int
    platform_type: str  # "ctfd" or "rctf"
    platform_url: str
    team_token: str | None  # decrypted
    team_name: str | None
    category_mapping: dict  # parsed from JSON
    created_at: str
    last_notification_id: str | None = None
    last_solve_ids: list[str] | None = None


@dataclass
class UserToken:
    guild_id: int
    ctftime_event_id: int
    discord_user_id: int
    auth_token: str  # decrypted
    platform_username: str | None
    validated_at: str


@dataclass
class MessageLeaderboardEntry:
    user_id: int
    message_count: int
    first_message_at: str | None
    last_message_at: str | None


@dataclass
class ChannelMessageStats:
    channel_id: int
    message_count: int
    first_message_at: str | None
    last_message_at: str | None


@dataclass
class UserMessageStats:
    guild_id: int
    user_id: int
    message_count: int
    active_channels: int
    first_message_at: str | None
    last_message_at: str | None
    rank: int
    top_channels: list[ChannelMessageStats]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def upsert_ctf_event(
        self,
        guild_id: int,
        ctftime_event_id: int,
        event_title: str,
        category_id: int,
        channels: dict,
        start_time: str | None,
        finish_time: str | None,
    ) -> None:
        channels_json = json.dumps(channels, ensure_ascii=False)
        created_at = _utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO ctf_events
                  (guild_id, ctftime_event_id, event_title, category_id, channels_json, start_time, finish_time, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, ctftime_event_id) DO UPDATE SET
                  ctftime_event_id=excluded.ctftime_event_id,
                  event_title=excluded.event_title,
                  category_id=excluded.category_id,
                  channels_json=excluded.channels_json,
                  start_time=excluded.start_time,
                  finish_time=excluded.finish_time
                """,
                (
                    guild_id,
                    ctftime_event_id,
                    event_title,
                    category_id,
                    channels_json,
                    start_time,
                    finish_time,
                    created_at,
                ),
            )
            await db.commit()

    async def get_ctf_event(
        self, guild_id: int, ctftime_event_id: int
    ) -> CtfEvent | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT guild_id, ctftime_event_id, event_title, category_id, channels_json, start_time, finish_time, created_at
                FROM ctf_events WHERE guild_id=? AND ctftime_event_id=?
                """,
                (guild_id, ctftime_event_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return None
        return CtfEvent(
            guild_id=row[0],
            ctftime_event_id=row[1],
            event_title=row[2],
            category_id=row[3],
            channels=json.loads(row[4]),
            start_time=row[5],
            finish_time=row[6],
            created_at=row[7],
        )

    async def list_ctf_events(self, guild_id: int) -> list[CtfEvent]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT guild_id, ctftime_event_id, event_title, category_id, channels_json, start_time, finish_time, created_at
                FROM ctf_events WHERE guild_id=? ORDER BY created_at DESC
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [
            CtfEvent(
                guild_id=row[0],
                ctftime_event_id=row[1],
                event_title=row[2],
                category_id=row[3],
                channels=json.loads(row[4]),
                start_time=row[5],
                finish_time=row[6],
                created_at=row[7],
            )
            for row in rows
        ]

    async def delete_ctf_event(self, guild_id: int, ctftime_event_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM challenges WHERE guild_id=? AND ctftime_event_id=?",
                (guild_id, ctftime_event_id),
            )
            await db.execute(
                "DELETE FROM scoreboard_state WHERE guild_id=? AND ctftime_event_id=?",
                (guild_id, ctftime_event_id),
            )
            await db.execute(
                "DELETE FROM scoreboard_config WHERE guild_id=? AND ctftime_event_id=?",
                (guild_id, ctftime_event_id),
            )
            await db.execute(
                "DELETE FROM ctf_events WHERE guild_id=? AND ctftime_event_id=?",
                (guild_id, ctftime_event_id),
            )
            await db.commit()

    async def upsert_scoreboard_config(
        self,
        guild_id: int,
        ctftime_event_id: int,
        type_name: str,
        url: str,
        auth_token: str | None,
        team_name: str | None,
        scoreboard_channel_id: int,
    ) -> None:
        stored_token = encrypt_token(auth_token, FERNET_KEY)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO scoreboard_config
                  (guild_id, ctftime_event_id, type, url, auth_token, team_name, scoreboard_channel_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, ctftime_event_id) DO UPDATE SET
                  type=excluded.type,
                  url=excluded.url,
                  auth_token=excluded.auth_token,
                  team_name=excluded.team_name,
                  scoreboard_channel_id=excluded.scoreboard_channel_id
                """,
                (
                    guild_id,
                    ctftime_event_id,
                    type_name,
                    url,
                    stored_token,
                    team_name,
                    scoreboard_channel_id,
                ),
            )
            await db.commit()

    async def get_scoreboard_config(
        self, guild_id: int, ctftime_event_id: int
    ) -> ScoreboardConfig | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT guild_id, ctftime_event_id, type, url, auth_token, team_name, scoreboard_channel_id
                FROM scoreboard_config WHERE guild_id=? AND ctftime_event_id=?
                """,
                (guild_id, ctftime_event_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return None
        return ScoreboardConfig(
            guild_id=row[0],
            ctftime_event_id=row[1],
            type=row[2],
            url=row[3],
            auth_token=decrypt_token(row[4], FERNET_KEY),
            team_name=row[5],
            scoreboard_channel_id=row[6],
        )

    async def list_scoreboard_configs(
        self, guild_id: int | None = None
    ) -> list[ScoreboardConfig]:
        """Return scoreboard configs.

        If guild_id is given, only configs for that guild are returned.
        Omit guild_id to fetch all configs (used internally by the polling loop).
        """
        async with aiosqlite.connect(self.db_path) as db:
            if guild_id is not None:
                cursor = await db.execute(
                    """
                    SELECT guild_id, ctftime_event_id, type, url, auth_token, team_name, scoreboard_channel_id
                    FROM scoreboard_config WHERE guild_id=?
                    """,
                    (guild_id,),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT guild_id, ctftime_event_id, type, url, auth_token, team_name, scoreboard_channel_id
                    FROM scoreboard_config
                    """
                )
            rows = await cursor.fetchall()
            await cursor.close()
        return [
            ScoreboardConfig(
                guild_id=row[0],
                ctftime_event_id=row[1],
                type=row[2],
                url=row[3],
                auth_token=decrypt_token(row[4], FERNET_KEY),
                team_name=row[5],
                scoreboard_channel_id=row[6],
            )
            for row in rows
        ]

    async def delete_scoreboard_config(self, guild_id: int, ctftime_event_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM scoreboard_config WHERE guild_id=? AND ctftime_event_id=?",
                (guild_id, ctftime_event_id),
            )
            await db.execute(
                "DELETE FROM scoreboard_state WHERE guild_id=? AND ctftime_event_id=?",
                (guild_id, ctftime_event_id),
            )
            await db.commit()

    async def upsert_scoreboard_state(
        self,
        guild_id: int,
        ctftime_event_id: int,
        last_hash: str | None,
        last_payload: str | None,
    ) -> None:
        updated_at = _utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO scoreboard_state (guild_id, ctftime_event_id, last_hash, last_payload, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, ctftime_event_id) DO UPDATE SET
                  last_hash=excluded.last_hash,
                  last_payload=excluded.last_payload,
                  updated_at=excluded.updated_at
                """,
                (guild_id, ctftime_event_id, last_hash, last_payload, updated_at),
            )
            await db.commit()

    async def get_scoreboard_state(
        self, guild_id: int, ctftime_event_id: int
    ) -> ScoreboardState | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT guild_id, ctftime_event_id, last_hash, last_payload, updated_at
                FROM scoreboard_state WHERE guild_id=? AND ctftime_event_id=?
                """,
                (guild_id, ctftime_event_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return None
        return ScoreboardState(
            guild_id=row[0],
            ctftime_event_id=row[1],
            last_hash=row[2],
            last_payload=row[3],
            updated_at=row[4],
        )

    # ── Message tracking ─────────────────────────────────────────────

    async def record_message(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        user_id: int,
        created_at: str,
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO message_events
                  (message_id, guild_id, channel_id, user_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (message_id, guild_id, channel_id, user_id, created_at),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def record_messages(
        self,
        messages: list[tuple[int, int, int, int, str]],
    ) -> int:
        if not messages:
            return 0
        async with aiosqlite.connect(self.db_path) as db:
            before = db.total_changes
            await db.executemany(
                """
                INSERT OR IGNORE INTO message_events
                  (message_id, guild_id, channel_id, user_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                messages,
            )
            await db.commit()
            return db.total_changes - before

    async def get_message_leaderboard(
        self,
        guild_id: int,
        limit: int = 10,
        channel_id: int | None = None,
    ) -> list[MessageLeaderboardEntry]:
        query = """
            SELECT user_id,
                   COUNT(*) AS message_count,
                   MIN(created_at) AS first_message_at,
                   MAX(created_at) AS last_message_at
            FROM message_events
            WHERE guild_id=?
        """
        params: list[int] = [guild_id]
        if channel_id is not None:
            query += " AND channel_id=?"
            params.append(channel_id)
        query += """
            GROUP BY user_id
            ORDER BY message_count DESC, last_message_at DESC
            LIMIT ?
        """
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()
            await cursor.close()

        return [
            MessageLeaderboardEntry(
                user_id=row[0],
                message_count=row[1],
                first_message_at=row[2],
                last_message_at=row[3],
            )
            for row in rows
        ]

    async def get_user_message_stats(
        self,
        guild_id: int,
        user_id: int,
        top_channel_limit: int = 5,
    ) -> UserMessageStats | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS message_count,
                       COUNT(DISTINCT channel_id) AS active_channels,
                       MIN(created_at) AS first_message_at,
                       MAX(created_at) AS last_message_at
                FROM message_events
                WHERE guild_id=? AND user_id=?
                """,
                (guild_id, user_id),
            )
            summary = await cursor.fetchone()
            await cursor.close()

            if not summary or summary[0] == 0:
                return None

            total_messages = summary[0]
            active_channels = summary[1]
            first_message_at = summary[2]
            last_message_at = summary[3]

            cursor = await db.execute(
                """
                SELECT 1 + COUNT(*)
                FROM (
                    SELECT user_id
                    FROM message_events
                    WHERE guild_id=?
                    GROUP BY user_id
                    HAVING COUNT(*) > ?
                )
                """,
                (guild_id, total_messages),
            )
            rank_row = await cursor.fetchone()
            await cursor.close()
            rank = rank_row[0] if rank_row and rank_row[0] else 1

            cursor = await db.execute(
                """
                SELECT channel_id,
                       COUNT(*) AS message_count,
                       MIN(created_at) AS first_message_at,
                       MAX(created_at) AS last_message_at
                FROM message_events
                WHERE guild_id=? AND user_id=?
                GROUP BY channel_id
                ORDER BY message_count DESC, last_message_at DESC
                LIMIT ?
                """,
                (guild_id, user_id, top_channel_limit),
            )
            channel_rows = await cursor.fetchall()
            await cursor.close()

        return UserMessageStats(
            guild_id=guild_id,
            user_id=user_id,
            message_count=total_messages,
            active_channels=active_channels,
            first_message_at=first_message_at,
            last_message_at=last_message_at,
            rank=rank,
            top_channels=[
                ChannelMessageStats(
                    channel_id=row[0],
                    message_count=row[1],
                    first_message_at=row[2],
                    last_message_at=row[3],
                )
                for row in channel_rows
            ],
        )

    # ── Challenge tracking ───────────────────────────────────────────

    def _row_to_challenge(self, row: Any) -> Challenge:
        row = list(row)  # convert aiosqlite.Row → list so Pyright indexing is trivially safe
        solved_by_raw = row[8]
        solved_by = json.loads(solved_by_raw) if solved_by_raw else []
        ctfd_files_raw = row[13] if len(row) > 13 else None
        ctfd_files = None
        if ctfd_files_raw:
            try:
                parsed_files = json.loads(ctfd_files_raw)
            except json.JSONDecodeError:
                parsed_files = None
            if isinstance(parsed_files, list):
                ctfd_files = [str(item) for item in parsed_files]
        return Challenge(
            id=row[0],
            guild_id=row[1],
            ctftime_event_id=row[2],
            challenge_name=row[3],
            category=row[4],
            thread_id=row[5],
            channel_id=row[6],
            status=row[7],
            solved_by=solved_by,
            created_at=row[9],
            solved_at=row[10],
            ctfd_challenge_id=row[11] if len(row) > 11 else None,
            ctfd_description=row[12] if len(row) > 12 else None,
            ctfd_files=ctfd_files,
            ctfd_message_id=row[14] if len(row) > 14 else None,
            platform_challenge_id=row[15] if len(row) > 15 else None,
        )

    async def create_challenge(
        self,
        guild_id: int,
        ctftime_event_id: int,
        challenge_name: str,
        category: str,
        thread_id: int,
        channel_id: int,
        *,
        ctfd_challenge_id: int | None = None,
        ctfd_description: str | None = None,
        ctfd_files: list[str] | None = None,
        ctfd_message_id: int | None = None,
        platform_challenge_id: str | None = None,
    ) -> int | None:
        created_at = _utc_now_iso()
        ctfd_files_json = (
            json.dumps(ctfd_files, ensure_ascii=False) if ctfd_files is not None else None
        )
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO challenges
                  (guild_id, ctftime_event_id, challenge_name, category,
                   thread_id, channel_id, status, solved_by, created_at,
                   ctfd_challenge_id, ctfd_description, ctfd_files_json,
                   ctfd_message_id, platform_challenge_id)
                VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, ctftime_event_id, challenge_name, category,
                 thread_id, channel_id, created_at, ctfd_challenge_id,
                 ctfd_description, ctfd_files_json, ctfd_message_id,
                 platform_challenge_id),
            )
            challenge_id = cursor.lastrowid
            await db.commit()
        return challenge_id

    async def get_challenge_by_thread(self, thread_id: int) -> Challenge | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id, guild_id, ctftime_event_id, challenge_name, category,
                       thread_id, channel_id, status, solved_by, created_at, solved_at,
                       ctfd_challenge_id, ctfd_description, ctfd_files_json, ctfd_message_id,
                       platform_challenge_id
                FROM challenges WHERE thread_id=?
                """,
                (thread_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return None
        return self._row_to_challenge(row)

    async def update_challenge_ctfd_metadata(
        self,
        thread_id: int,
        ctfd_challenge_id: int,
        ctfd_description: str | None,
        ctfd_files: list[str],
        ctfd_message_id: int | None,
    ) -> None:
        ctfd_files_json = json.dumps(ctfd_files, ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE challenges
                SET ctfd_challenge_id=?, ctfd_description=?,
                    ctfd_files_json=?, ctfd_message_id=?
                WHERE thread_id=?
                """,
                (
                    ctfd_challenge_id,
                    ctfd_description,
                    ctfd_files_json,
                    ctfd_message_id,
                    thread_id,
                ),
            )
            await db.commit()

    async def mark_challenge_done(
        self, thread_id: int, solver_ids: list[int]
    ) -> None:
        solved_at = _utc_now_iso()
        solved_by_json = json.dumps(solver_ids)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE challenges
                SET status='done', solved_by=?, solved_at=?
                WHERE thread_id=?
                """,
                (solved_by_json, solved_at, thread_id),
            )
            await db.commit()

    async def mark_challenge_open(self, thread_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE challenges
                SET status='open', solved_by='[]', solved_at=NULL
                WHERE thread_id=?
                """,
                (thread_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def list_challenges(
        self, guild_id: int, ctftime_event_id: int
    ) -> list[Challenge]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id, guild_id, ctftime_event_id, challenge_name, category,
                       thread_id, channel_id, status, solved_by, created_at, solved_at,
                       ctfd_challenge_id, ctfd_description, ctfd_files_json, ctfd_message_id,
                       platform_challenge_id
                FROM challenges
                WHERE guild_id=? AND ctftime_event_id=?
                ORDER BY created_at ASC
                """,
                (guild_id, ctftime_event_id),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._row_to_challenge(row) for row in rows]

    async def delete_challenge_by_thread(self, thread_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM challenges WHERE thread_id=?", (thread_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_challenges_for_event(
        self, guild_id: int, ctftime_event_id: int
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM challenges WHERE guild_id=? AND ctftime_event_id=?",
                (guild_id, ctftime_event_id),
            )
            await db.commit()

    # ── Platform config ─────────────────────────────────────────────

    async def upsert_platform_config(
        self,
        guild_id: int,
        ctftime_event_id: int,
        platform_type: str,
        platform_url: str,
        team_token: str | None,
        team_name: str | None,
        category_mapping: dict,
    ) -> None:
        stored_token = encrypt_token(team_token, FERNET_KEY)
        category_mapping_json = json.dumps(category_mapping, ensure_ascii=False)
        created_at = _utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO platform_config
                  (guild_id, ctftime_event_id, platform_type, platform_url,
                   team_token, team_name, category_mapping_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, ctftime_event_id) DO UPDATE SET
                  platform_type=excluded.platform_type,
                  platform_url=excluded.platform_url,
                  team_token=excluded.team_token,
                  team_name=excluded.team_name,
                  category_mapping_json=excluded.category_mapping_json
                """,
                (
                    guild_id,
                    ctftime_event_id,
                    platform_type,
                    platform_url,
                    stored_token,
                    team_name,
                    category_mapping_json,
                    created_at,
                ),
            )
            await db.commit()

    def _row_to_platform_config(self, row: Any) -> PlatformConfig:
        row = list(row)
        solve_ids = None
        if len(row) > 9 and row[9]:
            try:
                parsed = json.loads(row[9])
                if isinstance(parsed, list):
                    solve_ids = [str(x) for x in parsed]
            except json.JSONDecodeError:
                pass
        return PlatformConfig(
            guild_id=row[0],
            ctftime_event_id=row[1],
            platform_type=row[2],
            platform_url=row[3],
            team_token=decrypt_token(row[4], FERNET_KEY),
            team_name=row[5],
            category_mapping=json.loads(row[6]) if row[6] else {},
            created_at=row[7],
            last_notification_id=row[8] if len(row) > 8 else None,
            last_solve_ids=solve_ids,
        )

    _PLATFORM_CONFIG_COLS = (
        "guild_id, ctftime_event_id, platform_type, platform_url, "
        "team_token, team_name, category_mapping_json, created_at, "
        "last_notification_id, last_solve_ids_json"
    )

    async def get_platform_config(
        self, guild_id: int, ctftime_event_id: int
    ) -> PlatformConfig | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT {self._PLATFORM_CONFIG_COLS}
                FROM platform_config WHERE guild_id=? AND ctftime_event_id=?
                """,
                (guild_id, ctftime_event_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return None
        return self._row_to_platform_config(row)

    async def list_platform_configs(
        self, guild_id: int | None = None
    ) -> list[PlatformConfig]:
        async with aiosqlite.connect(self.db_path) as db:
            if guild_id is not None:
                cursor = await db.execute(
                    f"""
                    SELECT {self._PLATFORM_CONFIG_COLS}
                    FROM platform_config WHERE guild_id=?
                    """,
                    (guild_id,),
                )
            else:
                cursor = await db.execute(
                    f"""
                    SELECT {self._PLATFORM_CONFIG_COLS}
                    FROM platform_config
                    """
                )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._row_to_platform_config(row) for row in rows]

    async def update_platform_poll_state(
        self,
        guild_id: int,
        ctftime_event_id: int,
        *,
        last_notification_id: str | None = None,
        last_solve_ids: list[str] | None = None,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            updates: list[str] = []
            params: list[Any] = []
            if last_notification_id is not None:
                updates.append("last_notification_id=?")
                params.append(last_notification_id)
            if last_solve_ids is not None:
                updates.append("last_solve_ids_json=?")
                params.append(json.dumps(last_solve_ids, ensure_ascii=False))
            if not updates:
                return
            params.extend([guild_id, ctftime_event_id])
            await db.execute(
                f"""
                UPDATE platform_config SET {', '.join(updates)}
                WHERE guild_id=? AND ctftime_event_id=?
                """,
                tuple(params),
            )
            await db.commit()

    async def delete_platform_config(
        self, guild_id: int, ctftime_event_id: int
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM platform_config WHERE guild_id=? AND ctftime_event_id=?",
                (guild_id, ctftime_event_id),
            )
            await db.commit()

    # ── User tokens ─────────────────────────────────────────────────

    async def upsert_user_token(
        self,
        guild_id: int,
        ctftime_event_id: int,
        discord_user_id: int,
        auth_token: str,
        platform_username: str | None,
    ) -> None:
        stored_token = encrypt_token(auth_token, FERNET_KEY)
        validated_at = _utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_tokens
                  (guild_id, ctftime_event_id, discord_user_id,
                   auth_token, platform_username, validated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, ctftime_event_id, discord_user_id) DO UPDATE SET
                  auth_token=excluded.auth_token,
                  platform_username=excluded.platform_username,
                  validated_at=excluded.validated_at
                """,
                (
                    guild_id,
                    ctftime_event_id,
                    discord_user_id,
                    stored_token,
                    platform_username,
                    validated_at,
                ),
            )
            await db.commit()

    async def get_user_token(
        self, guild_id: int, ctftime_event_id: int, discord_user_id: int
    ) -> UserToken | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT guild_id, ctftime_event_id, discord_user_id,
                       auth_token, platform_username, validated_at
                FROM user_tokens
                WHERE guild_id=? AND ctftime_event_id=? AND discord_user_id=?
                """,
                (guild_id, ctftime_event_id, discord_user_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return None
        return UserToken(
            guild_id=row[0],
            ctftime_event_id=row[1],
            discord_user_id=row[2],
            auth_token=decrypt_token(row[3], FERNET_KEY) or str(row[3]),
            platform_username=row[4],
            validated_at=row[5],
        )

    async def delete_user_token(
        self, guild_id: int, ctftime_event_id: int, discord_user_id: int
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                DELETE FROM user_tokens
                WHERE guild_id=? AND ctftime_event_id=? AND discord_user_id=?
                """,
                (guild_id, ctftime_event_id, discord_user_id),
            )
            await db.commit()

    async def delete_user_tokens_for_event(
        self, guild_id: int, ctftime_event_id: int
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM user_tokens WHERE guild_id=? AND ctftime_event_id=?",
                (guild_id, ctftime_event_id),
            )
            await db.commit()
