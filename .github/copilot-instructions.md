---
applyTo: "**"
---

# Copilot instructions — Social Jukebox

Full working agreement: [AGENTS.md](../AGENTS.md). Architecture: [ARCHITECTURE.md](../ARCHITECTURE.md).

## Context
FastAPI app: a host links Spotify, guests scan a QR code and vote (no login) on what plays next.
A background worker (`app/worker.py`) auto-queues the top-voted track into the host's live Spotify
playback. Async SQLAlchemy + PostgreSQL. Server-rendered Jinja2 + vanilla JS + Tailwind (CDN).

## Rules
- **Keep docs in sync with code in the same change.** See the maintenance table in
  [AGENTS.md](../AGENTS.md) and add a [CHANGELOG.md](../CHANGELOG.md) entry for notable changes.
- Everything is async — never use blocking DB calls; get sessions via `Depends(get_db)`.
- No Alembic: add schema migrations as `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `init_db()`
  (`app/database.py`) alongside the model change.
- Access Spotify only through `get_spotify_client_for_user(user, db)`; tokens are Fernet-encrypted.
- Auth is Spotify-only via `get_current_user`; guest queue routes stay unauthenticated
  (identified by the `voter_id` cookie).
- New env vars go in `config.py`, `.env.example`, and `render.yaml` together.
- Keep the frontend build-free (no bundlers/frameworks); match existing Tailwind + vanilla JS style.
- Never commit secrets or `.env`.
