import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import init_db
from app.routers import auth_routes, pages, queue, spaces, spotify
from app.worker import worker_manager

settings = get_settings()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database initialized.")

    logger.info("Restarting active workers...")
    await worker_manager.restart_active_workers()

    yield

    # Shutdown
    logger.info("Stopping all workers...")
    await worker_manager.stop_all()
    logger.info("Shutdown complete.")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow all origins for MVP (guests on different devices)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(pages.router)
app.include_router(auth_routes.router)
app.include_router(spotify.router)
app.include_router(spaces.router)
app.include_router(queue.router)
