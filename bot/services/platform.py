from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from posixpath import basename as url_basename
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import aiohttp

log = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_DETAIL_CONCURRENCY = 8
_RCTF_LEADERBOARD_LIMIT = 100
_RCTF_VERSION_TTL_SECONDS = 900.0

# Fallback copy for CTFd attempt statuses that come back without a message.
_CTFD_SUBMIT_MESSAGES = {
    "correct": "Flag accepted.",
    "incorrect": "The flag was incorrect.",
    "partial": "Partially correct — this challenge needs more flags.",
    "already_solved": "You already solved this challenge.",
    "paused": "The CTF is paused.",
    "ratelimited": "You are submitting too fast. Slow down.",
    "authentication_required": "Authentication required — check your token.",
}

# rCTF returns errors as an HTTP status plus a `kind`. Map the kinds users can
# actually hit to readable copy so a wrong flag never renders as an HTTP dump.
_RCTF_SUBMIT_MESSAGES = {
    "goodFlag": "Flag accepted.",
    "badFlag": "The flag was incorrect.",
    "badAlreadySolvedChallenge": "You already solved this challenge.",
    "badNotStarted": "The CTF has not started yet.",
    "badEnded": "The CTF has ended.",
    "badChallenge": "Challenge not found on the platform.",
    "badPerms": "Your account is not permitted to submit.",
    "badToken": "Your rCTF token is invalid or expired — run `/auth login` again.",
    "badUnknownUser": "Your rCTF account could not be found.",
}

# base URL -> (api version, monotonic expiry). Adapters are constructed per
# command, so caching on the instance alone would re-probe constantly.
_rctf_version_cache: dict[str, tuple[int, float]] = {}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ChallengeFile:
    name: str
    url: str


@dataclass(slots=True)
class ChallengeHint:
    """A hint attached to a challenge.

    ``content`` is None while the hint is still locked — CTFd only sends the
    body once the team has paid for it.
    """
    id: str
    cost: int | None = None
    title: str | None = None
    content: str | None = None

    @property
    def unlocked(self) -> bool:
        return self.content is not None


@dataclass(slots=True)
class InstancerInfo:
    """Per-team challenge instance metadata (rCTF v2 only)."""
    lifetime_ms: int | None = None
    extendable: bool = False
    stoppable: bool = False
    actions: list[tuple[str, str]] = field(default_factory=list)


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
    state: str | None = None
    scheduled_at: str | None = None
    solved_by_me: bool | None = None
    attempts: int | None = None
    max_attempts: int | None = None
    # None => the platform has no hint concept, or detail was not fetched.
    # []   => the platform supports hints and this challenge has none.
    hints: list[ChallengeHint] | None = None
    instancer: InstancerInfo | None = None
    scoring_kind: str | None = None
    my_score: int | None = None


@dataclass(slots=True)
class TeamInfo:
    name: str
    score: float
    rank: int | None = None
    members: list[str] = field(default_factory=list)
    division: str | None = None
    division_rank: int | None = None


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
    category: str | None = None
    points: int | float | None = None
    blood_index: int | None = None


@dataclass(slots=True)
class Solver:
    """An account that solved a specific challenge."""
    name: str
    id: str | None = None
    solved_at: str | None = None
    profile_url: str | None = None
    blood_index: int | None = None


@dataclass(slots=True)
class SubmitResult:
    correct: bool
    message: str
    kind: str | None = None
    already_solved: bool = False
    retry_after_seconds: float | None = None


@dataclass(slots=True)
class PlatformMeta:
    """Event metadata reported by the platform itself."""
    name: str | None = None
    start_time: str | None = None
    end_time: str | None = None


@dataclass(slots=True)
class PlatformNotification:
    id: str
    title: str
    content: str
    date: str | None = None
    source: str = "platform"


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


def _parse_ctfd_place(value: object) -> int | None:
    """CTFd returns place as ``"1st"``, ``"2nd"``, etc. — strip the suffix."""
    if value is None:
        return None
    text = str(value).strip()
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else None


def _base_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "ctf-bot/1.0",
    }


