from __future__ import annotations

import hashlib
import json
import logging
from urllib.parse import urljoin, urlparse

import aiohttp

log = logging.getLogger(__name__)

CTFD_CANDIDATES = [
    "/api/v1/scoreboard",
    "/api/v1/scoreboard?count=1000",
    "/scoreboard?format=json",
    "/scores?format=json",
]

# rCTF /api/v1/leaderboard/now requires limit<=100
RCTF_LIMIT = 100


def _looks_like_ctfd_scoreboard(obj: dict) -> bool:
    if not isinstance(obj, dict):
        return False
    data = obj.get("data")
    if not isinstance(data, list) or not data:
        return False
    if not isinstance(data[0], dict):
        return False
    keys = set(data[0].keys())
    return ("name" in keys or "team" in keys) and ("score" in keys or "points" in keys)


def _normalize_entries(entries: list[dict]) -> list[dict]:
    normalized = []
    for idx, entry in enumerate(entries, start=1):
        name = (
            entry.get("name")
            or entry.get("team")
            or entry.get("account_name")
            or entry.get("username")
        )
        if isinstance(name, dict):
            name = name.get("name")
        score = entry.get("score", entry.get("points"))
        pos = entry.get("pos", entry.get("place", entry.get("rank", idx)))
        if name is None or score is None:
            continue
        normalized.append({"pos": int(pos), "name": str(name), "score": float(score)})
    normalized.sort(key=lambda x: x["pos"])
    return normalized


def _extract_rctf_leaderboard(payload: dict) -> list[dict] | None:
    """Extract entries from rCTF /api/v1/leaderboard/now response.

    Handles both the standard structure {data: {leaderboard: [...]}}
    and fallback structures {data: {scores: [...]}} etc.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    # Primary: data.leaderboard[]
    leaderboard = data.get("leaderboard")
    if isinstance(leaderboard, list):
        if not leaderboard:
            return []  # valid but empty (CTF not started / ended)
        entries = []
        for idx, item in enumerate(leaderboard, start=1):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            score = item.get("score")
            if name is None or score is None:
                continue
            entries.append({"name": str(name), "score": float(score), "pos": idx})
        return entries or None

    return None


def _rctf_base_url(url: str) -> str:
    """Normalize user-provided URL to scheme + host only.

    Accepts:
      - https://umdctf.io/scores
      - https://umdctf.io/#/scores
      - https://umdctf.io/api/v1/leaderboard/now
      - https://umdctf.io
    Always returns: https://umdctf.io/
    """
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _scoreboard_auth_values(auth_token: str | None) -> list[str | None]:
    if not auth_token or not auth_token.strip():
        return [None]
    token = auth_token.strip()
    lower = token.lower()
    if lower.startswith("token ") or lower.startswith("bearer "):
        return [token]
    return [f"Token {token}", f"Bearer {token}"]


async def fetch_ctfd_scoreboard(base_url: str, auth_token: str | None = None) -> list[dict]:
    base = base_url.rstrip("/") + "/"
    got_auth_error = False

    for auth_value in _scoreboard_auth_values(auth_token):
        headers: dict[str, str] = {"User-Agent": "ctf-bot/1.0"}
        if auth_value:
            headers["Authorization"] = auth_value

        async with aiohttp.ClientSession(headers=headers) as session:
            for path in CTFD_CANDIDATES:
                url = urljoin(base, path)
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        ct = (resp.headers.get("content-type") or "").lower()
                        if "json" not in ct:
                            continue
                        if resp.status in {401, 403}:
                            got_auth_error = True
                            break  # Wrong auth scheme — try next scheme
                        payload = await resp.json()
                except Exception:
                    continue
                if isinstance(payload, dict) and _looks_like_ctfd_scoreboard(payload):
                    return _normalize_entries(payload["data"])

    if got_auth_error:
        raise RuntimeError(
            "CTFd returned HTTP 401/403 — the auth_token is missing or incorrect. "
            "Check the token configured via /scoreboard."
        )
    raise RuntimeError("CTFd scoreboard endpoint not found or invalid.")


async def fetch_rctf_scoreboard(url: str, auth_token: str | None = None) -> list[dict]:
    base = _rctf_base_url(url)
    headers = {"User-Agent": "ctf-bot/1.0"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    api_url = f"{base}api/v1/leaderboard/now?limit={RCTF_LIMIT}&offset=0"

    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.get(
                api_url, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"rCTF API returned status {resp.status} for {api_url}"
                    )
                payload = await resp.json()
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"Failed to connect to rCTF at {base}: {exc}") from exc

        entries = _extract_rctf_leaderboard(payload)
        if entries is not None:
            return entries

        # Fallback: if data is a raw list
        data = payload.get("data")
        if isinstance(data, list):
            return _normalize_entries(data)

        raise RuntimeError(
            f"rCTF API returned unexpected format. "
            f"Response kind: {payload.get('kind', 'unknown')}"
        )


def make_payload_hash(entries: list[dict]) -> str:
    normalized = json.dumps(entries, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
