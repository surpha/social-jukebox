import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import QueueItem, Space, User, Vote
from app.routers.spotify import get_spotify_client_for_user, spotify_call
from app.schemas import (
    AddTrackRequest,
    NowPlayingResponse,
    QueueItemResponse,
    QueueResponse,
    SearchResult,
    VoteRequest,
)

router = APIRouter(prefix="/api/spaces/{code}", tags=["queue"])
settings = get_settings()
logger = logging.getLogger(__name__)

# Every guest polls /queue every few seconds; share one Spotify fetch per space.
_PLAYBACK_TTL = 3.0
_RECS_TTL = 300.0
_playback_cache: dict[uuid.UUID, tuple[float, dict | None, list]] = {}
_playback_locks: dict[uuid.UUID, asyncio.Lock] = {}
_recs_cache: dict[tuple[uuid.UUID, str | None], tuple[float, list]] = {}
_recs_locks: dict[uuid.UUID, asyncio.Lock] = {}


async def _get_playback_and_queue(space: Space, owner: User, db: AsyncSession) -> tuple[dict | None, list]:
    """Return (current_playback, upcoming_queue) for a space, cached for a few seconds."""
    cached = _playback_cache.get(space.id)
    if cached and cached[0] > time.monotonic():
        return cached[1], cached[2]

    lock = _playback_locks.setdefault(space.id, asyncio.Lock())
    async with lock:
        cached = _playback_cache.get(space.id)
        if cached and cached[0] > time.monotonic():
            return cached[1], cached[2]

        playback, upcoming = None, []
        try:
            sp = await get_spotify_client_for_user(owner, db)
            playback, queue_data = await asyncio.gather(
                spotify_call(sp.current_playback),
                spotify_call(sp.queue),
                return_exceptions=True,
            )
            if isinstance(playback, Exception):
                logger.warning(f"Spotify playback fetch failed for space {space.code}: {type(playback).__name__}")
                playback = None
            if not isinstance(queue_data, Exception) and queue_data:
                upcoming = (queue_data.get("queue") or [])[:20]
        except Exception as e:
            logger.warning(f"Spotify client failed for space {space.code}: {type(e).__name__}")

        _playback_cache[space.id] = (time.monotonic() + _PLAYBACK_TTL, playback, upcoming)
        return playback, upcoming


def _invalidate_playback(space_id: uuid.UUID) -> None:
    _playback_cache.pop(space_id, None)


def _to_search_result(t: dict) -> SearchResult:
    return SearchResult(
        track_id=t["id"],
        name=t["name"],
        artist=", ".join(a["name"] for a in t["artists"]),
        album_art=t["album"]["images"][0]["url"] if t["album"]["images"] else "",
        duration_ms=t["duration_ms"],
    )


async def _get_space_with_owner(code: str, db: AsyncSession) -> tuple[Space, User]:
    """Get space and eagerly load owner for Spotify access."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(Space).where(Space.code == code).options(selectinload(Space.owner))
    )
    space = result.scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    return space, space.owner


def _get_voter_id(voter_id: str | None) -> str:
    """Get or generate a voter ID."""
    if voter_id:
        return voter_id
    return str(uuid.uuid4())


@router.get("/search", response_model=list[SearchResult])
async def search_tracks(
    code: str,
    q: str,
    db: AsyncSession = Depends(get_db),
):
    """Search Spotify catalog using the space owner's credentials."""
    if not q or len(q.strip()) < 2:
        return []

    space, owner = await _get_space_with_owner(code, db)
    sp = await get_spotify_client_for_user(owner, db)

    try:
        results = await spotify_call(sp.search, q=q, type="track", limit=10)
    except Exception as e:
        logger.warning(f"Spotify search failed for space {code}: {type(e).__name__}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Search failed, try again")
    tracks = results.get("tracks", {}).get("items", [])

    return [_to_search_result(t) for t in tracks if t]


