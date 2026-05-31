import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import QueueItem, Space, User, Vote
from app.routers.spotify import get_spotify_client_for_user
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

    results = sp.search(q=q, type="track", limit=10)
    tracks = results.get("tracks", {}).get("items", [])

    return [
        SearchResult(
            track_id=t["id"],
            name=t["name"],
            artist=", ".join(a["name"] for a in t["artists"]),
            album_art=t["album"]["images"][0]["url"] if t["album"]["images"] else "",
            duration_ms=t["duration_ms"],
        )
        for t in tracks
    ]


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
    await db.flush()

    # Record the initial vote
    vid = _get_voter_id(voter_id)
    vote = Vote(queue_item_id=item.id, voter_id=vid)
    db.add(vote)
    await db.commit()

    # Set voter_id cookie if not present
    response.set_cookie(
        key="voter_id",
        value=vid,
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,
        samesite="lax",
    )

    return {"status": "added", "track_id": body.track_id, "vote_count": 1}


@router.post("/vote")
async def vote_track(
    code: str,
    body: VoteRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    voter_id: str | None = Cookie(default=None),
):
    """Upvote a track in the queue."""
    space, _ = await _get_space_with_owner(code, db)

    # Find the pending queue item
    result = await db.execute(
        select(QueueItem).where(
            QueueItem.space_id == space.id,
            QueueItem.track_id == body.track_id,
            QueueItem.status == "pending",
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found in queue")

    vid = _get_voter_id(voter_id)

    # Check if already voted
    result = await db.execute(
        select(Vote).where(Vote.queue_item_id == item.id, Vote.voter_id == vid)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already voted for this track")

    # Add vote
    vote = Vote(queue_item_id=item.id, voter_id=vid)
    db.add(vote)
    item.vote_count += 1
    await db.commit()

    # Set voter_id cookie if not present
    response.set_cookie(
        key="voter_id",
        value=vid,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
    )

    return {"status": "voted", "track_id": body.track_id, "vote_count": item.vote_count}


@router.get("/queue", response_model=QueueResponse)
async def get_queue(
    code: str,
    db: AsyncSession = Depends(get_db),
    voter_id: str | None = Cookie(default=None),
):
    """Get the current queue state: now playing + sorted pending tracks."""
    space, owner = await _get_space_with_owner(code, db)

    # Get now playing from Spotify
    now_playing = None
    try:
        sp = await get_spotify_client_for_user(owner, db)
        playback = sp.current_playback()
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
    except Exception:
        pass  # If Spotify fails, just show queue without now playing

    # Get sorted pending queue items
    result = await db.execute(
        select(QueueItem)
        .where(QueueItem.space_id == space.id, QueueItem.status == "pending")
        .order_by(QueueItem.vote_count.desc(), QueueItem.created_at.asc())
    )
    items = result.scalars().all()

    # Check which items the voter has voted for
    voted_item_ids = set()
    if voter_id:
        vote_result = await db.execute(
            select(Vote.queue_item_id).where(Vote.voter_id == voter_id)
        )
        voted_item_ids = {row[0] for row in vote_result.all()}

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
        )
        for item in items
    ]

    return QueueResponse(now_playing=now_playing, queue=queue_items, space_name=space.name)
