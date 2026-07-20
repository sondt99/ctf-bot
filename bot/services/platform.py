from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from html import unescape
from posixpath import basename as url_basename
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import aiohttp

log = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_DETAIL_CONCURRENCY = 8
_RCTF_LEADERBOARD_LIMIT = 100


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ChallengeFile:
    name: str
    url: str


@dataclass(slots=True)
class PlatformChallenge:
    id: str
    name: str
    category: str
    description: str | None
    author: str | None
    value: int | float | None
    solves: int | None
    files: list[ChallengeFile] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    connection_info: str | None = None
    url: str | None = None


@dataclass(slots=True)
class TeamInfo:
    name: str
    score: float
    rank: int | None = None
    members: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScoreEntry:
    pos: int
    name: str
    score: float


@dataclass(slots=True)
class Solve:
    challenge_name: str
    challenge_id: str | None = None
    solved_at: str | None = None
    solver: str | None = None


@dataclass(slots=True)
class SubmitResult:
    correct: bool
    message: str


@dataclass(slots=True)
class PlatformNotification:
    id: str
    title: str
    content: str
    date: str | None = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _clean_html(value: object) -> str | None:
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
    if not isinstance(value, str | bytes | bytearray | int | float):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_number(value: object) -> int | float | None:
    if not isinstance(value, str | bytes | bytearray | int | float):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _base_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "ctf-bot/1.0",
    }


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class PlatformAdapter(ABC):
    def __init__(self, base_url: str, auth_token: str | None = None) -> None:
        self.base_url = base_url
        self.auth_token = auth_token

    @property
    @abstractmethod
    def platform_type(self) -> str: ...

    @abstractmethod
    async def validate_token(self) -> tuple[bool, str]: ...

    @abstractmethod
    async def list_challenges(self) -> list[PlatformChallenge]: ...

    @abstractmethod
    async def get_scoreboard(self, limit: int = 100) -> list[ScoreEntry]: ...

    @abstractmethod
    async def get_team_info(self) -> TeamInfo | None: ...

    @abstractmethod
    async def get_team_solves(self) -> list[Solve]: ...

    @abstractmethod
    async def get_notifications(self, since_id: str | None = None) -> list[PlatformNotification]: ...

    @abstractmethod
    async def submit_flag(self, challenge_id: str, flag: str) -> SubmitResult: ...


# ---------------------------------------------------------------------------
# CTFd adapter
# ---------------------------------------------------------------------------

