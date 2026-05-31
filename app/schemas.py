import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr


# --- Auth Schemas ---

class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    name: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleAuthRequest(BaseModel):
    token: str  # Google OAuth ID token


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


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
    name: str


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
    name: str
    artist: str
    album_art: str
    duration_ms: int


class VoteRequest(BaseModel):
    track_id: str


class QueueItemResponse(BaseModel):
    id: uuid.UUID
    track_id: str
    name: str
    artist: str
    album_art: str
    duration_ms: int
    vote_count: int
    created_at: datetime
    has_voted: bool = False

    class Config:
        from_attributes = True


class NowPlayingResponse(BaseModel):
    track_id: str | None = None
    name: str | None = None
    artist: str | None = None
    album_art: str | None = None
    duration_ms: int | None = None
    progress_ms: int | None = None
    is_playing: bool = False


class UpNextResponse(BaseModel):
    track_id: str
    name: str
    artist: str
    album_art: str
    duration_ms: int
    vote_count: int

    class Config:
        from_attributes = True


class SearchResult(BaseModel):
    track_id: str
    name: str
    artist: str
    album_art: str
    duration_ms: int


class QueueResponse(BaseModel):
    now_playing: NowPlayingResponse | None = None
    up_next: UpNextResponse | None = None
    queue: list[QueueItemResponse] = []
    spotify_queue: list[SearchResult] = []
    space_name: str = ""
