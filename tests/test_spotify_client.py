import time
import uuid

import httpx
import pytest

from app import spotify_client as sc
from app.auth import create_access_token, create_oauth_state, decode_access_token, verify_oauth_state


@pytest.fixture
def mock_spotify(monkeypatch):
    calls = []

    def install(handler):
        def wrapped(request: httpx.Request):
            calls.append(request)
            return handler(request)

        monkeypatch.setattr(sc, "_client", httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
        return calls

    yield install
    sc._blocked_until.clear()
    sc._tokens.clear()


def seed_token(user_id, token="tok"):
    sc._tokens[user_id] = (token, time.time() + 3600)


async def test_api_returns_json_and_none_for_204(mock_spotify):
    user = uuid.uuid4()
    seed_token(user)
    mock_spotify(lambda r: httpx.Response(200, json={"ok": True}) if r.url.path.endswith("/me") else httpx.Response(204))

    assert await sc.api(user, "GET", "/me") == {"ok": True}
    assert await sc.api(user, "GET", "/me/player") is None


async def test_rate_limit_blocks_further_calls_without_hitting_spotify(mock_spotify):
    user = uuid.uuid4()
    seed_token(user)
    calls = mock_spotify(lambda r: httpx.Response(429, headers={"Retry-After": "30"}))

    with pytest.raises(sc.SpotifyRateLimited) as e:
        await sc.api(user, "GET", "/me/player")
    assert e.value.retry_after == 30
    with pytest.raises(sc.SpotifyRateLimited):
        await sc.api(user, "GET", "/me/player")
    assert len(calls) == 1


async def test_expired_token_is_refreshed_once_and_retried(mock_spotify, monkeypatch):
    user = uuid.uuid4()
    seed_token(user, "old")

    async def fake_access_token(user_id):
        return sc._tokens.get(user_id, ("new", 0))[0]

    monkeypatch.setattr(sc, "access_token", fake_access_token)
    calls = mock_spotify(
        lambda r: httpx.Response(401) if r.headers["Authorization"] == "Bearer old" else httpx.Response(200, json={"id": "x"})
    )

    assert await sc.api(user, "GET", "/me") == {"id": "x"}
    assert [c.headers["Authorization"] for c in calls] == ["Bearer old", "Bearer new"]


async def test_errors_raise_spotify_error(mock_spotify):
    user = uuid.uuid4()
    seed_token(user)
    mock_spotify(lambda r: httpx.Response(404, text="no device"))
    with pytest.raises(sc.SpotifyError) as e:
        await sc.api(user, "PUT", "/me/player/play")
    assert e.value.status == 404


def test_track_summary_handles_missing_art():
    t = {"id": "a" * 22, "name": "N", "artists": [{"name": "X"}, {"name": "Y"}], "album": {"images": []}, "duration_ms": 5}
    assert sc.track_summary(t) == {"track_id": "a" * 22, "name": "N", "artist": "X, Y", "album_art": "", "duration_ms": 5}


def test_token_encryption_roundtrip():
    assert sc.decrypt(sc.encrypt("secret")) == "secret"


def test_authorize_url_contains_state():
    url = sc.authorize_url("http://localhost/cb", sc.HOST_SCOPES, "STATE123")
    assert "state=STATE123" in url and "response_type=code" in url


def test_oauth_state_roundtrip_and_purpose_check():
    state = create_oauth_state("spotify_link", "user-1")
    assert verify_oauth_state(state, "spotify_link") == "user-1"
    assert verify_oauth_state(state, "spotify_login") is None
    assert verify_oauth_state("garbage", "spotify_link") is None
    # A state token must never work as a login session token.
    assert decode_access_token(state) is None


def test_access_token_roundtrip():
    uid = uuid.uuid4()
    assert decode_access_token(create_access_token(uid)) == str(uid)
