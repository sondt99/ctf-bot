"""Tests for bot.utils.embeds — timezone parsing and embed builders."""
from __future__ import annotations

import os
from datetime import timedelta, timezone

# Ensure TIMEZONE env is set before importing embeds
os.environ.setdefault("TIMEZONE", "UTC+7")
os.environ.setdefault("DISCORD_TOKEN", "test-token")


def test_parse_utc_plus():
    from bot.utils.embeds import _parse_timezone_offset

    tz = _parse_timezone_offset("UTC+7")
    assert tz.utcoffset(None) == timedelta(hours=7)


def test_parse_utc_minus():
    from bot.utils.embeds import _parse_timezone_offset

    tz = _parse_timezone_offset("UTC-5")
    assert tz.utcoffset(None) == timedelta(hours=-5)


def test_parse_utc_zero():
    from bot.utils.embeds import _parse_timezone_offset

    tz = _parse_timezone_offset("UTC+0")
    assert tz.utcoffset(None) == timedelta(0)


def test_parse_invalid_falls_back_to_utc(caplog):
    from bot.utils.embeds import _parse_timezone_offset
    import logging

    with caplog.at_level(logging.WARNING, logger="bot.utils.embeds"):
        tz = _parse_timezone_offset("Not/A/RealZone_XYZ")

    assert tz == timezone.utc
    assert "Unrecognized TIMEZONE" in caplog.text


def test_parse_iana_timezone():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from bot.utils.embeds import _parse_timezone_offset

    tz = _parse_timezone_offset("Asia/Ho_Chi_Minh")
    assert isinstance(tz, ZoneInfo)
    # UTC+7 has no DST: offset is always +7h
    dt = datetime(2026, 1, 1, tzinfo=tz)
    assert dt.utcoffset() == timedelta(hours=7)


def test_parse_iana_utc_alias():
    from zoneinfo import ZoneInfo
    from bot.utils.embeds import _parse_timezone_offset

    tz = _parse_timezone_offset("UTC")
    assert isinstance(tz, ZoneInfo)


def test_build_simple_embed():
    from bot.utils.embeds import build_simple_embed

    embed = build_simple_embed("My Title", "My Description")
    assert embed.title == "My Title"
    assert embed.description == "My Description"


def test_build_scoreboard_embed_single_team():
    from bot.utils.embeds import build_scoreboard_embed

    entries = [{"pos": 3, "name": "OurTeam", "score": 500}]
    embed = build_scoreboard_embed(entries, [], "http://ctf.example.com")
    assert embed.title == "Scoreboard Update"
    field_names = [f.name for f in embed.fields]
    assert "Team" in field_names


def test_build_scoreboard_embed_with_changes():
    from bot.utils.embeds import build_scoreboard_embed

    entries = [
        {"pos": 1, "name": "TeamA", "score": 1000},
        {"pos": 2, "name": "TeamB", "score": 900},
    ]
    changes = ["TeamA up to 1 (1000)"]
    embed = build_scoreboard_embed(entries, changes, "http://ctf.example.com")
    field_names = [f.name for f in embed.fields]
    assert "Changes" in field_names


def test_build_event_embed_removed():
    """build_event_embed must no longer exist (dead code removed in #11)."""
    import bot.utils.embeds as m

    assert not hasattr(m, "build_event_embed"), (
        "build_event_embed should have been removed as dead code"
    )
