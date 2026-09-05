"""Shared bot token resolution for /ctf connect.

Background polling and admin lookups run with no user attached, so the event
needs a token of its own. rCTF hands out a registration token its API will not
take as a bearer credential, hence the exchange fallback.
"""

import pytest

import bot.cogs.ctf as ctf_module
from bot.cogs.ctf import CtfCog
from bot.services.platform import _base_headers

resolve = CtfCog._resolve_bot_token


class _Adapter:
    def __init__(self, result):
        self._result = result

    async def validate_token(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeRCTF:
    """Stands in for RCTFAdapter across both construction sites."""

    exchange_result = None
    exchanged_with = None

    def __init__(self, url, auth_token=None):
        self.url = url
        self.auth_token = auth_token

    async def login_with_team_token(self, team_token):
        type(self).exchanged_with = team_token
        return type(self).exchange_result

    async def validate_token(self):
        return type(self).authed_result


@pytest.fixture
def patched(monkeypatch):
    def install(create_result, exchange=None, authed=(True, "VinSOC")):
        monkeypatch.setattr(
            ctf_module, "create_adapter",
            lambda platform, url, token: _Adapter(create_result),
        )
        _FakeRCTF.exchange_result = exchange
        _FakeRCTF.exchanged_with = None
        _FakeRCTF.authed_result = authed
        monkeypatch.setattr(ctf_module, "RCTFAdapter", _FakeRCTF)
    return install


async def test_valid_bearer_token_is_stored_as_is(patched):
    patched((True, "VinSOC"))
    assert await resolve("ctfd", "https://ctf.example.com", "tok") == (
        "tok", "VinSOC", "",
    )
    assert _FakeRCTF.exchanged_with is None


async def test_rctf_team_token_is_exchanged_before_storing(patched):
    patched((False, "badToken"), exchange="auth-abc")
    token, account, error = await resolve("rctf", "https://nnsc.tf", "team-xyz")

    assert (token, account, error) == ("auth-abc", "VinSOC", "")
    assert _FakeRCTF.exchanged_with == "team-xyz"


async def test_ctfd_never_attempts_the_rctf_exchange(patched):
    patched((False, "Token expired"))
    token, account, error = await resolve("ctfd", "https://ctf.example.com", "tok")

    assert token is None and account is None
    assert error == "Token expired"
    assert _FakeRCTF.exchanged_with is None


async def test_rctf_token_rejected_by_both_paths(patched):
    patched((False, "badToken"), exchange=None)
    assert await resolve("rctf", "https://nnsc.tf", "nope") == (None, None, "badToken")


async def test_rctf_exchange_that_yields_an_unusable_token_is_rejected(patched):
    patched((False, "badToken"), exchange="auth-abc", authed=(False, "badToken"))
    token, account, error = await resolve("rctf", "https://nnsc.tf", "team-xyz")

    assert token is None and account is None
    assert error == "badToken"


async def test_validation_error_is_reported_not_raised(patched):
    patched(RuntimeError("connection refused"))
    token, _, error = await resolve("ctfd", "https://ctf.example.com", "tok")

    assert token is None
    assert "connection refused" in error


def test_requests_never_advertise_brotli():
    # aiohttp's brotli shim breaks on distro builds whose Decompressor exposes
    # a no-argument process(), which surfaces as an unreadable payload error.
    assert "br" not in _base_headers()["Accept-Encoding"]
