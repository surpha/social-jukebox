import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models import QueueItem, Space, User
from app.routers.spotify import get_spotify_client, _encrypt

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

    async def _poll_loop(self, space_id: uuid.UUID, user_id: uuid.UUID):
        """Main polling loop for a space. Checks playback every 5 seconds."""
        logger.info(f"Poll loop started for space {space_id}")
        last_queued_track: str | None = None

        while True:
            try:
                await asyncio.sleep(5)
                await self._check_and_queue(space_id, user_id, last_queued_track)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error for space {space_id}: {e}")
                await asyncio.sleep(10)  # Back off on error

    async def _check_and_queue(
        self, space_id: uuid.UUID, user_id: uuid.UUID, last_queued_track: str | None
    ):
        """Check current playback and queue next track if needed."""
        async with async_session() as db:
            # Get user with fresh tokens
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user or not user.spotify_refresh_token:
                return

            # Get Spotify client
            sp = get_spotify_client(user)
            if not sp:
                return

            # Persist refreshed token if needed
            if hasattr(user, "_refreshed_token"):
                new_token = user._refreshed_token
                user.spotify_access_token = _encrypt(new_token["access_token"])
                user.spotify_refresh_token = _encrypt(new_token["refresh_token"])
                user.spotify_token_expires = datetime.fromtimestamp(
                    new_token["expires_at"], tz=timezone.utc
                )
                del user._refreshed_token
                await db.commit()

            # Check current playback
            try:
                playback = sp.current_playback()
            except Exception as e:
                logger.warning(f"Failed to get playback for space {space_id}: {e}")
                return

            if not playback or not playback.get("item"):
                return

            duration_ms = playback["item"]["duration_ms"]
            progress_ms = playback.get("progress_ms", 0)
            remaining_ms = duration_ms - progress_ms

            # If less than 15 seconds remaining, queue the next track
            if remaining_ms < 15000:
                # Get top-voted pending track
                result = await db.execute(
                    select(QueueItem)
                    .where(QueueItem.space_id == space_id, QueueItem.status == "pending")
                    .order_by(QueueItem.vote_count.desc(), QueueItem.created_at.asc())
                    .limit(1)
                )
                top_item = result.scalar_one_or_none()

                if not top_item:
                    return

                # Don't re-queue the same track
                if top_item.track_id == last_queued_track:
                    return

                # Push to Spotify queue
                try:
                    sp.add_to_queue(f"spotify:track:{top_item.track_id}")
                    top_item.status = "queued"
                    await db.commit()
                    logger.info(
                        f"Queued '{top_item.name}' by {top_item.artist} "
                        f"(votes: {top_item.vote_count}) in space {space_id}"
                    )
                except Exception as e:
                    logger.error(f"Failed to add to Spotify queue: {e}")


# Global singleton
worker_manager = WorkerManager()
