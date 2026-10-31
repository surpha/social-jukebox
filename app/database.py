from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# Supabase's transaction pooler (port 6543) can't hold asyncpg's prepared statements.
_connect_args = {"statement_cache_size": 0} if ":6543/" in settings.database_url else {}

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_recycle=1800,
    connect_args=_connect_args,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


_MIGRATIONS = [
    "ALTER TABLE votes ADD COLUMN IF NOT EXISTS vote_type VARCHAR(10) DEFAULT 'up'",
    "CREATE INDEX IF NOT EXISTS ix_votes_voter_id ON votes (voter_id)",
    "CREATE INDEX IF NOT EXISTS ix_queue_items_space_status ON queue_items (space_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_spaces_owner_id ON spaces (owner_id)",
]


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _MIGRATIONS:
            await conn.execute(text(stmt))
