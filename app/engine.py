"""Per-space playback engine.

One engine runs per active space. It is the only component that polls Spotify for
that space, owns the "on deck" lifecycle, and pushes state snapshots to SSE
subscribers, so guest reads never touch Spotify or the database.
"""
import asyncio
import json
import logging
import random
import time
import uuid
from typing import Protocol

from sqlalchemy import delete, select, update

from app.database import async_session
from app.models import QueueItem, Space, Vote
from app.spotify_client import SpotifyNotLinked, SpotifyRateLimited, api, track_summary

logger = logging.getLogger(__name__)

IDLE_POLL = 5.0
MIN_POLL = 0.75
SPOTIFY_QUEUE_REFRESH = 15.0
MAX_ON_DECK_MISSES = 3  # Spotify's queue endpoint lags, so tolerate a few misses before assuming a skip
PROGRESS_JUMP_MS = 2500


class SpotifyPort(Protocol):
    async def playback(self) -> dict | None: ...
    async def queue(self) -> list[dict]: ...
    async def add_to_queue(self, track_id: str) -> None: ...
    async def artist_top_tracks(self, artist_id: str) -> list[dict]: ...


class StorePort(Protocol):
    async def load_pending(self) -> list[dict]: ...
    async def load_on_deck(self) -> dict | None: ...
    async def mark_queued(self, item_id: str) -> bool: ...
    async def revert_queued(self, item_id: str) -> None: ...
    async def delete_item(self, item_id: str) -> None: ...


class LiveSpotify:
    def __init__(self, owner_id: uuid.UUID):
        self.owner_id = owner_id

    async def playback(self) -> dict | None:
        return await api(self.owner_id, "GET", "/me/player", params={"additional_types": "track"})

    async def queue(self) -> list[dict]:
        data = await api(self.owner_id, "GET", "/me/player/queue")
        return [t for t in (data or {}).get("queue", []) if t and t.get("id")]

    async def add_to_queue(self, track_id: str) -> None:
        await api(self.owner_id, "POST", "/me/player/queue", params={"uri": f"spotify:track:{track_id}"})

    async def artist_top_tracks(self, artist_id: str) -> list[dict]:
        data = await api(self.owner_id, "GET", f"/artists/{artist_id}/top-tracks")
        return (data or {}).get("tracks", [])


def item_to_dict(item: QueueItem) -> dict:
    return {
        "id": str(item.id),
        "track_id": item.track_id,
        "name": item.name,
        "artist": item.artist,
        "album_art": item.album_art,
        "duration_ms": item.duration_ms,
        "vote_count": item.vote_count,
    }


async def load_pending_items(db, space_id: uuid.UUID) -> list[dict]:
    result = await db.execute(
        select(QueueItem)
        .where(QueueItem.space_id == space_id, QueueItem.status == "pending")
        .order_by(QueueItem.vote_count.desc(), QueueItem.created_at.asc())
    )
    return [item_to_dict(i) for i in result.scalars().all()]


async def delete_items(db, item_ids: list) -> None:
    if not item_ids:
        return
    await db.execute(delete(Vote).where(Vote.queue_item_id.in_(item_ids)))
    await db.execute(delete(QueueItem).where(QueueItem.id.in_(item_ids)))


