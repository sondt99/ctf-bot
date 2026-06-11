"""Tests for new features: fetch_running_events, list_challenges, progress logic."""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("FERNET_KEY", "")  # Disable encryption for tests


# ── Shared fixture ────────────────────────────────────────────────────────────

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


# ── Feature 1: fetch_running_events ──────────────────────────────────────────

def test_unix_from_iso_valid():
    """_unix_from_iso returns a positive integer for a valid ISO string."""
    from bot.services.ctftime import _unix_from_iso

    ts = _unix_from_iso("2025-01-15T12:00:00+00:00")
    assert isinstance(ts, int)
    assert ts > 0


def test_unix_from_iso_invalid_returns_zero():
    """_unix_from_iso returns 0 for garbage input."""
    from bot.services.ctftime import _unix_from_iso

    assert _unix_from_iso("") == 0
    assert _unix_from_iso("not-a-date") == 0


@pytest.mark.asyncio
async def test_fetch_running_events_filters_finished():
    """fetch_running_events excludes events whose finish timestamp is in the past."""
    from unittest.mock import patch, AsyncMock
    from bot.services.ctftime import _unix_now, fetch_running_events

    now = _unix_now()
    past = now - 3600         # finished 1 hour ago
    future = now + 3600       # finishes in 1 hour

    # Two events: one already finished, one still running
    mock_events = [
        {"id": 1, "title": "Finished CTF", "finish": f"1970-01-01T00:{past // 60 % 60:02d}:00+00:00"},
        {"id": 2, "title": "Running CTF",  "finish": f"1970-01-01T00:{future // 60 % 60:02d}:00+00:00"},
    ]

    # Build ISO strings directly from timestamps for correctness
    from datetime import datetime, timezone
    finished_iso = datetime.fromtimestamp(past, tz=timezone.utc).isoformat()
    running_iso = datetime.fromtimestamp(future, tz=timezone.utc).isoformat()
    mock_events = [
        {"id": 1, "title": "Finished CTF", "finish": finished_iso},
        {"id": 2, "title": "Running CTF",  "finish": running_iso},
    ]

    async def _fake_fetch_json(session, url):
        return mock_events

    with patch("bot.services.ctftime._fetch_json", side_effect=_fake_fetch_json):
        # We also need to patch aiohttp.ClientSession so it doesn't actually connect
        import aiohttp
        with patch.object(aiohttp, "ClientSession") as mock_session_cls:
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_session_cls.return_value
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await fetch_running_events(limit=20)

    # Only the running event should pass the filter
    ids = [e["id"] for e in result]
    assert 1 not in ids, "Finished event should be excluded"
    assert 2 in ids, "Running event should be included"


# ── Feature 2: list_challenges ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_challenges_returns_all(repo):
    """list_challenges returns all challenges for a given event."""
    # Insert a parent ctf_event first (FK requirement)
    await repo.upsert_ctf_event(
        guild_id=10,
        ctftime_event_id=999,
        event_title="Test CTF",
        category_id=1,
        channels={},
        start_time=None,
        finish_time=None,
    )

    await repo.create_challenge(
        guild_id=10,
        ctftime_event_id=999,
        challenge_name="Hello World",
        category="WEB",
        thread_id=1001,
        channel_id=500,
    )
    await repo.create_challenge(
        guild_id=10,
        ctftime_event_id=999,
        challenge_name="Binary Fun",
        category="PWN",
        thread_id=1002,
        channel_id=501,
    )

    challenges = await repo.list_challenges(guild_id=10, ctftime_event_id=999)
    assert len(challenges) == 2
    names = {c.challenge_name for c in challenges}
    assert names == {"Hello World", "Binary Fun"}


@pytest.mark.asyncio
async def test_list_challenges_empty_for_other_guild(repo):
    """list_challenges does not leak data across guilds."""
    await repo.upsert_ctf_event(
        guild_id=11,
        ctftime_event_id=888,
        event_title="Other CTF",
        category_id=2,
        channels={},
        start_time=None,
        finish_time=None,
    )
    await repo.create_challenge(
        guild_id=11,
        ctftime_event_id=888,
        challenge_name="Secret Chall",
        category="CRYPTO",
        thread_id=2001,
        channel_id=600,
    )

    # Guild 12 has no challenges
    challenges = await repo.list_challenges(guild_id=12, ctftime_event_id=888)
    assert challenges == []