@router.get("/recommendations", response_model=list[SearchResult])
async def get_recommendations(
    code: str,
    db: AsyncSession = Depends(get_db),
):
    """Get recommendations closely related to what's currently playing and queued."""
    space, owner = await _get_space_with_owner(code, db)

    seed_tracks = []
    seed_artists = []

    # 1. Currently playing track (primary seed)
    playback, _ = await _get_playback_and_queue(space, owner, db)
    current_id = None
    if playback and playback.get("item"):
        item = playback["item"]
        current_id = item["id"]
        seed_tracks.append(item["id"])
        if item.get("artists"):
            seed_artists.append(item["artists"][0]["id"])

    # Recommendations only change with the song; reuse them for every guest.
    cache_key = (space.id, current_id)
    cached = _recs_cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    # All guests refetch at the same moment when the song changes; compute once.
    async with _recs_locks.setdefault(space.id, asyncio.Lock()):
        cached = _recs_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        recs = await _compute_recommendations(space, owner, db, seed_tracks, seed_artists)
        if len(_recs_cache) > 500:
            _recs_cache.clear()
        _recs_cache[cache_key] = (time.monotonic() + _RECS_TTL, recs)
        return recs


async def _compute_recommendations(
    space: Space, owner: User, db: AsyncSession, seed_tracks: list, seed_artists: list
) -> list[SearchResult]:
    import random

    sp = await get_spotify_client_for_user(owner, db)

    # 2. Tracks from the SJ queue (secondary seeds)
    try:
        result = await db.execute(
            select(QueueItem)
            .where(QueueItem.space_id == space.id, QueueItem.status.in_(["pending", "queued"]))
            .order_by(QueueItem.vote_count.desc())
            .limit(3)
        )
        queue_items = result.scalars().all()
        seed_tracks.extend([item.track_id for item in queue_items])
    except Exception:
        pass

    # Deduplicate
    seed_tracks = list(dict.fromkeys(seed_tracks))
    seed_artists = list(dict.fromkeys(seed_artists))

    # Spotify allows max 5 total seeds (tracks + artists combined)
    # Prioritize: current track + its artist + queue tracks
    if seed_tracks and seed_artists:
        # Use up to 3 track seeds + up to 2 artist seeds
        use_tracks = seed_tracks[:3]
        use_artists = seed_artists[:2]
        # Ensure total <= 5
        total = len(use_tracks) + len(use_artists)
        if total > 5:
            use_tracks = use_tracks[:5 - len(use_artists)]
    elif seed_tracks:
        use_tracks = seed_tracks[:5]
        use_artists = []
    else:
        use_tracks = []
        use_artists = []

    # Try Spotify recommendations API with tight seeds
    if use_tracks or use_artists:
        try:
            kwargs = {"limit": 15}
            if use_tracks:
                kwargs["seed_tracks"] = use_tracks
            if use_artists:
                kwargs["seed_artists"] = use_artists
            recs = await spotify_call(sp.recommendations, **kwargs)
            tracks = recs.get("tracks", [])
            # Filter out tracks already in queue or currently playing
            existing_ids = set(seed_tracks)
            tracks = [t for t in tracks if t["id"] not in existing_ids]
            if tracks:
                return [_to_search_result(t) for t in tracks[:6]]
        except Exception:
            pass

    # Fallback: related artists tracks
    if seed_artists:
        try:
            related = await spotify_call(sp.artist_related_artists, seed_artists[0])
            if related.get("artists"):
                artist_ids = [a["id"] for a in related["artists"][:3]]
                random.shuffle(artist_ids)
                top_tracks = await spotify_call(sp.artist_top_tracks, artist_ids[0])
                tracks = top_tracks.get("tracks", [])
                existing_ids = set(seed_tracks)
                tracks = [t for t in tracks if t["id"] not in existing_ids]
                if tracks:
                    return [_to_search_result(t) for t in tracks[:6]]
        except Exception:
            pass

    # Last resort: search for similar artist
    try:
        if seed_artists:
            artist_info = await spotify_call(sp.artist, seed_artists[0])
            query = f"artist:{artist_info['name']}"
        else:
            query = "top hits"
        results = await spotify_call(sp.search, q=query, type="track", limit=6)
        tracks = results.get("tracks", {}).get("items", [])
        return [_to_search_result(t) for t in tracks if t]
    except Exception:
        return []