class DbStore:
    def __init__(self, space_id: uuid.UUID):
        self.space_id = space_id

    async def load_pending(self) -> list[dict]:
        async with async_session() as db:
            return await load_pending_items(db, self.space_id)

    async def load_on_deck(self) -> dict | None:
        async with async_session() as db:
            result = await db.execute(
                select(QueueItem)
                .where(QueueItem.space_id == self.space_id, QueueItem.status == "queued")
                .order_by(QueueItem.created_at.desc())
            )
            items = list(result.scalars().all())
            if len(items) > 1:
                await delete_items(db, [i.id for i in items[1:]])
                await db.commit()
            return item_to_dict(items[0]) if items else None

    async def mark_queued(self, item_id: str) -> bool:
        async with async_session() as db:
            result = await db.execute(
                update(QueueItem)
                .where(QueueItem.id == uuid.UUID(item_id), QueueItem.status == "pending")
                .values(status="queued")
                .returning(QueueItem.id)
            )
            ok = result.scalar_one_or_none() is not None
            await db.commit()
            return ok

    async def revert_queued(self, item_id: str) -> None:
        async with async_session() as db:
            await db.execute(
                update(QueueItem).where(QueueItem.id == uuid.UUID(item_id)).values(status="pending")
            )
            await db.commit()

    async def delete_item(self, item_id: str) -> None:
        async with async_session() as db:
            await delete_items(db, [uuid.UUID(item_id)])
            await db.commit()


class Subscriber:
    __slots__ = ("event", "latest")

    def __init__(self, latest: str | None):
        self.event = asyncio.Event()
        self.latest = latest
        self.event.set()


