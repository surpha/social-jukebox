import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models import QueueItem, Space, User
from app.routers.spotify import get_spotify_client_for_user, spotify_call

logger = logging.getLogger(__name__)


class WorkerManager:
    """Manages per-space background polling tasks."""

    def __init__(self):
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}

    async def start_worker(self, space: Space, user: User, db: AsyncSession | None = None):
        """Start a background worker for a space."""
        if space.id in self._tasks:
            # Already running
            return

        task = asyncio.create_task(self._poll_loop(space.id, user.id))
        self._tasks[space.id] = task
        logger.info(f"Worker started for space '{space.name}' ({space.code})")

    async def stop_worker(self, space_id: uuid.UUID):
        """Stop a worker for a space."""
        task = self._tasks.pop(space_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info(f"Worker stopped for space {space_id}")

    async def stop_all(self):
        """Stop all running workers."""
        for space_id in list(self._tasks.keys()):
            await self.stop_worker(space_id)

    async def restart_active_workers(self):
        """Restart workers for all active spaces (called on app startup)."""
        async with async_session() as db:
            result = await db.execute(
                select(Space)
                .where(Space.is_active == True)
                .options(selectinload(Space.owner))
            )
            active_spaces = result.scalars().all()

            for space in active_spaces:
                await self.start_worker(space, space.owner, db)

            logger.info(f"Restarted {len(active_spaces)} active workers on startup")

    # Consecutive polls the on-deck track may be missing from Spotify's queue (and
    # not playing) before we treat it as skipped. Spotify's queue read lags.
    _MAX_ON_DECK_MISSES = 3

    async def _poll_loop(self, space_id: uuid.UUID, user_id: uuid.UUID):
        """Main polling loop for a space. Checks playback every 5 seconds."""
        logger.info(f"Poll loop started for space {space_id}")
        on_deck_misses = 0

        while True:
            try:
                await asyncio.sleep(5)
                on_deck_misses = await self._check_and_queue(space_id, user_id, on_deck_misses)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error for space {space_id}: {type(e).__name__}: {e}")
                await asyncio.sleep(10)  # Back off on error

    async def _check_and_queue(self, space_id: uuid.UUID, user_id: uuid.UUID, on_deck_misses: int) -> int:
        """Keep exactly one of our tracks on deck in Spotify; promote the next when it starts playing.

        Returns the updated miss counter for the on-deck track.
        """
        async with async_session() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user or not user.spotify_refresh_token:
                return on_deck_misses

            try:
                sp = await get_spotify_client_for_user(user, db)
            except Exception as e:
                logger.warning(f"Spotify client unavailable for space {space_id}: {type(e).__name__}")
                return on_deck_misses

            try:
                playback = await spotify_call(sp.current_playback)
            except Exception as e:
                logger.warning(f"Failed to get playback for space {space_id}: {type(e).__name__}")
                return on_deck_misses

            if not playback or not playback.get("item"):
                return on_deck_misses

            current_track_id = playback["item"]["id"]

            result = await db.execute(
                select(QueueItem)
                .where(QueueItem.space_id == space_id, QueueItem.status == "queued")
                .order_by(QueueItem.created_at.asc())
            )
            queued_items = list(result.scalars().all())
            on_deck = queued_items[-1] if queued_items else None
            # Older builds could leave several "queued" rows; keep only the newest.
            for stale in queued_items[:-1]:
                await db.delete(stale)

            if on_deck is not None:
                if on_deck.track_id == current_track_id:
                    # Our track started playing; it's done. Deleting (not marking
                    # "played") avoids unique-constraint clashes on replays.
                    await db.delete(on_deck)
                    on_deck = None
                    on_deck_misses = 0
                else:
                    try:
                        upcoming = await spotify_call(sp.queue)
                        ids = {t["id"] for t in (upcoming or {}).get("queue", []) if t and t.get("id")}
                        still_upcoming = on_deck.track_id in ids
                    except Exception:
                        still_upcoming = True

                    if still_upcoming:
                        await self._safe_commit(db, space_id)
                        return 0

                    on_deck_misses += 1
                    if on_deck_misses < self._MAX_ON_DECK_MISSES:
                        await self._safe_commit(db, space_id)
                        return on_deck_misses

                    logger.info(f"On-deck '{on_deck.name}' left Spotify's queue (skipped?) in space {space_id}")
                    await db.delete(on_deck)
                    on_deck = None
                    on_deck_misses = 0

            if not await self._safe_commit(db, space_id):
                return on_deck_misses

            result = await db.execute(
                select(QueueItem)
                .where(QueueItem.space_id == space_id, QueueItem.status == "pending")
                .order_by(QueueItem.vote_count.desc(), QueueItem.created_at.asc())
                .limit(1)
            )
            top_item = result.scalar_one_or_none()
            if not top_item:
                return on_deck_misses

            try:
                await spotify_call(sp.add_to_queue, f"spotify:track:{top_item.track_id}")
            except Exception as e:
                logger.error(f"Failed to add to Spotify queue for space {space_id}: {type(e).__name__}: {e}")
                return on_deck_misses

            top_item.status = "queued"
            if await self._safe_commit(db, space_id):
                logger.info(
                    f"Queued '{top_item.name}' by {top_item.artist} "
                    f"(votes: {top_item.vote_count}) in space {space_id}"
                )
            return 0

    @staticmethod
    async def _safe_commit(db: AsyncSession, space_id: uuid.UUID) -> bool:
        """Commit, rolling back on failure so the session stays usable."""
        try:
            await db.commit()
            return True
        except Exception as e:
            await db.rollback()
            logger.error(f"DB commit failed for space {space_id}: {type(e).__name__}: {e}")
            return False


# Global singleton
worker_manager = WorkerManager()