@router.post("/add", status_code=status.HTTP_201_CREATED)
async def add_track(
    code: str,
    body: AddTrackRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    voter_id: str | None = Cookie(default=None),
):
    """Add a track to the space's virtual queue."""
    space, _ = await _get_space_with_owner(code, db)

    # Check queue limit (max 50 pending songs per space)
    from sqlalchemy import func

    count_result = await db.execute(
        select(func.count()).where(
            QueueItem.space_id == space.id,
            QueueItem.status == "pending",
        )
    )
    pending_count = count_result.scalar()
    if pending_count >= 50:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Queue is full (max 50 songs)")

    # Check for duplicate pending track
    result = await db.execute(
        select(QueueItem).where(
            QueueItem.space_id == space.id,
            QueueItem.track_id == body.track_id,
            QueueItem.status == "pending",
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Track already in queue")

    # Create queue item
    item = QueueItem(
        space_id=space.id,
        track_id=body.track_id,
        name=body.name,
        artist=body.artist,
        album_art=body.album_art,
        duration_ms=body.duration_ms,
        vote_count=1,
        status="pending",
    )
    db.add(item)
    try:
        await db.flush()
        # Record the initial vote
        vid = _get_voter_id(voter_id)
        db.add(Vote(queue_item_id=item.id, voter_id=vid))
        await db.commit()
    except IntegrityError:
        # Another guest added the same track at the same moment.
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Track already in queue")

    _set_voter_cookie(response, vid)

    return {"status": "added", "track_id": body.track_id, "vote_count": 1}


def _set_voter_cookie(response: Response, vid: str) -> None:
    response.set_cookie(
        key="voter_id",
        value=vid,
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,
        samesite="lax",
    )


async def _apply_vote(
    code: str, track_id: str, direction: str, response: Response, db: AsyncSession, voter_id: str | None
) -> dict:
    """Toggle an up/down vote. Uses atomic SQL increments so concurrent votes aren't lost."""
    space, _ = await _get_space_with_owner(code, db)

    result = await db.execute(
        select(QueueItem.id).where(
            QueueItem.space_id == space.id,
            QueueItem.track_id == track_id,
            QueueItem.status == "pending",
        )
    )
    item_id = result.scalar_one_or_none()
    if not item_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found in queue")

    vid = _get_voter_id(voter_id)
    sign = 1 if direction == "up" else -1

    result = await db.execute(
        select(Vote).where(Vote.queue_item_id == item_id, Vote.voter_id == vid)
    )
    existing_vote = result.scalar_one_or_none()

    if existing_vote and existing_vote.vote_type == direction:
        await db.delete(existing_vote)
        delta = -sign
        new_status = "unvoted" if direction == "up" else "undownvoted"
    elif existing_vote:
        existing_vote.vote_type = direction
        delta = 2 * sign
        new_status = "voted" if direction == "up" else "downvoted"
    else:
        db.add(Vote(queue_item_id=item_id, voter_id=vid, vote_type=direction))
        delta = sign
        new_status = "voted" if direction == "up" else "downvoted"

    try:
        result = await db.execute(
            update(QueueItem)
            .where(QueueItem.id == item_id)
            .values(vote_count=QueueItem.vote_count + delta)
            .returning(QueueItem.vote_count)
        )
        vote_count = result.scalar_one()
        await db.commit()
    except IntegrityError:
        # Double-tap: the same voter's first vote is already being recorded.
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vote already in progress")

    _set_voter_cookie(response, vid)
    return {"status": new_status, "track_id": track_id, "vote_count": vote_count}


@router.post("/vote")
async def vote_track(
    code: str,
    body: VoteRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    voter_id: str | None = Cookie(default=None),
):
    """Toggle upvote on a track in the queue."""
    return await _apply_vote(code, body.track_id, "up", response, db, voter_id)


@router.post("/downvote")
async def downvote_track(
    code: str,
    body: VoteRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    voter_id: str | None = Cookie(default=None),
):
    """Toggle downvote on a track in the queue."""
    return await _apply_vote(code, body.track_id, "down", response, db, voter_id)


@router.get("/queue", response_model=QueueResponse)
async def get_queue(
    code: str,
    db: AsyncSession = Depends(get_db),
    voter_id: str | None = Cookie(default=None),
):
    """Get the current queue state: now playing + sorted pending tracks."""
    from app.schemas import UpNextResponse

    space, owner = await _get_space_with_owner(code, db)

    playback, spotify_queue_raw = await _get_playback_and_queue(space, owner, db)
    now_playing = None
    if playback and playback.get("item"):
        track = playback["item"]
        now_playing = NowPlayingResponse(
            track_id=track["id"],
            name=track["name"],
            artist=", ".join(a["name"] for a in track["artists"]),
            album_art=track["album"]["images"][0]["url"] if track["album"]["images"] else None,
            duration_ms=track["duration_ms"],
            progress_ms=playback.get("progress_ms", 0),
            is_playing=playback.get("is_playing", False),
        )

    # Read-only: the worker owns the on-deck item's lifecycle. Deleting it here
    # raced with Spotify's eventually-consistent queue and made "Up next" flap.
    up_next = None
    up_next_result = await db.execute(
        select(QueueItem)
        .where(QueueItem.space_id == space.id, QueueItem.status == "queued")
        .order_by(QueueItem.created_at.desc())
        .limit(1)
    )
    up_next_item = up_next_result.scalar_one_or_none()
    if up_next_item:
        up_next = UpNextResponse(
            track_id=up_next_item.track_id,
            name=up_next_item.name,
            artist=up_next_item.artist,
            album_art=up_next_item.album_art,
            duration_ms=up_next_item.duration_ms,
            vote_count=up_next_item.vote_count,
        )

    # Get sorted pending queue items
    result = await db.execute(
        select(QueueItem)
        .where(QueueItem.space_id == space.id, QueueItem.status == "pending")
        .order_by(QueueItem.vote_count.desc(), QueueItem.created_at.asc())
    )
    items = result.scalars().all()

    # Build Spotify queue list for display (filter out now playing, up_next, and SJ queue tracks)
    spotify_queue_items = []
    existing_track_ids = set()
    if now_playing:
        existing_track_ids.add(now_playing.track_id)
    if up_next and up_next_item:
        existing_track_ids.add(up_next_item.track_id)
    # Also filter out tracks already in SJ queue
    for item in items:
        existing_track_ids.add(item.track_id)

    for t in spotify_queue_raw:
        if t and t.get("id") and t["id"] not in existing_track_ids:
            spotify_queue_items.append(_to_search_result(t))
            if len(spotify_queue_items) >= 8:
                break

    # Check which items the voter has voted/downvoted for
    voted_item_ids = set()
    downvoted_item_ids = set()
    if voter_id:
        vote_result = await db.execute(
            select(Vote.queue_item_id, Vote.vote_type).where(Vote.voter_id == voter_id)
        )
        for row in vote_result.all():
            if row[1] == "down":
                downvoted_item_ids.add(row[0])
            else:
                voted_item_ids.add(row[0])

    queue_items = [
        QueueItemResponse(
            id=item.id,
            track_id=item.track_id,
            name=item.name,
            artist=item.artist,
            album_art=item.album_art,
            duration_ms=item.duration_ms,
            vote_count=item.vote_count,
            created_at=item.created_at,
            has_voted=item.id in voted_item_ids,
            has_downvoted=item.id in downvoted_item_ids,
        )
        for item in items
    ]

    return QueueResponse(now_playing=now_playing, up_next=up_next, queue=queue_items, spotify_queue=spotify_queue_items, space_name=space.name)
