"""Integration tests for bot.db.repository — uses in-memory SQLite."""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("FERNET_KEY", "")  # Disable encryption for most tests


@pytest.fixture
async def repo():
    from bot.db.database import init_db
    from bot.db.repository import Repository

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    await init_db(db_path)
    r = Repository(db_path)
    yield r
    os.unlink(db_path)


# ── CTF Events ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_and_get_ctf_event(repo):
    await repo.upsert_ctf_event(
        guild_id=1,
        ctftime_event_id=100,
        event_title="Test CTF",
        category_id=999,
        channels={"General": 1, "Scoreboard": 2},
        start_time=None,
        finish_time=None,
    )
    event = await repo.get_ctf_event(1, 100)
    assert event is not None
    assert event.event_title == "Test CTF"
    assert event.channels["General"] == 1


@pytest.mark.asyncio
async def test_list_ctf_events_returns_all_for_guild(repo):
    await repo.upsert_ctf_event(1, 101, "CTF One", 1, {}, None, None)
    await repo.upsert_ctf_event(1, 102, "CTF Two", 2, {}, None, None)
    await repo.upsert_ctf_event(2, 101, "Other Guild CTF", 3, {}, None, None)
    events = await repo.list_ctf_events(1)
    assert len(events) == 2
    titles = {e.event_title for e in events}
    assert titles == {"CTF One", "CTF Two"}


@pytest.mark.asyncio
async def test_delete_ctf_event_removes_it(repo):
    await repo.upsert_ctf_event(1, 200, "To Delete", 10, {}, None, None)
    assert await repo.get_ctf_event(1, 200) is not None
    await repo.delete_ctf_event(1, 200)
    assert await repo.get_ctf_event(1, 200) is None


# ── Scoreboard Config ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scoreboard_config_guild_filter(repo):
    """list_scoreboard_configs(guild_id=X) must only return that guild's configs."""
    await repo.upsert_ctf_event(1, 300, "G1 CTF", 1, {}, None, None)
    await repo.upsert_ctf_event(2, 300, "G2 CTF", 2, {}, None, None)
    await repo.upsert_scoreboard_config(1, 300, "ctfd", "http://g1.com", None, None, 10)
    await repo.upsert_scoreboard_config(2, 300, "ctfd", "http://g2.com", None, None, 20)

    g1_configs = await repo.list_scoreboard_configs(guild_id=1)
    assert len(g1_configs) == 1
    assert g1_configs[0].url == "http://g1.com"

    g2_configs = await repo.list_scoreboard_configs(guild_id=2)
    assert len(g2_configs) == 1
    assert g2_configs[0].url == "http://g2.com"

    all_configs = await repo.list_scoreboard_configs()
    assert len(all_configs) == 2


@pytest.mark.asyncio
async def test_scoreboard_config_delete(repo):
    await repo.upsert_ctf_event(1, 400, "CTF", 1, {}, None, None)
    await repo.upsert_scoreboard_config(1, 400, "rctf", "http://r.com", None, None, 5)
    configs = await repo.list_scoreboard_configs(guild_id=1)
    assert len(configs) == 1
    await repo.delete_scoreboard_config(1, 400)
    configs = await repo.list_scoreboard_configs(guild_id=1)
    assert len(configs) == 0


@pytest.mark.asyncio
async def test_scoreboard_config_token_passthrough_no_key(repo):
    """Without FERNET_KEY, token is stored and retrieved unchanged."""
    await repo.upsert_ctf_event(1, 500, "CTF", 1, {}, None, None)
    await repo.upsert_scoreboard_config(1, 500, "ctfd", "http://c.com", "my-token", None, 7)
    cfg = await repo.get_scoreboard_config(1, 500)
    assert cfg is not None
    assert cfg.auth_token == "my-token"


# ── Message Tracking ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_message_insert(repo):
    inserted = await repo.record_message(1001, 1, 100, 500, "2024-01-01T00:00:00+00:00")
    assert inserted is True


@pytest.mark.asyncio
async def test_record_message_duplicate_ignored(repo):
    await repo.record_message(2001, 1, 100, 500, "2024-01-01T00:00:00+00:00")
    inserted = await repo.record_message(2001, 1, 100, 500, "2024-01-01T00:00:00+00:00")
    assert inserted is False


@pytest.mark.asyncio
async def test_record_messages_batch(repo):
    messages = [
        (3001, 1, 100, 500, "2024-01-01T00:00:00+00:00"),
        (3002, 1, 100, 501, "2024-01-01T00:01:00+00:00"),
        (3003, 1, 101, 500, "2024-01-01T00:02:00+00:00"),
    ]
    count = await repo.record_messages(messages)
    assert count == 3


@pytest.mark.asyncio
async def test_get_message_leaderboard(repo):
    messages = [
        (4001, 1, 100, 10, "2024-01-01T00:00:00+00:00"),
        (4002, 1, 100, 10, "2024-01-01T00:01:00+00:00"),
        (4003, 1, 100, 20, "2024-01-01T00:02:00+00:00"),
    ]
    await repo.record_messages(messages)
    entries = await repo.get_message_leaderboard(guild_id=1, limit=10)
    assert len(entries) >= 1
    user_ids = [e.user_id for e in entries]
    assert 10 in user_ids


# ── Scoreboard State ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_and_get_scoreboard_state(repo):
    await repo.upsert_ctf_event(1, 600, "CTF", 1, {}, None, None)
    await repo.upsert_scoreboard_state(1, 600, "abc123", '[]')
    state = await repo.get_scoreboard_state(1, 600)
    assert state is not None
    assert state.last_hash == "abc123"
    assert state.last_payload == "[]"