def _epoch_ms_to_iso(value: object) -> str | None:
    """rCTF reports timestamps as epoch milliseconds; CTFd uses ISO strings.

    Normalise to ISO 8601 UTC so both adapters agree.
    """
    ms = _to_int(value)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(slots=True)
class _HttpResult:
    status: int
    payload: dict | None
    text: str
    is_json: bool


async def _request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    platform: str,
    json_body: dict | None = None,
) -> _HttpResult:
    """Perform a request and parse the body.

    Raises RuntimeError only for redirects and transport failures — never for
    HTTP error statuses. Error responses carry a machine-readable body on both
    platforms (CTFd ``message``/``errors``, rCTF ``kind``), and callers need to
    read it rather than see a raw status dump.
    """
    async with session.request(
        method, url, json=json_body, timeout=_TIMEOUT, allow_redirects=False,
    ) as resp:
        if resp.status in {301, 302, 303, 307, 308}:
            location = resp.headers.get("location") or "another page"
            raise RuntimeError(
                f"{platform} redirected to {location}; check the URL and API token."
            )

        body = await resp.text()
        content_type = (resp.headers.get("content-type") or "").lower()
        payload: dict | None = None
        is_json = False
        if "json" in content_type:
            try:
                parsed = await resp.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                payload = parsed
                is_json = True

    return _HttpResult(
        status=resp.status,
        payload=payload,
        text=body[:300],
        is_json=is_json,
    )


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class PlatformAdapter(ABC):
    # Capability flags. Callers branch on these rather than catching
    # NotImplementedError, because a platform genuinely lacking a feature is a
    # normal state, not an error.
    supports_hints: bool = False
    supports_notifications: bool = False
    supports_challenge_solvers: bool = False
    supports_team_members: bool = False
    supports_instancer: bool = False

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

    # -- optional capabilities ----------------------------------------------
    # Concrete defaults so adding a capability never breaks an existing adapter.

    async def get_challenge_solvers(
        self, challenge_id: str, limit: int = 50,
    ) -> list[Solver]:
        return []

    async def get_platform_meta(self) -> PlatformMeta | None:
        return None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def is_released(
    challenge: PlatformChallenge, now: datetime | None = None,
) -> bool:
    """False only when the challenge has a scheduled release still in the future.

    Modern CTFd already filters unreleased challenges out of the list endpoint,
    so this is defence for older forks and for tokens that see the admin view.
    """
    if not challenge.scheduled_at:
        return True
    try:
        scheduled = datetime.fromisoformat(challenge.scheduled_at)
    except ValueError:
        return True
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= scheduled


def _synthetic_id(prefix: str, names: list[str]) -> str:
    digest = hashlib.sha1("\x00".join(sorted(names)).encode()).hexdigest()[:16]
    return f"synthetic:{prefix}:{digest}"


def diff_challenge_notifications(
    previous_ids: list[str] | None,
    current: list[PlatformChallenge],
    *,
    max_names: int = 15,
) -> list[PlatformNotification]:
    """Synthesize notifications from a challenge-list delta.

    For platforms with no notification API (rCTF). Returns [] on the first poll
    — ``previous_ids is None`` means no baseline yet, and announcing every
    challenge as "new" on first sight would spam the channel.
    """
    if previous_ids is None:
        return []

    previous = set(previous_ids)
    by_id = {c.id: c for c in current}
    current_ids = set(by_id)

    notifications: list[PlatformNotification] = []

    added = sorted(current_ids - previous)
    if added:
        names = [by_id[cid].name for cid in added]
        shown = names[:max_names]
        body = "\n".join(f"- {n}" for n in shown)
        if len(names) > len(shown):
            body += f"\n... and {len(names) - len(shown)} more"
        notifications.append(PlatformNotification(
            id=_synthetic_id("added", names),
            title=f"{len(names)} new challenge(s) released",
            content=body,
            source="synthetic",
        ))

    removed = sorted(previous - current_ids)
    if removed:
        body = "\n".join(f"- `{cid}`" for cid in removed[:max_names])
        if len(removed) > max_names:
            body += f"\n... and {len(removed) - max_names} more"
        notifications.append(PlatformNotification(
            id=_synthetic_id("removed", removed),
            title=f"{len(removed)} challenge(s) removed",
            content=body,
            source="synthetic",
        ))

    return notifications