class CTFdAdapter(PlatformAdapter):

    def __init__(self, base_url: str, auth_token: str | None = None) -> None:
        super().__init__(base_url, auth_token)
        self._base = self._normalize_url(base_url)

    @property
    def platform_type(self) -> str:
        return "ctfd"

    # -- URL / auth helpers --------------------------------------------------

    @staticmethod
    def _normalize_url(raw: str) -> str:
        value = raw.strip()
        if not value:
            raise RuntimeError("CTFd URL cannot be empty.")

        if value.startswith("//"):
            value = f"http:{value}"
        elif "://" not in value:
            value = f"http://{value}"

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(
                "Invalid CTFd URL. Use a host like localhost:8000 "
                "or a full URL like http://localhost:8000."
            )

        if parsed.hostname == "localhost":
            netloc = "127.0.0.1"
            if parsed.port is not None:
                netloc = f"{netloc}:{parsed.port}"
            value = urlunsplit(
                (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
            )

        return value.rstrip("/") + "/"

    def _auth_values(self) -> list[str | None]:
        if not self.auth_token or not self.auth_token.strip():
            return [None]
        token = self.auth_token.strip()
        lowered = token.lower()
        if lowered.startswith("token ") or lowered.startswith("bearer "):
            return [token]
        return [f"Token {token}", f"Bearer {token}"]

    def _session_headers(self, auth_value: str | None) -> dict[str, str]:
        headers = _base_headers()
        if auth_value:
            headers["Authorization"] = auth_value
        return headers

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> dict:
        async with session.get(url, timeout=_TIMEOUT, allow_redirects=False) as resp:
            if resp.status in {301, 302, 303, 307, 308}:
                location = resp.headers.get("location") or "another page"
                raise RuntimeError(
                    f"CTFd redirected to {location}; check the URL and API token."
                )
            if resp.status in {401, 403}:
                body = await resp.text()
                message = self._extract_error(body)
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

    async def _post_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        body: dict,
    ) -> dict:
        async with session.post(
            url, json=body, timeout=_TIMEOUT, allow_redirects=False,
        ) as resp:
            if resp.status in {301, 302, 303, 307, 308}:
                location = resp.headers.get("location") or "another page"
                raise RuntimeError(
                    f"CTFd redirected to {location}; check the URL and API token."
                )
            if resp.status in {401, 403}:
                raw = await resp.text()
                message = self._extract_error(raw)
                raise RuntimeError(f"CTFd API returned HTTP {resp.status}: {message}")
            if resp.status >= 400:
                raw = (await resp.text())[:300]
                raise RuntimeError(f"CTFd API returned HTTP {resp.status}: {raw}")

            content_type = (resp.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                raw = (await resp.text())[:300]
                raise RuntimeError(f"CTFd returned non-JSON response: {raw}")

            payload = await resp.json()

        if not isinstance(payload, dict):
            raise RuntimeError("CTFd returned an unexpected JSON shape.")
        return payload

    @staticmethod
    def _extract_error(body: str) -> str:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body[:300] or "empty response body"
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("errors") or payload
            return str(message)
        return str(payload)

    # -- helpers for challenge normalization ---------------------------------

    def _normalize_files(self, raw_files: object) -> list[ChallengeFile]:
        if raw_files is None:
            return []
        if isinstance(raw_files, (str, bytes)):
            items: list[object] = [raw_files]
        elif isinstance(raw_files, list):
            items = raw_files
        else:
            return []

        files: list[ChallengeFile] = []
        for item in items:
            if isinstance(item, dict):
                item = item.get("url") or item.get("path") or item.get("name")
            if item is None:
                continue
            full_url = urljoin(self._base, str(item))
            name = url_basename(urlparse(full_url).path) or full_url
            files.append(ChallengeFile(name=name, url=full_url))
        return files

    @staticmethod
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

    # -- run a request trying each auth scheme in sequence -------------------

    async def _try_authed(
        self,
        fn,
        *args,
    ):
        last_error: Exception | None = None
        for auth_value in self._auth_values():
            async with aiohttp.ClientSession(
                headers=self._session_headers(auth_value),
            ) as session:
                try:
                    return await fn(session, *args)
                except Exception as exc:
                    last_error = exc
                    continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("No auth values to try.")

    # -- public interface ----------------------------------------------------

    async def validate_token(self) -> tuple[bool, str]:
        url = urljoin(self._base, "api/v1/users/me")

        async def _do(session: aiohttp.ClientSession) -> tuple[bool, str]:
            try:
                payload = await self._get_json(session, url)
                data = payload.get("data")
                if not isinstance(data, dict):
                    return False, "Unexpected response shape from /users/me"
                name = data.get("name") or data.get("username") or "unknown"
                return True, str(name)
            except RuntimeError as exc:
                return False, str(exc)

        try:
            return await self._try_authed(_do)
        except Exception as exc:
            return False, str(exc)

    async def list_challenges(self) -> list[PlatformChallenge]:
        list_url = urljoin(self._base, "api/v1/challenges")

        async def _do(session: aiohttp.ClientSession) -> list[PlatformChallenge]:
            payload = await self._get_json(session, list_url)
            raw_challenges = payload.get("data")
            if not isinstance(raw_challenges, list):
                raise RuntimeError("CTFd /api/v1/challenges returned no data list.")

            valid_raws = [r for r in raw_challenges if isinstance(r, dict)]
            sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)

            async def _fetch_detail(raw: dict) -> dict | None:
                cid = _to_int(raw.get("id"))
                if cid is None:
                    return None
                detail_url = urljoin(self._base, f"api/v1/challenges/{cid}")
                async with sem:
                    try:
                        detail_payload = await self._get_json(session, detail_url)
                        detail = detail_payload.get("data")
                        return detail if isinstance(detail, dict) else None
                    except Exception as exc:
                        log.info(
                            "Could not fetch CTFd challenge detail for %s: %s",
                            cid, exc,
                        )
                        return None

            details = await asyncio.gather(*[_fetch_detail(r) for r in valid_raws])

            challenges: list[PlatformChallenge] = []
            for raw, detail in zip(valid_raws, details):
                merged = dict(raw)
                if isinstance(detail, dict):
                    merged.update(detail)
                challenge = self._build_challenge(merged)
                if challenge is not None:
                    challenges.append(challenge)
            return challenges

        return await self._try_authed(_do)

    def _build_challenge(self, raw: dict) -> PlatformChallenge | None:
        cid = _to_int(raw.get("id"))
        if cid is None:
            return None

        name = str(raw.get("name") or f"challenge-{cid}").strip()
        if not name:
            name = f"challenge-{cid}"

        return PlatformChallenge(
            id=str(cid),
            name=name,
            category=str(raw["category"]).strip() if raw.get("category") else "",
            description=_clean_html(raw.get("description")),
            author=_clean_html(raw.get("author")),
            value=_to_number(raw.get("value")),
            solves=_to_int(raw.get("solves")),
            files=self._normalize_files(raw.get("files")),
            tags=self._normalize_tags(raw.get("tags")),
            connection_info=_clean_html(raw.get("connection_info")),
            url=urljoin(self._base, f"challenges#{cid}"),
        )

    async def get_scoreboard(self, limit: int = 100) -> list[ScoreEntry]:
        url = urljoin(self._base, "api/v1/scoreboard")

        async def _do(session: aiohttp.ClientSession) -> list[ScoreEntry]:
            payload = await self._get_json(session, url)
            data = payload.get("data")
            if not isinstance(data, list):
                return []

            entries: list[ScoreEntry] = []
            for idx, item in enumerate(data, start=1):
                if not isinstance(item, dict):
                    continue
                name = (
                    item.get("name")
                    or item.get("team")
                    or item.get("account_name")
                    or item.get("username")
                )
                if isinstance(name, dict):
                    name = name.get("name")
                score = item.get("score", item.get("points"))
                if name is None or score is None:
                    continue
                pos = item.get("pos", item.get("place", item.get("rank", idx)))
                entries.append(ScoreEntry(pos=int(pos), name=str(name), score=float(score)))
                if len(entries) >= limit:
                    break
            entries.sort(key=lambda e: e.pos)
            return entries

        return await self._try_authed(_do)

    async def get_team_info(self) -> TeamInfo | None:
        async def _do(session: aiohttp.ClientSession) -> TeamInfo | None:
            # Try teams mode first, fall back to users mode.
            for endpoint in ("api/v1/teams/me", "api/v1/users/me"):
                url = urljoin(self._base, endpoint)
                try:
                    payload = await self._get_json(session, url)
                except RuntimeError:
                    continue

                data = payload.get("data")
                if not isinstance(data, dict):
                    continue

                name = data.get("name") or data.get("username")
                if name is None:
                    continue

                score = float(data.get("score", 0))
                rank = _to_int(data.get("place"))

                members: list[str] = []
                raw_members = data.get("members")
                if isinstance(raw_members, list):
                    for m in raw_members:
                        if isinstance(m, dict):
                            members.append(str(m.get("name") or m.get("username") or ""))
                        elif isinstance(m, str):
                            members.append(m)
                members = [m for m in members if m]

                return TeamInfo(
                    name=str(name),
                    score=score,
                    rank=rank,
                    members=members,
                )
            return None

        return await self._try_authed(_do)

    async def get_team_solves(self) -> list[Solve]:
        async def _do(session: aiohttp.ClientSession) -> list[Solve]:
            for endpoint in ("api/v1/teams/me/solves", "api/v1/users/me/solves"):
                url = urljoin(self._base, endpoint)
                try:
                    payload = await self._get_json(session, url)
                except RuntimeError:
                    continue

                data = payload.get("data")
                if not isinstance(data, list):
                    continue

                solves: list[Solve] = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    chall = item.get("challenge") or {}
                    if isinstance(chall, dict):
                        cname = str(chall.get("name") or item.get("challenge_id", ""))
                        cid = str(chall.get("id") or item.get("challenge_id", ""))
                    else:
                        cname = str(item.get("challenge_id", ""))
                        cid = cname

                    solver = None
                    user = item.get("user")
                    if isinstance(user, dict):
                        solver = str(user.get("name") or user.get("username") or "")
                    elif isinstance(user, str):
                        solver = user

                    solves.append(Solve(
                        challenge_name=cname,
                        challenge_id=cid or None,
                        solved_at=str(item["date"]) if item.get("date") else None,
                        solver=solver or None,
                    ))
                return solves
            return []

        return await self._try_authed(_do)

    async def get_notifications(
        self, since_id: str | None = None,
    ) -> list[PlatformNotification]:
        path = "api/v1/notifications"
        if since_id is not None:
            path = f"{path}?since_id={since_id}"
        url = urljoin(self._base, path)

        async def _do(session: aiohttp.ClientSession) -> list[PlatformNotification]:
            try:
                payload = await self._get_json(session, url)
            except RuntimeError as exc:
                log.info("Could not fetch CTFd notifications: %s", exc)
                return []

            data = payload.get("data")
            if not isinstance(data, list):
                return []

            notifications: list[PlatformNotification] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                nid = item.get("id")
                if nid is None:
                    continue
                notifications.append(PlatformNotification(
                    id=str(nid),
                    title=str(item.get("title") or ""),
                    content=_clean_html(item.get("content")) or "",
                    date=str(item["date"]) if item.get("date") else None,
                ))
            return notifications

        return await self._try_authed(_do)

    async def submit_flag(self, challenge_id: str, flag: str) -> SubmitResult:
        url = urljoin(self._base, "api/v1/challenges/attempt")
        body = {"challenge_id": int(challenge_id), "submission": flag}

        async def _do(session: aiohttp.ClientSession) -> SubmitResult:
            payload = await self._post_json(session, url, body)
            data = payload.get("data")
            if isinstance(data, dict):
                status = str(data.get("status", "")).lower()
                message = str(data.get("message") or payload.get("message") or "")
                return SubmitResult(correct=status == "correct", message=message)
            message = str(payload.get("message") or "Unknown response")
            return SubmitResult(correct=payload.get("success", False) is True, message=message)

        return await self._try_authed(_do)


