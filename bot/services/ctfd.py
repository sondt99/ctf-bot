from __future__ import annotations

import logging
import json
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp


log = logging.getLogger(__name__)


@dataclass(slots=True)
class CtfdChallenge:
    id: int
    name: str
    category: str | None
    value: int | float | None
    solves: int | None
    description: str | None
    connection_info: str | None
    files: list[str]
    tags: list[str]
    challenge_type: str | None
    url: str


def _authorization_values(auth_token: str | None) -> list[str | None]:
    if not auth_token or not auth_token.strip():
        return [None]

    token = auth_token.strip()
    lowered = token.lower()
    if lowered.startswith("token ") or lowered.startswith("bearer "):
        return [token]

    return [f"Token {token}", f"Bearer {token}"]


def _headers(auth_value: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "ctf-bot/1.0",
    }
    if auth_value:
        headers["Authorization"] = auth_value
    return headers


def _normalize_base_url(base_url: str) -> str:
    value = base_url.strip()
    if not value:
        raise RuntimeError("CTFd URL cannot be empty.")

    if value.startswith("//"):
        value = f"http:{value}"
    elif "://" not in value:
        value = f"http://{value}"

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            "Invalid CTFd URL. Use a host like localhost:8000 or a full URL like http://localhost:8000."
        )

    if parsed.hostname == "localhost":
        netloc = "127.0.0.1"
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        value = urlunsplit(
            (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
        )

    return value.rstrip("/") + "/"


async def _get_json(session: aiohttp.ClientSession, url: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=20)
    async with session.get(url, timeout=timeout, allow_redirects=False) as resp:
        if resp.status in {301, 302, 303, 307, 308}:
            location = resp.headers.get("location") or "another page"
            raise RuntimeError(
                f"CTFd redirected to {location}; check the URL and API token."
            )
        if resp.status in {401, 403}:
            body = await resp.text()
            message = _extract_error_message(body)
            raise RuntimeError(f"CTFd API returned HTTP {resp.status}: {message}")
        if resp.status >= 400:
            body = (await resp.text())[:300]
            raise RuntimeError(f"CTFd API returned HTTP {resp.status}: {body}")

        content_type = (resp.headers.get("content-type") or "").lower()
        if "json" not in content_type:
            body = (await resp.text())[:300]
            raise RuntimeError(f"CTFd returned non-JSON response: {body}")

        payload = await resp.json()

    if not isinstance(payload, dict):
        raise RuntimeError("CTFd returned an unexpected JSON shape.")

    if payload.get("success") is False:
        message = payload.get("message") or payload.get("errors") or payload
        raise RuntimeError(f"CTFd API returned success=false: {message}")

    return payload


def _extract_error_message(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body[:300] or "empty response body"

    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("errors") or payload
        return str(message)
    return str(payload)


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_number(value: object) -> int | float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def _normalize_files(raw_files: object, base_url: str) -> list[str]:
    if raw_files is None:
        return []
    if isinstance(raw_files, (str, bytes)):
        items: list[object] = [raw_files]
    elif isinstance(raw_files, list):
        items = raw_files
    else:
        return []

    files: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("url") or item.get("path") or item.get("name")
        if item is None:
            continue
        files.append(urljoin(base_url, str(item)))
    return files


def _normalize_tags(raw_tags: object) -> list[str]:
    if not isinstance(raw_tags, list):
        return []

    tags: list[str] = []
    for tag in raw_tags:
        if isinstance(tag, dict):
            tag = tag.get("value") or tag.get("name")
        if tag is not None:
            tags.append(str(tag))
    return tags


def _normalize_challenge(raw: dict, base_url: str) -> CtfdChallenge | None:
    challenge_id = _to_int(raw.get("id"))
    if challenge_id is None:
        return None

    name = str(raw.get("name") or f"challenge-{challenge_id}").strip()
    if not name:
        name = f"challenge-{challenge_id}"

    return CtfdChallenge(
        id=challenge_id,
        name=name,
        category=str(raw["category"]).strip() if raw.get("category") else None,
        value=_to_number(raw.get("value")),
        solves=_to_int(raw.get("solves")),
        description=_clean_text(raw.get("description")),
        connection_info=_clean_text(raw.get("connection_info")),
        files=_normalize_files(raw.get("files"), base_url),
        tags=_normalize_tags(raw.get("tags")),
        challenge_type=str(raw["type"]).strip() if raw.get("type") else None,
        url=urljoin(base_url, f"challenges#{challenge_id}"),
    )


async def fetch_ctfd_challenges(
    base_url: str, auth_token: str | None = None
) -> list[CtfdChallenge]:
    base = _normalize_base_url(base_url)
    list_url = urljoin(base, "api/v1/challenges")
    last_error: Exception | None = None

    for auth_value in _authorization_values(auth_token):
        async with aiohttp.ClientSession(headers=_headers(auth_value)) as session:
            try:
                payload = await _get_json(session, list_url)
                raw_challenges = payload.get("data")
                if not isinstance(raw_challenges, list):
                    raise RuntimeError("CTFd /api/v1/challenges returned no data list.")

                challenges: list[CtfdChallenge] = []
                for raw in raw_challenges:
                    if not isinstance(raw, dict):
                        continue

                    merged = dict(raw)
                    challenge_id = _to_int(merged.get("id"))
                    if challenge_id is not None:
                        detail_url = urljoin(base, f"api/v1/challenges/{challenge_id}")
                        try:
                            detail_payload = await _get_json(session, detail_url)
                            detail = detail_payload.get("data")
                            if isinstance(detail, dict):
                                merged.update(detail)
                        except Exception as exc:
                            log.info(
                                "Could not fetch CTFd challenge detail for %s: %s",
                                challenge_id,
                                exc,
                            )

                    challenge = _normalize_challenge(merged, base)
                    if challenge is not None:
                        challenges.append(challenge)

                return challenges
            except Exception as exc:
                last_error = exc
                continue

    if last_error is not None:
        raise RuntimeError(f"Unable to fetch CTFd challenges: {last_error}") from last_error
    raise RuntimeError("Unable to fetch CTFd challenges.")
