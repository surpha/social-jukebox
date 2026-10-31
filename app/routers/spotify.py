import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_oauth_state, get_current_user, verify_oauth_state
from app.config import get_settings
from app.database import get_db
from app.engine import engine_manager
from app.models import User
from app.spotify_client import (
    HOST_SCOPES,
    SpotifyError,
    api,
    authorize_url,
    exchange_code,
    invalidate_token,
    store_tokens,
    track_summary,
)

router = APIRouter(prefix="/api/spotify", tags=["spotify"])
settings = get_settings()
logger = logging.getLogger(__name__)


@router.get("/link")
async def spotify_link(current_user: User = Depends(get_current_user)):
    """Returns the Spotify OAuth URL for the user to authorize."""
    state = create_oauth_state("spotify_link", str(current_user.id))
    return {"auth_url": authorize_url(settings.spotify_redirect_uri, HOST_SCOPES, state)}


@router.get("/callback")
async def spotify_callback(
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    state: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Handles Spotify OAuth callback, stores encrypted tokens."""
    if error or not code:
        return RedirectResponse(url=f"{settings.app_url}/dashboard?error=spotify_denied")

    user_id = verify_oauth_state(state, "spotify_link")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired state")

    try:
        token_info = await exchange_code(code, settings.spotify_redirect_uri)
    except SpotifyError:
        return RedirectResponse(url=f"{settings.app_url}/dashboard?error=spotify_denied")

    user = (await db.execute(select(User).where(User.id == UUID(user_id)))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    store_tokens(user, token_info)
    await db.commit()
    invalidate_token(user.id)

    return RedirectResponse(url=f"{settings.app_url}/dashboard?spotify=linked")


@router.get("/status")
async def spotify_status(current_user: User = Depends(get_current_user)):
    """Check if the current user has linked Spotify."""
    return {"linked": current_user.spotify_refresh_token is not None}


# --- Playback Controls (owner only) ---

_CONTROLS = {
    "play": ("PUT", "/me/player/play", "playing"),
    "pause": ("PUT", "/me/player/pause", "paused"),
    "skip": ("POST", "/me/player/next", "skipped"),
}


@router.post("/playback/{action}")
async def playback_control(action: str, current_user: User = Depends(get_current_user)):
    if action not in _CONTROLS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown action")
    method, path, result = _CONTROLS[action]
    try:
        await api(current_user.id, method, path)
    except SpotifyError as e:
        logger.warning(f"Playback {action} failed for user {current_user.id}: {e}")
        detail = "No active Spotify device. Open Spotify and start playing." if e.status == 404 else f"Could not {action}"
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    engine_manager.poke_owner(current_user.id)
    return {"status": result}


@router.get("/playback/now-playing")
async def playback_now_playing(current_user: User = Depends(get_current_user)):
    """Get current playback state for owner dashboard."""
    try:
        playback = await api(current_user.id, "GET", "/me/player", params={"additional_types": "track"})
    except SpotifyError:
        return {"is_playing": False}
    if not playback or not playback.get("item"):
        return {"is_playing": False}
    return {
        **track_summary(playback["item"]),
        "is_playing": playback.get("is_playing", False),
        "progress_ms": playback.get("progress_ms", 0),
    }


@router.get("/playback/queue")
async def playback_queue(current_user: User = Depends(get_current_user)):
    """Get Spotify's actual queue (next tracks lined up)."""
    try:
        data = await api(current_user.id, "GET", "/me/player/queue")
    except SpotifyError:
        return []
    return [track_summary(t) for t in (data or {}).get("queue", [])[:10] if t and t.get("id")]