# ---------------------------------------------------------------------------
# rCTF adapter
# ---------------------------------------------------------------------------

class RCTFAdapter(PlatformAdapter):

    def __init__(self, base_url: str, auth_token: str | None = None) -> None:
        super().__init__(base_url, auth_token)
        self._base = self._normalize_url(base_url)

    @property
    def platform_type(self) -> str:
        return "rctf"

    # -- URL / auth helpers --------------------------------------------------

    @staticmethod
    def _normalize_url(raw: str) -> str:
        parsed = urlparse(raw.strip())
        if not parsed.scheme or not parsed.netloc:
            raise RuntimeError("Invalid rCTF URL. Provide a full URL like https://ctf.example.com.")
        return f"{parsed.scheme}://{parsed.netloc}/"

    def _headers(self, authenticated: bool = True) -> dict[str, str]:
        headers = _base_headers()
        if authenticated and self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> dict:
        async with session.get(url, timeout=_TIMEOUT, allow_redirects=False) as resp:
            if resp.status in {301, 302, 303, 307, 308}:
                location = resp.headers.get("location") or "another page"
                raise RuntimeError(
                    f"rCTF redirected to {location}; check the URL."
                )
            if resp.status >= 400:
                body = (await resp.text())[:300]
                raise RuntimeError(f"rCTF API returned HTTP {resp.status}: {body}")

            content_type = (resp.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                body = (await resp.text())[:300]
                raise RuntimeError(f"rCTF returned non-JSON response: {body}")

            payload = await resp.json()

        if not isinstance(payload, dict):
            raise RuntimeError("rCTF returned an unexpected JSON shape.")
        return payload

    async def _post_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        body: dict,
    ) -> dict:
        async with session.post(
            url, json=body, timeout=_TIMEOUT, allow_redirects=False,
        ) as resp:
            if resp.status >= 400:
                raw = (await resp.text())[:300]
                raise RuntimeError(f"rCTF API returned HTTP {resp.status}: {raw}")

            content_type = (resp.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                raw = (await resp.text())[:300]
                raise RuntimeError(f"rCTF returned non-JSON response: {raw}")

            payload = await resp.json()

        if not isinstance(payload, dict):
            raise RuntimeError("rCTF returned an unexpected JSON shape.")
        return payload

    # -- public interface ----------------------------------------------------

    async def validate_token(self) -> tuple[bool, str]:
        test_url = f"{self._base}api/v1/auth/test"
        me_url = f"{self._base}api/v1/users/me"

        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                payload = await self._get_json(session, test_url)
                kind = payload.get("kind")
                if kind != "goodToken":
                    return False, f"Token validation failed: kind={kind}"

                me_payload = await self._get_json(session, me_url)
                data = me_payload.get("data")
                if isinstance(data, dict):
                    name = data.get("name") or data.get("username") or "unknown"
                    return True, str(name)
                return True, "unknown"
        except Exception as exc:
            return False, str(exc)

    async def list_challenges(self) -> list[PlatformChallenge]:
        url = f"{self._base}api/v1/challs"

        try:
            async with aiohttp.ClientSession(headers=self._headers(authenticated=False)) as session:
                payload = await self._get_json(session, url)
        except RuntimeError:
            # Some rCTF instances need auth for challenges
            try:
                async with aiohttp.ClientSession(headers=self._headers()) as session:
                    payload = await self._get_json(session, url)
            except Exception as exc:
                raise RuntimeError(f"Unable to fetch rCTF challenges: {exc}") from exc

        kind = payload.get("kind")
        if kind == "badNotStarted":
            return []

        data = payload.get("data")
        if not isinstance(data, list):
            raise RuntimeError("rCTF /api/v1/challs returned no data list.")

        challenges: list[PlatformChallenge] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            if cid is None:
                continue
            name = str(item.get("name") or f"challenge-{cid}").strip()

            raw_files = item.get("files") or []
            files: list[ChallengeFile] = []
            if isinstance(raw_files, list):
                for f in raw_files:
                    if isinstance(f, dict):
                        fname = str(f.get("name") or "file")
                        furl = str(f.get("url") or "")
                        if furl:
                            files.append(ChallengeFile(name=fname, url=furl))
                    elif isinstance(f, str):
                        files.append(ChallengeFile(
                            name=url_basename(urlparse(f).path) or f,
                            url=f,
                        ))

            challenges.append(PlatformChallenge(
                id=str(cid),
                name=name,
                category=str(item.get("category") or ""),
                description=item.get("description"),
                author=item.get("author"),
                value=_to_number(item.get("points") or item.get("value")),
                solves=_to_int(item.get("solves")),
                files=files,
                tags=list(item.get("tags") or []),
            ))
        return challenges

    async def get_scoreboard(self, limit: int = 100) -> list[ScoreEntry]:
        capped = min(limit, _RCTF_LEADERBOARD_LIMIT)
        url = f"{self._base}api/v1/leaderboard/now?limit={capped}&offset=0"

        headers = _base_headers()
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                payload = await self._get_json(session, url)
        except Exception as exc:
            raise RuntimeError(f"Unable to fetch rCTF scoreboard: {exc}") from exc

        data = payload.get("data")
        if not isinstance(data, dict):
            return []

        leaderboard = data.get("leaderboard")
        if not isinstance(leaderboard, list):
            return []

        entries: list[ScoreEntry] = []
        for idx, item in enumerate(leaderboard, start=1):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            score = item.get("score")
            if name is None or score is None:
                continue
            entries.append(ScoreEntry(pos=idx, name=str(name), score=float(score)))
        return entries

    async def get_team_info(self) -> TeamInfo | None:
        url = f"{self._base}api/v1/users/me"

        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                payload = await self._get_json(session, url)
        except Exception as exc:
            log.info("Could not fetch rCTF team info: %s", exc)
            return None

        data = payload.get("data")
        if not isinstance(data, dict):
            return None

        name = data.get("name") or data.get("username")
        if name is None:
            return None

        return TeamInfo(
            name=str(name),
            score=float(data.get("score", 0)),
            rank=_to_int(data.get("rank") or data.get("place")),
        )

    async def get_team_solves(self) -> list[Solve]:
        me_url = f"{self._base}api/v1/users/me"

        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                me_payload = await self._get_json(session, me_url)
                me_data = me_payload.get("data")
                if not isinstance(me_data, dict):
                    return []

                user_id = me_data.get("id")
                if user_id is None:
                    return []

                user_url = f"{self._base}api/v1/users/{user_id}"
                payload = await self._get_json(session, user_url)
        except Exception as exc:
            log.info("Could not fetch rCTF team solves: %s", exc)
            return []

        data = payload.get("data")
        if not isinstance(data, dict):
            return []

        raw_solves = data.get("solves")
        if not isinstance(raw_solves, list):
            return []

        solves: list[Solve] = []
        for item in raw_solves:
            if not isinstance(item, dict):
                continue
            cname = str(item.get("name") or item.get("challengeId") or "")
            cid = str(item.get("id") or item.get("challengeId") or "")
            created_at = item.get("createdAt")
            solves.append(Solve(
                challenge_name=cname,
                challenge_id=cid or None,
                solved_at=str(created_at) if created_at else None,
            ))
        return solves

    async def get_notifications(
        self, since_id: str | None = None,
    ) -> list[PlatformNotification]:
        _ = since_id
        return []

    async def submit_flag(self, challenge_id: str, flag: str) -> SubmitResult:
        url = f"{self._base}api/v1/challs/{challenge_id}/submit"
        body = {"flag": flag}

        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                payload = await self._post_json(session, url, body)
        except RuntimeError as exc:
            return SubmitResult(correct=False, message=str(exc))

        kind = payload.get("kind")
        message = str(payload.get("message") or kind or "")

        if kind == "goodFlag":
            return SubmitResult(correct=True, message=message)
        return SubmitResult(correct=False, message=message)

    # -- rCTF-specific -------------------------------------------------------

    async def login_with_team_token(self, team_token: str) -> str | None:
        url = f"{self._base}api/v1/auth/login"
        body = {"teamToken": team_token}

        try:
            async with aiohttp.ClientSession(headers=_base_headers()) as session:
                payload = await self._post_json(session, url, body)
        except Exception as exc:
            log.error("rCTF login failed: %s", exc)
            return None

        kind = payload.get("kind")
        if kind != "goodLogin":
            log.warning("rCTF login returned kind=%s", kind)
            return None

        data = payload.get("data")
        if isinstance(data, dict):
            return data.get("authToken") or None
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_adapter(
    platform_type: str,
    base_url: str,
    auth_token: str | None = None,
) -> PlatformAdapter:
    key = platform_type.strip().lower()
    if key == "ctfd":
        return CTFdAdapter(base_url, auth_token)
    if key == "rctf":
        return RCTFAdapter(base_url, auth_token)
    raise ValueError(f"Unknown platform type: {platform_type!r}. Supported: ctfd, rctf.")
