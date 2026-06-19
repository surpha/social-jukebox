from datetime import datetime, timezone

import spotipy
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    create_access_token,
    get_current_user,
)
from app.config import get_settings
from app.database import get_db
from app.models import User
from app.schemas import (
    UserResponse,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        name=current_user.name,
        has_spotify=current_user.spotify_refresh_token is not None,
        created_at=current_user.created_at,
    )


# --- Spotify OAuth Login/Signup ---

SPOTIFY_LOGIN_SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing user-top-read user-read-email user-read-private"


def _get_spotify_login_oauth() -> spotipy.SpotifyOAuth:
    return spotipy.SpotifyOAuth(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=settings.spotify_login_redirect_uri,
        scope=SPOTIFY_LOGIN_SCOPES,
        show_dialog=True,
    )


@router.get("/spotify/login")
async def spotify_login():
    """Returns the Spotify OAuth URL for login/signup."""
    oauth = _get_spotify_login_oauth()
    auth_url = oauth.get_authorize_url(state="login")
    return {"auth_url": auth_url}


@router.get("/spotify/callback")
async def spotify_login_callback(
    code: str = Query(default=None),
    error: str = Query(default=None),
    state: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Handles Spotify OAuth callback for login/signup. Creates or finds user, links Spotify tokens."""
    if error or not code:
        return RedirectResponse(url=f"{settings.app_url}/?error=spotify_denied")

    from app.routers.spotify import _encrypt

    oauth = _get_spotify_login_oauth()
    token_info = oauth.get_access_token(code, as_dict=True)

    if not token_info or "access_token" not in token_info:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to get Spotify token")

    # Get user profile from Spotify
    sp = spotipy.Spotify(auth=token_info["access_token"])
    spotify_profile = sp.current_user()

    spotify_id = spotify_profile.get("id")
    email = spotify_profile.get("email")
    display_name = spotify_profile.get("display_name") or spotify_id

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not get email from Spotify. Please ensure your Spotify account has a verified email.",
        )

    # Check if user exists by email
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user:
        # Update Spotify tokens for existing user
        user.spotify_access_token = _encrypt(token_info["access_token"])
        user.spotify_refresh_token = _encrypt(token_info["refresh_token"])
        user.spotify_token_expires = datetime.fromtimestamp(token_info["expires_at"], tz=timezone.utc)
        await db.commit()
    else:
        # Create new user with Spotify tokens
        user = User(
            email=email,
            name=display_name,
            spotify_access_token=_encrypt(token_info["access_token"]),
            spotify_refresh_token=_encrypt(token_info["refresh_token"]),
            spotify_token_expires=datetime.fromtimestamp(token_info["expires_at"], tz=timezone.utc),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    # Create JWT and redirect to dashboard with token
    token = create_access_token(user.id)
    return RedirectResponse(url=f"{settings.app_url}/dashboard?token={token}&spotify=linked")