# ---------------------------------------------------------------------------
# CTFd adapter
# ---------------------------------------------------------------------------

class CTFdAdapter(PlatformAdapter):

    supports_hints = True
    supports_notifications = True
    supports_challenge_solvers = True
    supports_team_members = True

    def __init__(self, base_url: str, auth_token: str | None = None) -> None:
        super().__init__(base_url, auth_token)
        self._base = self._normalize_url(base_url)
        self._resolved_auth: str | None = None
        self._auth_resolved = False

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

    async def _raw(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        body: dict | None = None,
    ) -> _HttpResult:
        return await _request_json(
            session, method, url, platform="CTFd", json_body=body,
        )

    def _check(self, result: _HttpResult) -> dict:
        """Turn a raw result into a payload, raising on any error status."""
        if result.status in {401, 403}:
            message = self._extract_error(result.text)
            raise RuntimeError(f"CTFd API returned HTTP {result.status}: {message}")
        if result.status >= 400:
            raise RuntimeError(
                f"CTFd API returned HTTP {result.status}: {result.text}"
            )
        if not result.is_json or result.payload is None:
            raise RuntimeError(f"CTFd returned non-JSON response: {result.text}")
        return result.payload

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> dict:
        payload = self._check(await self._raw(session, "GET", url))
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
        return self._check(await self._raw(session, "POST", url, body))

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

    # -- auth scheme resolution ----------------------------------------------

    async def _resolve_auth(self) -> str | None:
        """Pick between ``Token X`` and ``Bearer X`` once, then remember it.

        CTFd documents ``Token``, but forks and proxies vary. Probing here
        rather than retrying inside every call matters: most callers swallow
        RuntimeError and return a value, so a retry-on-exception loop would
        never reach the second scheme.
        """
        if self._auth_resolved:
            return self._resolved_auth

        candidates = self._auth_values()
        if len(candidates) == 1:
            self._resolved_auth = candidates[0]
            self._auth_resolved = True
            return self._resolved_auth

        probe_url = urljoin(self._base, "api/v1/users/me")
        for candidate in candidates:
            async with aiohttp.ClientSession(
                headers=self._session_headers(candidate),
            ) as session:
                try:
                    result = await self._raw(session, "GET", probe_url)
                except Exception as exc:
                    log.debug("CTFd auth probe failed for a scheme: %s", exc)
                    continue
                if result.status < 400:
                    self._resolved_auth = candidate
                    self._auth_resolved = True
                    return candidate

        # Nothing worked; fall back to the documented scheme so the caller
        # surfaces a real CTFd error rather than a probe artefact.
        self._resolved_auth = candidates[0]
        self._auth_resolved = True
        return self._resolved_auth

    async def _try_authed(self, fn, *args):
        auth_value = await self._resolve_auth()
        async with aiohttp.ClientSession(
            headers=self._session_headers(auth_value),
        ) as session:
            return await fn(session, *args)

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

    @staticmethod
    def _normalize_hints(raw_hints: object) -> list[ChallengeHint] | None:
        """None when the payload carried no hints key at all.

        That distinction matters: a missing key means the detail fetch failed
        (so hints are unknown), while an empty list means the challenge really
        has none.
        """
        if not isinstance(raw_hints, list):
            return None
        hints: list[ChallengeHint] = []
        for item in raw_hints:
            if not isinstance(item, dict):
                continue
            hid = item.get("id")
            if hid is None:
                continue
            hints.append(ChallengeHint(
                id=str(hid),
                cost=_to_int(item.get("cost")),
                title=_clean_html(item.get("title")),
                content=_clean_html(item.get("content")),
            ))
        return hints

    def _build_challenge(self, raw: dict) -> PlatformChallenge | None:
        cid = _to_int(raw.get("id"))
        if cid is None:
            return None

        # Challenges gated behind unmet prerequisites come back anonymized —
        # type "hidden", with name/category literally "???". Creating a Discord
        # thread called "???" helps nobody.
        state = raw.get("state")
        if raw.get("type") == "hidden" or state in {"hidden", "locked"}:
            log.debug("Skipping gated CTFd challenge %s (state=%s)", cid, state)
            return None

        name = str(raw.get("name") or f"challenge-{cid}").strip()
        if not name:
            name = f"challenge-{cid}"

        solved_by_me = raw.get("solved_by_me")

        return PlatformChallenge(
            id=str(cid),
            name=name,
            category=str(raw["category"]).strip() if raw.get("category") else "",
            description=_clean_html(raw.get("description")),
            # CTFd has no `author` column — the field is `attribution`. Keep
            # `author` as a fallback for forks that add one.
            author=_clean_html(raw.get("attribution") or raw.get("author")),
            value=_to_number(raw.get("value")),
            solves=_to_int(raw.get("solves")),
            files=self._normalize_files(raw.get("files")),
            tags=self._normalize_tags(raw.get("tags")),
            connection_info=_clean_html(raw.get("connection_info")),
            url=urljoin(self._base, f"challenges#{cid}"),
            state=str(state) if state else None,
            scheduled_at=(
                str(raw["scheduled_at"]) if raw.get("scheduled_at") else None
            ),
            solved_by_me=solved_by_me if isinstance(solved_by_me, bool) else None,
            attempts=_to_int(raw.get("attempts")),
            max_attempts=_to_int(raw.get("max_attempts")) or None,
            hints=self._normalize_hints(raw.get("hints")),
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

    async def _resolve_user_names(
        self,
        session: aiohttp.ClientSession,
        user_ids: list[int],
    ) -> list[str]:
        """Resolve CTFd user IDs to display names (teams mode returns int IDs)."""
        sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)
        names: list[str] = []

        async def _fetch(uid: int) -> str | None:
            url = urljoin(self._base, f"api/v1/users/{uid}")
            async with sem:
                try:
                    payload = await self._get_json(session, url)
                    data = payload.get("data")
                    if isinstance(data, dict):
                        return str(data.get("name") or data.get("username") or "")
                except Exception:
                    pass
            return None

        results = await asyncio.gather(*[_fetch(uid) for uid in user_ids])
        for n in results:
            if n:
                names.append(n)
        return names

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
                rank = _parse_ctfd_place(data.get("place"))

                members: list[str] = []
                raw_members = data.get("members")
                if isinstance(raw_members, list):
                    int_ids: list[int] = []
                    for m in raw_members:
                        if isinstance(m, dict):
                            members.append(str(m.get("name") or m.get("username") or ""))
                        elif isinstance(m, str):
                            members.append(m)
                        elif isinstance(m, int):
                            int_ids.append(m)
                    if int_ids and not members:
                        members = await self._resolve_user_names(
                            session, int_ids,
                        )
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
                return SubmitResult(
                    correct=status == "correct",
                    message=message or _CTFD_SUBMIT_MESSAGES.get(status, ""),
                    kind=status or None,
                    already_solved=status == "already_solved",
                )
            message = str(payload.get("message") or "Unknown response")
            return SubmitResult(correct=payload.get("success", False) is True, message=message)

        return await self._try_authed(_do)

    async def get_challenge_solvers(
        self, challenge_id: str, limit: int = 50,
    ) -> list[Solver]:
        url = urljoin(self._base, f"api/v1/challenges/{challenge_id}/solves")

        async def _do(session: aiohttp.ClientSession) -> list[Solver]:
            result = await self._raw(session, "GET", url)
            # Score/account visibility can be restricted; that is a normal
            # configuration, not an error worth surfacing to the user.
            if result.status in {403, 404}:
                return []
            payload = self._check(result)
            data = payload.get("data")
            if not isinstance(data, list):
                return []

            solvers: list[Solver] = []
            for item in data[:limit]:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not name:
                    continue
                account_url = item.get("account_url")
                solvers.append(Solver(
                    name=str(name),
                    id=str(item["account_id"]) if item.get("account_id") else None,
                    solved_at=str(item["date"]) if item.get("date") else None,
                    profile_url=(
                        urljoin(self._base, str(account_url)) if account_url else None
                    ),
                ))
            return solvers

        return await self._try_authed(_do)


