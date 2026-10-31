import asyncio
import json
import logging
import re
import time
import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session, get_db
from app.engine import engine_manager, load_pending_items
from app.models import QueueItem, Space, Vote
from app.schemas import AddTrackRequest, SearchResult, VoteRequest
from app.spotify_client import SpotifyError, api, track_summary

router = APIRouter(prefix="/api/spaces/{code}", tags=["queue"])
settings = get_settings()
logger = logging.getLogger(__name__)

MAX_PENDING = 50
HEARTBEAT_SECONDS = 15
OFFLINE_RECHECK_SECONDS = 10
_TRACK_ID = re.compile(r"^[A-Za-z0-9]{22}$")

_SEARCH_TTL = 300.0
_search_cache: dict[tuple[uuid.UUID, str], tuple[float, list[dict]]] = {}
# Server-side metadata for tracks we've shown guests, so /add can't be spoofed with fake names/art.
_track_cache: dict[str, dict] = {}


def _remember_tracks(tracks: list[dict]) -> None:
    if len(_track_cache) > 5000:
        _track_cache.clear()
    for t in tracks:
        _track_cache[t["track_id"]] = t


async def _get_space(code: str, db: AsyncSession) -> Space:
    space = (await db.execute(select(Space).where(Space.code == code))).scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    return space


def _set_voter_cookie(response: Response, vid: str) -> None:
    response.set_cookie(
        key="voter_id",
        value=vid,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=settings.app_url.startswith("https"),
    )


def _valid_voter_id(voter_id: str | None) -> str | None:
    if not voter_id:
        return None
    try:
        return str(uuid.UUID(voter_id))
    except ValueError:
        return None


@router.get("/search", response_model=list[SearchResult])
async def search_tracks(code: str, q: str = "", db: AsyncSession = Depends(get_db)):
    """Search Spotify catalog using the space owner's credentials."""
    q = q.strip()
    if len(q) < 2:
        return []
    space = await _get_space(code, db)
    await db.close()

    key = (space.owner_id, q.lower())
    cached = _search_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    try:
        data = await api(space.owner_id, "GET", "/search", params={"q": q[:100], "type": "track", "limit": 10})
    except SpotifyError as e:
        logger.warning(f"Spotify search failed for space {code}: {e}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Search failed, try again")

    results = [track_summary(t) for t in (data or {}).get("tracks", {}).get("items", []) if t and t.get("id")]
    _remember_tracks(results)
    if len(_search_cache) > 1000:
        _search_cache.clear()
    _search_cache[key] = (time.monotonic() + _SEARCH_TTL, results)
    return results


@router.get("/recommendations", response_model=list[SearchResult])
async def get_recommendations(code: str, db: AsyncSession = Depends(get_db)):
    space = await _get_space(code, db)
    engine = engine_manager.get(space.id)
    return engine.recommendations if engine else []


