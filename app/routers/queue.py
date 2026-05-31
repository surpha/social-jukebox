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


@router.get("/recommendations", response_model=list[SearchResult])
async def get_recommendations(
    code: str,
    db: AsyncSession = Depends(get_db),
):
    """Get recommendations closely related to what's currently playing and queued."""
    import random

    space, owner = await _get_space_with_owner(code, db)
    sp = await get_spotify_client_for_user(owner, db)

    seed_tracks = []
    seed_artists = []

    # 1. Currently playing track (primary seed)
    try:
        playback = sp.current_playback()
        if playback and playback.get("item"):
            item = playback["item"]
            seed_tracks.append(item["id"])
            if item.get("artists"):
                seed_artists.append(item["artists"][0]["id"])
    except Exception:
        pass

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
            recs = sp.recommendations(**kwargs)
            tracks = recs.get("tracks", [])
            # Filter out tracks already in queue or currently playing
            existing_ids = set(seed_tracks)
            tracks = [t for t in tracks if t["id"] not in existing_ids]
            if tracks:
                return [
                    SearchResult(
                        track_id=t["id"],
                        name=t["name"],
                        artist=", ".join(a["name"] for a in t["artists"]),
                        album_art=t["album"]["images"][0]["url"] if t["album"]["images"] else "",
                        duration_ms=t["duration_ms"],
                    )
                    for t in tracks[:6]
                ]
        except Exception:
            pass

    # Fallback: related artists tracks
    if seed_artists:
        try:
            related = sp.artist_related_artists(seed_artists[0])
            if related.get("artists"):
                artist_ids = [a["id"] for a in related["artists"][:3]]
                random.shuffle(artist_ids)
                top_tracks = sp.artist_top_tracks(artist_ids[0])
                tracks = top_tracks.get("tracks", [])
                existing_ids = set(seed_tracks)
                tracks = [t for t in tracks if t["id"] not in existing_ids]
                if tracks:
                    return [
                        SearchResult(
                            track_id=t["id"],
                            name=t["name"],
                            artist=", ".join(a["name"] for a in t["artists"]),
                            album_art=t["album"]["images"][0]["url"] if t["album"]["images"] else "",
                            duration_ms=t["duration_ms"],
                        )
                        for t in tracks[:6]
                    ]
        except Exception:
            pass

    # Last resort: search for similar artist
    try:
        if seed_artists:
            artist_info = sp.artist(seed_artists[0])
            query = f"artist:{artist_info['name']}"
        else:
            query = "top hits 2024"
        results = sp.search(q=query, type="track", limit=6)
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
    """Toggle upvote on a track in the queue."""
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
    existing_vote = result.scalar_one_or_none()

    if existing_vote:
        # Remove vote (toggle off)
        await db.delete(existing_vote)
        item.vote_count = max(0, item.vote_count - 1)
        await db.commit()
        response.set_cookie(key="voter_id", value=vid, max_age=60*60*24*30, httponly=True, samesite="lax")
        return {"status": "unvoted", "track_id": body.track_id, "vote_count": item.vote_count}

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
    sp = None
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
    except Exception as e:
        import logging
        logging.warning(f"Spotify playback fetch failed for space {code}: {type(e).__name__}")

    # Fetch Spotify's upcoming queue
    from app.schemas import UpNextResponse
    spotify_queue_track_ids = set()
    spotify_queue_raw = []
    if sp:
        try:
            spotify_queue_data = sp.queue()
            if spotify_queue_data and spotify_queue_data.get("queue"):
                spotify_queue_raw = spotify_queue_data["queue"][:20]
                spotify_queue_track_ids = {t["id"] for t in spotify_queue_raw}
        except Exception:
            pass

    # Get the "up next" track - only show truly "queued" (locked-in) items
    # Verify the item is still in Spotify's queue (not already played/skipped)
    up_next = None
    up_next_result = await db.execute(
        select(QueueItem)
        .where(QueueItem.space_id == space.id, QueueItem.status == "queued")
        .limit(1)
    )
    up_next_item = up_next_result.scalar_one_or_none()
    if up_next_item:
        if up_next_item.track_id in spotify_queue_track_ids:
            # Still in Spotify's queue — show it
            up_next = UpNextResponse(
                track_id=up_next_item.track_id,
                name=up_next_item.name,
                artist=up_next_item.artist,
                album_art=up_next_item.album_art,
                duration_ms=up_next_item.duration_ms,
                vote_count=up_next_item.vote_count,
            )
        else:
            # Already played or skipped — remove the stale entry
            await db.delete(up_next_item)
            await db.commit()

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
        if t["id"] not in existing_track_ids:
            spotify_queue_items.append(SearchResult(
                track_id=t["id"],
                name=t["name"],
                artist=", ".join(a["name"] for a in t["artists"]),
                album_art=t["album"]["images"][0]["url"] if t["album"]["images"] else "",
                duration_ms=t["duration_ms"],
            ))
            if len(spotify_queue_items) >= 8:
                break
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

    return QueueResponse(now_playing=now_playing, up_next=up_next, queue=queue_items, spotify_queue=spotify_queue_items, space_name=space.name)
