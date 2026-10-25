"""End-to-end API tests against a throwaway Postgres (skipped if pgserver isn't installed)."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from tests.conftest import HAVE_TEST_DB
from tests.test_engine import FakeSpotify

pytestmark = pytest.mark.skipif(not HAVE_TEST_DB, reason="pgserver not installed")

TRACK_A = "A" * 22
TRACK_B = "B" * 22


class FakeLive(FakeSpotify):
    def __init__(self, owner_id):
        super().__init__()
        self.owner_id = owner_id


@pytest.fixture
async def ctx(monkeypatch):
    from app import database, engine as engine_mod
    from app.auth import create_access_token
    from app.main import app
    from app.models import User
    from app.routers import queue
    from app.spotify_client import encrypt

    monkeypatch.setattr(engine_mod, "LiveSpotify", FakeLive)
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.drop_all)
    await database.init_db()

    async with database.async_session() as db:
        user = User(
            email="host@example.com", name="Host",
            spotify_access_token=encrypt("x"), spotify_refresh_token=encrypt("y"),
            spotify_token_expires=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    for tid in (TRACK_A, TRACK_B):
        queue._track_cache[tid] = {"track_id": tid, "name": f"Song {tid[0]}", "artist": "Artist", "album_art": "", "duration_ms": 1000}

    transport = httpx.ASGITransport(app=app)
    auth = {"Authorization": f"Bearer {create_access_token(user.id)}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as host, \
            httpx.AsyncClient(transport=transport, base_url="http://test") as guest:
        yield {"host": host, "guest": guest, "auth": auth, "user": user}

    await engine_mod.engine_manager.stop_all()
    await database.engine.dispose()


async def create_space(ctx, name="Party") -> str:
    res = await ctx["host"].post("/api/spaces", json={"name": name}, headers=ctx["auth"])
    assert res.status_code == 201, res.text
    return res.json()["code"]


async def test_add_vote_toggle_and_my_votes(ctx):
    host, guest = ctx["host"], ctx["guest"]
    code = await create_space(ctx)

    page = await guest.get(f"/s/{code}")
    assert page.status_code == 200 and code in page.text

    res = await host.post(f"/api/spaces/{code}/add", json={"track_id": TRACK_A, "name": "<script>spoof</script>"})
    assert res.status_code == 201
    assert (await host.post(f"/api/spaces/{code}/add", json={"track_id": TRACK_A})).status_code == 409
    assert (await host.post(f"/api/spaces/{code}/add", json={"track_id": "bad id"})).status_code == 422

    state = (await guest.get(f"/api/spaces/{code}/queue")).json()
    assert state["queue"][0]["name"] == "Song A"  # server-side metadata, not the spoofed name
    assert state["active"] is False

    assert (await host.get(f"/api/spaces/{code}/my-votes")).json() == {TRACK_A: "up"}

    r = (await guest.post(f"/api/spaces/{code}/vote", json={"track_id": TRACK_A})).json()
    assert r == {"track_id": TRACK_A, "vote_count": 2, "my_vote": "up"}
    r = (await guest.post(f"/api/spaces/{code}/downvote", json={"track_id": TRACK_A})).json()
    assert r["vote_count"] == 0 and r["my_vote"] == "down"
    r = (await guest.post(f"/api/spaces/{code}/downvote", json={"track_id": TRACK_A})).json()
    assert r["vote_count"] == 1 and r["my_vote"] is None
    assert (await guest.get(f"/api/spaces/{code}/my-votes")).json() == {}

    assert (await guest.post(f"/api/spaces/{code}/vote", json={"track_id": TRACK_B})).status_code == 404
    assert (await guest.get("/api/spaces/NOPE00/queue")).status_code == 404


async def test_concurrent_votes_are_not_lost(ctx):
    code = await create_space(ctx)
    await ctx["host"].post(f"/api/spaces/{code}/add", json={"track_id": TRACK_A})
    transport = ctx["host"]._transport

    async def one_guest():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.post(f"/api/spaces/{code}/vote", json={"track_id": TRACK_A})

    results = await asyncio.gather(*(one_guest() for _ in range(15)))
    assert all(r.status_code == 200 for r in results)
    state = (await ctx["guest"].get(f"/api/spaces/{code}/queue")).json()
    assert state["queue"][0]["vote_count"] == 16


async def test_live_engine_flow(ctx):
    from app.engine import engine_manager
    from app.database import async_session
    from app.models import QueueItem, Space, Vote

    host, guest = ctx["host"], ctx["guest"]
    code = await create_space(ctx)
    await host.post(f"/api/spaces/{code}/add", json={"track_id": TRACK_A})
    await host.post(f"/api/spaces/{code}/add", json={"track_id": TRACK_B})

    assert (await host.patch(f"/api/spaces/{code}/activate", headers=ctx["auth"])).status_code == 200
    async with async_session() as db:
        space = (await db.execute(select(Space).where(Space.code == code))).scalar_one()
    engine = engine_manager.get(space.id)
    assert engine is not None and len(engine.pending) == 2

    # A guest vote reaches the engine's in-memory state via the coalesced reload.
    sub = engine.subscribe()
    sub.event.clear()
    await guest.post(f"/api/spaces/{code}/vote", json={"track_id": TRACK_B})
    await asyncio.sleep(0.4)
    assert engine.pending[0]["track_id"] == TRACK_B and engine.pending[0]["vote_count"] == 2
    assert sub.event.is_set()

    # Near the end of the current song the top-voted track is handed to Spotify and persisted as queued.
    engine.spotify.play("current0000000000000000", progress=990_000, duration=1_000_000)
    await engine.tick()
    assert engine.spotify.added == [TRACK_B]
    async with async_session() as db:
        statuses = dict((await db.execute(select(QueueItem.track_id, QueueItem.status))).all())
    assert statuses == {TRACK_A: "pending", TRACK_B: "queued"}

    state = (await guest.get(f"/api/spaces/{code}/queue")).json()
    assert state["active"] and state["up_next"]["track_id"] == TRACK_B
    assert [q["track_id"] for q in state["queue"]] == [TRACK_A]

    # Once it plays, the row and its votes are removed.
    engine.spotify.play(TRACK_B, progress=100)
    await engine.tick()
    async with async_session() as db:
        assert (await db.execute(select(QueueItem.track_id))).scalars().all() == [TRACK_A]
        assert len((await db.execute(select(Vote))).scalars().all()) == 1

    # Deactivate stops the engine and clears the queue.
    assert (await host.patch(f"/api/spaces/{code}/deactivate", headers=ctx["auth"])).status_code == 200
    assert engine_manager.get(space.id) is None
    assert sub.latest is None
    assert (await guest.get(f"/api/spaces/{code}/queue")).json()["queue"] == []

    assert (await host.delete(f"/api/spaces/{code}", headers=ctx["auth"])).status_code == 204
    assert (await guest.get(f"/api/spaces/{code}/queue")).status_code == 404


async def test_restart_recovers_active_engines(ctx):
    from app.engine import engine_manager

    code = await create_space(ctx)
    await ctx["host"].post(f"/api/spaces/{code}/add", json={"track_id": TRACK_A})
    await ctx["host"].patch(f"/api/spaces/{code}/activate", headers=ctx["auth"])
    await engine_manager.stop_all()

    await engine_manager.restart_active()
    engines = list(engine_manager._engines.values())
    assert len(engines) == 1 and engines[0].pending[0]["track_id"] == TRACK_A


async def test_auth_and_oauth_guards(ctx):
    host = ctx["host"]
    assert (await host.get("/api/spaces")).status_code == 401
    assert (await host.post("/api/spaces", json={"name": ""}, headers=ctx["auth"])).status_code == 422

    forged = await host.get("/api/spotify/callback", params={"code": "c", "state": str(ctx["user"].id)})
    assert forged.status_code == 400

    login = await host.get("/api/auth/spotify/callback", params={"code": "c", "state": "x"}, follow_redirects=False)
    assert login.status_code == 307 and "error=spotify_denied" in login.headers["location"]

    start = await host.get("/api/auth/spotify/login")
    assert "state=" in start.json()["auth_url"] and "oauth_nonce" in start.cookies

    assert (await host.post("/api/spotify/playback/explode", headers=ctx["auth"])).status_code == 404


async def test_cors_is_restricted(ctx):
    res = await ctx["host"].options(
        "/api/spaces", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert res.headers.get("access-control-allow-origin") != "https://evil.example"