async def _resolve_track(track_id: str, space: Space) -> dict:
    engine = engine_manager.get(space.id)
    cached = _track_cache.get(track_id) or (engine.find_track(track_id) if engine else None)
    if cached:
        return cached
    owner_id = space.owner_id
    try:
        data = await api(owner_id, "GET", f"/tracks/{track_id}")
    except SpotifyError as e:
        if e.status in (400, 404):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Spotify unavailable, try again")
    if not data or not data.get("id"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")
    track = track_summary(data)
    _remember_tracks([track])
    return track


@router.post("/add", status_code=status.HTTP_201_CREATED)
async def add_track(
    code: str,
    body: AddTrackRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    voter_id: str | None = Cookie(default=None),
):
    """Add a track to the space's virtual queue."""
    if not _TRACK_ID.match(body.track_id):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid track id")
    space = await _get_space(code, db)

    pending_count = (
        await db.execute(
            select(func.count()).where(QueueItem.space_id == space.id, QueueItem.status == "pending")
        )
    ).scalar_one()
    if pending_count >= MAX_PENDING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Queue is full (max {MAX_PENDING} songs)")

    track = await _resolve_track(body.track_id, space)
    vid = _valid_voter_id(voter_id) or str(uuid.uuid4())

    item = QueueItem(
        space_id=space.id,
        track_id=track["track_id"],
        name=track["name"][:255],
        artist=track["artist"][:255],
        album_art=track["album_art"],
        duration_ms=track["duration_ms"],
        vote_count=1,
        status="pending",
    )
    db.add(item)
    try:
        await db.flush()
        db.add(Vote(queue_item_id=item.id, voter_id=vid, vote_type="up"))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Track already in queue")

    engine_manager.queue_changed(space.id)
    _set_voter_cookie(response, vid)
    return {"status": "added", "track_id": item.track_id, "vote_count": 1}


async def _apply_vote(
    code: str, track_id: str, direction: str, response: Response, db: AsyncSession, voter_id: str | None
) -> dict:
    """Toggle an up/down vote. Uses atomic SQL increments so concurrent votes aren't lost."""
    row = (
        await db.execute(
            select(QueueItem.id, QueueItem.space_id)
            .join(Space, Space.id == QueueItem.space_id)
            .where(Space.code == code, QueueItem.track_id == track_id, QueueItem.status == "pending")
        )
    ).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found in queue")
    item_id, space_id = row

    vid = _valid_voter_id(voter_id) or str(uuid.uuid4())
    sign = 1 if direction == "up" else -1

    existing = (
        await db.execute(select(Vote).where(Vote.queue_item_id == item_id, Vote.voter_id == vid))
    ).scalar_one_or_none()

    if existing and existing.vote_type == direction:
        await db.delete(existing)
        delta, my_vote = -sign, None
    elif existing:
        existing.vote_type = direction
        delta, my_vote = 2 * sign, direction
    else:
        db.add(Vote(queue_item_id=item_id, voter_id=vid, vote_type=direction))
        delta, my_vote = sign, direction

    try:
        vote_count = (
            await db.execute(
                update(QueueItem)
                .where(QueueItem.id == item_id)
                .values(vote_count=QueueItem.vote_count + delta)
                .returning(QueueItem.vote_count)
            )
        ).scalar_one()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vote already in progress")

    engine_manager.queue_changed(space_id)
    _set_voter_cookie(response, vid)
    return {"track_id": track_id, "vote_count": vote_count, "my_vote": my_vote}


@router.post("/vote")
async def vote_track(
    code: str, body: VoteRequest, response: Response,
    db: AsyncSession = Depends(get_db), voter_id: str | None = Cookie(default=None),
):
    return await _apply_vote(code, body.track_id, "up", response, db, voter_id)


@router.post("/downvote")
async def downvote_track(
    code: str, body: VoteRequest, response: Response,
    db: AsyncSession = Depends(get_db), voter_id: str | None = Cookie(default=None),
):
    return await _apply_vote(code, body.track_id, "down", response, db, voter_id)


async def _my_votes(code: str, voter_id: str | None, db: AsyncSession) -> dict[str, str]:
    vid = _valid_voter_id(voter_id)
    if not vid:
        return {}
    result = await db.execute(
        select(QueueItem.track_id, Vote.vote_type)
        .join(QueueItem, QueueItem.id == Vote.queue_item_id)
        .join(Space, Space.id == QueueItem.space_id)
        .where(Space.code == code, QueueItem.status == "pending", Vote.voter_id == vid)
    )
    return {track_id: vote_type for track_id, vote_type in result.all()}


@router.get("/my-votes")
async def my_votes(code: str, db: AsyncSession = Depends(get_db), voter_id: str | None = Cookie(default=None)):
    """This guest's votes on pending tracks: {track_id: "up" | "down"}."""
    return await _my_votes(code, voter_id, db)


def _offline_state(space: Space, pending: list[dict]) -> dict:
    return {
        "active": False,
        "space_name": space.name,
        "now_playing": None,
        "up_next": None,
        "queue": pending,
        "spotify_queue": [],
        "recommendations": [],
    }


@router.get("/queue")
async def get_queue(code: str, db: AsyncSession = Depends(get_db), voter_id: str | None = Cookie(default=None)):
    """One-shot snapshot (the live pages use /events instead)."""
    space = await _get_space(code, db)
    engine = engine_manager.get(space.id)
    state = json.loads(engine.payload()) if engine else _offline_state(space, await load_pending_items(db, space.id))
    state["my_votes"] = await _my_votes(code, voter_id, db)
    return state


async def _event_stream(space_id: uuid.UUID):
    yield "retry: 3000\n\n"
    last_offline = None
    while True:
        engine = engine_manager.get(space_id)
        if engine is None:
            async with async_session() as db:
                space = await db.get(Space, space_id)
                if space is None:
                    yield f"data: {json.dumps({'deleted': True})}\n\n"
                    return
                data = json.dumps(_offline_state(space, await load_pending_items(db, space_id)), separators=(",", ":"))
            yield f"data: {data}\n\n" if data != last_offline else ": ping\n\n"
            last_offline = data
            await asyncio.sleep(OFFLINE_RECHECK_SECONDS)
            continue

        last_offline = None
        sub = engine.subscribe()
        try:
            while True:
                try:
                    await asyncio.wait_for(sub.event.wait(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                sub.event.clear()
                if sub.latest is None:
                    break
                yield f"data: {sub.latest}\n\n"
        finally:
            engine.unsubscribe(sub)


@router.get("/events")
async def queue_events(code: str, db: AsyncSession = Depends(get_db)):
    """Server-Sent Events stream of the space state. Pushed only when something changes."""
    space = await _get_space(code, db)
    await db.close()
    return StreamingResponse(
        _event_stream(space.id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
