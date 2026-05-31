from datetime import datetime, timezone

import spotipy
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/api/spotify", tags=["spotify"])
settings = get_settings()

# Derive a Fernet key from the secret (must be 32 url-safe base64-encoded bytes)
import base64
import hashlib

_key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
_fernet = Fernet(_key)

SPOTIFY_SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing"


def _encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()


def _get_oauth_manager() -> spotipy.SpotifyOAuth:
    return spotipy.SpotifyOAuth(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=settings.spotify_redirect_uri,
        scope=SPOTIFY_SCOPES,
        show_dialog=True,
    )


def get_spotify_client(user: User) -> spotipy.Spotify | None:
    """Create an authenticated Spotify client for a user, handling token refresh."""
    if not user.spotify_refresh_token:
        return None

    oauth = _get_oauth_manager()
    token_info = {
        "access_token": _decrypt(user.spotify_access_token),
        "refresh_token": _decrypt(user.spotify_refresh_token),
        "expires_at": int(user.spotify_token_expires.timestamp()) if user.spotify_token_expires else 0,
    }

    # Check if token needs refresh
    if oauth.is_token_expired(token_info):
        new_token = oauth.refresh_access_token(token_info["refresh_token"])
        token_info = new_token
        # We'll update the DB in the calling context if needed
        user._refreshed_token = new_token  # Attach for caller to persist

    return spotipy.Spotify(auth=token_info["access_token"])


async def get_spotify_client_for_user(user: User, db: AsyncSession) -> spotipy.Spotify:
    """Get Spotify client and persist any refreshed tokens."""
    sp = get_spotify_client(user)
    if sp is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Spotify not linked")

    # Persist refreshed token if it was refreshed
    if hasattr(user, "_refreshed_token"):
        new_token = user._refreshed_token
        user.spotify_access_token = _encrypt(new_token["access_token"])
        user.spotify_refresh_token = _encrypt(new_token["refresh_token"])
        user.spotify_token_expires = datetime.fromtimestamp(new_token["expires_at"], tz=timezone.utc)
        del user._refreshed_token
        await db.commit()

    return sp


@router.get("/link")
async def spotify_link(current_user: User = Depends(get_current_user)):
    """Returns the Spotify OAuth URL for the user to authorize."""
    oauth = _get_oauth_manager()
    # Embed user ID in state for callback
    auth_url = oauth.get_authorize_url(state=str(current_user.id))
    return {"auth_url": auth_url}


@router.get("/callback")
async def spotify_callback(
    code: str = Query(...),
    state: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Handles Spotify OAuth callback, stores encrypted tokens."""
    oauth = _get_oauth_manager()
    token_info = oauth.get_access_token(code, as_dict=True)

    if not token_info or "access_token" not in token_info:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to get Spotify token")

    # Find user from state (user_id)
    from uuid import UUID
    from sqlalchemy import select

    try:
        user_id = UUID(state)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state parameter")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Store encrypted tokens
    user.spotify_access_token = _encrypt(token_info["access_token"])
    user.spotify_refresh_token = _encrypt(token_info["refresh_token"])
    user.spotify_token_expires = datetime.fromtimestamp(token_info["expires_at"], tz=timezone.utc)
    await db.commit()

    # Redirect to dashboard
    return RedirectResponse(url=f"{settings.app_url}/dashboard?spotify=linked")


@router.get("/status")
async def spotify_status(current_user: User = Depends(get_current_user)):
    """Check if the current user has linked Spotify."""
    return {
        "linked": current_user.spotify_refresh_token is not None,
    }
