import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import init_db
from app.engine import engine_manager
from app.routers import auth_routes, pages, queue, spaces, spotify
from app.spotify_client import close_http

settings = get_settings()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    await init_db()
    await engine_manager.restart_active()

    yield

    await engine_manager.stop_all()
    await close_http()
    logger.info("Shutdown complete.")


app = FastAPI(title=settings.app_name, version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.app_url.rstrip("/")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(pages.router)
app.include_router(auth_routes.router)
app.include_router(spotify.router)
app.include_router(spaces.router)
app.include_router(queue.router)


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    """Liveness probe for Render and uptime pingers."""
    return {"status": "ok"}
