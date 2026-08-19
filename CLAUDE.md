# CLAUDE.md

Guidance for Claude / Claude Code when working in this repository.

**The working agreement lives in [AGENTS.md](AGENTS.md) — read it first.** It covers commands,
conventions, security, and the documentation maintenance protocol. Technical detail is in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Quick orientation
- FastAPI + async SQLAlchemy + PostgreSQL app. Guests vote on songs; a background worker
  auto-queues the top-voted track into the host's live Spotify playback.
- Entry point: `app/main.py`. Routers in `app/routers/`. Worker in `app/worker.py`.
- Run: `uvicorn app.main:app --reload` (Python 3.11.9).

## Non-negotiables
- **Update docs in the same change as code** — follow the maintenance table in
  [AGENTS.md](AGENTS.md) and add a [CHANGELOG.md](CHANGELOG.md) entry.
- Async only; no blocking DB calls.
- No Alembic — schema migrations go in `init_db()` as `ADD COLUMN IF NOT EXISTS`.
- Go through `get_spotify_client_for_user` for Spotify; keep tokens encrypted.
- Spotify-only auth; guest routes stay unauthenticated (voter cookie).
- Frontend is build-free Jinja2 + vanilla JS + Tailwind CDN — keep it that way.
