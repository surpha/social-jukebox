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
        # Clear any leftover on-deck rows from a previous run so a fresh worker
        # starts with an empty slot and re-locks the current top-voted track.
        try:
            async with async_session() as db:
                stale = await db.execute(
                    select(QueueItem).where(
                        QueueItem.space_id == space_id, QueueItem.status == "queued"
                    )
                )
                for qi in stale.scalars().all():
                    qi.status = "played"
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to clear stale on-deck for space {space_id}: {e}")

        # Per-space on-deck tracking. `on_deck_id` is the single locked "next" track;
        # `seen` flips once Spotify's queue snapshot confirms it (handles eventual
        # consistency); `misses` counts reliable snapshots that omit it after it was
        # seen (real skip); `unseen` counts reliable snapshots before it ever appeared
        # (a stale row that never propagated), so the slot can never get stuck.
        state = {"on_deck_id": None, "seen": False, "misses": 0, "unseen": 0}

        while True:
            try:
                await asyncio.sleep(5)
                await self._check_and_queue(space_id, user_id, state)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error for space {space_id}: {e}")
                await asyncio.sleep(10)  # Back off on error

    @staticmethod
    def _reset_on_deck(state: dict) -> None:
        state["on_deck_id"] = None
        state["seen"] = False
        state["misses"] = 0
        state["unseen"] = 0

    async def _check_and_queue(
        self, space_id: uuid.UUID, user_id: uuid.UUID, state: dict,
    ) -> None:
        """Keep exactly one top-voted track locked in Spotify as the next song.

        Invariant: at most one 'queued' track on deck at a time. The slot is freed
        only when that track actually starts playing (advance) or is confirmed gone
        across multiple reliable snapshots (manual skip) — never on a single stale
        Spotify queue read, which would otherwise double-queue or flap.
        """
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

            # Reconcile the on-deck ("queued") item(s) and decide if the slot is free.
            queued_result = await db.execute(
                select(QueueItem)
                .where(QueueItem.space_id == space_id, QueueItem.status == "queued")
                .order_by(QueueItem.created_at.desc())
            )
            has_on_deck = False
            for qi in queued_result.scalars().all():
                if qi.track_id == current_track_id:
                    # It advanced to playing — free the slot so the next song locks in.
                    qi.status = "played"
                    if state["on_deck_id"] == qi.track_id:
                        self._reset_on_deck(state)
                    continue

                # Still ahead of playback — this is (or becomes) our locked next song.
                if state["on_deck_id"] != qi.track_id:
                    # Fresh worker/restart or a newly locked track: adopt it.
                    state["on_deck_id"] = qi.track_id
                    state["seen"] = qi.track_id in upcoming_ids
                    state["misses"] = 0
                    state["unseen"] = 0
                elif qi.track_id in upcoming_ids:
                    state["seen"] = True
                    state["misses"] = 0
                elif spotify_queue_known:
                    # Reliable snapshot omits it: a post-seen miss (skip) or, if it
                    # never propagated, an unseen strike (stale/leftover row).
                    if state["seen"]:
                        state["misses"] += 1
                    else:
                        state["unseen"] += 1

                # Release the slot on a *confirmed* skip (seen, then reliably gone for
                # two polls) or a stale row that never appeared after several reliable
                # snapshots (~30s) — so a leftover 'queued' row can never block queueing.
                if (state["seen"] and state["misses"] >= 2) or (
                    not state["seen"] and state["unseen"] >= 6
                ):
                    qi.status = "played"
                    self._reset_on_deck(state)
                    logger.info(
                        f"Freeing stuck/skipped on-deck '{qi.name}' in space {space_id}"
                    )
                    continue

                has_on_deck = True
            await db.commit()

            # Slot is free → lock in the current top-voted pending track.
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
                        state["on_deck_id"] = top_item.track_id
                        state["seen"] = True
                        state["misses"] = 0
                        state["unseen"] = 0
                        await db.commit()
                    else:
                        try:
                            sp.add_to_queue(f"spotify:track:{top_item.track_id}")
                            top_item.status = "queued"
                            state["on_deck_id"] = top_item.track_id
                            state["seen"] = False
                            state["misses"] = 0
                            state["unseen"] = 0
                            await db.commit()
                            logger.info(
                                f"Locked '{top_item.name}' by {top_item.artist} "
                                f"(votes: {top_item.vote_count}) as next in space {space_id}"
                            )
                        except Exception as e:
                            # 403 'Restriction violated' here means the host's device/context
                            # won't accept queue commands (idle device, ad, non-Premium).
                            logger.error(
                                f"Failed to add to Spotify queue in space {space_id}: {e}"
                            )


# Global singleton
worker_manager = WorkerManager()
