"""Platform fingerprinting for /challenge-fetch.

Regression cover for the case that made the command unusable on rCTF: an
ad-hoc URL with no saved /ctf connect config used to default to CTFd, so an
rCTF host answered CTFd's paths with an opaque 404.
"""

from aioresponses import aioresponses

from bot.cogs.challenge import _same_host
from bot.services.platform import detect_platform_type

RCTF_URL = "https://nnsc.tf/"
CTFD_URL = "https://ctf.example.com/"


async def test_detect_rctf_from_kind_envelope():
    with aioresponses() as mocked:
        mocked.get(
            f"{RCTF_URL}api/v1/challs",
            status=401,
            payload={"kind": "badToken", "message": "Invalid token."},
            content_type="application/json",
        )
        assert await detect_platform_type(RCTF_URL) == "rctf"


async def test_detect_rctf_when_challenges_are_public():
    with aioresponses() as mocked:
        mocked.get(
            f"{RCTF_URL}api/v1/challs",
            status=200,
            payload={"kind": "goodChallenges", "message": "ok", "data": []},
            content_type="application/json",
        )
        assert await detect_platform_type(RCTF_URL) == "rctf"


async def test_detect_ctfd_when_rctf_path_404s():
    with aioresponses() as mocked:
        # CTFd answers the rCTF path with a JSON 404 that carries no `kind`.
        mocked.get(
            f"{CTFD_URL}api/v1/challs",
            status=404,
            payload={"message": "The requested URL was not found on the server."},
            content_type="application/json",
        )
        mocked.get(
            f"{CTFD_URL}api/v1/challenges",
            status=200,
            payload={"success": True, "data": []},
            content_type="application/json",
        )
        assert await detect_platform_type(CTFD_URL) == "ctfd"


async def test_detect_ctfd_behind_auth():
    with aioresponses() as mocked:
        mocked.get(f"{CTFD_URL}api/v1/challs", status=404, body="<html>404</html>")
        mocked.get(
            f"{CTFD_URL}api/v1/challenges",
            status=403,
            payload={"message": "Authentication required"},
            content_type="application/json",
        )
        assert await detect_platform_type(CTFD_URL) == "ctfd"


async def test_detect_returns_none_for_unknown_host():
    with aioresponses() as mocked:
        mocked.get(f"{CTFD_URL}api/v1/challs", status=404, body="nope")
        mocked.get(f"{CTFD_URL}api/v1/challenges", status=404, body="nope")
        assert await detect_platform_type(CTFD_URL) is None


async def test_detect_returns_none_on_transport_failure():
    with aioresponses() as mocked:
        mocked.get(f"{CTFD_URL}api/v1/challs", exception=OSError("boom"))
        mocked.get(f"{CTFD_URL}api/v1/challenges", exception=OSError("boom"))
        assert await detect_platform_type(CTFD_URL) is None


def test_same_host_ignores_scheme_and_trailing_path():
    assert _same_host("https://nnsc.tf/", "https://nnsc.tf")
    assert _same_host("http://NNSC.TF/challs", "https://nnsc.tf")
    assert not _same_host("https://nnsc.tf/", "https://ctf.example.com")