class SpaceEngine:
    def __init__(self, space_id: uuid.UUID, name: str, spotify: SpotifyPort, store: StorePort, clock=time.monotonic):
        self.space_id = space_id
        self.name = name
        self.spotify = spotify
        self.store = store
        self.clock = clock

        self.now_playing: dict | None = None
        self.on_deck: dict | None = None
        self.pending: list[dict] = []
        self.recommendations: list[dict] = []
        self._spotify_queue: list[dict] = []

        self._np_at = 0.0
        self._current_id: str | None = None
        self._last_progress = 0
        self._misses = 0
        self._queue_fetched_at = float("-inf")

        self._wake = asyncio.Event()
        self._dirty = asyncio.Event()
        self._subs: set[Subscriber] = set()
        self._last_key: str | None = None
        self._sent_progress: tuple[int, float] = (0, 0.0)
        self._tasks: list[asyncio.Task] = []
        self._recs_task: asyncio.Task | None = None
        self.closed = False

    # --- lifecycle ---

    async def start(self) -> None:
        self.pending = await self.store.load_pending()
        self.on_deck = await self.store.load_on_deck()
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name=f"engine-poll-{self.space_id}"),
            asyncio.create_task(self._queue_loop(), name=f"engine-queue-{self.space_id}"),
        ]

    async def stop(self) -> None:
        self.closed = True
        tasks = self._tasks + ([self._recs_task] if self._recs_task else [])
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for sub in self._subs:
            sub.latest = None
            sub.event.set()
        self._subs.clear()

    def queue_changed(self) -> None:
        self._dirty.set()

    def poke(self) -> None:
        self._wake.set()

    # --- subscribers ---

    def subscribe(self) -> Subscriber:
        sub = Subscriber(self.payload())
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    # --- loops ---

    async def _poll_loop(self) -> None:
        while True:
            try:
                delay = await self.tick()
            except asyncio.CancelledError:
                raise
            except SpotifyRateLimited as e:
                logger.warning(f"Rate limited in space {self.space_id}; backing off {e.retry_after:.0f}s")
                delay = max(e.retry_after, IDLE_POLL)
            except SpotifyNotLinked:
                delay = 30.0
            except Exception as e:
                logger.error(f"Engine error in space {self.space_id}: {type(e).__name__}: {e}")
                delay = 10.0
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _queue_loop(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(0.15)  # coalesce bursts of votes into one reload
            self._dirty.clear()
            try:
                self.pending = await self.store.load_pending()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Queue reload failed in space {self.space_id}: {type(e).__name__}: {e}")
                await asyncio.sleep(1)
                self._dirty.set()
                continue
            self.publish()
            if self.on_deck is None and self.pending:
                self.poke()

    # --- core logic ---

    def estimated_progress(self) -> int:
        np = self.now_playing
        if not np:
            return 0
        progress = np["progress_ms"]
        if np["is_playing"]:
            progress += int((self.clock() - self._np_at) * 1000)
        return min(progress, np["duration_ms"]) if np["duration_ms"] else progress

    async def tick(self) -> float:
        """Poll Spotify once and react. Returns seconds until the next poll."""
        playback = await self.spotify.playback()
        now = self.clock()
        item = (playback or {}).get("item")

        if not item or not item.get("id") or item.get("type", "track") != "track":
            self.now_playing = None
            self._current_id = None
            self.publish()
            return IDLE_POLL

        track_id = item["id"]
        progress = playback.get("progress_ms") or 0
        duration = item.get("duration_ms") or 0
        playing = bool(playback.get("is_playing"))
        changed = track_id != self._current_id
        restarted = not changed and progress + 5000 < self._last_progress
        self._current_id = track_id
        self._last_progress = progress
        self.now_playing = {**track_summary(item), "progress_ms": progress, "is_playing": playing}
        self._np_at = now

        if changed:
            self._start_recs(item)

        refresh_queue = changed or now - self._queue_fetched_at >= SPOTIFY_QUEUE_REFRESH

        if self.on_deck:
            if self.on_deck["track_id"] == track_id and (changed or restarted):
                await self.store.delete_item(self.on_deck["id"])
                self.on_deck = None
                self._misses = 0
            elif self.on_deck["track_id"] != track_id and (changed or self._misses):
                upcoming = await self._refresh_spotify_queue(now)
                refresh_queue = False
                if upcoming is None or self.on_deck["track_id"] in upcoming:
                    self._misses = 0
                else:
                    self._misses += 1
                    if self._misses >= MAX_ON_DECK_MISSES:
                        logger.info(f"On-deck '{self.on_deck['name']}' left Spotify's queue in space {self.space_id}")
                        await self.store.delete_item(self.on_deck["id"])
                        self.on_deck = None
                        self._misses = 0

        remaining = duration - progress
        # Keep the on-deck slot filled at all times: Spotify can't un-queue, and a mid-song
        # skip must land on the voted song rather than whatever Spotify would play next.
        if self.on_deck is None and self.pending:
            await self._lock_in(self.pending[0])
            refresh_queue = True

        if refresh_queue:
            await self._refresh_spotify_queue(now)

        self.publish()

        if not playing:
            return IDLE_POLL
        delay = max(remaining, 0) / 1000 + 0.5  # wake right after the track ends
        if self._misses:
            delay = min(delay, 2.0)
        return min(IDLE_POLL, max(MIN_POLL, delay))

    async def _lock_in(self, top: dict) -> None:
        if not await self.store.mark_queued(top["id"]):
            self.pending = await self.store.load_pending()
            return
        try:
            await self.spotify.add_to_queue(top["track_id"])
        except BaseException:
            await self.store.revert_queued(top["id"])
            raise
        self.on_deck = top
        self.pending = [p for p in self.pending if p["id"] != top["id"]]
        self._misses = 0
        logger.info(f"Queued '{top['name']}' ({top['vote_count']} votes) in space {self.space_id}")

    async def _refresh_spotify_queue(self, now: float) -> set[str] | None:
        try:
            tracks = await self.spotify.queue()
        except SpotifyRateLimited:
            raise
        except Exception as e:
            logger.debug(f"Spotify queue fetch failed in space {self.space_id}: {e}")
            return None
        self._queue_fetched_at = now
        self._spotify_queue = [track_summary(t) for t in tracks[:20]]
        return {t["track_id"] for t in self._spotify_queue}

    def _start_recs(self, item: dict) -> None:
        if self._recs_task and not self._recs_task.done():
            self._recs_task.cancel()
        self._recs_task = asyncio.create_task(self._load_recs(item))

    async def _load_recs(self, item: dict) -> None:
        tracks: list[dict] = []
        for artist in (item.get("artists") or [])[:2]:
            if not artist.get("id"):
                continue
            try:
                tracks.extend(await self.spotify.artist_top_tracks(artist["id"]))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Recommendations fetch failed in space {self.space_id}: {e}")
        seen = {item["id"]}
        recs = []
        for t in tracks:
            if t and t.get("id") and t["id"] not in seen:
                seen.add(t["id"])
                recs.append(track_summary(t))
        random.shuffle(recs)
        self.recommendations = recs[:8]
        self.publish()

    # --- snapshots ---

    def find_track(self, track_id: str) -> dict | None:
        for t in (*self.recommendations, *self._spotify_queue):
            if t["track_id"] == track_id:
                return t
        return None

    def state(self) -> dict:
        taken = {p["track_id"] for p in self.pending}
        if self.now_playing:
            taken.add(self.now_playing["track_id"])
        if self.on_deck:
            taken.add(self.on_deck["track_id"])
        spotify_queue = [t for t in self._spotify_queue if t["track_id"] not in taken][:8]
        np = dict(self.now_playing) if self.now_playing else None
        if np:
            np.pop("progress_ms")
        return {
            "active": True,
            "space_name": self.name,
            "now_playing": np,
            "up_next": self.on_deck,
            "queue": self.pending,
            "spotify_queue": spotify_queue,
            "recommendations": self.recommendations,
        }

    def payload(self) -> str:
        state = self.state()
        if state["now_playing"]:
            state["now_playing"]["progress_ms"] = self.estimated_progress()
        return json.dumps(state, separators=(",", ":"))

    def _progress_jumped(self) -> bool:
        if not self.now_playing:
            return False
        sent, sent_at = self._sent_progress
        expected = sent + (int((self.clock() - sent_at) * 1000) if self.now_playing["is_playing"] else 0)
        return abs(self.estimated_progress() - expected) > PROGRESS_JUMP_MS

    def publish(self) -> None:
        """Push to subscribers if anything visible changed (progress drift excluded)."""
        key = json.dumps(self.state(), sort_keys=True)
        if key == self._last_key and not self._progress_jumped():
            return
        self._last_key = key
        self._sent_progress = (self.estimated_progress(), self.clock())
        payload = self.payload()
        for sub in self._subs:
            sub.latest = payload
            sub.event.set()


class EngineManager:
    def __init__(self):
        self._engines: dict[uuid.UUID, SpaceEngine] = {}
        self._lock = asyncio.Lock()

    def get(self, space_id: uuid.UUID) -> SpaceEngine | None:
        return self._engines.get(space_id)

    async def start(self, space_id: uuid.UUID, owner_id: uuid.UUID, name: str) -> SpaceEngine:
        async with self._lock:
            engine = self._engines.get(space_id)
            if engine:
                return engine
            engine = SpaceEngine(space_id, name, LiveSpotify(owner_id), DbStore(space_id))
            await engine.start()
            self._engines[space_id] = engine
            logger.info(f"Engine started for space '{name}' ({space_id})")
            return engine

    async def stop(self, space_id: uuid.UUID) -> None:
        async with self._lock:
            engine = self._engines.pop(space_id, None)
        if engine:
            await engine.stop()
            logger.info(f"Engine stopped for space {space_id}")

    async def stop_all(self) -> None:
        for space_id in list(self._engines):
            await self.stop(space_id)

    async def restart_active(self) -> None:
        async with async_session() as db:
            result = await db.execute(select(Space.id, Space.owner_id, Space.name).where(Space.is_active.is_(True)))
            rows = result.all()
        for space_id, owner_id, name in rows:
            try:
                await self.start(space_id, owner_id, name)
            except Exception as e:
                logger.error(f"Could not start engine for space {space_id}: {type(e).__name__}: {e}")
        logger.info(f"Restarted {len(rows)} active engines")

    def queue_changed(self, space_id: uuid.UUID) -> None:
        engine = self._engines.get(space_id)
        if engine:
            engine.queue_changed()

    def poke_owner(self, owner_id: uuid.UUID) -> None:
        for engine in self._engines.values():
            if isinstance(engine.spotify, LiveSpotify) and engine.spotify.owner_id == owner_id:
                engine.poke()


engine_manager = EngineManager()
