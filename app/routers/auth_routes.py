import secrets

from fastapi import APIRouter, Cookie, Depends, Query, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token, create_oauth_state, get_current_user, verify_oauth_state
from app.config import get_settings
from app.database import get_db
from app.models import User
from app.schemas import UserResponse
from app.spotify_client import LOGIN_SCOPES, SpotifyError, authorize_url, exchange_code, invalidate_token, request_with_token, store_tokens

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()

_NONCE_COOKIE = "oauth_nonce"


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        name=current_user.name,
        has_spotify=current_user.spotify_refresh_token is not None,
        created_at=current_user.created_at,
    )


@router.get("/spotify/login")
async def spotify_login(response: Response):
    """Returns the Spotify OAuth URL for login/signup."""
    nonce = secrets.token_urlsafe(16)
    response.set_cookie(
        _NONCE_COOKIE, nonce, max_age=600, httponly=True, samesite="lax",
        secure=settings.app_url.startswith("https"),
    )
    state = create_oauth_state("spotify_login", nonce)
    return {"auth_url": authorize_url(settings.spotify_login_redirect_uri, LOGIN_SCOPES, state)}


@router.get("/spotify/callback")
async def spotify_login_callback(
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    state: str = Query(default=""),
    oauth_nonce: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Handles Spotify OAuth callback for login/signup. Creates or finds user, links Spotify tokens."""
    denied = RedirectResponse(url=f"{settings.app_url}/?error=spotify_denied")
    if error or not code:
        return denied

    nonce = verify_oauth_state(state, "spotify_login")
    if not nonce or not oauth_nonce or not secrets.compare_digest(nonce, oauth_nonce):
        return denied

    try:
        token_info = await exchange_code(code, settings.spotify_login_redirect_uri)
        profile = await request_with_token(token_info["access_token"], "GET", "/me") or {}
    except SpotifyError:
        # Expired/reused code (e.g. user refreshed the callback page): just restart login.
        return denied

    email = profile.get("email")
    if not email:
        return RedirectResponse(url=f"{settings.app_url}/?error=no_email")
    display_name = profile.get("display_name") or profile.get("id") or email

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not user:
        user = User(email=email, name=display_name)
        db.add(user)
    store_tokens(user, token_info)
    await db.commit()
    await db.refresh(user)
    invalidate_token(user.id)

    # Fragment, not query string: keeps the JWT out of server logs and Referer headers.
    redirect = RedirectResponse(url=f"{settings.app_url}/dashboard#token={create_access_token(user.id)}")
    redirect.delete_cookie(_NONCE_COOKIE)
    return redirect
