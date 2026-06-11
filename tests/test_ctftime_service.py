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
        def get(self, url, **kwargs):
            nonlocal call_count
            call_count += 1

            class _Ctx:
                async def __aenter__(self_):
                    if call_count < 3:
                        raise aiohttp.ClientConnectionError("transient")
                    return _FakeResp()

                async def __aexit__(self_, *_):
                    pass

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
        def get(self, url, **kwargs):
            class _Ctx:
                async def __aenter__(self_):
                    raise aiohttp.ClientConnectionError("always fails")

                async def __aexit__(self_, *_):
                    pass

            return _Ctx()

    with pytest.raises((aiohttp.ClientConnectionError, RuntimeError)):
        await _fetch_json(_AlwaysFailSession(), "http://fake/")  # type: ignore[arg-type]
