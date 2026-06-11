from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp


BASE_URL = "https://ctftime.org/api/v1"
_MAX_RETRIES = 3

log = logging.getLogger(__name__)


def _unix_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> list | dict:
    """Fetch a JSON endpoint with retry and exponential backoff.

    Retries on transient network errors and 429/5xx responses.
    Respects the Retry-After header on rate-limit responses.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                    wait = min(retry_after, 30.0)
                    log.warning(
                        "CTFtime rate-limited (429). Waiting %.1fs before retry %d/%d.",
                        wait,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue
                if resp.status >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        wait = 2.0 ** attempt
                        log.warning(
                            "CTFtime returned %s. Waiting %.1fs before retry %d/%d.",
                            resp.status,
                            wait,
                            attempt + 1,
                            _MAX_RETRIES,
                        )
                        await asyncio.sleep(wait)
                        continue
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            wait = 2.0 ** attempt
            log.warning(
                "CTFtime request failed (%s). Waiting %.1fs before retry %d/%d.",
                exc,
                wait,
                attempt + 1,
                _MAX_RETRIES,
            )
            await asyncio.sleep(wait)
    raise RuntimeError(f"CTFtime request failed after {_MAX_RETRIES} retries.")


async def fetch_upcoming_events(limit: int = 20, window_days: int = 180) -> list[dict]:
    start_ts = _unix_now()
    finish_ts = int(
        (datetime.now(timezone.utc) + timedelta(days=window_days)).timestamp()
    )
    url = f"{BASE_URL}/events/?limit={limit}&start={start_ts}&finish={finish_ts}"

    async with aiohttp.ClientSession() as session:
        result = await _fetch_json(session, url)
        if not isinstance(result, list):
            raise RuntimeError("CTFtime returned unexpected JSON shape for events list.")
        return result


async def fetch_event(event_id: int) -> dict:
    url = f"{BASE_URL}/events/{event_id}/"
    async with aiohttp.ClientSession() as session:
        result = await _fetch_json(session, url)
        if not isinstance(result, dict):
            raise RuntimeError("CTFtime returned unexpected JSON shape for event.")
        return result
