"""A failed embed post must not cost the thread.

/challenge-fetch used to create the thread, post the embed, then record the
challenge. A failure in the middle left a thread Discord knew about and the
database did not, so the description never arrived and the next run built a
duplicate beside it instead of repairing it.
"""

from types import SimpleNamespace

import discord
import pytest

from bot.cogs.challenge import ChallengeCog

cog = object.__new__(ChallengeCog)


def _http_error(status=429):
    return discord.HTTPException(
        SimpleNamespace(status=status, reason="err"), "boom"
    )


def _forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "nope")


class _Thread:
    def __init__(self, failures=0, error=None, name="chal"):
        self.id = 1
        self.name = name
        self.failures = failures
        self.error = error or _http_error()
        self.calls = 0

    async def send(self, embed=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return SimpleNamespace(id=999)


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr("bot.cogs.challenge.asyncio.sleep", instant)


async def test_embed_posts_first_try():
    thread = _Thread()
    assert (await ChallengeCog._post_thread_embed(thread, None)).id == 999
    assert thread.calls == 1


async def test_transient_error_is_retried_then_succeeds():
    thread = _Thread(failures=2)
    assert (await ChallengeCog._post_thread_embed(thread, None)).id == 999
    assert thread.calls == 3


async def test_persistent_error_gives_up_without_raising():
    thread = _Thread(failures=99)
    assert await ChallengeCog._post_thread_embed(thread, None) is None
    assert thread.calls == 4


async def test_forbidden_is_not_retried():
    thread = _Thread(failures=99, error=_forbidden())
    assert await ChallengeCog._post_thread_embed(thread, None) is None
    assert thread.calls == 1


class _Channel:
    def __init__(self, active=(), archived=(), id=55):
        self.id = id
        self.threads = [SimpleNamespace(name=n, id=i) for i, n in enumerate(active)]
        self._archived = [
            SimpleNamespace(name=n, id=100 + i) for i, n in enumerate(archived)
        ]

    def archived_threads(self, limit=100):
        async def gen():
            for t in self._archived[:limit]:
                yield t
        return gen()


async def test_lists_active_and_archived_threads_once():
    channel = _Channel(active=["web-1"], archived=["old-chal"])
    cache = {}
    index = await cog._threads_by_name(channel, cache)

    assert set(index) == {"web-1", "old-chal"}
    assert index["old-chal"].id == 100
    assert cache[channel.id] is index


async def test_channel_is_listed_only_once_per_run():
    channel = _Channel(active=["a"], archived=["b"])
    cache = {}
    first = await cog._threads_by_name(channel, cache)
    channel.threads.append(SimpleNamespace(name="added-later", id=7))
    second = await cog._threads_by_name(channel, cache)

    assert second is first
    assert "added-later" not in second


async def test_active_thread_wins_over_an_archived_namesake():
    channel = _Channel(active=["dup"], archived=["dup"])
    index = await cog._threads_by_name(channel, {})

    assert index["dup"].id == 0


async def test_archive_scan_failure_is_not_fatal():
    class _Broken(_Channel):
        def archived_threads(self, limit=100):
            async def gen():
                raise _forbidden()
                yield  # pragma: no cover
            return gen()

    index = await cog._threads_by_name(_Broken(active=["kept"]), {})
    assert set(index) == {"kept"}