# ---------------------------------------------------------------------------
# rCTF adapter
# ---------------------------------------------------------------------------

class RCTFAdapter(PlatformAdapter):

    supports_challenge_solvers = True
    supports_team_members = True

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

    async def _raw(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        body: dict | None = None,
    ) -> _HttpResult:
        return await _request_json(
            session, method, url, platform="rCTF", json_body=body,
        )

    def _check_ok(self, result: _HttpResult) -> dict:
        if not result.is_json or result.payload is None:
            if result.status >= 400:
                raise RuntimeError(
                    f"rCTF API returned HTTP {result.status}: {result.text}"
                )
            raise RuntimeError(f"rCTF returned non-JSON response: {result.text}")
        return result.payload

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> dict:
        result = await self._raw(session, "GET", url)
        payload = self._check_ok(result)
        if result.status >= 400:
            kind = payload.get("kind", "")
            message = payload.get("message") or kind
            raise RuntimeError(
                f"rCTF API error (HTTP {result.status}, kind={kind}): {message}"
            )
        return payload

    async def _post_raw(
        self,
        session: aiohttp.ClientSession,
        url: str,
        body: dict,
    ) -> _HttpResult:
        return await self._raw(session, "POST", url, body)

    # -- v2/v1 negotiation ---------------------------------------------------

    async def _detect_api_version(
        self, session: aiohttp.ClientSession,
    ) -> int:
        cached = _rctf_version_cache.get(self._base)
        if cached is not None:
            version, expiry = cached
            if time.monotonic() < expiry:
                return version

        probe_url = f"{self._base}api/v2/integrations/client/config"
        try:
            result = await self._raw(session, "GET", probe_url)
            if result.status < 400 and result.is_json:
                version = 2
            else:
                version = 1
        except Exception:
            version = 1

        _rctf_version_cache[self._base] = (
            version,
            time.monotonic() + _RCTF_VERSION_TTL_SECONDS,
        )
        return version

    # -- helpers for challenge normalization ---------------------------------

    @staticmethod
    def _parse_files(raw_files: object) -> list[ChallengeFile]:
        if not isinstance(raw_files, list):
            return []
        files: list[ChallengeFile] = []
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
        return files

    def _build_challenge(self, item: dict) -> PlatformChallenge | None:
        cid = item.get("id")
        if cid is None:
            return None
        name = str(item.get("name") or f"challenge-{cid}").strip()

        instancer: InstancerInfo | None = None
        lifetime = _to_int(item.get("instancerLifetime"))
        if lifetime is not None:
            actions: list[tuple[str, str]] = []
            for a in (item.get("instancerActions") or []):
                if isinstance(a, dict) and a.get("name") and a.get("url"):
                    actions.append((str(a["name"]), str(a["url"])))
            instancer = InstancerInfo(
                lifetime_ms=lifetime,
                extendable=bool(item.get("instancerExtendable")),
                stoppable=bool(item.get("instancerStoppable")),
                actions=actions,
            )

        return PlatformChallenge(
            id=str(cid),
            name=name,
            category=str(item.get("category") or ""),
            description=_clean_html(item.get("description")),
            author=_clean_html(item.get("author")),
            value=_to_number(item.get("points") or item.get("value")),
            solves=_to_int(item.get("solves")),
            files=self._parse_files(item.get("files")),
            tags=list(item.get("tags") or []),
            url=f"{self._base}challs#{cid}",
            instancer=instancer,
            scoring_kind=str(item["scoringKind"]) if item.get("scoringKind") else None,
            my_score=_to_int(item.get("yourScore")),
        )

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
        async def _fetch(session: aiohttp.ClientSession) -> dict:
            api_ver = await self._detect_api_version(session)
            url = f"{self._base}api/v{api_ver}/challs"
            result = await self._raw(session, "GET", url)
            payload = self._check_ok(result)
            kind = payload.get("kind", "")
            if kind == "badNotStarted":
                return {"_empty": True}
            if result.status == 401:
                return {"_needs_auth": True, "_api_ver": api_ver}
            if result.status >= 400:
                raise RuntimeError(
                    f"rCTF API error (HTTP {result.status}, kind={kind}): "
                    f"{payload.get('message') or kind}"
                )
            return payload

        try:
            async with aiohttp.ClientSession(
                headers=self._headers(authenticated=False),
            ) as session:
                payload = await _fetch(session)
        except RuntimeError:
            payload = {"_needs_auth": True}

        if payload.get("_empty"):
            return []

        if payload.get("_needs_auth"):
            try:
                async with aiohttp.ClientSession(
                    headers=self._headers(),
                ) as session:
                    payload = await _fetch(session)
            except Exception as exc:
                raise RuntimeError(f"Unable to fetch rCTF challenges: {exc}") from exc
            if payload.get("_empty"):
                return []

        data = payload.get("data")
        if not isinstance(data, list):
            raise RuntimeError("rCTF /challs returned no data list.")

        challenges: list[PlatformChallenge] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            challenge = self._build_challenge(item)
            if challenge is not None:
                challenges.append(challenge)
        return challenges

    async def get_scoreboard(self, limit: int = 100) -> list[ScoreEntry]:
        capped = min(limit, _RCTF_LEADERBOARD_LIMIT)
        url = f"{self._base}api/v1/leaderboard/now?limit={capped}&offset=0"

        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
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
        me_url = f"{self._base}api/v1/users/me"

        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                payload = await self._get_json(session, me_url)
                data = payload.get("data")
                if not isinstance(data, dict):
                    return None

                name = data.get("name") or data.get("username")
                if name is None:
                    return None

                rank = _to_int(
                    data.get("globalPlace")
                    or data.get("rank")
                    or data.get("place")
                )

                members: list[str] = []
                members_url = f"{self._base}api/v1/users/me/members"
                try:
                    members_result = await self._raw(session, "GET", members_url)
                    if (
                        members_result.is_json
                        and members_result.payload
                        and members_result.status < 400
                    ):
                        raw_members = members_result.payload.get("data", [])
                        if isinstance(raw_members, list):
                            for m in raw_members:
                                if isinstance(m, dict):
                                    email = m.get("email")
                                    if email:
                                        members.append(str(email))
                except Exception:
                    pass

                return TeamInfo(
                    name=str(name),
                    score=float(data.get("score", 0)),
                    rank=rank,
                    members=members,
                    division=str(data["division"]) if data.get("division") else None,
                    division_rank=_to_int(data.get("divisionPlace")),
                )
        except Exception as exc:
            log.info("Could not fetch rCTF team info: %s", exc)
            return None

    async def get_team_solves(self) -> list[Solve]:
        me_url = f"{self._base}api/v1/users/me"

        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                payload = await self._get_json(session, me_url)
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
                solved_at=_epoch_ms_to_iso(created_at),
                category=str(item["category"]) if item.get("category") else None,
                points=_to_number(item.get("points")),
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
                result = await self._post_raw(session, url, body)
        except Exception as exc:
            return SubmitResult(correct=False, message=str(exc))

        kind: str | None = None
        message: str = ""

        if result.is_json and result.payload:
            kind = result.payload.get("kind")
            message = str(
                result.payload.get("message")
                or _RCTF_SUBMIT_MESSAGES.get(kind or "", "")
                or kind
                or ""
            )
            if kind == "badRateLimit":
                time_left = _to_number(result.payload.get("data", {}).get("timeLeft"))
                retry_after = (
                    float(time_left) / 1000 if time_left is not None else None
                )
                return SubmitResult(
                    correct=False,
                    message=message or "Rate limited — slow down.",
                    kind=kind,
                    retry_after_seconds=retry_after,
                )
        else:
            message = f"rCTF returned HTTP {result.status} (non-JSON)."

        if kind == "goodFlag":
            return SubmitResult(
                correct=True,
                message=message or _RCTF_SUBMIT_MESSAGES["goodFlag"],
                kind=kind,
            )

        return SubmitResult(
            correct=False,
            message=message or _RCTF_SUBMIT_MESSAGES.get(kind or "", f"Submission failed (kind={kind})."),
            kind=kind,
            already_solved=kind == "badAlreadySolvedChallenge",
        )

    async def get_challenge_solvers(
        self, challenge_id: str, limit: int = 50,
    ) -> list[Solver]:
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            api_ver = await self._detect_api_version(session)
            url = (
                f"{self._base}api/v{api_ver}/challs/{challenge_id}"
                f"/solves?limit={limit}&offset=0"
            )
            result = await self._raw(session, "GET", url)

        if result.status in {403, 404} or not result.is_json or not result.payload:
            return []

        data = result.payload.get("data")
        if not isinstance(data, dict):
            return []

        raw_solves = data.get("solves")
        if not isinstance(raw_solves, list):
            return []

        solvers: list[Solver] = []
        for item in raw_solves[:limit]:
            if not isinstance(item, dict):
                continue
            name = item.get("userName") or item.get("name")
            if not name:
                continue
            solvers.append(Solver(
                name=str(name),
                id=str(item["userId"]) if item.get("userId") else None,
                solved_at=_epoch_ms_to_iso(item.get("createdAt")),
            ))
        return solvers

    async def get_platform_meta(self) -> PlatformMeta | None:
        url = f"{self._base}api/v1/integrations/client/config"
        try:
            async with aiohttp.ClientSession(
                headers=self._headers(authenticated=False),
            ) as session:
                payload = await self._get_json(session, url)
        except Exception:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        return PlatformMeta(
            name=str(data["ctfName"]) if data.get("ctfName") else None,
            start_time=_epoch_ms_to_iso(data.get("startTime")),
            end_time=_epoch_ms_to_iso(data.get("endTime")),
        )

    # -- rCTF-specific -------------------------------------------------------

    async def login_with_team_token(self, team_token: str) -> str | None:
        url = f"{self._base}api/v1/auth/login"
        body = {"teamToken": team_token}

        try:
            async with aiohttp.ClientSession(headers=_base_headers()) as session:
                result = await self._post_raw(session, url, body)
        except Exception as exc:
            log.error("rCTF login failed: %s", exc)
            return None

        if not result.is_json or not result.payload:
            log.warning("rCTF login returned non-JSON (HTTP %s)", result.status)
            return None

        kind = result.payload.get("kind")
        if kind != "goodLogin":
            log.warning("rCTF login returned kind=%s", kind)
            return None

        data = result.payload.get("data")
        if isinstance(data, dict):
            return data.get("authToken") or None
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

