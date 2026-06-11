"""Tests for Feature 1 (CTFd auto-poll detection) and Feature 2 (/ctf export)."""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("FERNET_KEY", "")  # Disable encryption for tests


# ── Shared DB fixture ─────────────────────────────────────────────────────────

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


# ── Helpers shared between tests ──────────────────────────────────────────────

def _make_ctfd_challenge(id: int, name: str, category: str = "web"):
    """Create a minimal CtfdChallenge for testing."""
    from bot.services.ctfd import CtfdChallenge

    return CtfdChallenge(
        id=id,
        name=name,
        category=category,
        value=100,
        solves=5,
        description=f"Description for {name}",
        connection_info=None,
        files=[],
        tags=[],
        challenge_type="standard",
        url=f"http://example.com/challenges#{id}",
    )


def _make_challenge_record(
    ctfd_challenge_id: int | None,
    challenge_name: str = "tracked",
    status: str = "open",
):
    """Create a minimal Challenge dataclass for testing."""
    from bot.db.repository import Challenge

    return Challenge(
        id=1,
        guild_id=100,
        ctftime_event_id=999,
        challenge_name=challenge_name,
        category="WEB",
        thread_id=9001,
        channel_id=5001,
        status=status,
        solved_by=[],
        created_at="2026-01-01T00:00:00+00:00",
        solved_at=None,
        ctfd_challenge_id=ctfd_challenge_id,
    )


# ── Feature 1: CTFd auto-poll detection logic ─────────────────────────────────

def test_ctfd_poll_new_challenge_detection():
    """filter_new_ctfd_challenges returns only challenges not yet in the DB."""
    from bot.cogs.challenge import filter_new_ctfd_challenges

    fetched = [
        _make_ctfd_challenge(id=1, name="already-tracked"),
        _make_ctfd_challenge(id=2, name="brand-new"),
        _make_ctfd_challenge(id=3, name="another-new"),
    ]
    tracked = [
        _make_challenge_record(ctfd_challenge_id=1, challenge_name="already-tracked"),
    ]

    new_ones = filter_new_ctfd_challenges(fetched, tracked)

    assert len(new_ones) == 2
    ids = {c.id for c in new_ones}
    assert ids == {2, 3}
    names = {c.name for c in new_ones}
    assert "already-tracked" not in names
    assert "brand-new" in names
    assert "another-new" in names


def test_ctfd_poll_skips_existing_challenges():
    """filter_new_ctfd_challenges returns empty list when all fetched are already tracked."""
    from bot.cogs.challenge import filter_new_ctfd_challenges

    fetched = [
        _make_ctfd_challenge(id=10, name="chall-a"),
        _make_ctfd_challenge(id=20, name="chall-b"),
    ]
    tracked = [
        _make_challenge_record(ctfd_challenge_id=10, challenge_name="chall-a"),
        _make_challenge_record(ctfd_challenge_id=20, challenge_name="chall-b"),
    ]

    new_ones = filter_new_ctfd_challenges(fetched, tracked)

    assert new_ones == [], "All challenges are already tracked, nothing should be returned"


def test_ctfd_poll_no_tracked_returns_all():
    """filter_new_ctfd_challenges returns everything when nothing is tracked yet."""
    from bot.cogs.challenge import filter_new_ctfd_challenges

    fetched = [
        _make_ctfd_challenge(id=1, name="first"),
        _make_ctfd_challenge(id=2, name="second"),
    ]
    tracked: list = []

    new_ones = filter_new_ctfd_challenges(fetched, tracked)

    assert len(new_ones) == 2


def test_ctfd_poll_ignores_tracked_without_ctfd_id():
    """Challenges tracked without a ctfd_challenge_id do not block new detections."""
    from bot.cogs.challenge import filter_new_ctfd_challenges

    fetched = [
        _make_ctfd_challenge(id=5, name="manual-chall"),
    ]
    # A manually created challenge has ctfd_challenge_id=None
    tracked = [
        _make_challenge_record(ctfd_challenge_id=None, challenge_name="manual-chall"),
    ]

    new_ones = filter_new_ctfd_challenges(fetched, tracked)

    # The fetched challenge (id=5) is NOT in any tracked ctfd_challenge_id, so it's new
    assert len(new_ones) == 1
    assert new_ones[0].id == 5