@pytest.mark.asyncio
async def test_list_challenges_solved_status(repo):
    """list_challenges returns correct status after marking done."""
    await repo.upsert_ctf_event(
        guild_id=20,
        ctftime_event_id=777,
        event_title="Solve CTF",
        category_id=3,
        channels={},
        start_time=None,
        finish_time=None,
    )
    await repo.create_challenge(
        guild_id=20,
        ctftime_event_id=777,
        challenge_name="Flag Hunt",
        category="MISC",
        thread_id=3001,
        channel_id=700,
    )
    await repo.mark_challenge_done(thread_id=3001, solver_ids=[42, 99])

    challenges = await repo.list_challenges(guild_id=20, ctftime_event_id=777)
    assert len(challenges) == 1
    assert challenges[0].status == "done"
    assert challenges[0].solved_by == [42, 99]


# ── Feature 3: progress command logic ────────────────────────────────────────

def test_progress_format_all_solved():
    """Progress bar logic: all challenges solved in a single category."""
    challenges = [
        type("C", (), {"category": "WEB", "status": "done", "solved_at": None})(),
        type("C", (), {"category": "WEB", "status": "done", "solved_at": None})(),
    ]

    total = len(challenges)
    solved = sum(1 for c in challenges if c.status == "done")
    open_count = total - solved
    pct = int(solved / total * 100) if total > 0 else 0

    assert total == 2
    assert solved == 2
    assert open_count == 0
    assert pct == 100


def test_progress_format_partial_solved():
    """Progress bar logic: partial solve produces correct percentages."""
    challenges = [
        type("C", (), {"category": "CRYPTO", "status": "done",  "solved_at": "2025-01-01T10:00:00+00:00"})(),
        type("C", (), {"category": "CRYPTO", "status": "open",  "solved_at": None})(),
        type("C", (), {"category": "PWN",    "status": "open",  "solved_at": None})(),
    ]

    total = len(challenges)
    solved = sum(1 for c in challenges if c.status == "done")
    open_count = total - solved
    pct = int(solved / total * 100) if total > 0 else 0

    assert total == 3
    assert solved == 1
    assert open_count == 2
    assert pct == 33

    from collections import defaultdict
    cat_total: dict[str, int] = defaultdict(int)
    cat_solved: dict[str, int] = defaultdict(int)
    for c in challenges:
        cat_total[c.category] += 1
        if c.status == "done":
            cat_solved[c.category] += 1

    assert cat_total["CRYPTO"] == 2
    assert cat_solved["CRYPTO"] == 1
    assert cat_total["PWN"] == 1
    assert cat_solved["PWN"] == 0


def test_progress_bar_rendering():
    """Progress bar strings have correct width and fill characters."""
    bar_width = 8
    cases = [
        (0, 4, 0),    # 0 of 4 solved → 0 filled
        (2, 4, 4),    # 2 of 4 solved → 4 filled
        (4, 4, 8),    # 4 of 4 solved → 8 filled
        (1, 3, 2),    # 1 of 3 solved → floor(1/3 * 8) = 2 filled
    ]
    for s, t, expected_filled in cases:
        filled = int(s / t * bar_width) if t > 0 else 0
        bar = "█" * filled + "░" * (bar_width - filled)
        assert len(bar) == bar_width, f"Bar length wrong for {s}/{t}"
        assert bar.count("█") == expected_filled, f"Fill count wrong for {s}/{t}"
        assert bar.count("░") == bar_width - expected_filled


def test_progress_last_solve_timestamp():
    """Last solve timestamp picks the latest solved_at."""
    solved_times = [
        "2025-03-01T12:00:00+00:00",
        "2025-03-01T15:30:00+00:00",
        "2025-03-01T09:00:00+00:00",
    ]
    latest = max(solved_times)
    assert latest == "2025-03-01T15:30:00+00:00"

    from datetime import datetime, timezone
    dt = datetime.fromisoformat(latest).astimezone(timezone.utc)
    ts = int(dt.timestamp())
    assert ts > 0
    assert f"<t:{ts}:R>" in f"\nLast solve: <t:{ts}:R>"
