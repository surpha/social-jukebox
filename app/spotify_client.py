"""Async Spotify Web API client with per-user token caching and single-flight refresh."""
import asyncio
import base64
import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models import User

logger = logging.getLogger(__name__)
settings = get_settings()

API_BASE = "https://api.spotify.com/v1"
ACCOUNTS_BASE = "https://accounts.spotify.com"
HOST_SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing user-top-read"
LOGIN_SCOPES = HOST_SCOPES + " user-read-email user-read-private"

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()))


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()


class SpotifyError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"Spotify {status}: {message}")
        self.status = status


class SpotifyRateLimited(SpotifyError):
    def __init__(self, retry_after: float):
        super().__init__(429, f"rate limited for {retry_after:.0f}s")
        self.retry_after = retry_after


class SpotifyNotLinked(SpotifyError):
    def __init__(self):
        super().__init__(401, "Spotify not linked")


_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(6.0, connect=3.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _client


async def close_http() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# --- OAuth helpers ---

def authorize_url(redirect_uri: str, scope: str, state: str) -> str:
    params = {
        "client_id": settings.spotify_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "show_dialog": "true",
    }
    return f"{ACCOUNTS_BASE}/authorize?{urlencode(params)}"


async def _token_request(data: dict) -> dict:
    resp = await http().post(
        f"{ACCOUNTS_BASE}/api/token",
        data=data,
        auth=(settings.spotify_client_id, settings.spotify_client_secret),
    )
    if resp.status_code != 200:
        raise SpotifyError(resp.status_code, resp.text[:200])
    token = resp.json()
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600))
    return token


async def exchange_code(code: str, redirect_uri: str) -> dict:
    return await _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})


def store_tokens(user: User, token: dict) -> None:
    user.spotify_access_token = encrypt(token["access_token"])
    if token.get("refresh_token"):
        user.spotify_refresh_token = encrypt(token["refresh_token"])
    user.spotify_token_expires = datetime.fromtimestamp(token["expires_at"], tz=timezone.utc)


# --- Token manager ---

_tokens: dict[uuid.UUID, tuple[str, float]] = {}
_token_locks: dict[uuid.UUID, asyncio.Lock] = {}
_blocked_until: dict[uuid.UUID, float] = {}
_EXPIRY_MARGIN = 60


def invalidate_token(user_id: uuid.UUID) -> None:
    _tokens.pop(user_id, None)


async def access_token(user_id: uuid.UUID) -> str:
    cached = _tokens.get(user_id)
    if cached and cached[1] - _EXPIRY_MARGIN > time.time():
        return cached[0]

    async with _token_locks.setdefault(user_id, asyncio.Lock()):
        cached = _tokens.get(user_id)
        if cached and cached[1] - _EXPIRY_MARGIN > time.time():
            return cached[0]

        async with async_session() as db:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
            if not user or not user.spotify_refresh_token:
                raise SpotifyNotLinked()

            expires = user.spotify_token_expires.timestamp() if user.spotify_token_expires else 0
            if user.spotify_access_token and expires - _EXPIRY_MARGIN > time.time():
                token = decrypt(user.spotify_access_token)
            else:
                refreshed = await _token_request(
                    {"grant_type": "refresh_token", "refresh_token": decrypt(user.spotify_refresh_token)}
                )
                store_tokens(user, refreshed)
                await db.commit()
                token, expires = refreshed["access_token"], refreshed["expires_at"]

        _tokens[user_id] = (token, expires)
        return token


async def request_with_token(token: str, method: str, path: str, **kwargs) -> dict | None:
    resp = await http().request(method, f"{API_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs)
    if resp.status_code == 429:
        raise SpotifyRateLimited(float(resp.headers.get("Retry-After", "5")))
    if resp.status_code >= 400:
        raise SpotifyError(resp.status_code, resp.text[:200])
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


async def api(user_id: uuid.UUID, method: str, path: str, **kwargs) -> dict | None:
    """Call the Spotify Web API as `user_id`. Retries once on an expired token."""
    blocked = _blocked_until.get(user_id, 0) - time.monotonic()
    if blocked > 0:
        raise SpotifyRateLimited(blocked)

    for attempt in range(2):
        token = await access_token(user_id)
        try:
            return await request_with_token(token, method, path, **kwargs)
        except SpotifyRateLimited as e:
            _blocked_until[user_id] = time.monotonic() + e.retry_after
            raise
        except SpotifyError as e:
            if e.status == 401 and attempt == 0:
                invalidate_token(user_id)
                continue
            raise
    raise SpotifyError(401, "token rejected")


def track_summary(t: dict) -> dict:
    images = (t.get("album") or {}).get("images") or []
    return {
        "track_id": t["id"],
        "name": t["name"],
        "artist": ", ".join(a["name"] for a in t.get("artists", [])),
        "album_art": images[0]["url"] if images else "",
        "duration_ms": t.get("duration_ms", 0),
    }
