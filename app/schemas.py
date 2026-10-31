import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# --- Auth Schemas ---

class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    has_spotify: bool
    created_at: datetime

    class Config:
        from_attributes = True


# --- Space Schemas ---

class CreateSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class SpaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    is_active: bool
    created_at: datetime
    qr_url: str | None = None

    class Config:
        from_attributes = True


# --- Queue Schemas ---

class AddTrackRequest(BaseModel):
    track_id: str
    # Ignored: metadata is resolved server-side so guests can't spoof it.
    name: str | None = None
    artist: str | None = None
    album_art: str | None = None
    duration_ms: int | None = None


class VoteRequest(BaseModel):
    track_id: str


class SearchResult(BaseModel):
    track_id: str
    name: str
    artist: str
    album_art: str
    duration_ms: int