async def detect_platform_type(base_url: str) -> str | None:
    """Probe a URL and report whether it runs rCTF or CTFd.

    Returns None when neither fingerprint matches, so callers can ask the admin
    to pick instead of guessing — a wrong guess sends CTFd paths at an rCTF host
    and surfaces as an opaque 404.

    The probes are unauthenticated on purpose: both platforms answer their own
    challenge endpoint with a machine-readable JSON envelope even when they
    reject the request, and an unauthenticated probe never hands a token to a
    host that turns out to be the other platform.
    """
    try:
        rctf_base = RCTFAdapter._normalize_url(base_url)
    except RuntimeError:
        rctf_base = None
    try:
        ctfd_base = CTFdAdapter._normalize_url(base_url)
    except RuntimeError:
        ctfd_base = None
    if rctf_base is None and ctfd_base is None:
        return None

    async with aiohttp.ClientSession(headers=_base_headers()) as session:
        # rCTF first: it tags every response with `kind`, and CTFd answers the
        # rCTF path with a plain 404 that carries no such key.
        if rctf_base is not None:
            try:
                result = await _request_json(
                    session, "GET", f"{rctf_base}api/v1/challs", platform="rCTF",
                )
            except Exception:
                result = None
            if result is not None and result.is_json and result.payload is not None:
                if "kind" in result.payload:
                    return "rctf"

        if ctfd_base is not None:
            try:
                result = await _request_json(
                    session, "GET", f"{ctfd_base}api/v1/challenges", platform="CTFd",
                )
            except Exception:
                result = None
            if result is not None and result.is_json and result.payload is not None:
                payload = result.payload
                if "success" in payload or "data" in payload or result.status in {401, 403}:
                    return "ctfd"

    return None


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
