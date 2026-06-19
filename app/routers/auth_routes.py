from datetime import datetime, timezone

import spotipy
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from app.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.config import get_settings
from app.database import get_db
from app.models import User
from app.schemas import (
    GoogleAuthRequest,
    LoginRequest,
    SignupRequest,
    TokenResponse,
    UserResponse,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


@router.post("/signup", response_model=TokenResponse)
async def signup(body: SignupRequest, db: AsyncSession = Depends(get_db)):
    # Check if email already exists
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        name=body.name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(user.id)
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token(user.id)
    return TokenResponse(access_token=token)


@router.post("/google", response_model=TokenResponse)
async def google_auth(body: GoogleAuthRequest, db: AsyncSession = Depends(get_db)):
    # Verify the Google ID token
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://oauth2.googleapis.com/tokeninfo?id_token={body.token}")
        if resp.status_code != 200:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google token")
        google_data = resp.json()

    google_id = google_data.get("sub")
    email = google_data.get("email")
    name = google_data.get("name", email.split("@")[0])

    if not google_id or not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google token data")

    # Check if user exists by google_id or email
    result = await db.execute(select(User).where((User.google_id == google_id) | (User.email == email)))
    user = result.scalar_one_or_none()

    if user:
        # Link google_id if not already linked
        if not user.google_id:
            user.google_id = google_id
            await db.commit()
    else:
        # Create new user
        user = User(email=email, name=name, google_id=google_id)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    token = create_access_token(user.id)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        name=current_user.name,
        has_spotify=current_user.spotify_refresh_token is not None,
        created_at=current_user.created_at,
    )


@router.get("/google-client-id")
async def get_google_client_id():
    """Return Google Client ID for frontend initialization."""
    return {"client_id": settings.google_client_id}


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
    code: str = Query(...),
    state: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Handles Spotify OAuth callback for login/signup. Creates or finds user, links Spotify tokens."""
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
