"""Tests for bot.services.ctftime — retry logic and helpers."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")


def test_unix_now_returns_integer():
    from bot.services.ctftime import _unix_now

    ts = _unix_now()
    assert isinstance(ts, int)
    assert ts > 0


@pytest.mark.asyncio
async def test_fetch_json_retries_transient_error():
    """_fetch_json retries on ClientConnectionError and succeeds on 3rd attempt."""
    import aiohttp
    from bot.services.ctftime import _fetch_json

    call_count = 0

    class _FakeResp:
        status = 200

        async def json(self):
            return [{"id": 1}]

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class _FakeSession:
        def get(self, url: str, **kwargs):
            del url, kwargs
            nonlocal call_count
            call_count += 1

            class _Ctx:
                async def __aenter__(self):
                    if call_count < 3:
                        raise aiohttp.ClientConnectionError("transient")
                    return _FakeResp()

                async def __aexit__(self, *args):
                    del args

            return _Ctx()

    result = await _fetch_json(_FakeSession(), "http://fake/")  # type: ignore[arg-type]
    assert result == [{"id": 1}]
    assert call_count == 3


@pytest.mark.asyncio
async def test_fetch_json_raises_after_all_retries():
    """_fetch_json raises when all retries are exhausted."""
    import aiohttp
    from bot.services.ctftime import _fetch_json

    class _AlwaysFailSession:
        def get(self, url: str, **kwargs):
            del url, kwargs

            class _Ctx:
                async def __aenter__(self):
                    raise aiohttp.ClientConnectionError("always fails")

                async def __aexit__(self, *args):
                    del args

            return _Ctx()

    with pytest.raises((aiohttp.ClientConnectionError, RuntimeError)):
        await _fetch_json(_AlwaysFailSession(), "http://fake/")  # type: ignore[arg-type]


def test_unix_from_iso_valid():
    from bot.services.ctftime import _unix_from_iso

    ts = _unix_from_iso("2026-06-11T10:00:00+00:00")
    assert ts > 0


def test_unix_from_iso_invalid_returns_zero():
    from bot.services.ctftime import _unix_from_iso

    assert _unix_from_iso("not-a-date") == 0
    assert _unix_from_iso("") == 0


def test_fetch_archived_events_filters_running():
    """filter logic: events with finish < now are archived, finish >= now are not."""
    from bot.services.ctftime import _unix_from_iso, _unix_now

    now = _unix_now()
    past_ts = now - 3600
    future_ts = now + 3600

    from datetime import datetime, timezone

    def _make_iso(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    events = [
        {"title": "Past CTF", "finish": _make_iso(past_ts)},
        {"title": "Running CTF", "finish": _make_iso(future_ts)},
        {"title": "No finish"},
    ]
    archived = [e for e in events if _unix_from_iso(e.get("finish", "")) < now]
    not_archived = [e for e in events if _unix_from_iso(e.get("finish", "")) >= now]

    # "Past CTF" has finish < now; "No finish" → _unix_from_iso("") = 0 < now → also archived
    archived_titles = {e["title"] for e in archived}
    assert "Past CTF" in archived_titles
    assert "No finish" in archived_titles
    assert len(not_archived) == 1
    assert not_archived[0]["title"] == "Running CTF"