# ── Feature 2: /ctf export ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_export_json_format(repo):
    """JSON export contains correct fields for all challenges."""
    import json
    from datetime import datetime, timezone

    # Seed DB
    await repo.upsert_ctf_event(
        guild_id=200,
        ctftime_event_id=1234,
        event_title="Test Export CTF",
        category_id=3000,
        channels={},
        start_time=None,
        finish_time=None,
    )
    # 1 solved, 2 open
    await repo.create_challenge(
        guild_id=200,
        ctftime_event_id=1234,
        challenge_name="Web-01",
        category="WEB",
        thread_id=11001,
        channel_id=4001,
    )
    await repo.mark_challenge_done(thread_id=11001, solver_ids=[42, 99])

    await repo.create_challenge(
        guild_id=200,
        ctftime_event_id=1234,
        challenge_name="Crypto-01",
        category="CRYPTO",
        thread_id=11002,
        channel_id=4002,
    )
    await repo.create_challenge(
        guild_id=200,
        ctftime_event_id=1234,
        challenge_name="Pwn-01",
        category="PWN",
        thread_id=11003,
        channel_id=4003,
    )

    challenges = await repo.list_challenges(guild_id=200, ctftime_event_id=1234)
    event = await repo.get_ctf_event(guild_id=200, ctftime_event_id=1234)

    assert event is not None
    assert len(challenges) == 3

    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build JSON payload directly (same logic as the command)
    payload = {
        "event_id": event.ctftime_event_id,
        "event_title": event.event_title,
        "exported_at": exported_at,
        "challenges": [
            {
                "name": c.challenge_name,
                "category": c.category,
                "status": c.status,
                "solved_by": c.solved_by,
                "solved_at": c.solved_at,
                "thread_id": c.thread_id,
            }
            for c in challenges
        ],
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    data = json.loads(content)

    # Top-level fields
    assert data["event_id"] == 1234
    assert data["event_title"] == "Test Export CTF"
    assert "exported_at" in data
    assert len(data["challenges"]) == 3

    # Verify solved challenge
    solved = next(c for c in data["challenges"] if c["name"] == "Web-01")
    assert solved["status"] == "done"
    assert solved["solved_by"] == [42, 99]
    assert solved["solved_at"] is not None
    assert solved["thread_id"] == 11001
    assert solved["category"] == "WEB"

    # Verify open challenges
    open_names = {c["name"] for c in data["challenges"] if c["status"] == "open"}
    assert open_names == {"Crypto-01", "Pwn-01"}


@pytest.mark.asyncio
async def test_export_csv_format(repo):
    """CSV export has correct columns and rows."""
    import csv
    import io

    # Seed DB
    await repo.upsert_ctf_event(
        guild_id=300,
        ctftime_event_id=5678,
        event_title="CSV Export CTF",
        category_id=6000,
        channels={},
        start_time=None,
        finish_time=None,
    )
    await repo.create_challenge(
        guild_id=300,
        ctftime_event_id=5678,
        challenge_name="Rev-01",
        category="REV",
        thread_id=22001,
        channel_id=7001,
    )
    await repo.mark_challenge_done(thread_id=22001, solver_ids=[101])

    await repo.create_challenge(
        guild_id=300,
        ctftime_event_id=5678,
        challenge_name="Misc-01",
        category="MISC",
        thread_id=22002,
        channel_id=7002,
    )
    await repo.create_challenge(
        guild_id=300,
        ctftime_event_id=5678,
        challenge_name="For-01",
        category="FOR",
        thread_id=22003,
        channel_id=7003,
    )

    challenges = await repo.list_challenges(guild_id=300, ctftime_event_id=5678)
    assert len(challenges) == 3

    # Build CSV (same logic as the command)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["challenge_name", "category", "status", "solved_by", "solved_at", "thread_id"]
    )
    for c in challenges:
        solved_by_str = ";".join(str(uid) for uid in c.solved_by)
        writer.writerow(
            [
                c.challenge_name,
                c.category,
                c.status,
                solved_by_str,
                c.solved_at or "",
                c.thread_id,
            ]
        )
    csv_content = buf.getvalue()

    reader = csv.DictReader(io.StringIO(csv_content))
    rows = list(reader)

    # Correct number of data rows
    assert len(rows) == 3

    # Header columns present
    expected_columns = {"challenge_name", "category", "status", "solved_by", "solved_at", "thread_id"}
    assert set(reader.fieldnames or []) == expected_columns

    # Verify solved row
    solved_row = next(r for r in rows if r["challenge_name"] == "Rev-01")
    assert solved_row["status"] == "done"
    assert solved_row["category"] == "REV"
    assert solved_row["solved_by"] == "101"
    assert solved_row["solved_at"] != ""
    assert solved_row["thread_id"] == "22001"

    # Verify open rows
    open_rows = [r for r in rows if r["status"] == "open"]
    assert len(open_rows) == 2
    open_names = {r["challenge_name"] for r in open_rows}
    assert open_names == {"Misc-01", "For-01"}

    # Open rows have empty solved_by and solved_at
    for r in open_rows:
        assert r["solved_by"] == ""
        assert r["solved_at"] == ""
