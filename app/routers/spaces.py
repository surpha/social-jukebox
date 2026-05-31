import io
import secrets
import string

import qrcode
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import Space, User
from app.schemas import CreateSpaceRequest, SpaceResponse

router = APIRouter(prefix="/api/spaces", tags=["spaces"])
settings = get_settings()


def _generate_code(length: int = 6) -> str:
    """Generate a random alphanumeric space code."""
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


@router.post("", response_model=SpaceResponse, status_code=status.HTTP_201_CREATED)
async def create_space(
    body: CreateSpaceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new music space."""
    # Check user has linked Spotify
    if not current_user.spotify_refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You must link Spotify before creating a space",
        )

    # Generate unique code
    for _ in range(10):
        code = _generate_code()
        result = await db.execute(select(Space).where(Space.code == code))
        if not result.scalar_one_or_none():
            break
    else:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to generate unique code")

    space = Space(owner_id=current_user.id, name=body.name, code=code)
    db.add(space)
    await db.commit()
    await db.refresh(space)

    return SpaceResponse(
        id=space.id,
        name=space.name,
        code=space.code,
        is_active=space.is_active,
        created_at=space.created_at,
        qr_url=f"{settings.app_url}/api/spaces/{space.code}/qr",
    )


@router.get("", response_model=list[SpaceResponse])
async def list_spaces(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all spaces owned by the current user."""
    result = await db.execute(
        select(Space).where(Space.owner_id == current_user.id).order_by(Space.created_at.desc())
    )
    spaces = result.scalars().all()
    return [
        SpaceResponse(
            id=s.id,
            name=s.name,
            code=s.code,
            is_active=s.is_active,
            created_at=s.created_at,
            qr_url=f"{settings.app_url}/api/spaces/{s.code}/qr",
        )
        for s in spaces
    ]


@router.patch("/{code}/activate")
async def activate_space(
    code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Activate a space and start the background worker."""
    space = await _get_owned_space(code, current_user, db)
    space.is_active = True
    await db.commit()

    # Start background worker
    from app.worker import worker_manager
    await worker_manager.start_worker(space, current_user, db)

    return {"status": "active", "code": space.code}


@router.patch("/{code}/deactivate")
async def deactivate_space(
    code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a space and stop the background worker."""
    space = await _get_owned_space(code, current_user, db)
    space.is_active = False
    await db.commit()

    from app.worker import worker_manager
    await worker_manager.stop_worker(space.id)

    return {"status": "inactive", "code": space.code}


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_space(
    code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a space."""
    space = await _get_owned_space(code, current_user, db)

    # Stop worker if running
    from app.worker import worker_manager
    await worker_manager.stop_worker(space.id)

    await db.delete(space)
    await db.commit()


@router.get("/{code}/qr")
async def get_qr_code(code: str, db: AsyncSession = Depends(get_db)):
    """Generate and return a QR code PNG for the space join URL."""
    result = await db.execute(select(Space).where(Space.code == code))
    space = result.scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")

    join_url = f"{settings.app_url}/s/{space.code}"

    # Generate QR code
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(join_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="white", back_color="#121212")

    # Return as streaming PNG
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return StreamingResponse(buf, media_type="image/png")


async def _get_owned_space(code: str, user: User, db: AsyncSession) -> Space:
    """Helper to get a space owned by the user."""
    result = await db.execute(select(Space).where(Space.code == code, Space.owner_id == user.id))
    space = result.scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    return space
