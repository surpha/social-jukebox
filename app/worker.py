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
        last_known_track_id: str | None = None

        while True:
            try:
                await asyncio.sleep(5)
                last_known_track_id = await self._check_and_queue(
                    space_id, user_id, last_known_track_id
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error for space {space_id}: {e}")
                await asyncio.sleep(10)  # Back off on error

    async def _check_and_queue(
        self, space_id: uuid.UUID, user_id: uuid.UUID,
        last_known_track_id: str | None,
    ) -> str | None:
        """Keep exactly one top-voted track on deck in Spotify, idempotently."""
        async with async_session() as db:
            # Get user with fresh tokens
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user or not user.spotify_refresh_token:
                return last_known_track_id

            # Get Spotify client
            sp = get_spotify_client(user)
            if not sp:
                return last_known_track_id

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
                return last_known_track_id

            if not playback or not playback.get("item"):
                return last_known_track_id

            current_track_id = playback["item"]["id"]

            # Read Spotify's upcoming queue once (eventually-consistent; may be empty).
            upcoming_ids: set[str] = set()
            try:
                q = sp.queue()
                if q and q.get("queue"):
                    upcoming_ids = {t["id"] for t in q["queue"] if t.get("id")}
            except Exception:
                pass
            spotify_queue_known = len(upcoming_ids) > 0

            # Reconcile existing on-deck ("queued") items and decide if a slot is free.
            queued_result = await db.execute(
                select(QueueItem)
                .where(QueueItem.space_id == space_id, QueueItem.status == "queued")
            )
            has_on_deck = False
            for qi in queued_result.scalars().all():
                if qi.track_id == current_track_id:
                    # It's now playing — done with it.
                    qi.status = "played"
                elif spotify_queue_known and qi.track_id not in upcoming_ids:
                    # Reliably gone from Spotify's queue and not playing — it was skipped.
                    qi.status = "played"
                else:
                    # Still lined up (or we couldn't read the queue) — keep it on deck.
                    has_on_deck = True
            await db.commit()

            # Only promote a new track when nothing is on deck (prevents duplicates).
            if not has_on_deck:
                result = await db.execute(
                    select(QueueItem)
                    .where(QueueItem.space_id == space_id, QueueItem.status == "pending")
                    .order_by(QueueItem.vote_count.desc(), QueueItem.created_at.asc())
                    .limit(1)
                )
                top_item = result.scalar_one_or_none()

                if top_item and top_item.track_id != current_track_id:
                    if top_item.track_id in upcoming_ids:
                        # Already in Spotify's queue — record it, don't add a duplicate.
                        top_item.status = "queued"
                        await db.commit()
                    else:
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

            return current_track_id


# Global singleton
worker_manager = WorkerManager()
